"""Gestor de agentes: cola, sesiones del Agent SDK y máquina de estados.

Cada agente es una sesión ``ClaudeSDKClient`` (un proceso del CLI) con ``cwd`` en
su propio clon del repo. Ciclo de vida (ver docs/plan/hito-9-agentes.md):

    queued → working → idle ──(mensaje)──→ working → idle … ──(TTL)──→ done
                   └→ failed / cancelled / rate_limited

- ``JARVIS_AGENTS_MAX`` limita los agentes TRABAJANDO a la vez (la cuota de la
  suscripción se gasta trabajando). Los ``idle`` no cuentan, pero ocupan RAM (un
  proceso del CLI cada uno): si hay más ``idle`` que ``MAX``, se cierra el más
  antiguo.
- ``idle`` conserva la sesión para seguir con el mismo contexto y la caché de
  prompt caliente (1 h con la suscripción, verificado 2026-10-07); pasado
  ``JARVIS_AGENT_IDLE_TTL`` se cierra la sesión y el agente queda ``done``.

Fase 1: solo lectura (Read/Grep/Glob + web). La escritura, las ramas y los PR
llegan en la Fase 2.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
)

from src.worker.store import AgentStore

log = logging.getLogger("jarvis.worker")

# Herramientas de la Fase 1: el agente investiga y lee, no toca nada.
READ_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]

# Reglas que se añaden al prompt de Claude Code de cada agente.
_AGENT_RULES = """\
Eres un agente de Jarvis, el asistente personal del usuario, y trabajas en segundo plano en el repositorio de tu \
directorio actual. Nadie va a responder preguntas mientras trabajas: decide de forma razonable y explica tus supuestos.
MODO SOLO LECTURA: puedes leer el código y buscar en la web, pero no puedes modificar nada.
Cuando termines, responde con un informe breve en español (como máximo unas 15 líneas): conclusiones primero, \
sin tablas, porque Jarvis te lo resumirá en voz alta. Si te mandan un mensaje de seguimiento, contesta a eso."""


class AgentManager:
    """Coordina los agentes del worker.

    Args:
        store: Persistencia de estados.
        git_root: Carpeta con los bare repos (``<repo>.git``).
        work_root: Carpeta donde se clona cada agente.
        allowed_repos: Repos que los agentes pueden usar (lista blanca).
        max_working: Agentes trabajando a la vez.
        model: Modelo de los agentes.
        idle_ttl: Segundos que un agente ``idle`` conserva su sesión.
        turn_timeout: Tope de segundos por turno de un agente.
    """

    def __init__(
        self,
        store: AgentStore,
        git_root: Path,
        work_root: Path,
        allowed_repos: list[str],
        max_working: int = 2,
        model: str = "claude-sonnet-4-6",
        idle_ttl: float = 3600,
        turn_timeout: float = 1800,
    ) -> None:
        self.store = store
        self.git_root = git_root
        self.work_root = work_root
        self.allowed_repos = allowed_repos
        self.max_working = max_working
        self.model = model
        self.idle_ttl = idle_ttl
        self.turn_timeout = turn_timeout
        # El semáforo es el límite de concurrencia: solo lo retiene quien trabaja.
        self._slots = asyncio.Semaphore(max_working)
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # Mensajes recibidos mientras el agente trabajaba: se procesan al acabar el turno.
        self._pending: dict[str, list[str]] = {}
        self._reaper: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Ciclo de vida del gestor
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Limpia agentes huérfanos y arranca el cierre periódico de ``idle`` caducados."""
        orphans = self.store.fail_orphans()
        if orphans:
            log.warning("%d agentes de una ejecución anterior marcados como failed", orphans)
        self._reaper = asyncio.create_task(self._reap_idle_loop())

    async def shutdown(self) -> None:
        """Cierra todas las sesiones (al apagar el worker)."""
        if self._reaper:
            self._reaper.cancel()
        for task in list(self._tasks.values()):
            task.cancel()
        for agent_id in list(self._clients):
            await self._close_client(agent_id)

    # ------------------------------------------------------------------ #
    # API pública (la usa la app HTTP)
    # ------------------------------------------------------------------ #
    def create(self, repo: str, task: str) -> dict:
        """Encola un agente nuevo sobre ``repo`` con la tarea ``task``.

        Raises:
            ValueError: Si el repo no está en la lista blanca o no existe.
        """
        if repo not in self.allowed_repos:
            raise ValueError(f"Repo '{repo}' no permitido. Permitidos: {', '.join(self.allowed_repos)}.")
        if not (self.git_root / f"{repo}.git").is_dir():
            raise ValueError(f"No existe el repo '{repo}' en el servidor git.")
        agent_id = secrets.token_hex(3)
        agent = self.store.create(agent_id, repo, task.strip(), mode="read")
        self._tasks[agent_id] = asyncio.create_task(self._run_first(agent_id))
        return agent

    async def message(self, agent_id: str, text: str) -> dict:
        """Manda un mensaje a un agente según su estado.

        - ``queued``: se añade a la tarea.
        - ``working``: se guarda y se le pasa al acabar el turno.
        - ``idle``: retoma la sesión (misma conversación, caché caliente).

        Raises:
            ValueError: Si el agente no existe o ya terminó.
        """
        agent = self._require(agent_id)
        state = agent["state"]
        if state == "queued":
            return self.store.update(agent_id, task=f"{agent['task']}\n\nAdemás: {text}")
        if state == "working":
            self._pending.setdefault(agent_id, []).append(text)
            return agent
        if state == "idle" and agent_id in self._clients:
            self.store.update(agent_id, state="queued", idle_since=None)
            self._tasks[agent_id] = asyncio.create_task(self._run_followup(agent_id, text))
            return self.store.get(agent_id)
        raise ValueError(f"El agente {agent_id} está '{state}' y ya no acepta mensajes.")

    async def cancel(self, agent_id: str) -> dict:
        """Cancela un agente (interrumpe su turno si está trabajando)."""
        agent = self._require(agent_id)
        if agent["state"] in ("done", "merged", "failed", "cancelled"):
            return agent
        task = self._tasks.pop(agent_id, None)
        client = self._clients.get(agent_id)
        if client and agent["state"] == "working":
            try:
                await client.interrupt()
            except Exception:  # noqa: BLE001 - interrumpir es best-effort
                pass
        if task and not task.done():
            task.cancel()
        await self._close_client(agent_id)
        return self.store.update(agent_id, state="cancelled", substate=None, idle_since=None)

    # ------------------------------------------------------------------ #
    # Ejecución
    # ------------------------------------------------------------------ #
    async def _run_first(self, agent_id: str) -> None:
        """Primer turno: clona, abre la sesión y ejecuta la tarea."""
        async with self._slots:
            agent = self.store.get(agent_id)
            if agent["state"] != "queued":  # cancelado mientras esperaba
                return
            try:
                self.store.update(agent_id, state="working", substate="setup")
                workdir = await asyncio.to_thread(self._clone, agent_id, agent["repo"])
                client = ClaudeSDKClient(options=self._options(workdir))
                await client.connect()
                self._clients[agent_id] = client
                await self._turn(agent_id, agent["task"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - cualquier fallo deja el agente en failed
                log.exception("Agente %s falló", agent_id)
                self.store.update(agent_id, state="failed", substate=None, error=str(exc)[:500])
                await self._close_client(agent_id)
                return
        await self._after_turn(agent_id)

    async def _run_followup(self, agent_id: str, text: str) -> None:
        """Turno de seguimiento sobre una sesión ``idle``."""
        async with self._slots:
            if self.store.get(agent_id)["state"] != "queued":
                return
            try:
                await self._turn(agent_id, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Agente %s falló en el seguimiento", agent_id)
                self.store.update(agent_id, state="failed", substate=None, error=str(exc)[:500])
                await self._close_client(agent_id)
                return
        await self._after_turn(agent_id)

    async def _after_turn(self, agent_id: str) -> None:
        """Tras un turno: procesa mensajes pendientes o pasa a ``idle``."""
        pending = self._pending.pop(agent_id, [])
        if pending and agent_id in self._clients:
            self.store.update(agent_id, state="queued")
            self._tasks[agent_id] = asyncio.create_task(
                self._run_followup(agent_id, "\n\n".join(pending))
            )
            return
        if self.store.get(agent_id)["state"] == "working":
            self.store.update(agent_id, state="idle", substate=None, idle_since=time.time())
            await self._limit_idle()

    async def _turn(self, agent_id: str, prompt: str) -> None:
        """Ejecuta un turno y guarda el resultado, el coste y el nº de turnos."""
        client = self._clients[agent_id]
        self.store.update(agent_id, state="working", substate="thinking", idle_since=None)
        texts: list[str] = []

        async def consume() -> ResultMessage | None:
            await client.query(prompt)
            final = None
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
                elif isinstance(msg, RateLimitEvent):
                    # Fase 1: solo se registra. La pausa/reanudación es de la Fase 2.
                    log.warning("Agente %s: rate limit %s", agent_id, msg.rate_limit_info)
                elif isinstance(msg, ResultMessage):
                    final = msg
            return final

        final = await asyncio.wait_for(consume(), timeout=self.turn_timeout)
        agent = self.store.get(agent_id)
        result = (final.result if final and final.result else "\n".join(texts)).strip()
        if final and final.is_error:
            raise RuntimeError(result or "El agente terminó con error.")
        self.store.update(
            agent_id,
            result=result,
            cost_usd=(agent["cost_usd"] or 0) + ((final.total_cost_usd or 0) if final else 0),
            turns=(agent["turns"] or 0) + (final.num_turns if final else 0),
        )

    def _options(self, workdir: Path) -> ClaudeAgentOptions:
        """Opciones de la sesión de un agente (Fase 1: solo lectura)."""
        return ClaudeAgentOptions(
            model=self.model,
            cwd=str(workdir),
            tools=list(READ_TOOLS),
            allowed_tools=list(READ_TOOLS),
            # La jaula es el contenedor (usuario no root, sin secretos ajenos).
            permission_mode="bypassPermissions",
            # Carga el CLAUDE.md del proyecto: sus reglas valen también para el agente.
            setting_sources=["project"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": _AGENT_RULES},
        )

    def _clone(self, agent_id: str, repo: str) -> Path:
        """Clona el bare repo en la carpeta de trabajo del agente."""
        dest = self.work_root / agent_id
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--quiet", str(self.git_root / f"{repo}.git"), str(dest)],
            check=True, capture_output=True, text=True, timeout=120,
        )
        return dest

    # ------------------------------------------------------------------ #
    # Sesiones idle
    # ------------------------------------------------------------------ #
    async def _close_client(self, agent_id: str) -> None:
        """Cierra la sesión del CLI de un agente, si la tiene."""
        client = self._clients.pop(agent_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - cerrar es best-effort
                log.warning("Agente %s: fallo al cerrar la sesión", agent_id)

    async def _limit_idle(self) -> None:
        """Si hay más agentes ``idle`` que ``max_working``, cierra los más antiguos."""
        idle = sorted(
            (a for a in self.store.list() if a["state"] == "idle" and a["id"] in self._clients),
            key=lambda a: a["idle_since"] or 0,
        )
        for agent in idle[: max(0, len(idle) - self.max_working)]:
            await self._close_client(agent["id"])
            self.store.update(agent["id"], state="done", idle_since=None)

    async def _reap_idle_loop(self) -> None:
        """Cada minuto, cierra las sesiones ``idle`` que superan el TTL → ``done``."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for agent in self.store.list(include_final=False):
                since = agent["idle_since"]
                if agent["state"] == "idle" and since and now - since > self.idle_ttl:
                    await self._close_client(agent["id"])
                    self.store.update(agent["id"], state="done", idle_since=None)

    def _require(self, agent_id: str) -> dict:
        """Agente por id o ValueError."""
        agent = self.store.get(agent_id)
        if agent is None:
            raise ValueError(f"No existe el agente {agent_id}.")
        return agent


def manager_from_env() -> AgentManager:
    """Construye el gestor a partir de las variables de entorno del worker."""
    data = Path(os.getenv("JARVIS_WORKER_DATA", "/data"))
    repos = [r.strip() for r in os.getenv("JARVIS_AGENT_REPOS", "").split(",") if r.strip()]
    return AgentManager(
        store=AgentStore(data / "agents.sqlite"),
        git_root=Path(os.getenv("JARVIS_AGENT_GIT_ROOT", "/repos")),
        work_root=data / "work",
        allowed_repos=repos,
        max_working=int(os.getenv("JARVIS_AGENTS_MAX", "2")),
        model=os.getenv("JARVIS_AGENT_MODEL", "claude-sonnet-4-6"),
        idle_ttl=float(os.getenv("JARVIS_AGENT_IDLE_TTL", "3600")),
        turn_timeout=float(os.getenv("JARVIS_AGENT_TURN_TIMEOUT", "1800")),
    )
