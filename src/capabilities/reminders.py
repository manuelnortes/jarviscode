"""Capacidad de recordatorios / temporizadores.

Programa avisos a futuro ("recuérdame en 20 minutos sacar la pizza", "mañana a
las 8 avísame de la reunión"). Cuando un recordatorio vence, se entrega como
notificación push al móvil del usuario reutilizando la capacidad de ntfy
(``notify.publish_sync``).

Diseño:
  - Scheduler **APScheduler** (`AsyncIOScheduler`) con jobstore **SQLite**
    (`SQLAlchemyJobStore`): los recordatorios sobreviven a reinicios del
    contenedor; al arrancar, el scheduler recarga los pendientes.
  - El scheduler vive en el proceso del **servidor** (se arranca/para en el
    lifespan de FastAPI, ver `src/core/server.py`), por lo que dispara aunque no
    haya ninguna sesión de chat abierta — justo lo que se quiere en el NUC 24/7.
  - En el CLI local (proceso efímero, sin servidor) los recordatorios solo viven
    mientras el proceso siga en pie; la persistencia y el disparo fiable son cosa
    del servidor.

La función que se ejecuta al vencer (`_fire_reminder`) es de nivel de módulo a
propósito: el jobstore persistente guarda una referencia importable a ella, no
un closure.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from claude_agent_sdk import create_sdk_mcp_server, tool

from src.capabilities import notify

# Ruta de la BD SQLite del jobstore. En Docker se sobrescribe por entorno hacia un
# volumen persistente (/app/data); en dev cae a <proyecto>/data/reminders.sqlite.
_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "reminders.sqlite"
_DB_PATH = Path(os.getenv("JARVIS_REMINDERS_DB", str(_DEFAULT_DB)))

# Margen (segundos) para disparar un recordatorio que venció mientras el servidor
# estaba caído: al reiniciar, si no han pasado más de esto, se entrega igualmente.
_MISFIRE_GRACE = 3600

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Devuelve el scheduler compartido, creándolo de forma perezosa.

    El timezone se deja en automático: APScheduler usa la zona local del proceso
    (en el contenedor se fija con TZ=Europe/Madrid).
    """
    global _scheduler
    if _scheduler is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{_DB_PATH}")}
        _scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            job_defaults={"misfire_grace_time": _MISFIRE_GRACE, "coalesce": True},
        )
    return _scheduler


def start_scheduler() -> None:
    """Arranca el scheduler si no está corriendo (idempotente).

    Requiere un bucle de eventos activo (lo hay tanto en el lifespan de FastAPI
    como dentro del bucle del SDK cuando una herramienta lo invoca).
    """
    sched = get_scheduler()
    if not sched.running:
        sched.start()


