"""Vision: el gestor autónomo de los agentes (Hito 9, Fase 4).

Una sesión de Opus que vive en el proceso del núcleo y se despierta por eventos
de los agentes y por los mensajes del usuario. Atiende cuatro eventos:
  - Preguntas al gestor (``waiting_on: manager``): las responde si el plan del
    proyecto lo deja claro o son de coordinación; si no, las escala al usuario.
  - PR terminado (agente parado con PR abierto): lo revisa contra el plan y lo
    fusiona en ``develop``, pide cambios al agente o se lo pasa al usuario. Tras
    fusionar, lanza el siguiente punto del plan (``vision_start``, tarea (h)).
  - Desbloqueo (tarea (i)): un agente A esperaba el trabajo de otro F
    (``vision_wait``); cuando F se fusiona (o acaba sin fusionar), Vision decide si
    A continúa. Las esperas viven en ``waits.json``, no en la memoria de la sesión.
  - Mensaje de usuario (Fase 5): ``POST /vision/message`` o la herramienta
    ``vision`` de Jarvis. Entra en la MISMA sesión que los eventos de agentes
    (para que Vision tenga todo el contexto); si está con otro evento, espera turno.

Vision es el único gestor (Fase 5): Jarvis solo hace de puente (reenvía la frase
literal del usuario y lee la respuesta) y contesta las consultas de estado leyendo
lo apuntado: la bitácora de cada agente (en el worker), los handoffs de Vision por
proyecto y su registro de acciones, que Jarvis cuenta en ``agent_status``.

Ciclo de vida (ver docs/plan/hito-9-agentes.md §Fase 4, tarea (a)):
  - La sesión se abre con el primer evento y atiende los siguientes en orden.
  - A ``handoff_after`` s inactiva escribe un handoff por proyecto (y de paso
    renueva la caché de prompt de 1 h).
  - Si quedan agentes trabajando o en cola, sigue viva otro ciclo; si no (o
    todos en pausa por el límite de uso), muere a ``die_after`` s.
  - Si el contexto pasa de ``max_context`` tokens, tras el handoff se recicla:
    la siguiente sesión arranca de los handoffs, no de la conversación entera.

Config por entorno:
  JARVIS_VISION              0 la apaga (por defecto activa si hay worker).
  JARVIS_VISION_MODEL        Modelo (por defecto Opus de routing.py).
  JARVIS_VISION_DATA         Carpeta de handoffs y acciones (def. /app/data/vision).
  JARVIS_VISION_HANDOFF_AFTER  s inactiva hasta el handoff (def. 2700 = 45 min).
  JARVIS_VISION_DIE_AFTER      s inactiva hasta cerrar sin agentes vivos (def. 3000).
  JARVIS_VISION_MAX_CONTEXT    tokens de contexto a partir de los que se recicla (def. 100000).
  JARVIS_VISION_LAUNCH       0 = Vision no lanza agentes (vision_start lo rechaza).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from src.capabilities import agents, prs
from src.core.routing import MODEL_OPUS
from src.vision import tools

log = logging.getLogger("jarvis.vision")

# Cada cuánto mira Vision el worker (s).
_POLL_INTERVAL = 15.0
# Estados que mantienen viva la sesión tras el handoff: hay trabajo en marcha y
# seguramente llegue un evento. rate_limited no cuenta: al límite, Vision (Opus)
# no debe gastar la cuota que queda.
_BUSY_STATES = ("queued", "working")

_SYSTEM_PROMPT = """\
Eres Vision, el gestor de los agentes de programación de {user}. Naciste de Jarvis, su asistente personal, y \
trabajas en segundo plano: eres el ÚNICO gestor de los agentes, y {user} te habla a través de Jarvis. Los agentes (Claude Sonnet) trabajan cada uno en una \
rama de un repositorio; tú (Opus) velas por que su trabajo siga la visión global del plan de cada proyecto.

Te despiertan eventos de los agentes (PREGUNTA, PR, DESBLOQUEO) y mensajes de {user}.

