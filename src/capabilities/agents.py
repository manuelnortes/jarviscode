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

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

log = logging.getLogger("jarvis.agents")

_TIMEOUT = 15.0
# Estados en los que el agente sigue vivo (mismo criterio que el worker).
ACTIVE_STATES = ("queued", "working", "waiting_input", "idle", "rate_limited")
# Cambios de estado que merecen un aviso al móvil.
_NOTIFY_STATES = {
    "idle": "ha terminado",
    "waiting_input": "necesita una respuesta",
    "failed": "ha fallado",
    "rate_limited": "ha llegado al límite de uso y queda en pausa",
}
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
        extra = f", esperando a {agent['waiting_on']}: {agent.get('question') or ''}"
    if agent.get("error"):
        extra += f", error: {agent['error']}"
    line = f"[{agent['id']}] {agent['repo']} · {agent['state']}{extra} · tarea: {agent['task'][:160]}"
    if with_result and agent.get("result"):
        line += f"\nResultado:\n{agent['result']}"
    return line


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
    "Encarga una tarea a un agente de Claude que trabaja en segundo plano sobre un repositorio "
    "(por ahora solo lee e investiga: revisar código, proponer mejoras, buscar información). "
    "Devuelve su id. El usuario recibirá un aviso al móvil cuando termine.",
    {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Nombre del repositorio, p. ej. 'agent-sandbox'."},
            "task": {"type": "string", "description": "La tarea, redactada de forma completa y autocontenida."},
        },
        "required": ["repo", "task"],
    },
)
async def agent_start(args: dict) -> dict:
    async def go() -> str:
        agent = await _call("POST", "/agents", json={"repo": args["repo"], "task": args["task"]})
        return f"Agente {agent['id']} creado ({agent['state']})."

    return await _run(go())


@tool(
    "agent_status",
    "Estado de los agentes: sin id, los recientes; con id, uno concreto. Estados: queued (en cola), "
    "working, waiting_input (ha preguntado algo), idle (terminó y sigue disponible para más), "
    "rate_limited (pausado por límite de uso), done, merged, failed, cancelled.",
    {"type": "object", "properties": {"id": {"type": "string"}}},
)
async def agent_status(args: dict) -> dict:
    async def go() -> str:
        if args.get("id"):
            return _describe(await _call("GET", f"/agents/{args['id']}"))
        agents = (await _call("GET", "/agents"))[:10]
        return "\n".join(_describe(a) for a in agents) if agents else "No hay agentes."

    return await _run(go())


@tool(
    "agent_result",
    "Resultado (informe) del último turno de un agente. Resúmelo en voz en pocas frases.",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
async def agent_result(args: dict) -> dict:
    async def go() -> str:
        return _describe(await _call("GET", f"/agents/{args['id']}"), with_result=True)

    return await _run(go())


@tool(
    "agent_message",
    "Manda un mensaje a un agente: una instrucción adicional, una pregunta de seguimiento o la "
    "respuesta a lo que preguntó. Si estaba en idle, retoma su trabajo con todo su contexto.",
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
    "agent_cancel",
    "Cancela un agente.",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
async def agent_cancel(args: dict) -> dict:
    async def go() -> str:
        agent = await _call("POST", f"/agents/{args['id']}/cancel")
        return f"Agente {agent['id']}: {agent['state']}."

    return await _run(go())


_TOOLS = [agent_start, agent_status, agent_result, agent_message, agent_cancel]

# Nombre del servidor MCP. Las herramientas quedan como mcp__agents__<tool>.
SERVER_NAME = "agents"
AGENT_TOOL_NAMES = [f"mcp__{SERVER_NAME}__{t.name}" for t in _TOOLS]


def build_agents_server():
    """Crea el servidor MCP en proceso con las herramientas de agentes."""
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_TOOLS)


# --------------------------------------------------------------------------- #
# Avisos por ntfy (sondeo desde el lifespan del servidor)
# --------------------------------------------------------------------------- #
async def watch_loop(publish) -> None:
    """Sondea el worker y avisa de los cambios de estado relevantes.

    Args:
        publish: Función async ``publish(message, title=...)`` (la de ``notify``).
    """
    seen: dict[str, str] = {}
    first = True
    while True:
        try:
            agents = await _call("GET", "/agents")
            for agent in agents:
                prev = seen.get(agent["id"])
                seen[agent["id"]] = agent["state"]
                # En la primera vuelta solo se memoriza: no avisar de lo ya ocurrido.
                if first or prev == agent["state"] or agent["state"] not in _NOTIFY_STATES:
                    continue
                await publish(
                    f"{agent['repo']}: {agent['task'][:120]}",
                    title=f"Agente {agent['id']} {_NOTIFY_STATES[agent['state']]}",
                )
            first = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - el sondeo nunca debe tumbar el servidor
            log.warning("Sondeo de agentes: %s", exc)
        await asyncio.sleep(_WATCH_INTERVAL)
