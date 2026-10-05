"""Capacidad de notificaciones push vía ntfy.

Permite a Jarvis enviar avisos al móvil del usuario publicando en un topic de un
servidor ntfy (propio o público). Es local y sin OAuth: en ntfy el *topic* actúa como
secreto compartido (quien lo conoce, recibe y publica), por eso se usa un topic
con sufijo no adivinable.

Se publica con el formato JSON de ntfy (POST a la raíz del servidor con el campo
``topic`` en el cuerpo) en lugar de cabeceras HTTP: así el título y el mensaje
viajan como UTF-8 sin los problemas de codificación latin-1 de las cabeceras
(acentos del español).

Config por entorno:
  NTFY_BASE_URL  Base del servidor ntfy (por defecto https://ntfy.sh).
  NTFY_TOPIC     Topic donde publicar (p. ej. jarvis-a1b2c3). Obligatorio.
  NTFY_TOKEN     Token Bearer de acceso (tk_...). Si el servidor es
                 deny-all, sin token la publicación devuelve 403. Aquí el
                 secreto es el token, no el nombre del topic.

Expone tanto la herramienta MCP (``notify_user``) como helpers reutilizables
(``publish`` async / ``publish_sync``), que la capacidad de recordatorios usa
para entregar el aviso cuando un temporizador se dispara.
"""

from __future__ import annotations

import os

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

# Servidor ntfy por defecto si no se define NTFY_BASE_URL.
DEFAULT_BASE_URL = "https://ntfy.sh"

# Timeout de la petición HTTP a ntfy (segundos).
_TIMEOUT = 10.0

# ntfy usa prioridad numérica 1..5. Exponemos nombres legibles al modelo.
_PRIORITY_MAP = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5}


def _config() -> tuple[str, str]:
    """Lee la configuración de ntfy del entorno.

    Returns:
        Tupla (base_url_sin_barra_final, topic).

    Raises:
        RuntimeError: Si NTFY_TOPIC no está definido.
    """
    base = os.getenv("NTFY_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        raise RuntimeError(
            "NTFY_TOPIC no está configurado. Define el topic de ntfy en el entorno (.env)."
        )
    return base, topic


def _auth_headers() -> dict:
    """Cabecera de autenticación Bearer si hay token configurado (si no, vacía)."""
    token = os.getenv("NTFY_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _build_payload(
    topic: str,
    message: str,
    title: str | None,
    priority: str | None,
    tags: list[str] | str | None,
) -> dict:
    """Construye el cuerpo JSON que espera la API de publicación de ntfy."""
    payload: dict = {"topic": topic, "message": message}
    if title:
        payload["title"] = title
    if priority:
        payload["priority"] = _PRIORITY_MAP.get(priority, 3)
    if tags:
        payload["tags"] = tags if isinstance(tags, list) else [tags]
    return payload


async def publish(
    message: str,
    title: str | None = None,
    priority: str | None = None,
    tags: list[str] | str | None = None,
) -> None:
    """Publica una notificación en ntfy (versión async).

    Args:
        message: Cuerpo del aviso.
        title: Título corto opcional.
        priority: Una de min/low/default/high/max.
        tags: Lista de etiquetas/emojis de ntfy (p. ej. ["warning"]).
    """
    base, topic = _config()
    payload = _build_payload(topic, message, title, priority, tags)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(base, json=payload, headers=_auth_headers())
        resp.raise_for_status()


def publish_sync(
    message: str,
    title: str | None = None,
    priority: str | None = None,
    tags: list[str] | str | None = None,
) -> None:
    """Publica una notificación en ntfy (versión síncrona).

    La usa la capacidad de recordatorios desde el hilo del scheduler, donde un
    contexto síncrono es más simple que orquestar un bucle de eventos.
    """
    base, topic = _config()
    payload = _build_payload(topic, message, title, priority, tags)
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(base, json=payload, headers=_auth_headers())
        resp.raise_for_status()


def _text(message: str) -> dict:
    """Envuelve un texto en el formato de respuesta que espera el SDK MCP."""
    return {"content": [{"type": "text", "text": message}]}


# --------------------------------------------------------------------------- #
# Herramienta expuesta a Jarvis
# --------------------------------------------------------------------------- #
@tool(
    "notify_user",
    "Envía una notificación push al móvil del usuario (vía ntfy). Úsala para avisarle "
    "de algo, confirmarle el final de una tarea en segundo plano, o alertarle. "
    "Para avisos programados a futuro usa mejor la herramienta de recordatorios.",
    {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Cuerpo del aviso."},
            "title": {"type": "string", "description": "Título corto opcional."},
            "priority": {
                "type": "string",
                "enum": ["min", "low", "default", "high", "max"],
                "description": "Prioridad del aviso. Por defecto 'default'.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Etiquetas/emojis de ntfy opcionales, p. ej. ['warning'].",
            },
        },
        "required": ["message"],
    },
)
async def notify_user(args: dict) -> dict:
    await publish(
        args["message"],
        title=args.get("title"),
        priority=args.get("priority"),
        tags=args.get("tags"),
    )
    return _text("Notificación enviada al móvil del usuario.")


# --------------------------------------------------------------------------- #
# Servidor MCP en proceso
# --------------------------------------------------------------------------- #
_NOTIFY_TOOLS = [notify_user]

# Nombre del servidor MCP. Las herramientas quedan como mcp__notify__<tool>.
SERVER_NAME = "notify"

# Nombres completos para la lista blanca del núcleo.
NOTIFY_TOOL_NAMES = [f"mcp__{SERVER_NAME}__notify_user"]


def build_notify_server():
    """Crea el servidor MCP en proceso con la herramienta de notificaciones."""
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_NOTIFY_TOOLS)
