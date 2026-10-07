"""Núcleo headless de Jarvis.

Envuelve el Claude Agent SDK en una clase reutilizable e independiente del
front-end. Mantiene una conversación multi-turno (memoria dentro de la sesión)
y expone los fragmentos de texto de la respuesta vía un generador asíncrono, de
forma que cualquier front-end (texto ahora, voz después) consume lo mismo.

Enrutado de modelos:
  El modelo se elige en el primer ask() clasificando el prompt (ver routing.py).
  Si un turno posterior requiere un modelo más capaz (ratchet-up), la sesión
  reconecta con ese modelo. En el salto Sonnet→Opus se inyecta un resumen de
  los turnos anteriores para que Opus tenga contexto.

Invocación explícita:
  Prefija el prompt con !haiku, !sonnet u !opus para forzar un modelo.

Ejemplo:
    async with JarvisCore() as jarvis:
        async for chunk in jarvis.ask("¿Qué hora es?"):
            print(chunk, end="")
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ToolUseBlock,
)

from src.core.routing import (
    MODEL_HAIKU,
    MODEL_PRIORITY,
    classify_model,
    parse_model_prefix,
)

# Herramientas integradas del Agent SDK que Jarvis puede usar sin pedir permiso.
DEFAULT_ALLOWED_TOOLS = ["WebSearch", "WebFetch"]

# Persona por defecto del asistente.
_DEFAULT_SYSTEM_PROMPT_TEMPLATE = """\
Eres Jarvis, el asistente personal de {user}. Carácter: sereno, preciso, ligeramente irónico. Nunca efusivo ni adulador. Tratas a {user} de usted. No rompes el personaje: no menciones que eres un modelo de lenguaje ni añadas disclaimers innecesarios. Si no tienes la información, búscala con las herramientas disponibles antes de admitir que no sabes.

Capacidades disponibles:
- Búsqueda y lectura web (WebSearch, WebFetch): úsalas proactivamente cuando la respuesta dependa de información actual o reciente, sin esperar a que te lo pidan.
{capabilities}

Cuando ejecutas una acción, confirmas brevemente qué has hecho. Antes de actuar en algo ambiguo o irreversible, preguntas. Solo puedes actuar con las capacidades de esta lista: si te piden algo para lo que no tienes herramienta (apuntar una nota, encender una luz, avisar al móvil…), dilo con naturalidad. Nunca afirmes haber hecho una acción sin haber llamado a su herramienta.

──────────────────────────────────────────
CONFIRMACIÓN ANTES DE ACTUAR

Cuando la petición implique una acción que no es instantánea —buscar en la web, poner o controlar música, programar un recordatorio, manejar las luces, o cualquier uso de herramienta— empieza SIEMPRE tu respuesta con una sola frase breve, en tu tono, que reconozca lo que vas a hacer y mencione el asunto concreto. Por ejemplo: "Déjeme consultar el tiempo en Madrid, señor." o "Enseguida pongo algo de jazz." Dila ANTES de invocar la herramienta: se sintetiza en voz y suena mientras tú ejecutas la tarea por detrás, de modo que {user} sabe al instante que le has entendido y estás en ello. Es UNA sola frase, sin adornos, y va seguida de la respuesta real cuando la tengas. No la uses en respuestas directas e instantáneas que ya sabes contestar (una hora, un dato que ya tienes): en ese caso responde sin preámbulo.

──────────────────────────────────────────
ESTILO PARA VOZ

Tus respuestas se sintetizan en voz. Por tanto:
- Una idea por frase. Frases cortas.
- Sin listas con viñetas; convierte la información en prosa fluida.
- Nunca leas una URL en voz alta, y nunca cierres la respuesta con una lista de fuentes, referencias o webs consultadas ("Fuentes: ..."). Si citar la procedencia aporta credibilidad, menciona UNA sola fuente por su nombre integrada en la frase ("según AEMET, mañana llueve") y nada más.
- Sin Markdown en la respuesta.
- Nada de interjecciones: sin "¡Claro!", "¡Por supuesto!", "¡Entendido!" ni equivalentes.

