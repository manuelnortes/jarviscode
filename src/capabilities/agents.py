"""Capacidad de agentes: la conversación gestiona agentes de Claude en segundo plano.

Los agentes corren en el contenedor ``jarvis-worker``. Aquí solo hay un cliente
HTTP fino: la cola, las sesiones y los estados viven en el worker
(``src/worker/``), así sobreviven a que la conversación se cierre o se recicle.

Dos modos de la conversación (Hito 9, Fase 5):
  - **Puente** (con Vision activa): Vision es el único gestor. Jarvis solo tiene
    ``vision`` (reenvía la frase literal del usuario y lee la respuesta) y las de
    solo lectura (``agent_status``, ``agent_result``, ficheros de proyecto) para
    contestar "¿cómo van?" sin despertar a Vision (Opus).
  - **Gestor** (``JARVIS_VISION=0``): Jarvis encarga, sigue, cancela y fusiona
    él mismo, como antes de la Fase 5.

Además expone helpers para el núcleo:
  - ``has_active()``: si hay agentes vivos (para el TTL largo de la conversación).
  - ``watch_loop()``: sondeo periódico que avisa por ntfy de los cambios de estado
    importantes (lo arranca el lifespan del servidor). Lo hace el núcleo y no el
    worker para que el worker no necesite el token de ntfy.

Config por entorno:
  JARVIS_AGENTS_URL     URL del worker (p. ej. http://127.0.0.1:8113). Sin ella
                        la capacidad no se registra.
  JARVIS_WORKER_TOKEN   Token compartido con el worker.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

from src.capabilities.project_files import TOOLS as PROJECT_TOOLS

log = logging.getLogger("jarvis.agents")

_TIMEOUT = 15.0
# Estados en los que el agente sigue vivo (mismo criterio que el worker).
ACTIVE_STATES = ("queued", "working", "waiting_input", "idle", "rate_limited", "paused")
# Cambios de estado que merecen un aviso al móvil.
_NOTIFY_STATES = {
    "idle": "ha terminado",
    "failed": "ha fallado",
    "rate_limited": "ha llegado al límite de uso y queda en pausa",
    "paused": "ha llegado al límite de uso: trabajo guardado y en pausa",
}
# Texto del aviso del ciclo del idle (D10). Los minutos son los reales: los
# umbrales del worker (35/45/55 por defecto) son configurables.
_IDLE_TEXT = "lleva {mins} min sin trabajo: pronto documentará su estado y cerrará la sesión (se podrá retomar)"
# Ventana en la que un agente cerrado sin nada pendiente sigue siendo relevante
# para "¿cómo van los agentes?" (s). Pasada, solo sale si el usuario pide los anteriores.
_RECENT_WINDOW = 24 * 3600
# Cada cuánto sondea el núcleo al worker (s).
_WATCH_INTERVAL = 15.0


def _base() -> str:
    """URL base del worker ('' si la capacidad no está configurada)."""
    return os.getenv("JARVIS_AGENTS_URL", "").strip().rstrip("/")


def is_enabled() -> bool:
    """True si hay worker configurado."""
    return bool(_base())


async def _call(method: str, path: str, **kwargs) -> dict | list:
    """Petición al worker con el token compartido.

    Raises:
        RuntimeError: Con el detalle del worker si responde con error.
    """
    headers = {"X-Jarvis-Token": os.getenv("JARVIS_WORKER_TOKEN", "")}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, _base() + path, headers=headers, **kwargs)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise RuntimeError(str(detail))
    return resp.json()


async def has_active() -> bool:
    """True si el worker tiene agentes vivos (un fallo de red cuenta como False)."""
    if not is_enabled():
        return False
    try:
        return bool(await _call("GET", "/agents", params={"active": 1}))
    except (httpx.HTTPError, RuntimeError):
        return False


async def list_agents() -> list[dict]:
    """Lista completa de agentes (para ``GET /agents`` del núcleo)."""
    return await _call("GET", "/agents")


async def get_agent(agent_id: str) -> dict:
    """Un agente por id."""
    return await _call("GET", f"/agents/{agent_id}")


async def merge_check(agent_id: str) -> dict:
    """El agente, si su PR se puede fusionar ahora (si no, RuntimeError con el motivo)."""
    return await _call("GET", f"/agents/{agent_id}/merge-check")


async def mark_merged(agent_id: str, shas: dict) -> dict:
    """Avisa al worker de que el núcleo ha fusionado el PR del agente."""
    return await _call("POST", f"/agents/{agent_id}/merged", json=shas)


async def mark_closed(agent_id: str) -> dict:
    """Cierra en el worker el PR del agente sin fusionar."""
    return await _call("POST", f"/agents/{agent_id}/closed")


def _relevant(agents: list[dict], now: float | None = None) -> tuple[list[dict], list[dict]]:
    """Separa los agentes que importan ahora de los antiguos (Fase 4).

    Relevantes: los vivos, los que esperan algo del usuario aunque sean viejos (PR
    abierto sin fusionar, fallo sin revisar) y los cerrados en las últimas 24 h. Los
    cancelados nunca: los canceló el usuario, o dio por revisado el fallo. El resto no se
    menciona salvo que el usuario pregunte por los anteriores: oír cada vez la lista
    de agentes de hace días no aporta nada.

    Returns:
        (relevantes ordenados: vivos → recién terminados → pendientes viejos, antiguos)
    """
    now = time.time() if now is None else now
    relevant, old = [], []
    for agent in agents:
        recent = now - (agent.get("updated_at") or 0) < _RECENT_WINDOW
        if agent.get("state") == "cancelled":
            old.append(agent)
        elif agent.get("state") in ACTIVE_STATES:
            relevant.append((0, agent))
        elif recent:
            relevant.append((1, agent))
        elif agent.get("pr_status") == "open" or agent.get("state") == "failed":
            relevant.append((2, agent))
        else:
            old.append(agent)
    # Dentro de cada grupo, lo más reciente primero.
    relevant.sort(key=lambda g: (g[0], -(g[1].get("updated_at") or 0)))
    return [a for _, a in relevant], old


def _ago(ts: float) -> str:
    """'hace N min / h / días' legible para el modelo."""
    secs = max(0, time.time() - ts)
    if secs < 3600:
        return f"hace {int(secs // 60)} min"
    if secs < 2 * 86400:
        return f"hace {int(secs // 3600)} h"
    return f"hace {int(secs // 86400)} días"


def _describe(agent: dict, with_result: bool = False) -> str:
    """Una línea (o un bloque, con resultado) legible para el modelo."""
    extra = ""
    if agent.get("waiting_on"):
        who = "el usuario" if agent["waiting_on"] == "user" else "ti (el gestor)"
        extra = f", pregunta y espera a {who}: {agent.get('question') or ''}"
    if agent.get("state") == "idle" and agent.get("idle_since"):
        extra += f", en idle desde hace {int((time.time() - agent['idle_since']) // 60)} min"
    wait = _waits().get(agent["id"])
    if wait:
        extra += f", espera a que se fusione el trabajo del agente {wait['blocked_by']} ({wait.get('reason', '')})"
    if agent.get("pr_status"):
        tests = {1: "tests OK", 0: "tests FALLAN"}.get(agent.get("pr_tests_passed"), "tests sin informar")
        extra += f", PR {agent['pr_status']} en la rama {agent.get('branch')} ({tests})"
    if agent.get("error"):
        extra += f", error: {agent['error']}"
    mode = "código" if agent.get("mode") == "code" else "lectura"
    line = f"[{agent['id']}] {agent['repo']} · {mode} · {agent['state']}{extra} · tarea: {agent['task'][:160]}"
    if with_result:
        # El detalle lleva el encargo entero y la bitácora: con eso (y el handoff)
        # el gestor responde al agente sin depender de la memoria de la conversación.
        if len(agent["task"]) > 160:
            line += f"\nEncargo completo:\n{agent['task']}"
        if agent.get("manager_notes"):
            merged = " (ya volcada al PLAN.md en el PR fusionado)" if agent.get("notes_merged") else ""
            line += f"\nBitácora del gestor{merged}:\n{agent['manager_notes']}"
        if agent.get("result"):
            line += f"\nResultado:\n{agent['result']}"
        if agent.get("pr_status"):
            line += (f"\nPR: {agent.get('pr_title')}\n{agent.get('pr_body') or ''}"
                     f"\nCambios:\n{agent.get('pr_diffstat') or ''}")
        if agent.get("handoff"):
            line += f"\nHandoff (estado para retomar):\n{agent['handoff']}"
    return line


async def _usage_line() -> str:
    """Una línea con el uso de la suscripción y la salvaguarda de los agentes."""
    try:
        usage = await _call("GET", "/usage")
    except (httpx.HTTPError, RuntimeError):
        return "Uso de la suscripción: desconocido."
    windows = usage.get("windows") or {}
    if usage.get("unknown"):
        return ("Uso de la suscripción: DESCONOCIDO (la sonda de uso falla): por seguridad no se "
                "pueden lanzar ni continuar agentes.")
    if not windows:
        return "Uso de la suscripción: aún sin datos."
    # "ventana de 5 horas" y no "5 h" a secas: Jarvis lo leía como "límite diario".
    labels = {"five_hour": "ventana de 5 horas", "seven_day": "semanal (7 días)"}
    # "limits" por ventana desde la tarea (d); "limit" (uno solo) en workers antiguos.
    limits = usage.get("limits") or {}
    parts = []
    for name, w in windows.items():
        limit = limits.get(name, usage["limit"])
        parts.append(f"{labels.get(name, name)} {(w.get('utilization') or 0) * 100:.0f} % "
                     f"(límite {limit * 100:.0f} %)")
    line = f"Uso de la suscripción: {', '.join(parts)}"
    if usage.get("blocked"):
        line += ": BLOQUEADO, no se pueden lanzar ni continuar agentes hasta que se reinicie la ventana"
    return line + "."


def _vision_lines() -> list[str]:
    """Lo que ha hecho Vision desde la última vez que se contó (y lo marca como contado)."""
    from src.vision.session import pop_unreported

    try:
        done = pop_unreported()
    except (OSError, ValueError):
        return []
    if not done:
        return []
    lines = ["Vision (el gestor autónomo) desde la última consulta:"]
    for e in done:
        who = f", agente {e['agent']}" if e["agent"] != "-" else ""  # las propuestas no tienen agente
        lines.append(f"- {e['repo']}{who}: {e.get('what', '')[:200]} → {e['verdict']}")
    return lines


def _waits() -> dict[str, dict]:
    """Esperas entre agentes apuntadas por Vision (vision_wait)."""
    from src.vision.session import load_waits

    try:
        return load_waits()
    except (OSError, ValueError):
        return {}


def _text(message: str) -> dict:
    """Envuelve un texto en el formato de respuesta que espera el SDK MCP."""
    return {"content": [{"type": "text", "text": message}]}


async def _run(coro) -> dict:
    """Ejecuta una llamada al worker convirtiendo errores en texto para el modelo."""
    try:
        return _text(await coro)
    except (httpx.HTTPError, RuntimeError) as exc:
        return _text(f"Error con los agentes: {exc}")


# Reglas de encargo: viven SOLO aquí y las usan agent_start (Jarvis) y vision_start
# (Vision), para que lo que se añada llegue a los dos a la vez (tarea (g) del Hito 9).
TASK_RULES = (
    "UN SOLO punto por encargo: si hay varias cosas que hacer, lanza un agente por cada una (se "
    "encolan y arrancan cuando hay hueco); nunca 'X + Y + Z' en el mismo encargo. Los que dependan "
    "del PR de otro, después de que se fusione. "
    "El encargo, completo y autocontenido (el agente no ve la conversación), con estas "
    "secciones: QUÉ HACER · ALCANCE (qué entra y qué no) · CONTEXTO (ficheros relevantes y "
    "las decisiones del plan que aplican, citadas: 'D3', '§Fase 2'…) · HECHO CUANDO "
    "(criterio comprobable, tests incluidos) · NO TOCAR. Empieza pidiéndole que lea el "
    "PLAN.md del proyecto. Si la tarea consume una API, da los nombres reales de los campos "
    "o una respuesta real de ejemplo (para los tests); las variables de entorno, con su "
    "nombre exacto del .env.example."
)


async def start_agent(repo: str, task: str, mode: str = "read") -> tuple[dict, str]:
    """Lanza un agente en el worker (lo usan agent_start y vision_start).

    Returns:
        (el agente creado, aviso de la puesta al día de la rama de integración o '')
    """
    from src.capabilities import prs

    sync = ""
    if mode == "code":
        # El trabajo autónomo parte de la rama de integración (develop), al día con la principal.
        sync = await prs.ensure_integration(repo)
    agent = await _call("POST", "/agents", json={"repo": repo, "task": task, "mode": mode})
    return agent, sync


# --------------------------------------------------------------------------- #
# Herramientas
# --------------------------------------------------------------------------- #
@tool(
    "agent_start",
    "Encarga una tarea a un agente de Claude que trabaja en segundo plano sobre un repositorio. "
    "mode='read' para revisar, investigar o proponer (no toca nada); mode='code' para cambiar código: "
    "trabaja en una rama propia, pasa los tests y deja un PR que el usuario revisa (nunca se fusiona "
    "solo). Devuelve su id. Si las notificaciones están activas, el usuario recibirá un aviso al "
    "móvil cuando termine.",
    {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Nombre del repositorio, p. ej. 'agent-sandbox'."},
            "task": {"type": "string", "description": TASK_RULES},
            "mode": {"type": "string", "enum": ["read", "code"], "description": "Por defecto 'read'."},
        },
        "required": ["repo", "task"],
    },
)
async def agent_start(args: dict) -> dict:
    async def go() -> str:
        agent, sync = await start_agent(args["repo"], args["task"], args.get("mode") or "read")
        branch = f", rama {agent['branch']}" if agent.get("branch") else ""
        return f"Agente {agent['id']} creado ({agent['state']}{branch}). {sync}".strip()

    return await _run(go())


@tool(
    "agent_status",
    "Estado de los agentes: sin id, solo los relevantes (vivos, con algo pendiente del usuario o "
    "terminados en las últimas 24 h); con include_old=true, también los anteriores (solo si el "
    "usuario los pide explícitamente); con id, uno concreto. Estados: queued (en cola), "
    "working, waiting_input (ha preguntado algo), idle (terminó; sesión viva para seguir), "
    "paused (en pausa por el límite de uso, con su trabajo guardado; se retoma con agent_message), "
    "rate_limited (pausado por límite de uso, estado antiguo), done (sesión cerrada; se puede retomar en frío con "
    "agent_message), merged, failed, cancelled.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "include_old": {"type": "boolean", "description": "Incluir los antiguos. Por defecto false."},
        },
    },
)
async def agent_status(args: dict) -> dict:
    async def go() -> str:
        if args.get("id"):
            return _describe(await _call("GET", f"/agents/{args['id']}"))
        agents = await _call("GET", "/agents")
        if not agents:
            return "No hay agentes.\n" + await _usage_line()
        relevant, old = _relevant(agents)
        if args.get("include_old"):
            lines = [_describe(a) for a in relevant]
            lines += ["Anteriores (ya cerrados, sin nada pendiente):"] + [_describe(a) for a in old[:10]]
        elif relevant:
            lines = [_describe(a) for a in relevant]
            if old:
                # Para que el modelo sepa que existen, no para leerlo en voz alta.
                lines.append(f"(Hay {len(old)} agentes antiguos ocultos: no los menciones salvo que te pregunten por ellos.)")
        else:
            last = max(old, key=lambda a: a.get("updated_at") or 0)
            lines = [f"Ningún agente en marcha ni pendiente. El último terminó {_ago(last.get('updated_at') or 0)} "
                     "(dilo en una sola frase, sin enumerar los antiguos)."]
        return "\n".join(lines + _vision_lines() + [await _usage_line()])

    return await _run(go())


@tool(
    "agent_result",
    "Detalle de un agente: encargo completo, bitácora del gestor (decisiones ya tomadas con el "
    "usuario), resultado del último turno, su PR (título, descripción, cambios, tests) y su handoff "
    "si lo tiene. Para contarlo, resúmelo en voz en pocas frases.",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
async def agent_result(args: dict) -> dict:
    async def go() -> str:
        return _describe(await _call("GET", f"/agents/{args['id']}"), with_result=True)

    return await _run(go())


@tool(
    "agent_message",
    "Manda un mensaje a un agente: una instrucción adicional, una pregunta de seguimiento o la "
    "respuesta a lo que preguntó. Si estaba en idle, retoma su trabajo con todo su contexto; si "
    "estaba en done, se retoma en frío a partir de su handoff.",
    {
        "type": "object",
        "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
        "required": ["id", "text"],
    },
)
async def agent_message(args: dict) -> dict:
    async def go() -> str:
        agent = await _call("POST", f"/agents/{args['id']}/message", json={"text": args["text"]})
        return f"Mensaje entregado. Agente {agent['id']}: {agent['state']}."

    return await _run(go())


@tool(
    "agent_note",
    "Apunta en la bitácora del agente una decisión o criterio del usuario sobre su encargo (alcance, "
    "preferencias, respuestas, cambios de plan). Sobrevive a que esta conversación se cierre: es lo "
    "que permite responder al agente más adelante sin contestar a ciegas. Las respuestas a sus "
    "preguntas con agent_message se apuntan solas.",
    {
        "type": "object",
        "properties": {"id": {"type": "string"}, "note": {"type": "string"}},
        "required": ["id", "note"],
    },
)
async def agent_note(args: dict) -> dict:
    async def go() -> str:
        agent = await _call("POST", f"/agents/{args['id']}/note", json={"text": args["note"]})
        return f"Apuntado en la bitácora del agente {agent['id']}."

    return await _run(go())


@tool(
    "agent_escalate",
    "Pasa al usuario la pregunta pendiente de un agente cuando es una decisión suya (de diseño, de "
    "gusto o de prioridades) y no la puedes responder tú con lo que sabes.",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
async def agent_escalate(args: dict) -> dict:
    async def go() -> str:
        agent = await _call("POST", f"/agents/{args['id']}/escalate")
        return f"Pregunta pasada al usuario: {agent.get('question') or ''}"

    return await _run(go())


@tool(
    "agent_cancel",
    "Cancela un agente. Sobre uno que ha fallado (failed), lo da por revisado: deja de salir en "
    "agent_status.",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
async def agent_cancel(args: dict) -> dict:
    async def go() -> str:
        agent = await _call("POST", f"/agents/{args['id']}/cancel")
        return f"Agente {agent['id']}: {agent['state']}."

    return await _run(go())


@tool(
    "pr_merge",
    "Fusiona el PR de un agente en su rama base y borra la rama (el merge guarda todo para un "
    "rollback). Dos pasos obligatorios: llámala primero SIN confirm para obtener el resumen, léeselo "
    "al usuario (avisa si los tests fallan) y pregúntale si fusiona; solo si responde que sí de forma "
    "explícita a ESA pregunta, llámala con confirm=true. Nunca con confirm=true por iniciativa propia.",
    {
        "type": "object",
        "properties": {"id": {"type": "string"}, "confirm": {"type": "boolean"}},
        "required": ["id"],
    },
)
async def pr_merge(args: dict) -> dict:
    from src.capabilities import prs

    async def go() -> str:
        if not args.get("confirm"):
            agent = await merge_check(args["id"])
            return prs.summary(agent) + "\nPide confirmación explícita al usuario antes de fusionar."
        agent = await prs.merge(args["id"])
        note = "" if agent["branch_deleted"] else " (la rama no se pudo borrar; no afecta a la fusión)"
        return (f"PR fusionado en {agent['repo']}/{agent['base_branch']}{note}. "
                f"Commit {agent['pr_merge_sha'][:10]}; rollback posible.")

    return await _run(go())


@tool(
    "vision",
    "Habla con Vision, el gestor de los agentes: todo lo que sea ACTUAR sobre agentes o PRs (encargar, "
    "lanzar el siguiente punto, responder a un agente, retomar, cancelar, fusionar) y lo que el usuario "
    "quiera decirle o preguntarle a Vision. text = la frase del usuario LITERAL, tal cual la dijo (sin "
    "resumir ni reescribir: Vision decide). Devuelve la respuesta de Vision; puede tardar un rato.",
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
)
async def vision_tool(args: dict) -> dict:
    from src.vision import session

    vision = session.current()
    if vision is None:
        return _text("Vision no está activa (solo vive en el servidor del núcleo): no puedo actuar sobre los agentes.")
    try:
        return _text("Vision responde:\n" + await vision.message(args["text"]))
    except RuntimeError as exc:
        return _text(str(exc))


# Puente (Vision activa): una herramienta de acción y las de lectura.
_BRIDGE_TOOLS = [vision_tool, agent_status, agent_result, *PROJECT_TOOLS]
# Gestor (Vision apagada): Jarvis lo hace todo, como antes de la Fase 5.
_MANAGER_TOOLS = [
    agent_start, agent_status, agent_result, agent_message, agent_note, agent_escalate, agent_cancel,
    pr_merge, *PROJECT_TOOLS,
]

# Nombre del servidor MCP. Las herramientas quedan como mcp__agents__<tool>.
SERVER_NAME = "agents"


def bridge_mode() -> bool:
    """True si Vision gestiona los agentes y Jarvis solo hace de puente."""
    from src.vision.session import is_enabled as vision_enabled

    return vision_enabled()


def _tools() -> list:
    return _BRIDGE_TOOLS if bridge_mode() else _MANAGER_TOOLS


def agent_tool_names() -> list[str]:
    """Nombres MCP de las herramientas de agentes de la conversación (según el modo)."""
    return [f"mcp__{SERVER_NAME}__{t.name}" for t in _tools()]


def build_agents_server():
    """Crea el servidor MCP en proceso con las herramientas de agentes (según el modo)."""
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_tools())


# --------------------------------------------------------------------------- #
# Avisos: por ntfy (sondeo) o a través del gestor (contexto del turno)
# --------------------------------------------------------------------------- #
def _notices(agent: dict) -> list[tuple[str, str]]:
    """Avisos vigentes de un agente: lista de (clave, texto).

    La clave identifica el aviso para no repetirlo (por ntfy o en la conversación).
    """
    out = []
    label = f"Agente {agent['id']} ({agent['repo']}: {agent['task'][:80]})"
    if agent["state"] == "waiting_input" and agent.get("waiting_on") == "user":
        out.append((f"{agent['id']}:ask:{agent.get('question')}",
                    f"{label} necesita tu respuesta: {agent.get('question')}"))
    if agent["state"] == "waiting_input" and agent.get("waiting_on") == "manager":
        out.append((f"{agent['id']}:askmgr:{agent.get('question')}",
                    f"{label} pregunta al gestor: {agent.get('question')}"))
    if agent["state"] == "idle" and agent.get("idle_warned"):
        mins = int((time.time() - (agent.get("idle_since") or time.time())) // 60)
        out.append((f"{agent['id']}:idle:{agent.get('idle_since')}",
                    f"{label} {_IDLE_TEXT.format(mins=mins)}."))
    return out


async def pending_notices(seen: set[str]) -> list[str]:
    """Avisos aún no mostrados en esta conversación (y los marca como vistos).

    Lo usa el núcleo cuando no hay ntfy: los avisos llegan "a través del gestor",
    como una línea de contexto en el siguiente turno.
    """
    if not is_enabled():
        return []
    try:
        agents = await _call("GET", "/agents", params={"active": 1})
    except (httpx.HTTPError, RuntimeError):
        return []
    from src.vision.session import is_enabled as vision_enabled

    # Con Vision activa, las preguntas al gestor las atiende ella, no la conversación.
    skip_mgr = vision_enabled()
    fresh = []
    for agent in agents:
        for key, text in _notices(agent):
            if skip_mgr and ":askmgr:" in key:
                continue
            if key not in seen:
                seen.add(key)
                fresh.append(text)
    return fresh


async def watch_loop(publish) -> None:
    """Sondea el worker y avisa por ntfy de los cambios relevantes.

    Avisa de: fin de turno (con PR si lo hay), fallos, pausas por límite de uso,
    preguntas que pasan al usuario y el aviso de los 35 min en idle. Las
    preguntas al gestor no se mandan al móvil: si el gestor no responde, a los 5
    min pasan al usuario y entonces sí.

    Args:
        publish: Función async ``publish(message, title=...)`` (la de ``notify``).
    """
    seen_state: dict[str, str] = {}
    seen_keys: set[str] = set()
    first = True
    while True:
        try:
            agents = await _call("GET", "/agents")
            for agent in agents:
                prev = seen_state.get(agent["id"])
                seen_state[agent["id"]] = agent["state"]
                keys = [(k, t) for k, t in _notices(agent) if ":askmgr:" not in k]
                # En la primera vuelta solo se memoriza: no avisar de lo ya ocurrido.
                if first:
                    seen_keys.update(k for k, _ in keys)
                    continue
                if prev != agent["state"] and agent["state"] in _NOTIFY_STATES:
                    what = _NOTIFY_STATES[agent["state"]]
                    if agent["state"] == "idle" and agent.get("pr_status") == "open":
                        what = "ha terminado y su PR está listo"
                    await publish(
                        f"{agent['repo']}: {agent['task'][:120]}", title=f"Agente {agent['id']} {what}"
                    )
                for key, text in keys:
                    if key not in seen_keys:
                        seen_keys.add(key)
                        await publish(text, title=f"Agente {agent['id']}")
            first = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - el sondeo nunca debe tumbar el servidor
            log.warning("Sondeo de agentes: %s", exc)
        await asyncio.sleep(_WATCH_INTERVAL)
