"""Capacidad de agentes: la conversación gestiona agentes de Claude en segundo plano.

Jarvis (el "gestor") encarga tareas a agentes que corren en el contenedor
``jarvis-worker`` y los vigila: estado, resultado, mensajes de seguimiento y
cancelación. Aquí solo hay un cliente HTTP fino: la cola, las sesiones y los
estados viven en el worker (``src/worker/``), así sobreviven a que la
conversación se cierre o se recicle.

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

log = logging.getLogger("jarvis.agents")

_TIMEOUT = 15.0
# Estados en los que el agente sigue vivo (mismo criterio que el worker).
ACTIVE_STATES = ("queued", "working", "waiting_input", "idle", "rate_limited")
# Cambios de estado que merecen un aviso al móvil.
_NOTIFY_STATES = {
    "idle": "ha terminado",
    "failed": "ha fallado",
    "rate_limited": "ha llegado al límite de uso y queda en pausa",
}
# Texto del aviso del ciclo del idle (D10). Los minutos son los reales: los
# umbrales del worker (35/45/55 por defecto) son configurables.
_IDLE_TEXT = "lleva {mins} min sin trabajo: pronto documentará su estado y cerrará la sesión (se podrá retomar)"
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


def _describe(agent: dict, with_result: bool = False) -> str:
    """Una línea (o un bloque, con resultado) legible para el modelo."""
    extra = ""
    if agent.get("waiting_on"):
        who = "el usuario" if agent["waiting_on"] == "user" else "ti (el gestor)"
        extra = f", pregunta y espera a {who}: {agent.get('question') or ''}"
    if agent.get("state") == "idle" and agent.get("idle_since"):
        extra += f", en idle desde hace {int((time.time() - agent['idle_since']) // 60)} min"
    if agent.get("pr_status"):
        tests = {1: "tests OK", 0: "tests FALLAN"}.get(agent.get("pr_tests_passed"), "tests sin informar")
        extra += f", PR {agent['pr_status']} en la rama {agent.get('branch')} ({tests})"
    if agent.get("error"):
        extra += f", error: {agent['error']}"
    mode = "código" if agent.get("mode") == "code" else "lectura"
    line = f"[{agent['id']}] {agent['repo']} · {mode} · {agent['state']}{extra} · tarea: {agent['task'][:160]}"
    if with_result:
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
    if not windows:
        return "Uso de la suscripción: aún sin datos."
    parts = [f"{name} {(w.get('utilization') or 0) * 100:.0f} %" for name, w in windows.items()]
    line = f"Uso de la suscripción: {', '.join(parts)} (límite de agentes {usage['limit'] * 100:.0f} %)"
    if usage.get("blocked"):
        line += ": BLOQUEADO, no se pueden lanzar ni continuar agentes hasta que se reinicie la ventana"
    return line + "."


def _text(message: str) -> dict:
    """Envuelve un texto en el formato de respuesta que espera el SDK MCP."""
    return {"content": [{"type": "text", "text": message}]}


async def _run(coro) -> dict:
    """Ejecuta una llamada al worker convirtiendo errores en texto para el modelo."""
    try:
        return _text(await coro)
    except (httpx.HTTPError, RuntimeError) as exc:
        return _text(f"Error del worker de agentes: {exc}")


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
            "task": {"type": "string", "description": "La tarea, redactada de forma completa y autocontenida."},
            "mode": {"type": "string", "enum": ["read", "code"], "description": "Por defecto 'read'."},
        },
        "required": ["repo", "task"],
    },
)
async def agent_start(args: dict) -> dict:
    async def go() -> str:
        body = {"repo": args["repo"], "task": args["task"], "mode": args.get("mode") or "read"}
        agent = await _call("POST", "/agents", json=body)
        branch = f", rama {agent['branch']}" if agent.get("branch") else ""
        return f"Agente {agent['id']} creado ({agent['state']}{branch})."

    return await _run(go())


@tool(
    "agent_status",
    "Estado de los agentes: sin id, los recientes; con id, uno concreto. Estados: queued (en cola), "
    "working, waiting_input (ha preguntado algo), idle (terminó; sesión viva para seguir), "
    "rate_limited (pausado por límite de uso), done (sesión cerrada; se puede retomar en frío con "
    "agent_message), merged, failed, cancelled.",
    {"type": "object", "properties": {"id": {"type": "string"}}},
)
async def agent_status(args: dict) -> dict:
    async def go() -> str:
        if args.get("id"):
            return _describe(await _call("GET", f"/agents/{args['id']}"))
        agents = (await _call("GET", "/agents"))[:10]
        lines = [_describe(a) for a in agents] or ["No hay agentes."]
        return "\n".join(lines + [await _usage_line()])

    return await _run(go())


@tool(
    "agent_result",
    "Resultado del último turno de un agente, con su PR (título, descripción, cambios, tests) y su "
    "handoff si lo tiene. Resúmelo en voz en pocas frases.",
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
    "Cancela un agente.",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
async def agent_cancel(args: dict) -> dict:
    async def go() -> str:
        agent = await _call("POST", f"/agents/{args['id']}/cancel")
        return f"Agente {agent['id']}: {agent['state']}."

    return await _run(go())


_TOOLS = [agent_start, agent_status, agent_result, agent_message, agent_escalate, agent_cancel]

# Nombre del servidor MCP. Las herramientas quedan como mcp__agents__<tool>.
SERVER_NAME = "agents"
AGENT_TOOL_NAMES = [f"mcp__{SERVER_NAME}__{t.name}" for t in _TOOLS]


def build_agents_server():
    """Crea el servidor MCP en proceso con las herramientas de agentes."""
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_TOOLS)


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
    fresh = []
    for agent in agents:
        for key, text in _notices(agent):
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
