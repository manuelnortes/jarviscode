"""Gestor de agentes: cola, sesiones del Agent SDK y máquina de estados.

Cada agente es una sesión ``ClaudeSDKClient`` (un proceso del CLI) con ``cwd`` en
su propio clon del repo. Ver docs/plan/hito-9-agentes.md (D1–D12). Ciclo de vida:

    queued → working ⇄ waiting_input
               │  └→ (salvaguarda de uso) handoff → paused ─(autónomo: al reiniciarse la
               │       ventana · sesión: mensaje del usuario)→ working (en frío)
               └→ idle ──35 min: aviso──45: handoff──55: cierre──→ done (retomable en frío)
                    └─(mensaje)→ working …
    cualquier estado vivo → failed / cancelled

- ``JARVIS_AGENTS_MAX`` limita los agentes TRABAJANDO a la vez (la cuota se gasta
  trabajando). Esperando una respuesta (``waiting_input``) se libera el hueco.
  Los ``idle`` no cuentan, pero ocupan RAM: si hay más ``idle`` que ``MAX``, el
  más antiguo hace su ritual de cierre y se cierra.
- Modos: ``read`` (lee e informa) y ``code`` (trabaja en una rama ``agent/*``,
  hace commits y el runner empuja la rama y publica un PR ligero).
- Ciclo del ``idle`` (D10), todo con la caché de prompt de 1 h aún caliente: a los
  35 min se marca ``idle_warned`` (el núcleo avisa), a los 45 el agente escribe su
  handoff (modo código: fichero ``<rama>.md`` commiteado en la rama) y a los 55 se
  cierra la sesión → ``done``. Un mensaje posterior abre una sesión nueva
  sembrada con el handoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
)

from src.worker import usage as usage_probe
from src.worker.settings import Hot
from src.worker.prs import LocalPRProvider, PRProvider, commits_ahead, git, run_suite
from src.worker.store import AgentStore
from src.worker.tools import build_agent_server

log = logging.getLogger("jarvis.worker")

READ_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]
CODE_TOOLS = READ_TOOLS + ["Edit", "Write", "Bash"]
MODES = ("read", "code")

# Mensaje con el que se reanuda un agente pausado por límite de uso.
_RESUME_TEXT = "Continúa con la tarea donde la dejaste."

# Cada cuánto revisa el gestor los temporizadores (idle, rate limit).
_REAP_INTERVAL = 30.0

_COMMON_RULES = """\
Eres un agente de Jarvis, el asistente personal del usuario, y trabajas en segundo plano en el repositorio de tu \
directorio actual. Si de verdad no puedes decidir algo razonablemente, usa la herramienta ask; si no, decide y deja \
claros tus supuestos. Si te mandan un mensaje de seguimiento, contesta a eso.
- Ejecuta el encargo, no investigues: lee solo los ficheros que nombra y los imprescindibles para \
tocarlos; nada de explorar el repo ni releer lo que ya has leído. Lee varios ficheros a la vez cuando puedas.
- Escribe telegráfico (preguntas, descripción del PR, respuestas): lo esencial, sin preámbulos, sin \
repetir el encargo ni narrar lo que vas haciendo. El código y los mensajes de commit, normales."""

_READ_RULES = """
MODO SOLO LECTURA: puedes leer el código y buscar en la web, pero no modificar nada.
Cuando termines, responde con un informe breve en español (como máximo unas 15 líneas): conclusiones primero, \
sin tablas, porque Jarvis te lo resumirá en voz alta."""

_CODE_RULES = """
MODO CÓDIGO: trabajas en la rama {branch} (ya creada y activa; rama base: {base}).
- Cambios pequeños y enfocados en la tarea; sigue tus CLAUDE.md (el de usuario y el del proyecto, si los hay).
- Prohibido inventar contratos: si necesitas un dato que no está en el encargo ni en el repo (campos de una API, nombres de variables de entorno, formato de un fichero…), pregúntalo con ask en vez de suponerlo. Las variables de entorno, con el nombre exacto del .env.example.
- No ejecutes la suite de tests: al terminar tu turno el sistema la ejecuta sobre tu último commit y, si falla, te \
devuelve la salida. Si necesitas dependencias, el entorno virtual va en .venv (no lo commitees). No escribas tests \
salvo que el encargo lo pida; entonces, uno mínimo sumado a la suite existente, nunca un fichero grande.
- No edites el PLAN.md (ni para marcar tu casilla): lo actualiza el gestor. No tomes decisiones de diseño (variables \
nuevas, precedencias, contratos…): si falta una, pregúntala con ask; si crees que el plan debe cambiar, dilo en la \
descripción del PR.
- Haz commit de tu trabajo siguiendo la convención de tu CLAUDE.md (si no hay, mensajes claros). NO hagas push ni \
cambies de rama: el sistema empuja tu rama.
- Al terminar, llama a submit_pr con título y descripción. Después responde con un resumen \
breve en español (como máximo 10 líneas), sin tablas."""

# Suite en rojo tras un turno: lo manda el runner como mensaje del siguiente turno.
_MAX_RED_FIXES = 2
_SUITE_RED = """\
El sistema ha ejecutado la suite de tests sobre tu último commit y falla. Si fallan tests que no has escrito tú, \
tu cambio ha roto algo que ya funcionaba: arregla tu código y no toques esos tests. Para entender el fallo puedes \
ejecutar solo el test que falla; la suite completa la vuelve a pasar el sistema. Haz commit del arreglo y termina.

Final de la salida:
{output}"""

_HANDOFF_CODE = """\
RITUAL DE CIERRE: esta sesión se va a cerrar {why}. Escribe (o actualiza) el fichero `{branch}.md` en la \
raíz del repo con: qué se pidió, qué has hecho, estado actual, qué falta y cómo retomarlo en frío \
sin esta conversación. Haz commit de ese fichero (sin push). Responde solo con un resumen de ese handoff en 5 líneas."""

_HANDOFF_READ = """\
RITUAL DE CIERRE: esta sesión se va a cerrar {why}. Responde solo con un handoff para retomar en frío sin \
esta conversación: qué se pidió, qué encontraste, qué quedó pendiente (como máximo 15 líneas)."""

_COLD_RESUME = """\
Retomas en frío una tarea anterior: es una sesión nueva y no tienes la conversación previa.
Tarea original:
{task}

Handoff de la sesión anterior:
{handoff}