def shutdown_scheduler() -> None:
    """Para el scheduler de forma ordenada (al cerrar el servidor)."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)


def _fire_reminder(message: str) -> None:
    """Callback que se ejecuta cuando un recordatorio vence.

    De nivel de módulo (no closure) para que el jobstore persistente pueda
    referenciarla por ruta de importación. Entrega el aviso por ntfy.
    """
    notify.publish_sync(
        message,
        title="⏰ Recordatorio",
        priority="high",
        tags=["alarm_clock"],
    )


def _local_tz():
    """Zona horaria local del proceso (para fechas naive del modelo)."""
    return datetime.now().astimezone().tzinfo


def _resolve_run_date(at: str | None, in_seconds: int | None) -> datetime:
    """Calcula la fecha/hora absoluta de disparo a partir de los argumentos.

    Args:
        at: Fecha/hora ISO 8601 (p. ej. '2026-06-25T21:00'). Si es naive, se
            interpreta en la zona horaria local.
        in_seconds: Alternativa relativa: segundos a partir de ahora.

    Returns:
        Un datetime *aware* en zona local.

    Raises:
        ValueError: Si no se da ninguno de los dos, o el formato de `at` es inválido.
    """
    if in_seconds is not None:
        return datetime.now(_local_tz()) + timedelta(seconds=int(in_seconds))
    if at:
        dt = datetime.fromisoformat(at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_local_tz())
        return dt
    raise ValueError("Indica 'at' (fecha/hora ISO) o 'in_seconds' (segundos desde ahora).")


def _text(message: str) -> dict:
    """Envuelve un texto en el formato de respuesta que espera el SDK MCP."""
    return {"content": [{"type": "text", "text": message}]}


# --------------------------------------------------------------------------- #
# Herramientas expuestas a Jarvis
# --------------------------------------------------------------------------- #
@tool(
    "set_reminder",
    "Programa un recordatorio que avisará al usuario en el móvil cuando venza. Indica "
    "el momento con 'at' (fecha/hora ISO 8601 absoluta) o con 'in_seconds' "
    "(segundos desde ahora). Tienes la hora actual en la etiqueta [ahora ...] del "
    "mensaje, úsala para convertir expresiones como 'en 20 minutos' o 'mañana a las 8'.",
    {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Texto del recordatorio tal y como debe leerlo el usuario.",
            },
            "at": {
                "type": "string",
                "description": "Momento absoluto en ISO 8601, p. ej. '2026-06-25T21:00'.",
            },
            "in_seconds": {
                "type": "integer",
                "description": "Alternativa: segundos a partir de ahora (p. ej. 1200 = 20 min).",
                "minimum": 1,
            },
        },
        "required": ["message"],
    },
)
async def set_reminder(args: dict) -> dict:
    run_date = _resolve_run_date(args.get("at"), args.get("in_seconds"))
    rid = secrets.token_hex(3)
    sched = get_scheduler()
    # Lazy-start: en CLI no hay servidor que lo arranque; aquí ya estamos dentro
    # de un bucle de eventos, así que es seguro iniciarlo. Idempotente.
    if not sched.running:
        sched.start()
    sched.add_job(
        _fire_reminder,
        trigger="date",
        run_date=run_date,
        args=[args["message"]],
        id=rid,
        replace_existing=False,
    )
    return _text(
        f"Recordatorio '{rid}' programado para {run_date:%d/%m/%Y %H:%M}: {args['message']}"
    )


@tool(
    "list_reminders",
    "Lista los recordatorios pendientes del usuario, con su identificador y la hora a "
    "la que vencerán.",
    {"type": "object", "properties": {}},
)
async def list_reminders(args: dict) -> dict:
    jobs = get_scheduler().get_jobs()
    if not jobs:
        return _text("No hay recordatorios pendientes.")
    lines = []
    for job in jobs:
        when = job.next_run_time
        when_str = f"{when:%d/%m/%Y %H:%M}" if when else "sin fecha"
        msg = job.args[0] if job.args else "(sin texto)"
        lines.append(f"[{job.id}] {when_str} — {msg}")
    return _text("Recordatorios pendientes:\n" + "\n".join(lines))


@tool(
    "cancel_reminder",
    "Cancela un recordatorio pendiente por su identificador (el que devuelve "
    "list_reminders o set_reminder).",
    {
        "type": "object",
        "properties": {
            "reminder_id": {"type": "string", "description": "Identificador del recordatorio."},
        },
        "required": ["reminder_id"],
    },
)
async def cancel_reminder(args: dict) -> dict:
    rid = args["reminder_id"]
    sched = get_scheduler()
    if sched.get_job(rid) is None:
        return _text(f"No existe ningún recordatorio con id '{rid}'.")
    sched.remove_job(rid)
    return _text(f"Recordatorio '{rid}' cancelado.")


# --------------------------------------------------------------------------- #
# Servidor MCP en proceso
# --------------------------------------------------------------------------- #
_REMINDER_TOOLS = [set_reminder, list_reminders, cancel_reminder]

# Nombre del servidor MCP. Las herramientas quedan como mcp__reminders__<tool>.
SERVER_NAME = "reminders"

# Nombres completos para la lista blanca del núcleo.
REMINDER_TOOL_NAMES = [
    f"mcp__{SERVER_NAME}__set_reminder",
    f"mcp__{SERVER_NAME}__list_reminders",
    f"mcp__{SERVER_NAME}__cancel_reminder",
]


def build_reminders_server():
    """Crea el servidor MCP en proceso con las herramientas de recordatorios."""
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_REMINDER_TOOLS)