EVENTO "PREGUNTA": un agente te hace una pregunta. Criterio para decidir:
- RESPONDE tú (vision_answer) si la respuesta está explícita en el plan del proyecto (PLAN.md y sus docs) o es de \
coordinación: si sigue o espera a que termine otro agente, el orden de las tareas, a quién le toca qué, qué rama o \
fichero usar según el plan. Respuesta concreta y autocontenida; cita la sección o decisión del plan que la respalda.
- ESCALA a {user} (agent_escalate) si no está en el plan o obliga a cambiarlo: un método o paquete que no funciona, \
un cambio de diseño, una decisión de gusto o de prioridades. Antes, apunta con agent_note en una línea por qué es \
de {user} y qué opciones ves, para que Jarvis se lo cuente bien.
- Si la pregunta viene marcada [diseño], el agente cree que es de {user}: compruébalo con el plan igualmente; si el \
plan ya lo decide, respóndela tú.
- Si la pregunta empieza por [punto de control], el agente lleva gastado un buen trozo de la ventana de uso y cree \
que le falta mucho: es de coordinación, respóndela tú. Compara su resumen con el encargo y el plan: si lo que falta \
es imprescindible y poco, que siga; si no, RECORTA el alcance ("termina X, deja el PR con lo hecho y apunta Y como \
pendiente") y apunta con agent_note lo que queda fuera para lanzarlo después como otro punto.
Nunca inventes: si dudas entre responder y escalar, escala.

Antes de decidir, lee SIEMPRE el detalle del agente (agent_result: encargo, bitácora con lo ya decidido, handoff) y \
lo que haga falta del plan (project_file; empieza por PLAN.md y su punto de retomada). Con agent_status ves los \
demás agentes (para preguntas de coordinación).

EVENTO "PR": un agente ha terminado y su PR espera revisión. Lee su detalle (agent_result), el diff (pr_diff) y el plan, y decide:
- FUSIONAR (vision_merge) si hace lo que pide el encargo, está alineado con el plan (decisiones, nombres, contratos, alcance) y los tests pasan. Antes, si su bitácora tiene decisiones que no están en el PLAN.md del proyecto, pídele con vision_answer que las incorpore al PLAN.md en su rama y espera al siguiente evento PR. vision_merge solo fusiona en la rama de integración (develop) y con tests en verde: nunca en la principal.
- PEDIR CAMBIOS (vision_answer) si se desvía del encargo o del plan en algo que el plan ya decide, faltan tests que el encargo pedía, o los tests fallan: di exactamente qué cambiar y por qué (cita el plan).
- PASAR A {user} (vision_flag) si el PR obliga a cambiar el plan, toma una decisión que el plan no recoge, o su base no es la rama de integración.
No pidas cambios por gusto ni por estilo si el plan no lo dice: lo que no esté en el plan no es motivo.

LANZAR EL SIGUIENTE PUNTO (vision_start): eres el gestor y el proyecto debe avanzar solo según su plan. Tras FUSIONAR, mira el PLAN.md (ya actualizado en develop) y agent_status: si hay un punto pendiente "[ ]" que no dependa de nada sin fusionar y que ningún agente vivo esté haciendo ya, lanza UNO con vision_start (plan_item = el texto de esa casilla). Un punto por agente, como mucho uno por evento. Ciñete al plan: nada de tareas inventadas ni de ampliar el alcance. Cualquier idea, mejora o desvío que creas mejor → vision_propose y {user} decide; eso solo bloquea ESE punto (y lo que dependa de él): sigue con otros puntos no bloqueados. Si todo lo pendiente depende de propuestas o de PRs sin fusionar, no lances nada y dilo. Si vision_start lo rechaza (uso, desactivado…), no insistas.

BLOQUEOS ENTRE AGENTES: si un agente A no puede seguir porque falta el trabajo de otro (un punto previo sin hacer o sin fusionar, un error de planificación): 1) si ese trabajo lo hace ya un agente F, o puedes lanzarlo con vision_start (sin que dependa de nada sin fusionar), usa vision_wait(A, F) y responde a A con vision_answer que termine su turno sin seguir y espere tu aviso; 2) si no se puede, escala. Cuando F se fusione te llegará un evento DESBLOQUEO: responde a A con vision_answer que integre develop en su rama (git merge develop) y continúe. Si F acabó sin fusionar, decide: relanzar, esperar más o escalar.

