"""Gestor de agentes: cola, sesiones del Agent SDK y máquina de estados.

Cada agente es una sesión ``ClaudeSDKClient`` (un proceso del CLI) con ``cwd`` en
su propio clon del repo. Ver docs/plan/hito-9-agentes.md (D1–D12). Ciclo de vida:

    queued → working ⇄ waiting_input
               │  └→ rate_limited → (resets_at) → working
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

from src.worker.prs import LocalPRProvider, PRProvider, commits_ahead, git
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
claros tus supuestos. Si te mandan un mensaje de seguimiento, contesta a eso."""

_READ_RULES = """
MODO SOLO LECTURA: puedes leer el código y buscar en la web, pero no modificar nada.
Cuando termines, responde con un informe breve en español (como máximo unas 15 líneas): conclusiones primero, \
sin tablas, porque Jarvis te lo resumirá en voz alta."""

_CODE_RULES = """
MODO CÓDIGO: trabajas en la rama {branch} (ya creada y activa; rama base: {base}).
- Cambios pequeños y enfocados en la tarea; sigue el CLAUDE.md del proyecto si lo hay.
- Si el proyecto tiene tests, ejecútalos y déjalos en verde (si necesitas dependencias, crea un entorno virtual en \
.venv y no lo commitees).
- Haz commit de tu trabajo con mensajes claros. NO hagas push ni cambies de rama: el sistema empuja tu rama.
- Al terminar, llama a submit_pr con título, descripción y resultado de los tests. Después responde con un resumen \
breve en español (como máximo 10 líneas), sin tablas."""

_HANDOFF_CODE = """\
RITUAL DE CIERRE: esta sesión se va a cerrar por inactividad. Escribe (o actualiza) el fichero `{branch}.md` en la \
raíz del repo con: qué se pidió, qué has hecho, estado actual (tests incluidos), qué falta y cómo retomarlo en frío \
sin esta conversación. Haz commit de ese fichero (sin push). Responde solo con un resumen de ese handoff en 5 líneas."""

_HANDOFF_READ = """\
RITUAL DE CIERRE: esta sesión se va a cerrar por inactividad. Responde solo con un handoff para retomar en frío sin \
esta conversación: qué se pidió, qué encontraste, qué quedó pendiente (como máximo 15 líneas)."""

_COLD_RESUME = """\
Retomas en frío una tarea anterior: es una sesión nueva y no tienes la conversación previa.
Tarea original:
{task}

Handoff de la sesión anterior:
{handoff}

Nuevo mensaje:
{text}"""