Nuevo mensaje:
{text}"""


# (f) Punto de control: se manda en la misma sesión tras interrumpir al agente.
_CHECKPOINT = """\
PUNTO DE CONTROL: te he interrumpido porque desde el último control la ventana de uso de la suscripción ha subido \
un {pct:.0f} %. Resume en 3 líneas qué llevas hecho y estima cuánto te falta. Si ya habías terminado, repite tu \
resumen final. Si te falta poco, continúa con la tarea. Si te falta mucho, pregunta a Vision con ask \
(kind='clarification', empezando por "[punto de control]", con tu resumen y tu estimación) si recortar el alcance \
y sigue lo que te diga."""

# (e) Tipos de token de ResultMessage.usage que se guardan en las métricas.
_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

# (e2) Filas de métricas separadas por más de esto (s) son tandas distintas: el hueco entre
# ellas (otro uso de la cuenta, otro día) no se atribuye a los agentes.
_METRICS_GAP = 1800.0


def _tokens(usage: dict | None) -> dict:
    """Tokens de un ``ResultMessage.usage`` (por consulta, no acumulado)."""
    return {k: int((usage or {}).get(k) or 0) for k in _TOKEN_KEYS}


def _window_moved(rows: list[dict], window: str) -> float:
    """(e2) Cuánto subió una ventana (0–1) de punta a punta en cada tanda de filas.

    Recorre las lecturas en orden (antes₁, después₁, antes₂…) y suma solo las subidas:
    una bajada es un reinicio de la ventana y ese paso no cuenta.
    """
    moved, prev, prev_ts = 0.0, None, None
    for row in rows:
        if prev_ts is not None and row["ts"] - row.get("secs", 0) - prev_ts > _METRICS_GAP:
            prev = None  # tanda nueva
        for snap in (row.get("before") or {}, row.get("after") or {}):
            value = snap.get(window)
            if value is None:
                continue
            if prev is not None and value > prev:
                moved += value - prev
            prev = value
        prev_ts = row["ts"]
    return moved


class RateLimited(Exception):
    """El agente chocó con el límite de uso de la suscripción."""

    def __init__(self, resets_at: float | None, safeguard: bool = False) -> None:
        super().__init__("Límite de uso alcanzado.")
        self.resets_at = resets_at
        # True = lo paró nuestra salvaguarda antes del turno (aún se puede hacer el
        # handoff); False = la API rechazó el turno (no queda margen ni para eso).
        self.safeguard = safeguard


def _slug(text: str, limit: int = 30) -> str:
    """Slug ASCII corto para el nombre de la rama."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z0-9]+", plain)
    slug = ""
    for word in words:
        if len(slug) + len(word) + 1 > limit:
            break
        slug = f"{slug}-{word}" if slug else word
    return slug or "tarea"