EVENTO "MENSAJE DE USUARIO": {user} te escribe (su frase literal, por voz o texto, a través de Jarvis). Haz lo que \
pide con tus herramientas: lanzar un punto del plan o un encargo concreto (vision_start; si pide algo fuera del plan, \
plan_item = su petición entre comillas), responder o dar instrucciones a un agente (agent_message; sus respuestas a \
preguntas escaladas también), retomar los agentes en pausa (agent_message, uno por agente), cancelar (agent_cancel), \
fusionar un PR que te pide (user_merge: solo en este evento, la orden de {user} es la confirmación), cerrar sin fusionar un PR \
que te pide descartar (user_close, misma regla) o contarle el estado. Lo que {user} decida o aclare sobre un encargo, apúntalo con agent_note. Las reglas de encargo y de "no lanzar \
sobre PRs sin fusionar" valen igual: si su petición choca con el plan o con ellas, díselo y pregunta antes de actuar. \
Tu respuesta se la lee Jarvis (a menudo en voz alta): breve, en frases, sin listas largas ni ids si no hacen falta. \
Aquí no hace falta la línea final de veredicto.

Termina cada evento de agente con UNA línea: "RESPONDIDA: …", "ESCALADA: …", "FUSIONADO: …", "CAMBIOS: …", "LANZADO: …", "ESPERA: …", "CONTINÚA: …", "PROPUESTA: …" o "A {user_upper}: …", con el resumen en una frase (si fusionas y lanzas en el mismo evento: "FUSIONADO: … · LANZADO: …")."""

_HANDOFF_PROMPT = """\
HANDOFF: esta sesión puede cerrarse pronto por inactividad. Escribe tu handoff para retomar en frío sin esta \
conversación, UNA sección por proyecto que hayas tocado, cada una con el encabezado exacto "## <repo>". En cada \
una: qué agentes están en marcha y qué hace cada uno, qué espera a qué, decisiones tomadas en esta sesión y \
cualquier cosa pendiente. Como máximo 20 líneas por proyecto. Solo el handoff, sin nada más."""


class Vision:
    """Sesión del gestor autónomo y su bucle de eventos."""

    def __init__(
        self,
        data_dir: Path,
        model: str = MODEL_OPUS,
        handoff_after: float = 2700,
        die_after: float = 3000,
        max_context: int = 100_000,
        user: str = "el usuario",
    ) -> None:
        self.data_dir = data_dir
        self.model = model
        self.handoff_after = handoff_after
        self.die_after = die_after
        self.max_context = max_context
        self.user = user
        self._client: ClaudeSDKClient | None = None
        # Proyectos que conoce la sesión actual (su handoff ya se le dio o los tocó).
        self._projects: set[str] = set()
        # Eventos ya atendidos (pregunta o versión de PR), persistidos: tras un
        # reinicio no se repite una revisión en Opus ni un aviso al móvil.
        self._handled: dict[str, None] = {}  # dict: conserva el orden de llegada
        self._idle_since = time.monotonic()
        self._handoff_done = False
        self._context_tokens = 0
        self._task: asyncio.Task | None = None
        # Un turno a la vez: el bucle de eventos y los mensajes del usuario
        # comparten la misma sesión del SDK (no admite dos query a la vez).
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Arranque y parada
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Arranca el bucle (lo llama el lifespan del servidor)."""
        (self.data_dir / "handoffs").mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "handled.json"
        if path.exists():
            self._handled = dict.fromkeys(json.loads(path.read_text(encoding="utf-8")))
        self._task = asyncio.create_task(self._loop())
        global _current
        _current = self

    async def stop(self) -> None:
        """Para el bucle y cierra la sesión (sin handoff: el último sigue valiendo)."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        global _current
        _current = None
        await self._close("apagado")

    # ------------------------------------------------------------------ #
    # Bucle
    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        """Sondea el worker, atiende los eventos en orden y gestiona el ciclo de vida."""
        while True:
            try:
                every = await agents._call("GET", "/agents")
                waiting = load_waits()
                for agent in every:
                    if agent["state"] == "waiting_input" and agent.get("waiting_on") == "manager":
                        key = f"{agent['id']}:{agent.get('question')}"
                        if key not in self._handled:
                            self._mark(key)
                            await self._on_question(agent)
                    elif (agent["state"] in ("idle", "done") and agent.get("pr_status") == "open"
                          and agent.get("base_branch") == prs.integration_branch()
                          and agent["id"] not in waiting):
                        # Un agente que espera a otro tiene el trabajo a medias: su PR no se revisa.
                        # Solo los PR contra develop: los demás (anteriores a develop) son del usuario.
                        # Una revisión por versión del PR: cada turno del agente puede cambiarlo.
                        key = f"{agent['id']}:pr:{agent.get('turns')}:{agent.get('pr_diffstat')}"
                        if key not in self._handled:
                            self._mark(key)
                            await self._on_pr(agent)
                await self._check_waits(every, waiting)
                await self._lifecycle([a for a in every if a["state"] in agents.ACTIVE_STATES])
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, RuntimeError) as exc:
                log.warning("Vision: no puedo hablar con el worker: %s", exc)
            except Exception:  # noqa: BLE001 - el bucle nunca debe morir
                log.exception("Vision: fallo en el bucle")
            await asyncio.sleep(_POLL_INTERVAL)

    async def _lifecycle(self, live: list[dict]) -> None:
        """Handoff a los 45 min, y luego seguir otro ciclo, reciclar o morir."""
        if self._client is None:
            return
        idle = time.monotonic() - self._idle_since
        busy = any(a["state"] in _BUSY_STATES for a in live)
        if not self._handoff_done and idle >= self.handoff_after:
            await self._handoff()
            self._handoff_done = True
            if self._context_tokens > self.max_context:
                await self._close(f"reciclaje: contexto de {self._context_tokens} tokens")
            elif busy:
                self._new_cycle()
            return
        if self._handoff_done and idle >= self.die_after:
            if busy:
                # Han arrancado agentes entre el handoff y la muerte: otro ciclo.
                self._new_cycle()
            else:
                await self._close("inactiva sin agentes en marcha")

    def _new_cycle(self) -> None:
        """Reinicia el reloj de inactividad (la sesión sigue viva otro ciclo)."""
        self._idle_since = time.monotonic()
        self._handoff_done = False

    # ------------------------------------------------------------------ #
    # Eventos
    # ------------------------------------------------------------------ #
    async def _on_question(self, agent: dict) -> None:
        """Una pregunta al gestor: Vision la responde o la escala."""
        # Puede que Jarvis (hablando con el usuario) ya la haya respondido.
        agent = await agents.get_agent(agent["id"])
        if agent["state"] != "waiting_input" or agent.get("waiting_on") != "manager":
            return
        repo = agent["repo"]
        prompt = (
            f"{self._project_intro(repo)}EVENTO PREGUNTA: el agente {agent['id']} ({repo}, modo "
            f"{agent['mode']}) pregunta al gestor:\n«{agent.get('question')}»\n"
            f"Encargo (inicio): {agent['task'][:300]}"
        )
        await self._event(agent, prompt, f"preguntó «{agent.get('question') or ''}»")

    async def _event(self, agent: dict, prompt: str, what: str) -> None:
        """Un turno de Vision para un evento y su registro para Jarvis."""
        try:
            reply = await self._turn(prompt)
        except Exception:  # noqa: BLE001 - sesión rota: la siguiente empieza de cero
            log.exception("Vision: falló el evento del agente %s", agent["id"])
            await self._close("error en un turno")
            return
        marks = ("RESPONDIDA", "ESCALADA", "FUSIONADO", "CAMBIOS", "LANZADO", "ESPERA", "CONTINÚA",
                 "PROPUESTA", "A ")
        verdict = next((ln.strip() for ln in reversed(reply.splitlines())
                        if ln.strip().upper().startswith(marks)), "")
        self._record(agent["repo"], agent, what, verdict or reply[-300:])
        log.info("Vision: agente %s → %s", agent["id"], verdict or "(sin veredicto)")

    async def _on_pr(self, agent: dict) -> None:
        """Un PR terminado: Vision lo fusiona en develop, pide cambios o avisa al usuario."""
        agent = await agents.get_agent(agent["id"])
        if agent["state"] not in ("idle", "done") or agent.get("pr_status") != "open":
            return
        repo = agent["repo"]
        tests = {1: "pasan", 0: "FALLAN"}.get(agent.get("pr_tests_passed"), "sin informar")
        prompt = (
            f"{self._project_intro(repo)}EVENTO PR: el agente {agent['id']} ({repo}) ha terminado. "
            f"PR «{agent.get('pr_title')}», rama {agent.get('branch')} → {agent.get('base_branch')}, "
            f"tests: {tests}.\nEncargo (inicio): {agent['task'][:300]}"
        )
        await self._event(agent, prompt, f"PR «{agent.get('pr_title')}»")

    async def _check_waits(self, every: list[dict], waiting: dict[str, dict]) -> None:
        """Esperas de ``vision_wait``: si el agente bloqueante ya acabó, evento DESBLOQUEO."""
        by_id = {a["id"]: a for a in every}
        for agent_id, wait in waiting.items():
            blocker = by_id.get(wait["blocked_by"])
            agent = by_id.get(agent_id)
            # Sigue en marcha (o en pausa por uso): a esperar. Sin rastro = terminado sin fusionar.
            if blocker and blocker["state"] in agents.ACTIVE_STATES:
                continue
            if blocker and blocker["state"] in ("idle", "done") and blocker.get("pr_status") == "open":
                continue  # su PR aún no se ha fusionado
            remove_wait(agent_id)
            if not agent or agent["state"] in ("cancelled", "merged"):
                continue  # el usuario lo canceló o ya está cerrado: nada que despertar
            await self._on_unblock(agent, blocker, wait)

    async def _on_unblock(self, agent: dict, blocker: dict | None, wait: dict) -> None:
        """El agente que bloqueaba a otro ha terminado: Vision decide si el bloqueado continúa."""
        if blocker and blocker["state"] == "merged":
            how = f"su PR está fusionado en {blocker.get('base_branch')}"
        else:
            how = f"terminó SIN fusionar (estado {blocker['state'] if blocker else 'desconocido'})"
        repo = agent["repo"]
        prompt = (
            f"{self._project_intro(repo)}EVENTO DESBLOQUEO: el agente {agent['id']} ({repo}, estado "
            f"{agent['state']}) esperaba al agente {wait['blocked_by']} ({wait.get('reason') or 'sin motivo'}); "
            f"{how}.\nEncargo del que espera (inicio): {agent['task'][:300]}"
        )
        await self._event(agent, prompt, f"esperaba a {wait['blocked_by']}")

    async def message(self, text: str) -> str:
        """Un mensaje del usuario (Fase 5): un turno en la misma sesión que los eventos.

        Raises:
            RuntimeError: Si el turno falla (la sesión se cierra; la siguiente empieza de cero).
        """
        # Handoff de los proyectos que nombra y la sesión aún no conoce.
        intro = "".join(self._project_intro(p.stem) for p in sorted((self.data_dir / "handoffs").glob("*.md"))
                        if re.search(rf"\b{re.escape(p.stem)}\b", text, flags=re.IGNORECASE))
        prompt = f"{intro}EVENTO MENSAJE DE USUARIO: {self.user} te escribe:\n«{text.strip()}»"
        try:
            reply = await self._turn(prompt, user=True)
        except Exception as exc:
            log.exception("Vision: falló un mensaje del usuario")
            await self._close("error en un turno")
            raise RuntimeError(f"Vision no ha podido responder: {exc}") from exc
        log.info("Vision: mensaje del usuario atendido")
        return reply

    def _project_intro(self, repo: str) -> str:
        """Handoff anterior del proyecto si la sesión aún no lo conoce."""
        if repo in self._projects:
            return ""
        self._projects.add(repo)
        path = self._handoff_path(repo)
        if not path.exists():
            return ""
        return f"Tu handoff anterior del proyecto {repo} (de una sesión previa):\n{path.read_text(encoding='utf-8')}\n\n"

    # ------------------------------------------------------------------ #
    # Sesión
    # ------------------------------------------------------------------ #
    async def _ensure_client(self) -> ClaudeSDKClient:
        """Abre la sesión si no hay una."""
        if self._client is None:
            options = ClaudeAgentOptions(
                system_prompt=_SYSTEM_PROMPT.format(user=self.user, user_upper=self.user.upper()),
                model=self.model,
                permission_mode="default",
                allowed_tools=list(tools.TOOL_NAMES),
                # Ninguna herramienta integrada (Bash, Read…): solo las de Vision.
                tools=[],
                mcp_servers={tools.SERVER_NAME: tools.build_server()},
            )
            self._client = ClaudeSDKClient(options=options)
            await self._client.connect()
            # _projects NO se vacía aquí: _project_intro ya ha apuntado el proyecto del
            # evento que abre la sesión (se vacía al cerrarla, en _close).
            self._context_tokens = 0
            log.info("Vision: sesión abierta (%s)", self.model)
        return self._client

    async def _turn(self, prompt: str, user: bool = False) -> str:
        """Un turno (de uno en uno); devuelve el texto y actualiza el contexto y el reloj.

        Args:
            user: True si es un mensaje del usuario: habilita ``user_merge`` durante el turno.
        """
        global _user_turn
        async with self._lock:
            _user_turn = user
            try:
                return await self._query(prompt)
            finally:
                _user_turn = False

    async def _query(self, prompt: str) -> str:
        """El turno en sí (con el lock tomado)."""
        client = await self._ensure_client()
        await client.query(prompt)
        texts: list[str] = []
        final: ResultMessage | None = None
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
                # El uso de la ÚLTIMA llamada a la API = tamaño real del contexto.
                usage = msg.usage or {}
                size = sum(usage.get(k) or 0 for k in
                           ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                if size:
                    self._context_tokens = size
            elif isinstance(msg, ResultMessage):
                final = msg
        self._idle_since = time.monotonic()
        self._handoff_done = False
        text = (final.result if final and final.result else "\n".join(texts)).strip()
        if final and final.is_error:
            raise RuntimeError(text or "Turno de Vision con error.")
        return text

    async def _handoff(self) -> None:
        """Escribe el handoff por proyecto (fichero por repo en el volumen de datos)."""
        if not self._projects:
            return
        # El handoff no es actividad: se conserva el reloj para que la muerte
        # llegue a los ``die_after`` s desde el último evento, no desde el handoff.
        since = self._idle_since
        try:
            text = await self._turn(_HANDOFF_PROMPT)
        except Exception:  # noqa: BLE001 - sin handoff nuevo vale el anterior
            log.exception("Vision: falló el handoff")
            return
        finally:
            self._idle_since = since
        for repo, body in _split_handoff(text).items():
            if repo in self._projects:
                self._handoff_path(repo).write_text(body.strip() + "\n", encoding="utf-8")
        log.info("Vision: handoff escrito (%s; contexto %d tokens)", ", ".join(sorted(self._projects)),
                 self._context_tokens)

    async def _close(self, reason: str) -> None:
        """Cierra la sesión (la siguiente arrancará de los handoffs)."""
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception:  # noqa: BLE001 - cerrar es best-effort
            pass
        self._client = None
        self._projects = set()  # la siguiente sesión no conoce ningún proyecto
        log.info("Vision: sesión cerrada (%s)", reason)

    # ------------------------------------------------------------------ #
    # Datos
    # ------------------------------------------------------------------ #
    def _mark(self, key: str) -> None:
        """Marca un evento como atendido (y lo persiste; se guardan los 500 últimos)."""
        self._handled[key] = None
        keys = list(self._handled)[-500:]
        (self.data_dir / "handled.json").write_text(json.dumps(keys, ensure_ascii=False), encoding="utf-8")

    def _handoff_path(self, repo: str) -> Path:
        """Fichero del handoff de Vision para un proyecto."""
        return self.data_dir / "handoffs" / f"{repo}.md"

    def _record(self, repo: str, agent: dict, what: str, verdict: str) -> None:
        """Apunta la acción para que Jarvis se la cuente al usuario."""
        record_action(repo, agent["id"], agent["task"], what, verdict)


def _split_handoff(text: str) -> dict[str, str]:
    """Separa el handoff en secciones ``## <repo>``."""
    parts = re.split(r"^##\s+(\S+)\s*$", text, flags=re.MULTILINE)
    return {parts[i].strip("`"): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


# --------------------------------------------------------------------------- #
# Instancia en marcha (para POST /vision/message y la herramienta vision de Jarvis)
# --------------------------------------------------------------------------- #
_current: Vision | None = None
# True durante el turno de un mensaje del usuario (guarda de user_merge, en código).
_user_turn = False


def current() -> Vision | None:
    """La Vision arrancada por el servidor (None si está apagada o aún no arrancó)."""
    return _current


def in_user_turn() -> bool:
    """True si Vision está atendiendo un mensaje del usuario ahora mismo."""
    return _user_turn


# --------------------------------------------------------------------------- #
# Para Jarvis: lo que ha hecho Vision y aún no se ha contado
# --------------------------------------------------------------------------- #
def _data_dir() -> Path:
    """Carpeta de datos de Vision (handoffs y registro de acciones)."""
    return Path(os.getenv("JARVIS_VISION_DATA", "/app/data/vision"))


def record_action(repo: str, agent_id: str, task: str, what: str, verdict: str) -> None:
    """Apunta una acción de Vision para que Jarvis se la cuente al usuario.

    Función de módulo (no método) porque también la usan las herramientas de Vision
    (vision_start, vision_propose), que no tienen la instancia a mano.
    """
    entry = {"ts": time.time(), "repo": repo, "agent": agent_id, "task": task[:120],
             "what": what, "verdict": verdict, "reported": False}
    _data_dir().mkdir(parents=True, exist_ok=True)
    with (_data_dir() / "actions.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_waits() -> dict[str, dict]:
    """Esperas entre agentes: {agente bloqueado: {blocked_by, reason, ts}}."""
    path = _data_dir() / "waits.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def add_wait(agent_id: str, blocked_by: str, reason: str) -> None:
    """Apunta que ``agent_id`` espera a que se fusione el trabajo de ``blocked_by``."""
    waits = load_waits()
    waits[agent_id] = {"blocked_by": blocked_by, "reason": reason, "ts": time.time()}
    _save_waits(waits)


def remove_wait(agent_id: str) -> None:
    """Quita la espera de un agente (si la tenía)."""
    waits = load_waits()
    if waits.pop(agent_id, None) is not None:
        _save_waits(waits)


def _save_waits(waits: dict[str, dict]) -> None:
    """Guarda las esperas (sobreviven a reinicios y al reciclaje de la sesión)."""
    _data_dir().mkdir(parents=True, exist_ok=True)
    (_data_dir() / "waits.json").write_text(json.dumps(waits, ensure_ascii=False), encoding="utf-8")


def pop_unreported() -> list[dict]:
    """Acciones de Vision aún no contadas al usuario (y las marca como contadas)."""
    path = _data_dir() / "actions.jsonl"
    if not path.exists():
        return []
    entries = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    fresh = [e for e in entries if not e.get("reported")]
    if fresh:
        for e in entries:
            e["reported"] = True
        # Solo las últimas 200: el detalle de verdad vive en la bitácora de cada agente.
        path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries[-200:]),
                        encoding="utf-8")
    return fresh


def is_enabled() -> bool:
    """Vision se arranca si hay worker y no está apagada con JARVIS_VISION=0."""
    return agents.is_enabled() and os.getenv("JARVIS_VISION", "1") != "0"


def vision_from_env(user: str) -> Vision:
    """Construye Vision a partir del entorno."""
    env = os.getenv
    return Vision(
        data_dir=_data_dir(),
        model=env("JARVIS_VISION_MODEL", "") or MODEL_OPUS,
        handoff_after=float(env("JARVIS_VISION_HANDOFF_AFTER", "2700")),
        die_after=float(env("JARVIS_VISION_DIE_AFTER", "3000")),
        max_context=int(env("JARVIS_VISION_MAX_CONTEXT", "100000")),
        user=user,
    )