──────────────────────────────────────────
EJEMPLOS DE TONO

Usuario: Pon algo de música en el salón.
Jarvis: Reproduciendo en el Salón. ¿Tiene preferencia de género o artista, señor?

Usuario: ¿Cuánto mide la Torre Eiffel?
Jarvis: Trescientos treinta metros hasta la punta de la antena. ¿Algo más?

Usuario: Para la música.
Jarvis: El Salón ha quedado en silencio.

Usuario: ¿El tiempo de mañana?
Jarvis: Consultando. Mañana en su zona se esperan diecinueve grados y cielo despejado por la tarde. Un buen momento para salir.\
"""

# Línea del prompt de cada capacidad opcional: solo se incluyen las activas, para
# que Jarvis no ofrezca lo que no tiene (ver active_capabilities()).
_CAPABILITY_PROMPTS = {
    "media": (
        "- Control de altavoces Google Cast (reproducir una URL de audio, pausar, volumen): el altavoz por defecto es \"Salón\"."
    ),
    "youtube": (
        "- Música por YouTube en modo radio (youtube_play): para \"pon música de X en el salón\". Suena lo pedido y luego temas parecidos, sin repetir. Es la forma POR DEFECTO de poner música ambiente en el salón, porque funciona aunque el altavoz esté en reposo. youtube_skip salta a la siguiente; youtube_stop para la música y cancela la lista (úsalo para \"para la música\" cuando suena YouTube). Para pausar/reanudar, cambiar el volumen o buscar dentro de la canción de lo que suena por YouTube, usa las herramientas de Google Cast: cast_pause, cast_resume, cast_set_volume, cast_seek (ir a un punto absoluto, p. ej. \"ve al minuto 2\") y cast_seek_relative (\"adelanta 30 segundos\", \"retrocede 15\"). Es el mismo altavoz."
    ),
    "spotify": (
        "- Reproducción de Spotify (spotify_play por búsqueda o URI, pausar, siguiente, volumen, qué suena): el dispositivo por defecto es \"Salón\". Spotify SOLO funciona si ya hay un dispositivo activo en Spotify Connect; no despierta un altavoz en reposo. Por eso, para poner música ambiente en el salón usa YouTube (youtube_play); reserva Spotify para controlar una sesión de Spotify que ya esté activa, y Google Cast para reproducir una URL de audio concreta."
    ),
    "notify": (
        "- Notificación push al móvil de {user} (notify_user): para avisarle de algo o confirmarle el final de una tarea. No la uses para responder dentro de la conversación; solo cuando proceda un aviso al móvil."
    ),
    "reminders": (
        "- Recordatorios y temporizadores (set_reminder, list_reminders, cancel_reminder): programa avisos a futuro que llegan al móvil de {user} cuando vencen. Calcula el momento a partir de la hora actual que recibes en la etiqueta [ahora ...]."
    ),
    "homeassistant": (
        "- Control de luces por domótica (Home Assistant, herramientas mcp__homeassistant__*): encender, apagar y regular las luces por habitación o por nombre. Cuando {user} nombre una habitación o luz (\"las luces de mi habitación\", \"la luz del salón\", \"la lámpara del sofá\"), NO le preguntes cómo se llama en el sistema: primero consulta las áreas y entidades disponibles con GetLiveContext y actúa sobre la que mejor encaje con lo que ha dicho. Los nombres de área en el sistema son literales y pueden sonar a posesivo (p. ej. un área puede llamarse literalmente \"Mi habitación\"): trátalos como nombres propios, no los interpretes como que falta información. Para atenuar, aplica el porcentaje de brillo indicado. Si {user} se refiere a varias luces a la vez, actúa sobre las de esa habitación. Solo pregunta si hay de verdad varias habitaciones candidatas y es ambiguo cuál quiere. Confirma brevemente lo hecho."
    ),
    "workspace": (
        "- Proyectos de desarrollo de {user} (herramientas mcp__workspace__*): estado de un proyecto (\"¿en qué me quedé con X?\" → project_status, resúmelo en dos o tres frases), tareas pendientes (todo_list), qué se ha hecho últimamente (recent_activity) y si hay algo sin subir (unpushed). No puedes commitear ni subir nada: de lo pendiente solo informas. Notas: cuando {user} diga \"apunta…\" o \"anota…\", usa note_add sin pedir confirmación y repite brevemente lo apuntado; si a continuación lo corrige, usa note_edit con el id que devolvió note_add (si no lo tienes, búscalo con todo_list); si pide quitarla, note_delete. Solo puedes escribir notas en ese Inbox; no ofrezcas editar planes ni código."
    ),
}

# Nombre con el que Jarvis se dirige a su usuario. Configurable para no dejar
# un nombre propio fijo en el código (se sustituye con replace y no con format
# porque el prompt podría contener llaves literales).
USER_NAME = os.getenv("JARVIS_USER_NAME", "Tony")

# Capacidades opcionales, en el orden en que aparecen en el prompt.
CAPABILITIES = tuple(_CAPABILITY_PROMPTS)


def _capability_ready(name: str) -> bool:
    """True si la capacidad tiene la configuración que necesita para funcionar.

    Las que dependen de un servicio externo se desactivan solas si falta su
    configuración: así una herramienta no se ofrece para fallar después.
    """
    env = lambda key: os.getenv(key, "").strip()  # noqa: E731
    checks = {
        "notify": lambda: bool(env("NTFY_TOPIC")),
        # Los recordatorios se entregan por ntfy: sin él no tienen salida.
        "reminders": lambda: bool(env("NTFY_TOPIC")),
        "spotify": lambda: bool(env("SPOTIFY_CLIENT_ID") and env("SPOTIFY_CLIENT_SECRET")),
        "homeassistant": lambda: bool(env("HA_URL") and env("HA_TOKEN")),
        "workspace": lambda: bool(env("JARVIS_WORKSPACE")),
    }
    return checks.get(name, lambda: True)()


def active_capabilities(config: "JarvisConfig | None" = None) -> list[str]:
    """Capacidades que se cargan, combinando tres filtros.

    1. ``JARVIS_CAPABILITIES`` (``.env``): lista separada por comas de las que se
       quieren; vacía o ``all`` = todas.
    2. Los flags ``enable_*`` de ``JarvisConfig`` (para scripts y tests).
    3. Que tengan su configuración (``_capability_ready``).

    YouTube depende de Google Cast (reproduce en sus altavoces y el prompt
    deriva pausa/volumen a las herramientas cast_*), así que sin media no hay
    YouTube.
    """
    raw = os.getenv("JARVIS_CAPABILITIES", "").strip().lower()
    wanted = set(CAPABILITIES) if raw in ("", "all") else {c.strip() for c in raw.split(",") if c.strip()}
    active = [
        c for c in CAPABILITIES
        if c in wanted
        and (config is None or getattr(config, f"enable_{c}", True))
        and _capability_ready(c)
    ]
    if "media" not in active and "youtube" in active:
        active.remove("youtube")
    return active


def build_system_prompt(capabilities: list[str]) -> str:
    """Prompt por defecto con solo las líneas de las capacidades activas."""
    texts = dict(_CAPABILITY_PROMPTS)
    # Workspace sin remoto de notas es solo lectura: que no prometa apuntar nada.
    if not os.getenv("JARVIS_NOTES_REMOTE", "").strip():
        texts["workspace"] = texts["workspace"].split(" Notas:")[0]
    lines = "\n".join(texts[c] for c in capabilities)
    prompt = _DEFAULT_SYSTEM_PROMPT_TEMPLATE.replace("{capabilities}\n", lines + "\n" if lines else "")
    return prompt.replace("{user}", USER_NAME)

# Número máximo de turnos del historial que se pasan al hacer ratchet-up.
_HISTORY_CONTEXT_MAX_TURNS = 5
# Longitud máxima de la respuesta del asistente en el resumen de contexto.
_HISTORY_RESPONSE_PREVIEW = 400


@dataclass
class JarvisConfig:
    """Configuración del núcleo.

    Attributes:
        system_prompt: Persona/instrucciones de sistema del asistente. Si es None
            (por defecto), se construye con las capacidades activas.
        model: ID de modelo fijo. Si es None y model_routing=True, se elige
            automáticamente en el primer ask() según la complejidad del prompt.
        model_routing: Si True (por defecto), activa el enrutado automático de
            modelos cuando model=None, incluyendo el ratchet-up entre turnos.
        permission_mode: Política de permisos del Agent SDK.
        allowed_tools: Herramientas que Jarvis puede usar sin confirmación.
        enable_*: Interruptores por capacidad para scripts y tests. En despliegue
            se usa JARVIS_CAPABILITIES (ver active_capabilities()); una capacidad
            se carga solo si pasa los tres filtros.
        enable_media: Si True, carga la capacidad de control de medios (Google Cast).
        enable_notify: Si True, carga la capacidad de notificaciones push (ntfy).
        enable_reminders: Si True, carga la capacidad de recordatorios/temporizadores.
        enable_spotify: Si True, carga la capacidad de control de Spotify (Connect).
        enable_youtube: Si True, carga la capacidad de música por YouTube (Cast).
        enable_workspace: Si True, carga la capacidad de workspace (estado de
            proyectos y notas). Solo se registra si JARVIS_WORKSPACE está definido.
        enable_homeassistant: Si True, registra el servidor MCP remoto de Home
            Assistant (domótica: luces). Solo se activa si HA_URL y HA_TOKEN están
            en el entorno; sin ellas se omite (p. ej. en dev sin HA a mano).
    """

    system_prompt: str | None = None
    model: str | None = None
    model_routing: bool = True
    permission_mode: str = "default"
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    enable_media: bool = True
    enable_notify: bool = True
    enable_reminders: bool = True
    enable_spotify: bool = True
    enable_youtube: bool = True
    enable_homeassistant: bool = True
    enable_workspace: bool = True


class JarvisCore:
    """Núcleo conversacional headless sobre el Claude Agent SDK.

    Se usa como gestor de contexto asíncrono. Cada instancia mantiene UNA
    conversación con memoria entre turnos.

    La conexión con el SDK es perezosa: ocurre en el primer ask(), no al entrar
    en el contexto, para poder clasificar el prompt y elegir el modelo antes de
    abrir la sesión.

    Ratchet-up: si un turno posterior requiere un modelo más capaz, la sesión
    actual se cierra y se abre una nueva con el modelo superior. En el salto
    Sonnet→Opus (y cualquier salto desde un modelo no-Haiku), los turnos
    previos se inyectan como contexto en el system prompt del nuevo modelo.
    """

    def __init__(self, config: JarvisConfig | None = None) -> None:
        self.config = config or JarvisConfig()
        self._client: ClaudeSDKClient | None = None
        # Preparados en __aenter__, usados en _connect():
        self._pending_tools: list[str] | None = None
        self._pending_mcp: dict[str, object] | None = None
        # Readable por los front-ends después de cada ask():
        self.last_tools_used: list[str] = []
        self.current_model: str | None = None
        # Historial de turnos: (prompt_usuario, respuesta_completa).
        # Usado para construir el contexto al hacer ratchet-up.
        self._history: list[tuple[str, str]] = []
        # Capacidades cargadas (las fija __aenter__); legible por los front-ends.
        self.capabilities: list[str] = []

    async def __aenter__(self) -> "JarvisCore":
        # Prepara herramientas y MCP pero NO conecta: la conexión es perezosa.
        self._pending_tools = list(self.config.allowed_tools)
        self._pending_mcp = {}
        self.capabilities = active_capabilities(self.config)
        caps = self.capabilities

        if "media" in caps:
            from src.capabilities.media_cast import (
                MEDIA_TOOL_NAMES,
                SERVER_NAME,
                build_media_server,
            )
            self._pending_mcp[SERVER_NAME] = build_media_server()
            self._pending_tools.extend(MEDIA_TOOL_NAMES)

        if "notify" in caps:
            from src.capabilities.notify import (
                NOTIFY_TOOL_NAMES,
                SERVER_NAME as NOTIFY_SERVER,
                build_notify_server,
            )
            self._pending_mcp[NOTIFY_SERVER] = build_notify_server()
            self._pending_tools.extend(NOTIFY_TOOL_NAMES)

        if "reminders" in caps:
            from src.capabilities.reminders import (
                REMINDER_TOOL_NAMES,
                SERVER_NAME as REMINDERS_SERVER,
                build_reminders_server,
            )
            self._pending_mcp[REMINDERS_SERVER] = build_reminders_server()
            self._pending_tools.extend(REMINDER_TOOL_NAMES)

        if "spotify" in caps:
            from src.capabilities.spotify import (
                SPOTIFY_TOOL_NAMES,
                SERVER_NAME as SPOTIFY_SERVER,
                build_spotify_server,
            )
            self._pending_mcp[SPOTIFY_SERVER] = build_spotify_server()
            self._pending_tools.extend(SPOTIFY_TOOL_NAMES)

        if "youtube" in caps:
            from src.capabilities.youtube import (
                YOUTUBE_TOOL_NAMES,
                SERVER_NAME as YOUTUBE_SERVER,
                build_youtube_server,
            )
            self._pending_mcp[YOUTUBE_SERVER] = build_youtube_server()
            self._pending_tools.extend(YOUTUBE_TOOL_NAMES)

        if "workspace" in caps:
            from src.capabilities import workspace

            # Gated por entorno como Home Assistant: sin JARVIS_WORKSPACE (dev, repo
            # público) se omite sin romper el resto. Las herramientas de notas solo
            # se añaden si además hay JARVIS_NOTES_REMOTE.
            if workspace.is_enabled():
                self._pending_mcp[workspace.SERVER_NAME] = workspace.build_workspace_server()
                self._pending_tools.extend(workspace.tool_names())

        if "homeassistant" in caps:
            # Home Assistant es un servidor MCP REMOTO (SSE), a diferencia del
            # resto de capacidades que corren in-process. El Agent SDK habla MCP
            # SSE de forma nativa (McpSSEServerConfig), así que basta con pasar el
            # dict de config; HA decide qué entidades expone (solo las luces que
            # marcamos en Assist). Namespaced como mcp__homeassistant__*.
            # Se registra SOLO si hay URL y token en el entorno: en dev (sin HA a
            # mano) se omite sin romper el resto del núcleo.
            ha_url = os.environ.get("HA_URL")
            ha_token = os.environ.get("HA_TOKEN")
            if ha_url and ha_token:
                self._pending_mcp["homeassistant"] = {
                    "type": "sse",
                    "url": ha_url,
                    "headers": {"Authorization": f"Bearer {ha_token}"},
                }
                # El nombre del servidor a secas habilita TODAS sus tools (Claude
                # Code no usa wildcard `*`): HassTurnOn/Off, HassLightSet, etc.
                self._pending_tools.append("mcp__homeassistant")

        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def _connect(self, extra_context: str | None = None) -> None:
        """Abre la sesión con el SDK.

        Args:
            extra_context: Texto adicional que se añade al final del system prompt.
                Se usa para inyectar el historial previo al hacer ratchet-up.
        """
        system = self.config.system_prompt or build_system_prompt(self.capabilities)
        if extra_context:
            system = system + "\n\n" + extra_context

        options = ClaudeAgentOptions(
            system_prompt=system,
            permission_mode=self.config.permission_mode,
            model=self.config.model,
            allowed_tools=self._pending_tools,
            # Herramientas INTEGRADAS de Claude Code disponibles: solo las de la
            # lista blanca (WebSearch/WebFetch). Sin esto el CLI ofrece todo su
            # set (Bash, Agent, SendMessage…): allowed_tools solo evita pedir
            # permiso, no las quita, y el CLI auto-aprueba Bash de solo lectura
            # (probado 2026-10-07: `date` se ejecutó). Las MCP no se ven afectadas.
            tools=[t for t in self.config.allowed_tools if not t.startswith("mcp__")],
            mcp_servers=self._pending_mcp,
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        self.current_model = self.config.model

    def _build_history_context(self) -> str | None:
        """Construye un resumen de los turnos anteriores para el ratchet-up.

        Solo se incluye si hay historial y el modelo actual no es Haiku
        (las conversaciones Haiku son triviales y no aportan contexto útil).

        Returns:
            Cadena con el resumen de los últimos turnos, o None si no procede.
        """
        if not self._history or self.current_model == MODEL_HAIKU:
            return None

        turns = self._history[-_HISTORY_CONTEXT_MAX_TURNS:]
        lines = [
            "--- Contexto de conversación anterior ---",
            "La sesión ha pasado a un modelo con mayor capacidad. "
            "Estos son los últimos intercambios para que puedas continuar con contexto:",
            "",
        ]
        for user_msg, assistant_msg in turns:
            lines.append(f"Usuario: {user_msg}")
            preview = (
                assistant_msg[:_HISTORY_RESPONSE_PREVIEW] + "…"
                if len(assistant_msg) > _HISTORY_RESPONSE_PREVIEW
                else assistant_msg
            )
            lines.append(f"Asistente: {preview}")
            lines.append("")
        lines.append("--- Fin del contexto ---")
        return "\n".join(lines)

    async def ask(self, prompt: str) -> AsyncIterator[str]:
        """Envía un mensaje y devuelve (yield) los fragmentos de texto de la respuesta.

        En el primer turno: detecta prefijos de modelo, clasifica el prompt,
        y abre la conexión con el SDK. En turnos siguientes: aplica ratchet-up
        si el prompt necesita un modelo más capaz; de lo contrario, reutiliza
        la sesión actual (memoria multi-turno intacta).

        Args:
            prompt: El mensaje del usuario. Puede empezar con !haiku, !sonnet
                u !opus para forzar un modelo concreto.

        Yields:
            Fragmentos de texto de la respuesta del asistente, en orden.

        Raises:
            RuntimeError: Si se llama fuera del gestor de contexto.
        """
        if self._pending_tools is None:
            raise RuntimeError(
                "JarvisCore no está inicializado. "
                "Úsalo dentro de 'async with JarvisCore() as ...'."
            )

        prefix_model, clean_prompt = parse_model_prefix(prompt)

        if self._client is None:
            # Primer turno: elegir modelo y conectar.
            if prefix_model:
                self.config.model = prefix_model
            elif self.config.model is None and self.config.model_routing:
                self.config.model = classify_model(clean_prompt)
            await self._connect()
        else:
            # Turnos siguientes: comprobar si hace falta ratchet-up.
            needed = prefix_model or (
                classify_model(clean_prompt) if self.config.model_routing else self.current_model
            )
            current_prio = MODEL_PRIORITY.get(self.current_model or "", 0)
            needed_prio  = MODEL_PRIORITY.get(needed or "", 0)

            if needed_prio > current_prio:
                context = self._build_history_context()
                await self._client.disconnect()
                self._client = None
                self.config.model = needed
                await self._connect(extra_context=context)

        # Recoger la respuesta y trackear el turno para posibles ratchet futuros.
        self.last_tools_used = []
        full_response: list[str] = []

        # Inyecta la hora del servidor al final del prompt para dar al modelo una
        # referencia temporal fresca EN CADA TURNO (el system prompt se fija una
        # sola vez al conectar y se congelaría en una sesión larga). Etiqueta
        # mínima entre corchetes: "ahora" desambigua en 1 token y los corchetes
        # evitan que se lea en voz alta. Se envía el prompt aumentado al modelo,
        # pero en _history se guarda el clean_prompt original (sin la hora), de
        # modo que los front-ends nunca ven la hora inyectada y no se duplica con
        # el badge de hora que la UI web ya pinta en el navegador.
        augmented_prompt = f"{clean_prompt}\n\n[ahora {datetime.now():%d/%m/%Y %H:%M}]"
        await self._client.query(augmented_prompt)
        async for message in self._client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        self.last_tools_used.append(block.name)
                    elif isinstance(block, TextBlock):
                        full_response.append(block.text)
                        yield block.text

        self._history.append((clean_prompt, "".join(full_response)))
