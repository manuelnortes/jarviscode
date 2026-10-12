"""Métricas de Vision y coste por tarea (Hito 9, Fase 6, bloque 6).

Los agentes ya apuntan una fila por turno en el worker (``turn_metrics.jsonl``, ``GET /metrics``).
Aquí se apunta lo mismo para Vision, en un fichero aparte de su volumen de datos
(``vision_metrics.jsonl``; Vision vive en el núcleo, no en el worker), y se cruzan ambos en un
resumen **por tarea** (tarea = agente): turnos de Vision que lo lanzan + turnos del agente +
turnos de Vision que lo revisan, fusionan y marcan el plan.

Unidad principal: **tokens**. El $ es una estimación del CLI y va solo como referencia
(``cost_usd_ref``). El % de la ventana de 5 h mide la cuenta entera (Jarvis, Vision y agentes
comparten token), así que no se reparte por tarea: solo sirve como estimación global.

Atribución de un turno de Vision a una tarea: el agente del evento que lo provocó, los que
lanzó con ``vision_start`` y los que tocó con alguna herramienta (argumento ``id``). Si un
turno toca varios agentes, sus tokens se reparten a partes iguales; si no toca ninguno
(handoff, una pregunta general del usuario), va a ``vision_overhead``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from pathlib import Path

import httpx

from src.capabilities import agents

log = logging.getLogger("jarvis.vision")

# Tipos de token de ResultMessage.usage (los mismos que guarda el worker).
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

# Agentes tocados en el turno en curso. Variables de módulo como ``_user_turn`` de
# session.py: los turnos de Vision van de uno en uno (lock), así que no se pisan.
_touched: set[str] = set()
_launched: list[str] = []


def _path() -> Path:
    """Fichero de métricas de Vision (junto a sus handoffs y su registro de acciones)."""
    return Path(os.getenv("JARVIS_VISION_DATA", "/app/data/vision")) / "vision_metrics.jsonl"


# --------------------------------------------------------------------------- #
# Durante el turno
# --------------------------------------------------------------------------- #
def begin_turn() -> None:
    """Empieza un turno: olvida los agentes del anterior."""
    _touched.clear()
    _launched.clear()


def launched(agent_id: str) -> None:
    """``vision_start`` acaba de lanzar este agente: el turno cuenta como su lanzamiento."""
    _launched.append(agent_id)


def tracked(sdk_tool):
    """Envuelve una herramienta de Vision para apuntar el agente que toca (argumento ``id``)."""
    handler = sdk_tool.handler

    async def wrapper(args: dict) -> dict:
        if isinstance(args, dict) and args.get("id"):
            _touched.add(str(args["id"]))
        return await handler(args)

    return dataclasses.replace(sdk_tool, handler=wrapper)


def tokens(usage: dict | None) -> dict:
    """Tokens de un ``ResultMessage.usage`` (por consulta, no acumulado)."""
    return {k: int((usage or {}).get(k) or 0) for k in TOKEN_KEYS}


async def usage_snapshot() -> dict:
    """Uso de las ventanas según el worker (su lectura cacheada; sondea solo si está vieja).

    Mismo formato que el ``before``/``after`` de las filas del worker.
    """
    try:
        usage = await agents._call("GET", "/usage")
    except (httpx.HTTPError, RuntimeError):
        return {}
    snap = {k: w.get("utilization") for k, w in (usage.get("windows") or {}).items()}
    snap["probed_at"] = usage.get("probed_at")
    return snap


def record(row: dict) -> None:
    """Apunta la fila del turno con los agentes tocados (una métrica perdida no tumba nada)."""
    row = {**row, "touched": sorted(_touched), "launched": list(_launched)}
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        with _path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("Vision: no se pudo guardar la métrica del turno")


# --------------------------------------------------------------------------- #
# Resumen (GET /api/metrics del núcleo)
# --------------------------------------------------------------------------- #
def load() -> list[dict]:
    """Todas las filas de Vision."""
    if not _path().exists():
        return []
    with _path().open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _zero() -> dict:
    return dict.fromkeys(TOKEN_KEYS, 0)


def _add(acc: dict, tok: dict | None, share: float = 1.0) -> None:
    for k in TOKEN_KEYS:
        acc[k] += (tok or {}).get(k, 0) * share


def _total(tok: dict) -> int:
    return round(sum(tok[k] for k in TOKEN_KEYS))


def _ints(tok: dict) -> dict:
    """Tokens redondeados (el reparto entre agentes deja decimales) con su total."""
    return {**{k: round(v) for k, v in tok.items()}, "total": _total(tok)}


def _agents_summary(worker: dict) -> dict:
    """Resumen del worker con los tokens delante y el $ como referencia."""
    s = worker.get("summary") or {}
    out = {"turns": s.get("turns", 0), "checkpoints": s.get("checkpoints", 0), "tokens": s.get("tokens") or {}}
    for window in ("five_hour", "seven_day"):
        out[f"{window}_pct_moved"] = s.get(f"{window}_pct_moved")
        out[f"{window}_pct_per_mtok"] = s.get(f"{window}_pct_per_mtok")
    out["cost_usd_ref"] = s.get("cost_usd")
    return out


def summarize(worker: dict, agent_list: list[dict], vision_rows: list[dict],
              tasks_limit: int = 20, last: int = 20) -> dict:
    """Cruza las filas del worker (todas) con las de Vision y saca el coste por tarea.

    Args:
        worker: Respuesta de ``GET /metrics`` del worker (con todas sus filas).
        agent_list: ``GET /agents`` del worker (repo, encargo, estado, fechas).
        vision_rows: Filas de ``vision_metrics.jsonl``.
        tasks_limit: Tareas a devolver, las más recientes primero.
        last: Últimas filas de Vision a devolver en crudo.
    """
    info = {a["id"]: a for a in agent_list}
    tasks: dict[str, dict] = {}

    def task(agent_id: str, repo: str | None = None) -> dict:
        return tasks.setdefault(agent_id, {
            "repo": repo, "agent_turns": 0, "vision_turns": 0, "agent_tokens": _zero(),
            "vision_tokens": _zero(), "cost": 0.0, "launched_at": None, "first_ts": None,
        })

    for row in worker.get("rows") or []:
        t = task(row["agent"], row.get("repo"))
        t["agent_turns"] += 1
        _add(t["agent_tokens"], row.get("tokens"))
        t["cost"] += row.get("cost_usd") or 0
        start = row["ts"] - row.get("secs", 0)
        t["first_ts"] = min(t["first_ts"] or start, start)

    vision_total, overhead = _zero(), _zero()
    vision_cost, overhead_cost, by_kind = 0.0, 0.0, {}
    for row in vision_rows:
        tok = row.get("tokens") or {}
        _add(vision_total, tok)
        vision_cost += row.get("cost_usd") or 0
        kind = by_kind.setdefault(row.get("kind") or "?", {"turns": 0, "tokens": 0})
        kind["turns"] += 1
        kind["tokens"] += sum(tok.get(k, 0) for k in TOKEN_KEYS)
        ids = set(row.get("launched") or []) | set(row.get("touched") or [])
        if row.get("for_agent"):
            ids.add(row["for_agent"])
        if not ids:
            _add(overhead, tok)
            overhead_cost += row.get("cost_usd") or 0
            continue
        share = 1 / len(ids)
        for agent_id in ids:
            t = task(agent_id, row.get("repo"))
            t["vision_turns"] += 1
            _add(t["vision_tokens"], tok, share)
            t["cost"] += (row.get("cost_usd") or 0) * share
        for agent_id in row.get("launched") or []:
            tasks[agent_id]["launched_at"] = row["ts"] - row.get("secs", 0)

    out_tasks = []
    for agent_id, t in tasks.items():
        a = info.get(agent_id, {})
        combined = _zero()
        _add(combined, t["agent_tokens"])
        _add(combined, t["vision_tokens"])
        # Tiempo real: del turno de Vision que lo lanzó (o del alta del agente) a la fusión.
        start = t["launched_at"] or a.get("created_at") or t["first_ts"]
        merged = a.get("merged_at")
        out_tasks.append({
            "agent": agent_id, "repo": a.get("repo") or t["repo"], "task": (a.get("task") or "")[:100],
            "state": a.get("state"), "pr_status": a.get("pr_status"),
            "tokens": {"total": _total(combined), "agent": _total(t["agent_tokens"]),
                       "vision": _total(t["vision_tokens"])},
            "tokens_detail": _ints(combined),
            "turns": {"agent": t["agent_turns"], "vision": t["vision_turns"]},
            "wall_secs": round(merged - start) if merged and start else None,
            "launched_by_vision": t["launched_at"] is not None,
            "cost_usd_ref": round(t["cost"], 4),
            "_sort": start or 0,
        })
    out_tasks.sort(key=lambda x: x.pop("_sort"), reverse=True)

    return {
        "unit": "tokens (cost_usd_ref es una estimación del CLI, solo de referencia)",
        "agents": _agents_summary(worker),
        "vision": {
            "turns": len(vision_rows), "tokens": _ints(vision_total), "by_kind": by_kind,
            "overhead_tokens": _total(overhead), "cost_usd_ref": round(vision_cost, 4),
            "overhead_cost_usd_ref": round(overhead_cost, 4),
        },
        "tasks": out_tasks[:tasks_limit],
        "vision_rows": vision_rows[-last:],
        "generated_at": round(time.time(), 1),
    }