class AgentManager:
    """Coordina los agentes del worker.

    Args:
        store: Persistencia de estados.
        git_root: Carpeta con los bare repos (``<repo>.git``).
        work_root: Carpeta donde se clona cada agente.
        allowed_repos: Repos que los agentes pueden usar (lista blanca). None = los bare que
            haya en ``git_root`` en cada momento (alta y baja en caliente, bloque 5).
        max_working: Agentes trabajando a la vez.
        model: Modelo de los agentes.
        effort: Esfuerzo de razonamiento de los agentes (low, medium, high…).
        idle_warn: Segundos en ``idle`` hasta el aviso.
        handoff_at: Segundos en ``idle`` hasta el ritual de cierre.
        idle_ttl: Segundos en ``idle`` hasta cerrar la sesión.
        turn_timeout: Tope de segundos TRABAJANDO por turno (sin contar esperas).
        manager_timeout: Segundos que una aclaración espera al gestor antes de pasar al usuario
            (red de seguridad: con Vision activo, el gestor responde o escala él mismo).
        manager_first: Si True, también las preguntas de diseño van primero al gestor
            (Vision decide si son del usuario); si False, van directas al usuario.
        usage_limit: Fracción de uso de la ventana de 5 h de la suscripción (0–1) a partir
            de la cual no se crean agentes ni empiezan turnos nuevos (salvaguarda).
        usage_limit_7d: Lo mismo para la ventana semanal (la peligrosa: tarda días en reiniciarse).
        probe_token: Token OAuth para la sonda de uso (``usage.py``). Vacío = sin sonda:
            la salvaguarda solo ve los RateLimitEvent (ciegos lejos del aviso).
        probe_every: Segundos entre sondas mientras haya agentes vivos (y antes de lanzar
            uno si la última lectura es más vieja).
        probe_stale: Segundos a partir de los que la última lectura no vale y el uso
            pasa a desconocido (no se lanza nada).
        usage_mode: Qué pasa con los ``paused`` al reiniciarse la ventana: ``session``
            (esperan a que el usuario diga que se retomen) o ``autonomous`` (se retoman solos).
        pr_provider: Dónde se publican los PR.
        base_branch: Rama base preferida (la de integración, ``develop``) si existe en
            el repo; si no, la principal. Vacía = siempre la principal.
        config_repo: Bare repo opcional con las reglas comunes de los agentes
            (``agents/CLAUDE.md`` + ``rules/``; en el homelab, ``dotclaude``). Si no
            existe, los agentes solo cargan el CLAUDE.md del proyecto.
        checkpoint: (f) Subida de la ventana de 5 h (0–1) durante un turno que dispara
            un punto de control. 0 = apagado.

    Los atributos ``Hot`` (bloque 5) guardan el valor del entorno y devuelven el de
    ``settings.json`` si se cambió en caliente desde la web.
    """

    max_working = Hot("JARVIS_AGENTS_MAX")
    model = Hot("JARVIS_AGENT_MODEL")
    effort = Hot("JARVIS_AGENT_EFFORT")
    idle_warn = Hot("JARVIS_AGENT_IDLE_WARN")
    handoff_at = Hot("JARVIS_AGENT_HANDOFF_AT")
    idle_ttl = Hot("JARVIS_AGENT_IDLE_TTL")
    turn_timeout = Hot("JARVIS_AGENT_TURN_TIMEOUT")
    manager_timeout = Hot("JARVIS_AGENT_MANAGER_TIMEOUT")
    manager_first = Hot("JARVIS_AGENT_ASK_MANAGER_FIRST")
    usage_limit = Hot("JARVIS_AGENT_USAGE_LIMIT")
    usage_limit_7d = Hot("JARVIS_AGENT_USAGE_LIMIT_7D")
    usage_mode = Hot("JARVIS_AGENT_USAGE_MODE")
    probe_every = Hot("JARVIS_USAGE_PROBE_EVERY")
    checkpoint = Hot("JARVIS_AGENT_CHECKPOINT")

    def __init__(
        self,
        store: AgentStore,
        git_root: Path,
        work_root: Path,
        allowed_repos: list[str] | None = None,
        max_working: int = 2,
        model: str = "claude-sonnet-4-6",
        effort: str = "low",
        idle_warn: float = 2100,
        handoff_at: float = 2700,
        idle_ttl: float = 3300,
        turn_timeout: float = 1800,
        manager_timeout: float = 900,
        manager_first: bool = True,
        usage_limit: float = 0.60,
        usage_limit_7d: float = 0.75,
        usage_mode: str = "session",
        probe_token: str = "",
        probe_every: float = 120,
        probe_stale: float = 600,
        pr_provider: PRProvider | None = None,
        base_branch: str = "develop",
        config_repo: Path | None = None,
        metrics_path: Path | None = None,
        checkpoint: float = 0.05,
    ) -> None:
        self.store = store
        # (e) Métricas por turno en JSONL: barato y sin dependencias. None = no se guardan.
        # TODO (sin prisa): exponerlas a Prometheus (ya está en el homelab) para verlas en Grafana.
        self.metrics_path = metrics_path
        # (f) Subida de la ventana de 5 h (0–1) que dispara un punto de control. 0 = apagado.
        self.checkpoint = checkpoint
        self.git_root = git_root
        self.work_root = work_root
        self._allowed_repos = allowed_repos
        self.max_working = max_working
        self.model = model
        # Esfuerzo bajo: los agentes ejecutan un encargo ya decidido, no investigan;
        # el razonamiento cuenta como salida y se relee en cada vuelta.
        self.effort = effort
        self.idle_warn = idle_warn
        self.handoff_at = handoff_at
        self.idle_ttl = idle_ttl
        self.turn_timeout = turn_timeout
        self.manager_timeout = manager_timeout
        self.manager_first = manager_first
        self.usage_limit = usage_limit
        self.usage_limit_7d = usage_limit_7d
        self.usage_mode = usage_mode
        self.probe_token = probe_token
        self.probe_every = probe_every
        self.probe_stale = probe_stale
        self.prs = pr_provider or LocalPRProvider()
        self.base_branch = base_branch
        self.config_repo = config_repo
        # Huecos de trabajo. Se gestionan a mano (no `async with`) porque un agente
        # que pregunta suelta el suyo y lo recupera al recibir la respuesta. Limitador
        # propio y no Semaphore: el tope (max_working) se puede cambiar en caliente.
        self._holding: set[str] = set()
        self._slot_free = asyncio.Event()
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # Mensajes recibidos mientras el agente trabajaba: se procesan al acabar el turno.
        self._pending: dict[str, list[str]] = {}
        # Respuestas que espera un agente bloqueado en `ask`.
        self._answers: dict[str, asyncio.Future] = {}
        # Último ``total_cost_usd`` visto por sesión del CLI: es ACUMULADO por sesión
        # (verificado 2026-10-08: 0,0028 → 0,0049 → 0,0074 en tres consultas), así
        # que cada turno suma solo la diferencia. Se reinicia al abrir sesión nueva.
        self._session_cost: dict[str, float] = {}
        # Datos de PR que el agente dejó con submit_pr en el turno en curso.
        self._pr_requests: dict[str, dict] = {}
        # Turnos automáticos de arreglo enviados por suite en rojo (se reinicia en verde).
        self._red_fixes: dict[str, int] = {}
        # Último estado de uso visto por ventana (five_hour, seven_day…). Con token, de
        # la sonda (usage.py); sin él, de los RateLimitEvent del CLI. Alimenta la
        # salvaguarda y GET /usage.
        self.windows: dict[str, dict] = {}
        self._probed_at: float | None = None  # última sonda buena
        self._probe_error = ""
        self._probe_lock = asyncio.Lock()  # sin sondas duplicadas si piden a la vez
        self._reaper: asyncio.Task | None = None
        self._prober: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Ciclo de vida del gestor
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Limpia agentes huérfanos y arranca la revisión periódica de temporizadores."""
        orphans = self.store.fail_orphans()
        if orphans:
            log.warning("%d agentes de una ejecución anterior marcados como failed", orphans)
        self._reaper = asyncio.create_task(self._reap_loop())
        if self.probe_token:
            self._prober = asyncio.create_task(self._probe_loop())

    async def shutdown(self) -> None:
        """Cierra todas las sesiones (al apagar el worker)."""
        for loop_task in (self._reaper, self._prober):
            if loop_task:
                loop_task.cancel()
        for task in list(self._tasks.values()):
            task.cancel()
        for agent_id in list(self._clients):
            await self._close_client(agent_id)

    # ------------------------------------------------------------------ #
    # API pública (la usa la app HTTP)
    # ------------------------------------------------------------------ #
    def create(self, repo: str, task: str, mode: str = "read") -> dict:
        """Encola un agente nuevo sobre ``repo`` con la tarea ``task``.

        Raises:
            ValueError: Si el repo no está permitido o no existe, o el modo no es válido.
        """
        if mode not in MODES:
            raise ValueError(f"Modo '{mode}' no válido: {', '.join(MODES)}.")
        if repo not in self.allowed_repos:
            raise ValueError(f"Repo '{repo}' no permitido. Permitidos: {', '.join(self.allowed_repos)}.")
        if not (self.git_root / f"{repo}.git").is_dir():
            raise ValueError(f"No existe el repo '{repo}' en el servidor git.")
        blocked, resets_at = self._over_usage()
        if blocked:
            raise ValueError(self._usage_message(resets_at))
        agent_id = secrets.token_hex(3)
        agent = self.store.create(agent_id, repo, task.strip(), mode=mode)
        if mode == "code":
            agent = self.store.update(agent_id, branch=f"agent/{agent_id}-{_slug(task)}")
        self._spawn(agent_id, self._run(agent_id, first=True))
        return agent

    async def message(self, agent_id: str, text: str, by: str = "") -> dict:
        """Manda un mensaje a un agente; qué hace depende de su estado.

        - ``queued``: se añade a la tarea.
        - ``working`` / ``rate_limited``: se guarda y se le pasa en el siguiente turno.
        - ``waiting_input``: es la respuesta a su pregunta.
        - ``idle``: retoma la sesión (mismo contexto, caché caliente).
        - ``done``: sesión nueva sembrada con el handoff (retomar en frío).
        - ``paused``: igual que ``done`` (o desde cero si no llegó a empezar).

        Args:
            agent_id: Agente.
            text: Mensaje.
            by: Quién responde (p. ej. ``vision``), para la bitácora. Vacío = Jarvis/usuario.

        Raises:
            ValueError: Si el agente no existe o ya no acepta mensajes.
        """
        agent = self._require(agent_id)
        state = agent["state"]
        if state == "queued":
            return self.store.update(agent_id, task=f"{agent['task']}\n\nAdemás: {text}")
        if state in ("working", "rate_limited"):
            self._pending.setdefault(agent_id, []).append(text)
            return agent
        if state == "waiting_input":
            fut = self._answers.get(agent_id)
            if fut and not fut.done():
                fut.set_result(text)
                # Una respuesta a su pregunta es una decisión sobre el encargo: a la bitácora.
                who = f" ({by})" if by else ""
                self.store.append_note(agent_id, f"P: {agent.get('question') or '?'} → R{who}: {text}")
            return self.store.get(agent_id)
        if state == "idle" and agent_id in self._clients and not self._busy(agent_id):
            self.store.update(agent_id, state="queued", idle_since=None)
            self._spawn(agent_id, self._run(agent_id, prompt=text))
            return self.store.get(agent_id)
        if state == "idle":  # está haciendo su ritual de cierre: se atiende después
            self._pending.setdefault(agent_id, []).append(text)
            return agent
        if state == "done":
            self.store.update(agent_id, state="queued", idle_since=None)
            self._spawn(agent_id, self._run(agent_id, prompt=text, cold=True))
            return self.store.get(agent_id)
        if state == "paused":  # el usuario lo retoma tras la pausa por límite de uso
            self._resume(agent, text)
            return self.store.get(agent_id)
        raise ValueError(f"El agente {agent_id} está '{state}' y ya no acepta mensajes.")

    def note(self, agent_id: str, text: str) -> dict:
        """Apunta una decisión o criterio en la bitácora del gestor del agente.

        Raises:
            ValueError: Si el agente no existe o la nota está vacía.
        """
        self._require(agent_id)
        if not text.strip():
            raise ValueError("La nota está vacía.")
        return self.store.append_note(agent_id, text)

    def escalate(self, agent_id: str) -> dict:
        """El gestor pasa al usuario la pregunta pendiente de un agente (D9)."""
        agent = self._require(agent_id)
        if agent["state"] != "waiting_input":
            raise ValueError(f"El agente {agent_id} no está esperando una respuesta.")
        return self.store.update(agent_id, waiting_on="user")

    async def cancel(self, agent_id: str) -> dict:
        """Cancela un agente (interrumpe su turno si está trabajando).

        Sobre un agente ``failed`` sirve para darlo por revisado: pasa a ``cancelled``
        y deja de salir en "¿cómo van los agentes?" (Fase 4).
        """
        agent = self._require(agent_id)
        if agent["state"] == "failed":
            return self.store.update(agent_id, state="cancelled")
        if agent["state"] in ("done", "merged", "cancelled"):
            return agent
        client = self._clients.get(agent_id)
        if client and agent["state"] == "working":
            try:
                await client.interrupt()
            except Exception:  # noqa: BLE001 - interrumpir es best-effort
                pass
        task = self._tasks.pop(agent_id, None)
        if task and not task.done():
            task.cancel()
        await self._close_client(agent_id)
        self._release(agent_id)
        return self.store.update(
            agent_id, state="cancelled", substate=None, idle_since=None, waiting_on=None, question=None
        )

    async def mark_merged(self, agent_id: str, shas: dict) -> dict:
        """Marca como fusionado el PR de un agente (la fusión la hace el núcleo).

        El worker no fusiona: su UID solo puede empujar ``agent/*`` (D12). El
        núcleo comprueba con ``merge_check`` antes de fusionar y llama aquí al
        terminar; si el agente seguía en ``idle``, se cierra su sesión.

        Args:
            agent_id: Agente con PR abierto.
            shas: ``base_sha``, ``branch_sha`` y ``merge_sha`` (para el rollback).

        Raises:
            ValueError: Si el agente no existe o no tiene PR abierto.
        """
        agent = self._require(agent_id)
        if agent.get("pr_status") != "open":
            raise ValueError(f"El agente {agent_id} no tiene un PR abierto.")
        task = self._tasks.pop(agent_id, None)
        if task and not task.done():
            task.cancel()
        await self._close_client(agent_id)
        self._release(agent_id)
        self._pending.pop(agent_id, None)
        return self.store.update(
            agent_id, state="merged", substate=None, idle_since=None, waiting_on=None,
            question=None, pr_status="merged", merged_at=time.time(),
            pr_base_sha=shas.get("base_sha"), pr_branch_sha=shas.get("branch_sha"),
            pr_merge_sha=shas.get("merge_sha"),
            # La bitácora ya va en el PLAN.md del PR (el gestor se la pasa al agente
            # antes de dar el PR por bueno): queda marcada, no se borra.
            notes_merged=1 if agent.get("manager_notes") else 0,
        )

    async def mark_closed(self, agent_id: str) -> dict:
        """Cierra sin fusionar el PR de un agente (redundante o descartado).

        La rama se queda en el bare: cerrar no borra trabajo. Se guarda su punta en
        ``pr_branch_sha`` por si alguien la borra después y hay que recuperarla.

        Raises:
            ValueError: Si el agente no existe, no tiene PR abierto o está trabajando.
        """
        agent = self._require(agent_id)
        if agent.get("pr_status") != "open":
            raise ValueError(f"El agente {agent_id} no tiene un PR abierto.")
        # Como para fusionar, el agente parado; pero aquí también vale un cancelado o
        # fallido (su PR quedó huérfano y cerrarlo es justo lo que hace falta).
        if agent["state"] not in ("idle", "done", "cancelled", "failed") or self._busy(agent_id):
            raise ValueError(f"El agente {agent_id} está '{agent['state']}': espera a que termine para cerrar su PR.")
        await self._close_client(agent_id)
        self._release(agent_id)
        self._pending.pop(agent_id, None)
        return self.store.update(
            agent_id, state="done" if agent["state"] == "done" else "cancelled", substate=None,
            idle_since=None, waiting_on=None, question=None, pr_status="closed",
            pr_branch_sha=self._branch_sha(agent),
        )

    def _branch_sha(self, agent: dict) -> str | None:
        """Punta de la rama del agente en el bare (None si no se puede leer)."""
        try:
            return git(self.git_root / f"{agent['repo']}.git", "rev-parse", f"refs/heads/{agent['branch']}") or None
        except Exception:  # noqa: BLE001 - el SHA es informativo; cerrar no depende de él
            return None

    def merge_check(self, agent_id: str) -> dict:
        """Comprueba que el PR de un agente se puede fusionar ahora.

        Solo con el agente parado (``idle`` sin turno en curso, o ``done``): si
        está trabajando o haciendo su ritual de cierre, la rama aún puede cambiar.

        Raises:
            ValueError: Con el motivo si no se puede fusionar.
        """
        agent = self._require(agent_id)
        if agent.get("pr_status") != "open":
            raise ValueError(f"El agente {agent_id} no tiene un PR abierto.")
        if agent["state"] not in ("idle", "done") or self._busy(agent_id):
            raise ValueError(
                f"El agente {agent_id} está '{agent['state']}': espera a que termine para fusionar."
            )
        return agent

    # ------------------------------------------------------------------ #
    # Herramientas del agente (las llama src/worker/tools.py)
    # ------------------------------------------------------------------ #
    async def ask(self, agent_id: str, question: str, kind: str) -> str:
        """Bloquea al agente hasta que el gestor o el usuario respondan (D9).

        Mientras espera suelta su hueco de trabajo para no bloquear a los demás.
        """
        fut = asyncio.get_running_loop().create_future()
        self._answers[agent_id] = fut
        waiting_on = "user" if kind == "design" and not self.manager_first else "manager"
        if kind == "design" and self.manager_first:
            # Con Vision, el gestor ve todas las preguntas primero: la marca le dice
            # que el agente la considera de diseño (probablemente del usuario).
            question = f"[diseño] {question}"
        self.store.update(
            agent_id, state="waiting_input", substate=None, waiting_on=waiting_on, question=question
        )
        self._release(agent_id)
        escalation = None
        if waiting_on == "manager":
            escalation = asyncio.create_task(self._escalate_after(agent_id, fut))
        try:
            answer = await fut
        finally:
            if escalation:
                escalation.cancel()
            self._answers.pop(agent_id, None)
        await self._acquire(agent_id)
        self.store.update(agent_id, state="working", substate="thinking", waiting_on=None, question=None)
        return answer

    def record_pr_request(self, agent_id: str, data: dict) -> None:
        """Guarda los datos de PR que el agente dejó con ``submit_pr``."""
        self._pr_requests[agent_id] = data

    def usage(self) -> dict:
        """Uso conocido de la suscripción por ventana, el límite y si bloquea."""
        state, resets_at = self._usage_state()
        return {
            "windows": self.windows,
            "limit": self.usage_limit,  # el de 5 h (compatibilidad)
            "limits": {"five_hour": self.usage_limit, "seven_day": self.usage_limit_7d},
            "blocked": state != "ok",
            "unknown": state == "unknown",
            "resets_at": resets_at,
            "source": "probe" if self.probe_token else "events",
            "probed_at": self._probed_at,
            "probe_error": self._probe_error,
        }

    async def refresh_usage(self, force: bool = False) -> None:
        """Sondea el uso real si la última lectura tiene más de ``probe_every`` s.

        Nunca lanza: si la sonda falla se queda la lectura anterior, que deja de valer
        a los ``probe_stale`` s (entonces la salvaguarda bloquea por uso desconocido).
        """
        if not self.probe_token:
            return
        async with self._probe_lock:
            if not force and self._probed_at and time.time() - self._probed_at < self.probe_every:
                return
            try:
                windows = await asyncio.to_thread(usage_probe.probe, self.probe_token)
            except Exception as exc:  # noqa: BLE001 - la salvaguarda decide con lo que haya
                self._probe_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("Sonda de uso fallida: %s", self._probe_error)
                return
            now = time.time()
            for window in windows.values():
                window["seen_at"] = now
            self.windows.update(windows)
            self._probed_at = now
            self._probe_error = ""

    async def _probe_loop(self) -> None:
        """Sonda periódica mientras haya agentes vivos (sin agentes no se gasta nada)."""
        while True:
            try:
                if self.store.list(include_final=False):
                    await self.refresh_usage()
            except Exception:  # noqa: BLE001 - el bucle nunca debe morir
                log.exception("Fallo en el bucle de la sonda de uso")
            await asyncio.sleep(self.probe_every)

    def _usage_state(self, now: float | None = None) -> tuple[str, float | None]:
        """Estado de la salvaguarda: ``ok``, ``over`` o ``unknown`` (ver ``usage.assess``)."""
        return usage_probe.assess(
            self.windows, self.usage_limit, self.usage_limit_7d,
            probing=bool(self.probe_token), probed_at=self._probed_at,
            now=now or time.time(), stale_after=self.probe_stale,
        )

    def _over_usage(self, now: float | None = None) -> tuple[bool, float | None]:
        """Salvaguarda de uso: ¿bloquea? (ventana en su límite o uso desconocido).

        Returns:
            Tupla (bloqueado, hora de reinicio más tardía de las ventanas que bloquean).
        """
        state, resets_at = self._usage_state(now)
        return state != "ok", resets_at

    def _usage_message(self, resets_at: float | None) -> str:
        """Texto para el gestor cuando la salvaguarda de uso bloquea."""
        if self._usage_state()[0] == "unknown":
            return ("No se puede leer el uso de la suscripción (la sonda falla o no hay lectura "
                    "reciente): por seguridad no se lanzan agentes. "
                    f"{self._probe_error or 'Aún no hay lectura.'}")
        over = [
            f"{name} al {w['utilization'] * 100:.0f} % (límite "
            f"{usage_probe.limit_for(name, self.usage_limit, self.usage_limit_7d) * 100:.0f} %)"
            for name, w in self.windows.items()
            if w.get("utilization") is not None
            and w["utilization"] >= usage_probe.limit_for(name, self.usage_limit, self.usage_limit_7d)
        ]
        if resets_at:
            fmt = "%H:%M" if resets_at - time.time() < 86400 else "%d/%m %H:%M"
            when = time.strftime(fmt, time.localtime(resets_at))
        else:
            when = "desconocida"
        return (f"Uso de la suscripción por encima del límite de agentes: {', '.join(over)}. "
                f"Se reinicia a las {when}.")

    # ------------------------------------------------------------------ #
    # Ejecución
    # ------------------------------------------------------------------ #
    def _spawn(self, agent_id: str, coro) -> None:
        """Lanza la tarea de un agente y la registra."""
        self._tasks[agent_id] = asyncio.create_task(coro)

    def _busy(self, agent_id: str) -> bool:
        """True si el agente tiene una tarea en marcha (turno o ritual)."""
        task = self._tasks.get(agent_id)
        return bool(task and not task.done())

    @property
    def allowed_repos(self) -> list[str]:
        """Repos permitidos: la lista fija si se dio; si no, los bare de ``git_root`` ahora."""
        if self._allowed_repos is not None:
            return self._allowed_repos
        try:
            return sorted(p.name.removesuffix(".git") for p in self.git_root.glob("*.git") if p.is_dir())
        except OSError:
            return []

    async def _acquire(self, agent_id: str) -> None:
        """Toma un hueco de trabajo para el agente.

        Con el tope lleno espera a que se suelte uno; además mira cada pocos segundos por si
        se subió ``max_working`` en caliente. Al bajarlo no se corta a nadie: los que trabajan
        acaban y los nuevos esperan. Comprobar y apuntar van sin ``await`` entre medias.
        """
        while len(self._holding) >= self.max_working:
            self._slot_free.clear()
            try:
                await asyncio.wait_for(self._slot_free.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        self._holding.add(agent_id)

    def _release(self, agent_id: str) -> None:
        """Suelta el hueco del agente, si lo tiene (idempotente)."""
        if agent_id in self._holding:
            self._holding.discard(agent_id)
            self._slot_free.set()

    async def _run(
        self, agent_id: str, prompt: str | None = None, first: bool = False, cold: bool = False
    ) -> None:
        """Un turno completo: hueco → (sesión) → turno → PR → siguiente estado.

        Args:
            agent_id: Agente.
            prompt: Mensaje del turno (None en el primero: se usa la tarea).
            first: Primer turno (clona y abre la sesión).
            cold: Retomar en frío (sesión nueva sembrada con el handoff).
        """
        try:
            await self._acquire(agent_id)
            agent = self.store.get(agent_id)
            if agent["state"] != "queued":  # cancelado mientras esperaba
                return
            await self.refresh_usage()
            blocked, resets_at = self._over_usage()
            if blocked:
                # Salvaguarda: no empieza el turno. El mensaje se guarda para cuando
                # se reinicie la ventana (el primer turno usa la tarea, que ya está).
                # Sin repetir el "continúa" si ya venía de una reanudación.
                rest = (prompt or "").removeprefix(_RESUME_TEXT).strip()
                if rest and not first:
                    self._pending.setdefault(agent_id, []).insert(0, rest)
                raise RateLimited(resets_at, safeguard=True)
            self.store.update(agent_id, state="working", substate="setup", idle_warned=0, handoff_done=0)
            if first or cold:
                workdir = await asyncio.to_thread(self._prepare_workdir, agent_id)
                agent = self.store.get(agent_id)
                await self._open_client(agent_id, agent, workdir)
            if first:
                prompt = agent["task"]
            elif cold:
                prompt = _COLD_RESUME.format(
                    task=agent["task"], handoff=agent["handoff"] or agent["result"] or "(sin handoff)",
                    text=prompt,
                )
            await self._turn(agent_id, prompt)
            if await self._publish_pr(agent_id):
                self._send_red_suite(agent_id)
        except asyncio.CancelledError:
            raise
        except RateLimited as exc:
            await self._pause(agent_id, exc)
            return
        except Exception as exc:  # noqa: BLE001 - cualquier fallo deja el agente en failed
            log.exception("Agente %s falló", agent_id)
            self.store.update(agent_id, state="failed", substate=None, error=str(exc)[:500])
            await self._close_client(agent_id)
            return
        finally:
            self._release(agent_id)
        await self._after_turn(agent_id)

    async def _pause(self, agent_id: str, exc: RateLimited) -> None:
        """Límite de uso: guarda el trabajo, cierra la sesión y deja el agente en ``paused``.

        Con sesión abierta y margen (lo paró la salvaguarda, no la API), el agente
        escribe antes su handoff: al retomarlo, una sesión nueva arranca de ahí en vez
        de recargar toda la conversación sin caché. Se llama con el hueco tomado.
        """
        resume = exc.resets_at or (time.time() + 900)
        agent = self.store.get(agent_id)
        if exc.safeguard and agent_id in self._clients and (agent["turns"] or 0) > 0:
            why = "por el límite de uso de la suscripción (se retomará en una sesión nueva)"
            prompt = (_HANDOFF_CODE.format(branch=agent["branch"], why=why) if agent["mode"] == "code"
                      else _HANDOFF_READ.format(why=why))
            try:
                result_before = agent["result"]
                summary = await self._turn(agent_id, prompt, substate="handoff")
                self.store.update(agent_id, result=result_before, handoff=summary, handoff_done=1)
                await self._publish_pr(agent_id)
            except Exception:  # noqa: BLE001 - sin handoff se retoma con el último resultado
                log.exception("Agente %s: falló el handoff de la pausa", agent_id)
        await self._close_client(agent_id)
        log.warning("Agente %s: límite de uso → paused (ventana hasta %s)", agent_id, time.ctime(resume))
        self.store.update(agent_id, state="paused", substate=None, idle_since=None, resume_at=resume)

    async def _after_turn(self, agent_id: str, keep_idle_since: float | None = None) -> None:
        """Tras un turno: procesa mensajes pendientes o pasa a ``idle``."""
        pending = self._pending.pop(agent_id, [])
        if pending and agent_id in self._clients:
            self.store.update(agent_id, state="queued", idle_since=None)
            self._spawn(agent_id, self._run(agent_id, prompt="\n\n".join(pending)))
            return
        if self.store.get(agent_id)["state"] == "working":
            self.store.update(
                agent_id, state="idle", substate=None, idle_since=keep_idle_since or time.time()
            )
            await self._limit_idle()

    async def _turn(self, agent_id: str, prompt: str, substate: str = "thinking") -> str:
        """Ejecuta un turno y guarda resultado, coste, tokens y nº de turnos.

        El tope ``turn_timeout`` solo cuenta el tiempo en ``working``: una espera
        en ``ask`` no consume el presupuesto del turno.

        (f) Puntos de control: si la ventana de 5 h sube ``checkpoint`` desde el inicio
        del tramo, se interrumpe al agente (la sesión sigue viva, con su contexto) y se le
        manda ``_CHECKPOINT`` en la misma sesión: resume, estima lo que falta y sigue o
        pide a Vision que le recorte el alcance. Cada tramo es una fila de métricas.

        Returns:
            El texto final del agente.

        Raises:
            RateLimited: Si el turno chocó con el límite de uso.
            TimeoutError: Si trabajó más de ``turn_timeout``.
        """
        client = self._clients[agent_id]
        self.store.update(agent_id, state="working", substate=substate)
        worked = 0.0  # compartido por todos los tramos: el tope es del turno entero
        while True:
            started = time.time()
            usage_before = self._usage_snapshot()
            base = self._five_hour()
            texts: list[str] = []
            limit_hit: list[float | None] = []

            async def consume(query: str) -> ResultMessage | None:
                await client.query(query)
                final = None
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
                    elif isinstance(msg, RateLimitEvent):
                        info = msg.rate_limit_info
                        # Con sonda, sus lecturas mandan: el evento traía 0 % lejos del aviso
                        # y machacaría el dato real. El "rejected" sí cuenta siempre (abajo).
                        if not self.probe_token:
                            self.windows[info.rate_limit_type or "unknown"] = {
                                "status": info.status, "resets_at": info.resets_at,
                                "utilization": info.utilization, "seen_at": time.time(),
                            }
                        if info.status == "rejected":
                            limit_hit.append(info.resets_at)
                    elif isinstance(msg, ResultMessage):
                        final = msg
                return final

            task = asyncio.create_task(consume(prompt))
            checkpoint = False
            while True:
                done, _ = await asyncio.wait({task}, timeout=15)
                if done:
                    break
                if self.store.get(agent_id)["state"] != "working":
                    continue  # esperando en ask: ni cuenta tiempo ni se interrumpe
                worked += 15
                if worked > self.turn_timeout:
                    try:
                        await client.interrupt()
                    finally:
                        task.cancel()
                    raise TimeoutError(f"El agente trabajó más de {self.turn_timeout:.0f} s en un turno.")
                if base is None:
                    base = self._five_hour()  # sin lectura al empezar: cuenta desde la primera
                if not checkpoint and self._checkpoint_due(base, substate):
                    checkpoint = True
                    log.info("Agente %s: punto de control (5 h de %.0f %% a %.0f %%)",
                             agent_id, base * 100, (self._five_hour() or 0) * 100)
                    try:
                        await client.interrupt()
                    except Exception:  # noqa: BLE001 - si no se puede, el turno sigue sin control
                        log.exception("Agente %s: no se pudo interrumpir en el punto de control", agent_id)
            final = task.result()

            result = (final.result if final and final.result else "\n".join(texts)).strip()
            if final and final.is_error:
                if limit_hit or "limit" in result.lower():
                    raise RateLimited(limit_hit[0] if limit_hit else None)
                # Tras interrumpir, el CLI puede cerrar el tramo como error: es el corte buscado.
                if not checkpoint:
                    raise RuntimeError(result or "El agente terminó con error.")
            agent = self.store.get(agent_id)
            # total_cost_usd es ACUMULADO por sesión; usage (tokens) viene POR CONSULTA
            # (verificado 2026-10-10 con dos consultas en la misma sesión).
            session_total = (final.total_cost_usd or 0) if final else 0
            turn_cost = max(0.0, session_total - self._session_cost.get(agent_id, 0.0))
            if final:
                self._session_cost[agent_id] = session_total
            self.store.update(
                agent_id,
                result=result,
                cost_usd=(agent["cost_usd"] or 0) + turn_cost,
                turns=(agent["turns"] or 0) + (final.num_turns if final else 0),
            )
            await self._record_turn(agent, substate, started, turn_cost,
                                    final.num_turns if final else 0, usage_before,
                                    tokens=_tokens(final.usage if final else None),
                                    checkpoint=checkpoint)
            if not checkpoint:
                return result
            prompt = _CHECKPOINT.format(pct=self.checkpoint * 100)
            self.store.update(agent_id, state="working", substate="checkpoint")

    def _five_hour(self) -> float | None:
        """Utilización conocida de la ventana de 5 h (0–1), sin sondear."""
        return self.windows.get("five_hour", {}).get("utilization")

    def _checkpoint_due(self, base: float | None, substate: str) -> bool:
        """(f) ¿Toca punto de control? La ventana subió ``checkpoint`` desde ``base``.

        Nunca en el handoff (es el cierre de la sesión). Mide la ventana real, así que el
        uso en paralelo de la misma cuenta también cuenta: es lo que importa para los límites.
        """
        now = self._five_hour()
        if self.checkpoint <= 0 or substate == "handoff" or base is None or now is None:
            return False
        return now - base >= self.checkpoint - 1e-9

    def _usage_snapshot(self) -> dict:
        """Última lectura de uso conocida (sin sondear): utilización por ventana y su hora."""
        snap = {k: w.get("utilization") for k, w in self.windows.items()}
        snap["probed_at"] = self._probed_at
        return snap

    async def _record_turn(
        self, agent: dict, substate: str, started: float, cost: float, num_turns: int, before: dict,
        tokens: dict | None = None, checkpoint: bool = False,
    ) -> None:
        """(e) Apunta una fila por turno (o tramo): tokens, coste y uso de las ventanas antes/después.

        Los tokens son la medida buena (con muestras se extrapola cuánto % sube la ventana
        cada X tokens); el $ es una estimación del CLI y se guarda solo como referencia.
        Sin sondas extra: el "después" es la última lectura, refrescada solo si tiene más de
        ``probe_every`` s. Lo que gaste en paralelo la misma cuenta (Vision, la conversación con
        Jarvis…) ensucia el delta: vale como estimación, se afina con muchas muestras.
        """
        if not self.metrics_path:
            return
        try:
            await self.refresh_usage()
            row = {
                "ts": round(time.time(), 1), "agent": agent["id"], "repo": agent["repo"],
                "mode": agent["mode"], "substate": substate, "model": self.model,
                "secs": round(time.time() - started, 1), "tokens": tokens or {},
                "cost_usd": round(cost, 5), "num_turns": num_turns, "checkpoint": checkpoint,
                "before": before, "after": self._usage_snapshot(),
            }
            with self.metrics_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except Exception:  # noqa: BLE001 - una métrica perdida no tumba el turno
            log.exception("No se pudo guardar la métrica del turno de %s", agent["id"])

    def metrics(self, last: int = 50) -> dict:
        """Últimas filas de métricas y un resumen: tokens, coste y % de cada ventana movido.

        (e2) El % movido se mide de punta a punta por tandas (filas separadas por menos de
        ``_METRICS_GAP``), no sumando el delta de cada turno: con turnos cortos (< 120 s
        entre sondas) el antes y el después eran la misma lectura y todo salía 0 %.
        """
        rows = []
        if self.metrics_path and self.metrics_path.exists():
            with self.metrics_path.open(encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh if line.strip()]
        cost = sum(r.get("cost_usd") or 0 for r in rows)
        tokens = {k: sum((r.get("tokens") or {}).get(k, 0) for r in rows) for k in _TOKEN_KEYS}
        tokens["total"] = sum(tokens.values())
        # Las filas antiguas (antes de guardar tokens) suman $ pero no tokens: el % por
        # millón de tokens solo con las que los tienen, para no falsear la proporción.
        with_tokens = [r for r in rows if r.get("tokens")]
        summary: dict = {
            "turns": len(rows), "checkpoints": sum(1 for r in rows if r.get("checkpoint")),
            "tokens": tokens, "cost_usd": round(cost, 4),
        }
        for window in ("five_hour", "seven_day"):
            moved = _window_moved(rows, window)
            moved_tok = _window_moved(with_tokens, window)
            summary[f"{window}_pct_moved"] = round(moved * 100, 1)
            summary[f"{window}_pct_per_mtok"] = (
                round(moved_tok * 100 / (tokens["total"] / 1e6), 2) if tokens["total"] else None)
            summary[f"{window}_pct_per_usd"] = round(moved * 100 / cost, 2) if cost else None
        return {"summary": summary, "rows": rows[-last:]}

    async def _publish_pr(self, agent_id: str) -> bool:
        """Modo código: si hay commits, empuja la rama, actualiza el PR y pasa la suite.

        La suite la ejecuta el runner (cero tokens) cada vez que cambia la punta de la
        rama; el verde queda ligado a ese commit (``pr_tests_sha``) para que un verde
        viejo no valga tras un commit nuevo.

        Returns:
            True si la suite acaba de fallar sobre un commit nuevo.
        """
        agent = self.store.get(agent_id)
        request = self._pr_requests.pop(agent_id, {})
        if agent["mode"] != "code":
            return False
        workdir = self.work_root / agent_id
        ahead = await asyncio.to_thread(commits_ahead, workdir, agent["base_branch"])
        if not ahead:
            return False
        self.store.update(agent_id, substate="pushing")
        fields = await asyncio.to_thread(self.prs.publish, workdir, agent, request)
        self.store.update(agent_id, **fields)
        head = await asyncio.to_thread(git, workdir, "rev-parse", "HEAD")
        if head == agent.get("pr_tests_sha"):
            return False
        self.store.update(agent_id, substate="testing")
        passed, output = await asyncio.to_thread(run_suite, workdir)
        self.store.update(agent_id, pr_tests_passed=passed, pr_tests_output=output, pr_tests_sha=head)
        if passed:
            self._red_fixes.pop(agent_id, None)
        return not passed

    def _send_red_suite(self, agent_id: str) -> None:
        """Suite en rojo: se la devuelve al agente en el siguiente turno, con tope de intentos.

        Agotado el tope, el agente queda en ``idle`` con el PR en rojo y decide el gestor.
        """
        tries = self._red_fixes.get(agent_id, 0)
        if tries >= _MAX_RED_FIXES:
            log.warning("Agente %s: suite en rojo tras %d arreglos; decide el gestor", agent_id, tries)
            return
        self._red_fixes[agent_id] = tries + 1
        output = self.store.get(agent_id)["pr_tests_output"] or ""
        self._pending.setdefault(agent_id, []).append(_SUITE_RED.format(output=output[-2000:]))

    async def _open_client(self, agent_id: str, agent: dict, workdir: Path) -> None:
        """Abre la sesión del CLI de un agente con las opciones de su modo."""
        await self._close_client(agent_id)
        code = agent["mode"] == "code"
        server, tool_names = build_agent_server(self, agent_id, code_mode=code)
        builtin = CODE_TOOLS if code else READ_TOOLS
        rules = _COMMON_RULES + (
            _CODE_RULES.format(branch=agent["branch"], base=agent["base_branch"]) if code else _READ_RULES
        )
        await asyncio.to_thread(self._sync_agent_config)
        client = ClaudeSDKClient(options=ClaudeAgentOptions(
            model=self.model,
            effort=self.effort,
            cwd=str(workdir),
            tools=list(builtin),
            allowed_tools=list(builtin) + tool_names,
            mcp_servers={"agent": server},
            # La jaula es el contenedor (usuario no root, sin secretos ajenos).
            permission_mode="bypassPermissions",
            # "user": reglas comunes de los agentes (ver _sync_agent_config);
            # "project": el CLAUDE.md del repo, cuyas reglas valen también para el agente.
            setting_sources=["user", "project"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": rules},
        ))
        await client.connect()
        self._clients[agent_id] = client
        self._session_cost[agent_id] = 0.0

    def _sync_agent_config(self) -> None:
        """Instala las reglas comunes de los agentes como config de usuario del CLI.

        Clona (o actualiza) ``config_repo`` en ``work_root/../agent-config`` y deja en
        ``~/.claude`` un CLAUDE.md que solo importa ``agents/CLAUDE.md`` + un enlace a
        ``rules/``. Se llama antes de cada sesión: así un cambio de reglas llega al
        siguiente agente sin reconstruir la imagen. Se lee del bare (solo lo
        commiteado), no de una copia de trabajo a medias. Si falla, el agente
        arranca igual con lo que hubiera: las reglas no deben tumbar un encargo.
        """
        if not self.config_repo or not self.config_repo.is_dir():
            return
        dest = self.work_root.parent / "agent-config"
        try:
            if not (dest / ".git").is_dir():
                subprocess.run(
                    ["git", "clone", "--quiet", str(self.config_repo), str(dest)],
                    check=True, capture_output=True, text=True, timeout=120,
                )
            else:
                git(dest, "fetch", "--quiet", "origin")
                git(dest, "reset", "--quiet", "--hard", "origin/HEAD")
        except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
            log.warning("No se pudieron actualizar las reglas de los agentes: %s", exc)
        home = Path.home() / ".claude"
        home.mkdir(parents=True, exist_ok=True)
        if (dest / "agents" / "CLAUDE.md").is_file():
            (home / "CLAUDE.md").write_text(f"@{dest}/agents/CLAUDE.md\n")
        rules = home / "rules" / "shared"
        if (dest / "rules").is_dir() and not rules.is_symlink():
            rules.parent.mkdir(exist_ok=True)
            rules.symlink_to(dest / "rules")

    def _prepare_workdir(self, agent_id: str) -> Path:
        """Clon de trabajo del agente, listo en su rama (reutiliza el que haya).

        Primera vez: clona y, en modo código, crea la rama ``agent/*``. Al retomar
        en frío: reutiliza el clon si sigue ahí; si no, lo recrea desde la rama
        empujada (si existe) para no perder el trabajo hecho.
        """
        agent = self.store.get(agent_id)
        dest = self.work_root / agent_id
        if not (dest / ".git").is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--quiet", str(self.git_root / f"{agent['repo']}.git"), str(dest)],
                check=True, capture_output=True, text=True, timeout=120,
            )
        git(dest, "fetch", "--quiet", "origin")
        base = agent["base_branch"]
        if not base:
            # Trabajo autónomo sobre la rama de integración si existe (la crea el núcleo).
            wanted = self.base_branch and git(dest, "ls-remote", "--heads", "origin", self.base_branch, check=False)
            base = self.base_branch if wanted else (
                git(dest, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[-1])
        self.store.update(agent_id, base_branch=base)
        branch = agent["branch"]
        if branch:
            current = git(dest, "branch", "--show-current", check=False)
            if current != branch:
                remote = git(dest, "ls-remote", "--heads", "origin", branch, check=False)
                start = f"origin/{branch}" if remote else f"origin/{base}"
                git(dest, "checkout", "--quiet", "-B", branch, start)
        return dest

    # ------------------------------------------------------------------ #
    # Esperas, ritual de cierre y temporizadores
    # ------------------------------------------------------------------ #
    async def _escalate_after(self, agent_id: str, fut: asyncio.Future) -> None:
        """Si el gestor no responde a tiempo, la pregunta pasa al usuario (D9)."""
        await asyncio.sleep(self.manager_timeout)
        agent = self.store.get(agent_id)
        if not fut.done() and agent["state"] == "waiting_input" and agent["waiting_on"] == "manager":
            self.store.update(agent_id, waiting_on="user")

    async def _handoff(self, agent_id: str, close: bool = False) -> None:
        """Ritual de cierre (D10): el agente documenta cómo retomar en frío.

        Mantiene ``idle_since`` para no reiniciar los temporizadores del idle.

        Args:
            agent_id: Agente en ``idle`` con sesión viva.
            close: Si True, cierra la sesión al terminar (→ ``done``).
        """
        agent = self.store.get(agent_id)
        since = agent["idle_since"]
        try:
            await self._acquire(agent_id)
            if self.store.get(agent_id)["state"] != "idle":  # cancelado o retomado mientras esperaba hueco
                return
            why = "por inactividad"
            prompt = (_HANDOFF_CODE.format(branch=agent["branch"], why=why) if agent["mode"] == "code"
                      else _HANDOFF_READ.format(why=why))
            # El handoff no pisa el resultado de la tarea: se guarda aparte.
            result_before = agent["result"]
            summary = await self._turn(agent_id, prompt, substate="handoff")
            self.store.update(agent_id, result=result_before, handoff=summary, handoff_done=1)
            await self._publish_pr(agent_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - un ritual fallido no tumba al agente
            log.exception("Agente %s: falló el ritual de cierre", agent_id)
            self.store.update(agent_id, handoff_done=1)
        finally:
            self._release(agent_id)
        if close:
            await self._close_client(agent_id)
            self.store.update(agent_id, state="done", substate=None, idle_since=None)
            return
        await self._after_turn(agent_id, keep_idle_since=since)

    async def _retire(self, agent_id: str) -> None:
        """Cierra un ``idle``: ritual de cierre si falta y sesión cerrada → ``done``."""
        if not self.store.get(agent_id)["handoff_done"]:
            await self._handoff(agent_id, close=True)
            return
        await self._close_client(agent_id)
        self.store.update(agent_id, state="done", substate=None, idle_since=None)

    async def _close_client(self, agent_id: str) -> None:
        """Cierra la sesión del CLI de un agente, si la tiene."""
        client = self._clients.pop(agent_id, None)
        self._session_cost.pop(agent_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - cerrar es best-effort
                log.warning("Agente %s: fallo al cerrar la sesión", agent_id)

    async def _limit_idle(self) -> None:
        """Si hay más ``idle`` vivos que ``max_working``, retira los más antiguos."""
        idle = sorted(
            (a for a in self.store.list() if a["state"] == "idle"
             and a["id"] in self._clients and not self._busy(a["id"])),
            key=lambda a: a["idle_since"] or 0,
        )
        for agent in idle[: max(0, len(idle) - self.max_working)]:
            self._spawn(agent["id"], self._retire(agent["id"]))

    async def _reap_loop(self) -> None:
        """Revisa periódicamente el ciclo del ``idle`` (D10) y los ``rate_limited``."""
        while True:
            await asyncio.sleep(_REAP_INTERVAL)
            try:
                self._reap_once(time.time())
            except Exception:  # noqa: BLE001 - el bucle nunca debe morir
                log.exception("Fallo revisando temporizadores")

    def _reap_once(self, now: float) -> None:
        """Una pasada de temporizadores."""
        for agent in self.store.list(include_final=False):
            agent_id = agent["id"]
            if self._busy(agent_id):
                continue
            if agent["state"] == "idle" and agent_id in self._clients and agent["idle_since"]:
                idle_for = now - agent["idle_since"]
                if idle_for > self.idle_ttl:
                    self._spawn(agent_id, self._retire(agent_id))
                elif idle_for > self.handoff_at and not agent["handoff_done"]:
                    self._spawn(agent_id, self._handoff(agent_id))
                elif idle_for > self.idle_warn and not agent["idle_warned"]:
                    self.store.update(agent_id, idle_warned=1)
            elif (agent["state"] == "paused" and self.usage_mode == "autonomous"
                  and agent["resume_at"] and now >= agent["resume_at"]):
                # Modo autónomo: al reiniciarse la ventana, se retoma solo (en frío).
                self._resume(agent, "")
            elif agent["state"] == "rate_limited" and agent["resume_at"] and now >= agent["resume_at"]:
                # Estado antiguo (antes de ``paused``): se retoma como antes.
                self._resume(agent, "")

    def _resume(self, agent: dict, text: str) -> None:
        """Retoma un agente en pausa: de cero si no empezó; si no, en frío desde su handoff."""
        agent_id = agent["id"]
        self.store.update(agent_id, state="queued", resume_at=None)
        if not agent["turns"] and agent_id not in self._clients:
            if text:
                self.store.update(agent_id, task=f"{agent['task']}\n\nAdemás: {text}")
            self._spawn(agent_id, self._run(agent_id, first=True))
            return
        pending = self._pending.pop(agent_id, [])
        prompt = "\n\n".join([_RESUME_TEXT, *pending, *([text] if text else [])])
        self._spawn(agent_id, self._run(agent_id, prompt=prompt, cold=agent_id not in self._clients))

    def _require(self, agent_id: str) -> dict:
        """Agente por id o ValueError."""
        agent = self.store.get(agent_id)
        if agent is None:
            raise ValueError(f"No existe el agente {agent_id}.")
        return agent


def manager_from_env() -> AgentManager:
    """Construye el gestor a partir de las variables de entorno del worker."""
    data = Path(os.getenv("JARVIS_WORKER_DATA", "/data"))
    env = os.getenv
    return AgentManager(
        store=AgentStore(data / "agents.sqlite"),
        metrics_path=data / "turn_metrics.jsonl",
        git_root=Path(env("JARVIS_AGENT_GIT_ROOT", "/repos")),
        work_root=data / "work",
        max_working=int(env("JARVIS_AGENTS_MAX", "2")),
        model=env("JARVIS_AGENT_MODEL", "claude-sonnet-4-6"),
        effort=env("JARVIS_AGENT_EFFORT", "") or "low",
        idle_warn=float(env("JARVIS_AGENT_IDLE_WARN", "2100")),
        handoff_at=float(env("JARVIS_AGENT_HANDOFF_AT", "2700")),
        idle_ttl=float(env("JARVIS_AGENT_IDLE_TTL", "3300")),
        turn_timeout=float(env("JARVIS_AGENT_TURN_TIMEOUT", "1800")),
        manager_timeout=float(env("JARVIS_AGENT_MANAGER_TIMEOUT", "900")),
        manager_first=env("JARVIS_AGENT_ASK_MANAGER_FIRST", "1") != "0",
        usage_limit=_percent(env("JARVIS_AGENT_USAGE_LIMIT", "60")),
        usage_limit_7d=_percent(env("JARVIS_AGENT_USAGE_LIMIT_7D", "75")),
        usage_mode=env("JARVIS_AGENT_USAGE_MODE", "session").strip().lower(),
        # La sonda usa el mismo token que los agentes. JARVIS_USAGE_PROBE=0 la apaga.
        probe_token=(env("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
                     if env("JARVIS_USAGE_PROBE", "1") != "0" else ""),
        probe_every=float(env("JARVIS_USAGE_PROBE_EVERY", "120")),
        checkpoint=_percent(env("JARVIS_AGENT_CHECKPOINT", "5")),
        base_branch=env("JARVIS_INTEGRATION_BRANCH", "develop").strip(),
        config_repo=Path(env("JARVIS_AGENT_CONFIG_REPO", "/config.git")),
    )


def _percent(raw: str) -> float:
    """Porcentaje del entorno a fracción: "85" → 0.85 (acepta también "0.85")."""
    value = float(raw)
    return value / 100 if value > 1 else value