class RateLimited(Exception):
    """El agente chocó con el límite de uso de la suscripción."""

    def __init__(self, resets_at: float | None) -> None:
        super().__init__("Límite de uso alcanzado.")
        self.resets_at = resets_at


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
        allowed_repos: Repos que los agentes pueden usar (lista blanca).
        max_working: Agentes trabajando a la vez.
        model: Modelo de los agentes.
        idle_warn: Segundos en ``idle`` hasta el aviso.
        handoff_at: Segundos en ``idle`` hasta el ritual de cierre.
        idle_ttl: Segundos en ``idle`` hasta cerrar la sesión.
        turn_timeout: Tope de segundos TRABAJANDO por turno (sin contar esperas).
        manager_timeout: Segundos que una aclaración espera al gestor antes de pasar al usuario.
        usage_limit: Fracción de uso de la ventana de la suscripción (0–1) a partir de
            la cual no se crean agentes ni empiezan turnos nuevos (salvaguarda).
        pr_provider: Dónde se publican los PR.
    """

    def __init__(
        self,
        store: AgentStore,
        git_root: Path,
        work_root: Path,
        allowed_repos: list[str],
        max_working: int = 2,
        model: str = "claude-sonnet-4-6",
        idle_warn: float = 2100,
        handoff_at: float = 2700,
        idle_ttl: float = 3300,
        turn_timeout: float = 1800,
        manager_timeout: float = 300,
        usage_limit: float = 0.85,
        pr_provider: PRProvider | None = None,
    ) -> None:
        self.store = store
        self.git_root = git_root
        self.work_root = work_root
        self.allowed_repos = allowed_repos
        self.max_working = max_working
        self.model = model
        self.idle_warn = idle_warn
        self.handoff_at = handoff_at
        self.idle_ttl = idle_ttl
        self.turn_timeout = turn_timeout
        self.manager_timeout = manager_timeout
        self.usage_limit = usage_limit
        self.prs = pr_provider or LocalPRProvider()
        # Huecos de trabajo. Se gestionan a mano (no `async with`) porque un agente
        # que pregunta suelta el suyo y lo recupera al recibir la respuesta.
        self._slots = asyncio.Semaphore(max_working)
        self._holding: set[str] = set()
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # Mensajes recibidos mientras el agente trabajaba: se procesan al acabar el turno.
        self._pending: dict[str, list[str]] = {}
        # Respuestas que espera un agente bloqueado en `ask`.
        self._answers: dict[str, asyncio.Future] = {}
        # Datos de PR que el agente dejó con submit_pr en el turno en curso.
        self._pr_requests: dict[str, dict] = {}
        # Último estado de uso visto por ventana (five_hour, seven_day…), de los
        # RateLimitEvent del CLI. Alimenta la salvaguarda y GET /usage.
        self.windows: dict[str, dict] = {}
        self._reaper: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Ciclo de vida del gestor
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Limpia agentes huérfanos y arranca la revisión periódica de temporizadores."""
        orphans = self.store.fail_orphans()
        if orphans:
            log.warning("%d agentes de una ejecución anterior marcados como failed", orphans)
        self._reaper = asyncio.create_task(self._reap_loop())

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

    async def message(self, agent_id: str, text: str) -> dict:
        """Manda un mensaje a un agente; qué hace depende de su estado.

        - ``queued``: se añade a la tarea.
        - ``working`` / ``rate_limited``: se guarda y se le pasa en el siguiente turno.
        - ``waiting_input``: es la respuesta a su pregunta.
        - ``idle``: retoma la sesión (mismo contexto, caché caliente).
        - ``done``: sesión nueva sembrada con el handoff (retomar en frío).

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
        raise ValueError(f"El agente {agent_id} está '{state}' y ya no acepta mensajes.")

    def escalate(self, agent_id: str) -> dict:
        """El gestor pasa al usuario la pregunta pendiente de un agente (D9)."""
        agent = self._require(agent_id)
        if agent["state"] != "waiting_input":
            raise ValueError(f"El agente {agent_id} no está esperando una respuesta.")
        return self.store.update(agent_id, waiting_on="user")

    async def cancel(self, agent_id: str) -> dict:
        """Cancela un agente (interrumpe su turno si está trabajando)."""
        agent = self._require(agent_id)
        if agent["state"] in ("done", "merged", "failed", "cancelled"):
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

    # ------------------------------------------------------------------ #
    # Herramientas del agente (las llama src/worker/tools.py)
    # ------------------------------------------------------------------ #
    async def ask(self, agent_id: str, question: str, kind: str) -> str:
        """Bloquea al agente hasta que el gestor o el usuario respondan (D9).

        Mientras espera suelta su hueco de trabajo para no bloquear a los demás.
        """
        fut = asyncio.get_running_loop().create_future()
        self._answers[agent_id] = fut
        waiting_on = "user" if kind == "design" else "manager"
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
        blocked, resets_at = self._over_usage()
        return {
            "windows": self.windows,
            "limit": self.usage_limit,
            "blocked": blocked,
            "resets_at": resets_at,
        }

    def _over_usage(self, now: float | None = None) -> tuple[bool, float | None]:
        """Salvaguarda de uso: ¿alguna ventana vigente está en el límite o por encima?

        Solo cuentan las lecturas vigentes (ventana aún sin reiniciar). Sin datos,
        no se bloquea.

        Returns:
            Tupla (bloqueado, hora de reinicio más tardía de las ventanas que bloquean).
        """
        now = now or time.time()
        resets = [
            w.get("resets_at") for w in self.windows.values()
            if w.get("utilization") is not None and w["utilization"] >= self.usage_limit
            and (not w.get("resets_at") or w["resets_at"] > now)
        ]
        if not resets:
            return False, None
        known = [r for r in resets if r]
        return True, (max(known) if known else None)

    def _usage_message(self, resets_at: float | None) -> str:
        """Texto para el gestor cuando la salvaguarda de uso bloquea."""
        pct = max((w.get("utilization") or 0) for w in self.windows.values()) * 100
        when = time.strftime("%H:%M", time.localtime(resets_at)) if resets_at else "desconocida"
        return (f"Uso de la suscripción al {pct:.0f} %, por encima del límite de agentes "
                f"({self.usage_limit * 100:.0f} %). La ventana se reinicia a las {when}.")

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

    async def _acquire(self, agent_id: str) -> None:
        """Toma un hueco de trabajo para el agente."""
        await self._slots.acquire()
        self._holding.add(agent_id)

    def _release(self, agent_id: str) -> None:
        """Suelta el hueco del agente, si lo tiene (idempotente)."""
        if agent_id in self._holding:
            self._holding.discard(agent_id)
            self._slots.release()

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
            blocked, resets_at = self._over_usage()
            if blocked:
                # Salvaguarda: no empieza el turno. El mensaje se guarda para cuando
                # se reinicie la ventana (el primer turno usa la tarea, que ya está).
                # Sin repetir el "continúa" si ya venía de una reanudación.
                rest = (prompt or "").removeprefix(_RESUME_TEXT).strip()
                if rest and not first:
                    self._pending.setdefault(agent_id, []).insert(0, rest)
                raise RateLimited(resets_at)
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
            await self._publish_pr(agent_id)
        except asyncio.CancelledError:
            raise
        except RateLimited as exc:
            resume = exc.resets_at or (time.time() + 900)
            log.warning("Agente %s: límite de uso; reanuda a las %s", agent_id, time.ctime(resume))
            self.store.update(agent_id, state="rate_limited", substate=None, resume_at=resume)
            return
        except Exception as exc:  # noqa: BLE001 - cualquier fallo deja el agente en failed
            log.exception("Agente %s falló", agent_id)
            self.store.update(agent_id, state="failed", substate=None, error=str(exc)[:500])
            await self._close_client(agent_id)
            return
        finally:
            self._release(agent_id)
        await self._after_turn(agent_id)

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
        """Ejecuta un turno y guarda resultado, coste y nº de turnos.

        El tope ``turn_timeout`` solo cuenta el tiempo en ``working``: una espera
        en ``ask`` no consume el presupuesto del turno.

        Returns:
            El texto final del agente.

        Raises:
            RateLimited: Si el turno chocó con el límite de uso.
            TimeoutError: Si trabajó más de ``turn_timeout``.
        """
        client = self._clients[agent_id]
        self.store.update(agent_id, state="working", substate=substate)
        texts: list[str] = []
        limit_hit: list[float | None] = []

        async def consume() -> ResultMessage | None:
            await client.query(prompt)
            final = None
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
                elif isinstance(msg, RateLimitEvent):
                    info = msg.rate_limit_info
                    self.windows[info.rate_limit_type or "unknown"] = {
                        "status": info.status, "resets_at": info.resets_at,
                        "utilization": info.utilization, "seen_at": time.time(),
                    }
                    if info.status == "rejected":
                        limit_hit.append(info.resets_at)
                elif isinstance(msg, ResultMessage):
                    final = msg
            return final

        task = asyncio.create_task(consume())
        worked = 0.0
        while True:
            done, _ = await asyncio.wait({task}, timeout=15)
            if done:
                break
            if self.store.get(agent_id)["state"] == "working":
                worked += 15
            if worked > self.turn_timeout:
                try:
                    await client.interrupt()
                finally:
                    task.cancel()
                raise TimeoutError(f"El agente trabajó más de {self.turn_timeout:.0f} s en un turno.")
        final = task.result()

        result = (final.result if final and final.result else "\n".join(texts)).strip()
        if final and final.is_error:
            if limit_hit or "limit" in result.lower():
                raise RateLimited(limit_hit[0] if limit_hit else None)
            raise RuntimeError(result or "El agente terminó con error.")
        agent = self.store.get(agent_id)
        self.store.update(
            agent_id,
            result=result,
            cost_usd=(agent["cost_usd"] or 0) + ((final.total_cost_usd or 0) if final else 0),
            turns=(agent["turns"] or 0) + (final.num_turns if final else 0),
        )
        return result

    async def _publish_pr(self, agent_id: str) -> None:
        """Modo código: si hay commits nuevos, empuja la rama y actualiza el PR."""
        agent = self.store.get(agent_id)
        request = self._pr_requests.pop(agent_id, {})
        if agent["mode"] != "code":
            return
        workdir = self.work_root / agent_id
        ahead = await asyncio.to_thread(commits_ahead, workdir, agent["base_branch"])
        if not ahead:
            return
        self.store.update(agent_id, substate="pushing")
        fields = await asyncio.to_thread(self.prs.publish, workdir, agent, request)
        self.store.update(agent_id, **fields)

    async def _open_client(self, agent_id: str, agent: dict, workdir: Path) -> None:
        """Abre la sesión del CLI de un agente con las opciones de su modo."""
        await self._close_client(agent_id)
        code = agent["mode"] == "code"
        server, tool_names = build_agent_server(self, agent_id, code_mode=code)
        builtin = CODE_TOOLS if code else READ_TOOLS
        rules = _COMMON_RULES + (
            _CODE_RULES.format(branch=agent["branch"], base=agent["base_branch"]) if code else _READ_RULES
        )
        client = ClaudeSDKClient(options=ClaudeAgentOptions(
            model=self.model,
            cwd=str(workdir),
            tools=list(builtin),
            allowed_tools=list(builtin) + tool_names,
            mcp_servers={"agent": server},
            # La jaula es el contenedor (usuario no root, sin secretos ajenos).
            permission_mode="bypassPermissions",
            # Carga el CLAUDE.md del proyecto: sus reglas valen también para el agente.
            setting_sources=["project"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": rules},
        ))
        await client.connect()
        self._clients[agent_id] = client

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
        base = agent["base_branch"] or git(dest, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[-1]
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
            prompt = (_HANDOFF_CODE.format(branch=agent["branch"]) if agent["mode"] == "code"
                      else _HANDOFF_READ)
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
            elif agent["state"] == "rate_limited" and agent["resume_at"] and now >= agent["resume_at"]:
                self.store.update(agent_id, state="queued", resume_at=None)
                if not agent["turns"] and agent_id not in self._clients:
                    # Bloqueado antes de su primer turno: arranca de cero.
                    self._spawn(agent_id, self._run(agent_id, first=True))
                    continue
                pending = self._pending.pop(agent_id, [])
                text = "\n\n".join([_RESUME_TEXT, *pending])
                cold = agent_id not in self._clients
                self._spawn(agent_id, self._run(agent_id, prompt=text, cold=cold))

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
    env = os.getenv
    return AgentManager(
        store=AgentStore(data / "agents.sqlite"),
        git_root=Path(env("JARVIS_AGENT_GIT_ROOT", "/repos")),
        work_root=data / "work",
        allowed_repos=repos,
        max_working=int(env("JARVIS_AGENTS_MAX", "2")),
        model=env("JARVIS_AGENT_MODEL", "claude-sonnet-4-6"),
        idle_warn=float(env("JARVIS_AGENT_IDLE_WARN", "2100")),
        handoff_at=float(env("JARVIS_AGENT_HANDOFF_AT", "2700")),
        idle_ttl=float(env("JARVIS_AGENT_IDLE_TTL", "3300")),
        turn_timeout=float(env("JARVIS_AGENT_TURN_TIMEOUT", "1800")),
        manager_timeout=float(env("JARVIS_AGENT_MANAGER_TIMEOUT", "300")),
        usage_limit=_percent(env("JARVIS_AGENT_USAGE_LIMIT", "85")),
    )


def _percent(raw: str) -> float:
    """Porcentaje del entorno a fracción: "85" → 0.85 (acepta también "0.85")."""
    value = float(raw)
    return value / 100 if value > 1 else value
