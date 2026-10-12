# Jarvis

[🇬🇧 English](README.md) | 🇪🇸 Español

**Asistente de voz** autoalojado para casa, que habla español, al estilo del J.A.R.V.I.S. de Iron Man.
El razonamiento pesado lo hace **Claude** mediante el [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview),
autenticado con una **suscripción** normal de Claude (sin API key de pago por token). Todo lo demás
—voz a texto, texto a voz, wake word, control de medios y domótica— corre en local en un pequeño
servidor siempre encendido.

El núcleo es ***headless***: un servicio FastAPI con APIs HTTP y WebSocket. Voz y texto son
simplemente front-ends intercambiables que hablan con él.

## Funcionalidades

- **Claude como cerebro** vía Agent SDK, facturado contra la suscripción Pro/Max
  (`claude setup-token`).
- **Enrutado automático de modelos**: Haiku para charla, Sonnet para tareas reales, Opus para
  razonamiento profundo. Dentro de una sesión el modelo solo *sube* (llevándose el contexto
  reciente). Se puede forzar con el prefijo `!haiku` / `!sonnet` / `!opus`.
- **Personaje**: un mayordomo formal, sereno y de ironía seca, definido por completo en el system
  prompt (ejemplos few-shot, salida pensada para ser hablada y una frase de confirmación antes de
  las acciones lentas).
- **Pipeline de voz**: STT con [faster-whisper](https://github.com/SYSTRAN/faster-whisper) + TTS con
  [Piper](https://github.com/rhasspy/piper), streaming por frases (empieza a hablar tras la
  primera), limpieza de Markdown/URLs y reescritura fonética de anglicismos.
- **Front-ends**
  - **UI web de voz** (la sirve el propio núcleo): captura del micro en el navegador, audio por
    WebSocket, "orbe" animado, chat de texto y widget multimedia. Funciona en escritorio y móvil.
  - **Satélite ambiente**: contenedor aparte con micro USB, wake word (`hey_jarvis` de
    [openWakeWord](https://github.com/dscripka/openWakeWord)) + VAD Silero, turnos de seguimiento,
    interrupción ("Hey Jarvis, cancela") y respuestas por un altavoz **Google Cast**.
  - REPL de texto, cliente WebSocket remoto y cliente de escritorio push-to-talk.
- **Capacidades** (servidores MCP / herramientas en proceso)
  - Búsqueda y lectura web.
  - Control de **Google Cast**: reproducir, pausar, volumen, avanzar, qué suena.
  - **Música manos libres por YouTube**: búsqueda con `yt-dlp` → cola Mix de YouTube → cast, con
    un watcher que encadena las pistas.
  - Control de **Spotify Connect** (cuando hay un dispositivo activo).
  - **Notificaciones push** vía [ntfy](https://ntfy.sh).
  - **Recordatorios y temporizadores** que sobreviven a reinicios (APScheduler + SQLite), entregados por ntfy.
  - **Domótica** a través del servidor MCP de Home Assistant (luces por habitación o por nombre).
  - **Workspace**: el estado de tus proyectos de desarrollo por voz, y notas dictadas que se guardan en git.
  - **Agentes en segundo plano**: delega tareas en agentes de Claude que trabajan sobre un repo en un
    contenedor aislado mientras sigues hablando; Jarvis los vigila y te avisa cuando terminan.
  - **Vision, el gestor autónomo**: una sesión de Opus que revisa los pull requests de los agentes
    contra el plan del proyecto, los fusiona en `develop`, responde a sus preguntas y lanza el
    siguiente punto del plan, sin que tengas que estar. Se puede usar sin voz, por HTTP: ver
    [docs/vision.es.md](docs/vision.es.md).

Cada capacidad se puede activar o desactivar: ver [Capacidades](#capacidades).

El diseño y sus decisiones están en [docs/arquitectura.md](docs/arquitectura.md).

## Arquitectura en un vistazo

```
 Navegador (UI web) ──WS /voice──┐
 Satélite (micro + wake word) ───┤      ┌──────────── Núcleo Jarvis (FastAPI) ───────────┐
 Clientes de texto ───WS /ws─────┴────► │ STT (faster-whisper) → JarvisCore → TTS (Piper) │
                                        │        │ Claude Agent SDK (Haiku/Sonnet/Opus)   │
                                        │        └─ Tools MCP: web · Cast · YouTube ·     │
                                        │           Spotify · ntfy · recordatorios · HA · │
                                        │           workspace · agents                   │
                                        └────────────────────────────────────────────────┘
                                                 │ respuestas: stream de audio o Google Cast
```

## Requisitos

- Python 3.11+ (la imagen Docker usa 3.13) y Node.js (el Agent SDK usa el CLI de Claude Code).
- Unos **3 GB de RAM libre** con el modelo Whisper `medium` por defecto (medido: ~2 GB con los
  modelos de voz cargados, más ~0,35 GB por sesión abierta; `small` necesita bastante menos).
- Suscripción Claude Pro/Max y un token de larga duración generado con `claude setup-token`.
- **No** definir `ANTHROPIC_API_KEY`: si existe, el SDK factura por token e ignora la suscripción.
- Opcional, según capacidad: altavoces Google Cast en la LAN, un topic de ntfy, una app de Spotify
  Developer (cuenta Premium), Home Assistant con la integración MCP Server y un micro USB para el
  satélite.

## Arranque rápido (Docker)

```bash
cp .env.example .env
# Rellena CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), JARVIS_USER_NAME y las capacidades que quieras.
docker compose up -d --build jarvis
# UI web y API en http://<host>:8200  ·  health check: GET /health
# ¿Puerto ocupado? Define JARVIS_PORT=<puerto> (y TZ si no estás en Europe/Madrid) en .env.
```

El compose usa `network_mode: host` para que funcione el descubrimiento Cast/mDNS. El modelo de
Whisper se cachea en un volumen; los recordatorios y el token de Spotify viven en otro.

Para levantar también el satélite ambiente, pasa el dispositivo de sonido al contenedor y ajusta
las variables `JARVIS_SAT_*` de `docker-compose.yml` (micro, ganancia, altavoz Cast, umbrales):

```bash
docker compose up -d --build
```

> La UI web no tiene autenticación propia: ponla detrás de un proxy inverso con SSO
> (p. ej. Authelia) o déjala solo en la LAN.

## Capacidades

Todas las capacidades son opcionales. Una capacidad se carga solo si pasa **tres filtros**:

1. Está en `JARVIS_CAPABILITIES` (separadas por comas). Vacía o `all` = todas.
   Ejemplo, solo música y notas: `JARVIS_CAPABILITIES=media,youtube,workspace`.
2. Su flag `enable_*` de `JarvisConfig` está activo (por defecto; para scripts y tests).
3. Tiene su configuración. Las que dependen de un servicio externo **se desactivan solas** si faltan
   sus variables, así Jarvis nunca ofrece una herramienta que vaya a fallar.

El system prompt solo describe las capacidades activas. La búsqueda web está siempre activa.

| Nombre | Qué hace | Necesita | Se apaga sola si |
|---|---|---|---|
| `media` | Google Cast: reproducir una URL, pausa, volumen, seek, qué suena | Altavoces Cast en la LAN (`network_mode: host`) | – |
| `youtube` | Música manos libres: "pon X en el salón" → radio de un Mix de YouTube en un altavoz Cast | `media` | `media` está apagada |
| `spotify` | Control de Spotify Connect (solo con un dispositivo ya activo) | App de Spotify Developer + Premium; `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, token con `python -m scripts.spotify_auth` | faltan client ID/secret |
| `notify` | Notificaciones push al móvil | Un topic de ntfy: `NTFY_TOPIC` (+ `NTFY_BASE_URL`, `NTFY_TOKEN`) | `NTFY_TOPIC` vacío |
| `reminders` | Recordatorios y temporizadores que sobreviven a reinicios | `notify` (se entregan por ahí) | `NTFY_TOPIC` vacío |
| `homeassistant` | Luces por habitación o por nombre | Home Assistant con la integración MCP Server; `HA_URL`, `HA_TOKEN` | falta la URL o el token |
| `agents` | Agentes de Claude en segundo plano sobre tus repos: "que un agente revise X", "¿cómo van?", "¿qué ha encontrado?", "arregla X en Y" | El contenedor `jarvis-worker`; ver abajo | `JARVIS_AGENTS_URL` vacío |
| `workspace` | Tus proyectos por voz: "¿en qué me quedé con X?", tareas pendientes, commits recientes, qué falta por subir; notas dictadas en un *Inbox* | Tu carpeta de proyectos montada en solo lectura; ver abajo | `JARVIS_WORKSPACE` vacío |

### Configurar workspace

Copia `docker-compose.override.example.yml` como `docker-compose.override.yml` (ignorado por git;
Docker Compose lo fusiona solo) y pon las rutas de tu máquina. Después, en el `.env`:

| Variable | Ejemplo | Para qué |
|---|---|---|
| `JARVIS_WORKSPACE` | `/workspace` | Carpeta de proyectos (solo lectura). Cada proyecto es una carpeta con `PLAN.md` o `README.md`; "¿en qué me quedé con X?" lee la sección *punto de retomada* / *estado* de su `PLAN.md` |
| `JARVIS_GIT_ROOT` | `/opt/git` | Opcional. Carpeta con bare repos (`<proyecto>.git`) para "¿qué se hizo esta semana?" desde todos tus equipos |
| `JARVIS_NOTES_REMOTE` | `/opt/git/notes.git` | Opcional. Repo git de las notas. Sin él la capacidad es solo de lectura |
| `JARVIS_NOTES_FILE` | `TODO.md` | Fichero dentro de ese repo. Jarvis añade una sección **Inbox** y solo toca sus propias líneas |

Las notas se commitean y se **suben solas** como `Jarvis <jarvis@localhost>`, un commit por cambio
(añadir, corregir, borrar), sin reescribir nunca el historial. Jarvis repite lo que ha apuntado;
dile "no, era…" para corregirlo.

### Configurar agentes

Los agentes corren en un **contenedor aparte**, `jarvis-worker` (perfil de Compose `agents`), así nunca
ven el `.env` del núcleo, tu carpeta de proyectos ni otros tokens: solo el token de Claude, sus propios
clones y los bare repos que permitas. Escucha solo en `127.0.0.1` y exige un token compartido. Cada
agente es una sesión de Claude Code (Sonnet por defecto) sobre su propio clon del repo.

Dos modos:

- **read**: el agente lee el código y busca en la web, y después informa.
- **code**: el agente trabaja en su propia rama `agent/<id>-<slug>` y hace commits; el runner empuja
  **solo esa rama**, ejecuta él la suite de tests del proyecto (sin gastar tokens) y abre un **pull
  request ligero** (título, descripción, diffstat, resultado de los tests ligado a ese commit). Los agentes de código salen de una **rama de integración** (`develop`, que
  se crea y se pone al día con la principal sola); a la rama principal no llega nada sin ti.

**Vision** es el gestor ([docs/vision.es.md](docs/vision.es.md)): revisa cada PR terminado contra el
`PLAN.md` del proyecto y lo fusiona en `develop`, pide cambios al agente o te lo pasa, y después lanza
el siguiente punto del plan (un punto por agente). `develop` → principal lo revisas tú. La fusión está
protegida en el código: solo en la rama de integración y solo con la suite en verde sobre la punta actual de la rama.
Los agentes no editan el `PLAN.md` (lo mantiene Vision en `develop`) ni añaden tests salvo que Vision lo pida. También puedes
fusionar o cerrar un PR desde el panel **PRs** de la web (con su diff completo) o pidiéndoselo a Jarvis.

Los agentes pueden **preguntar** mientras trabajan: las preguntas van primero a Vision, que responde
lo que el plan deja claro y te escala el resto; si no responde en `JARVIS_AGENT_MANAGER_TIMEOUT`, la
pregunta pasa a ti. Un agente que espera libera su hueco para los demás. Un agente que necesita el
trabajo de otro puede esperarlo: Vision le dice que siga cuando ese PR se fusiona.

Un agente terminado queda en **idle** con su sesión (y la caché de prompt de una hora caliente) para
seguir. A los 35 minutos recibes un aviso; a los 45 escribe un **handoff** (en modo código, un
fichero con el nombre de su rama, `agent/<id>-<slug>.md`, commiteado en la rama) y a los 55 se
cierra la sesión. Si le escribes después, retoma desde el handoff en una sesión nueva.

**Uso**: agentes, Vision y Jarvis gastan de la cuenta de Claude del token que configures (y tus
propias sesiones de Claude también, si usas la misma cuenta). El worker lee el uso real (una sonda de
1 token a Haiku mientras hay agentes) y, al llegar a `JARVIS_AGENT_USAGE_LIMIT`, no lanza nada y pausa
los agentes tras guardar su trabajo (handoff); se retoman cuando lo dices, o solos al reiniciarse la
ventana (`JARVIS_AGENT_USAGE_MODE=autonomous`). Cada +5 % de la ventana de 5 h durante un turno
(`JARVIS_AGENT_CHECKPOINT`) el agente para, estima lo que le falta y pide a Vision que le recorte el
alcance si hace falta. `GET /api/metrics` en el núcleo da el gasto en tokens de agentes y Vision, por tarea
(lanzamiento, turnos del agente, revisión y fusión) y por turno.

1. En el `.env`: `JARVIS_AGENTS_URL=http://127.0.0.1:8113`, un `JARVIS_WORKER_TOKEN` aleatorio y
   `JARVIS_AGENT_REPOS_DIR` (por defecto `./agent-repos`).
2. Los repos permitidos son los bare que haya en `JARVIS_AGENT_REPOS_DIR`: el compose monta esa carpeta
   en `/repos` en el worker y en el núcleo (que fusiona ahí los PR), con propagación de montajes, así que
   dar uno de alta no pide reiniciar. Para cada repo, ejecuta antes, como root en el servidor git,
   `scripts/agent_repo_setup.sh <repo>.git` (da escritura al usuario del worker solo donde git la
   necesita e instala un hook `pre-receive` que rechaza cualquier push de ese usuario fuera de las
   ramas `agent/*`) y después `mount --bind /ruta/a/<repo>.git <JARVIS_AGENT_REPOS_DIR>/<repo>.git`
   (más una línea en `/etc/fstab`). Para darlo de baja, desmóntalo.
3. `docker compose --profile agents up -d --build jarvis-worker` y después reconstruye el núcleo.

**Ajustes en caliente:** el botón ⚙ de la web cambia sin reiniciar la mayoría de las variables de abajo
(agentes a la vez, modelos, esfuerzos, ciclo del idle, límites de uso hasta el 95 %, puntos de control,
ciclo de Vision…). El cambio manda sobre el `.env` hasta que lo restableces; se guarda en el volumen
`jarvis-settings` (`settings.json`, con un `audit.jsonl` de valor viejo → nuevo). API: `GET`/`PUT
/api/settings` (`{"CLAVE": valor | null}`, todo o nada; `null` vuelve al `.env`).

| Variable | Por defecto | Para qué |
|---|---|---|
| `JARVIS_AGENTS_MAX` | `2` | Agentes **trabajando** a la vez; el resto espera en cola |
| `JARVIS_AGENT_MODEL` | `claude-sonnet-4-6` | Modelo de los agentes |
| `JARVIS_AGENT_IDLE_WARN` / `_HANDOFF_AT` / `_IDLE_TTL` | `2100` / `2700` / `3300` | Ciclo del idle (s): aviso, handoff, cierre de la sesión |
| `JARVIS_AGENT_MANAGER_TIMEOUT` | `900` | Segundos que una pregunta espera al gestor antes de pasar a ti |
| `JARVIS_AGENT_TURN_TIMEOUT` | `1800` | Máximo de segundos trabajando por turno (esperar una respuesta no cuenta) |
| `JARVIS_AGENT_USAGE_LIMIT` / `_7D` | `60` / `75` | Salvaguarda (%) de la ventana de 5 h / semanal (cuenta todo lo que gaste esa cuenta): no arrancan agentes y los que trabajan se pausan tras un handoff |
| `JARVIS_INTEGRATION_BRANCH` | `develop` | Rama donde se fusiona el trabajo autónomo; vacía = desactivada |
| `JARVIS_AGENTS_SESSION_TIMEOUT_SECONDS` | `3600` | Timeout de la sesión de voz mientras gestiona agentes vivos (en vez de 90 s) |
| `JARVIS_WORKER_MEM_LIMIT` | `4g` | Tope de RAM del worker (~0,35 GB por sesión de agente viva) |

Las variables propias de Vision (`JARVIS_VISION*`) están en [docs/vision.es.md](docs/vision.es.md#configuración).

Estados de un agente (`GET /agents`): `queued`, `working`, `waiting_input` (ha preguntado algo),
`idle` (terminado, sesión conservada para seguir), `rate_limited`, `paused` (límite de uso, trabajo
guardado), `done` (sesión cerrada, se puede retomar), `merged`, `failed`, `cancelled`. Con ntfy llega un aviso cuando un agente termina (con su PR), falla,
se pausa, necesita tu respuesta o lleva un rato en idle; sin ntfy, Jarvis te lo cuenta en tu
siguiente turno. Pregunta "¿cómo van los agentes?", "¿qué ha encontrado?", "lanza el siguiente punto
de X", "fusiónalo", "cancélalo". Las consultas las responde Jarvis con lo apuntado; las peticiones de
actuar van a Vision. `GET /usage` en el worker muestra el último uso conocido de la ventana de la suscripción.

## Desarrollo local

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-voice.txt   # pila de voz (STT/TTS)
cp .env.example .env                 # y rellénalo

python scripts/smoke_test.py         # comprueba que el Agent SDK responde con tu suscripción
python -m src.core.server            # API + UI web en 127.0.0.1:8200
python -m src.frontends.cli          # REPL de texto
python -m src.frontends.voice        # push-to-talk de escritorio
```

El binario de Piper y la voz (`es_ES-davefx-medium`) van en `piper/` y `voices/` (ignorados por
git); las rutas se configuran con `JARVIS_PIPER_BIN` y `JARVIS_TTS_VOICE`.

### Variables de entorno útiles

| Variable | Defecto | Qué controla |
|----------|---------|--------------|
| `JARVIS_USER_NAME` | `Tony` | Cómo se dirige Jarvis a ti |
| `JARVIS_STT_MODEL` | `small` | Modelo Whisper (`medium` en el compose) |
| `JARVIS_MEM_LIMIT` | `4g` | Tope de RAM del contenedor Docker (sin swap); súbelo con modelos Whisper mayores |
| `JARVIS_STT_PRELOAD` | `1` | Carga los modelos de voz al arrancar; `0` ahorra ~2 GB si solo usas texto |
| `JARVIS_STT_DEVICE` | `cpu` | `cpu` o `cuda` |
| `JARVIS_TTS_VOICE` | voz es_ES local | Ruta a la voz `.onnx` de Piper |
| `JARVIS_CAPABILITIES` | todas | Capacidades a cargar, separadas por comas; las variables de cada una están en [Capacidades](#capacidades) |

La lista completa está en [`.env.example`](.env.example).

### Ajustar el enrutado de modelos

[`src/core/routing.py`](src/core/routing.py) contiene los IDs de modelo, las señales que disparan
Sonnet/Opus y el umbral de longitud, todo comentado y editable sin tocar el núcleo.

## Estructura

```
src/
├── core/          # Núcleo headless: JarvisCore, enrutado, servidor FastAPI, WS de voz, clips TTS
├── capabilities/  # Tools MCP: Cast, YouTube, Spotify, ntfy, recordatorios, workspace, agents
├── worker/        # Runner de agentes (contenedor aparte): cola, sesiones de Claude, estados, uso
├── vision/        # Vision, el gestor autónomo de los agentes (sesión de Opus, herramientas)
├── voice/         # Captura de audio, STT (faster-whisper), TTS (Piper)
├── frontends/     # REPL de texto, cliente WS, push-to-talk de escritorio
├── satellite/     # Satélite ambiente: micro, wake word, VAD, máquina de estados
└── web/           # UI web de voz (JS vanilla)
scripts/           # Smoke test, diagnósticos y pruebas end-to-end
```

## Hoja de ruta

- Descargar STT/TTS en una RTX de sobremesa despertada bajo demanda (Wake-on-LAN), con el servidor
  siempre encendido como respaldo.
- Voces de personaje con RVC sobre Piper.
- Vision independiente: separarlo del núcleo en un servicio propio, para usar agentes + Vision sin Jarvis.
- Pull requests en GitHub/Bitbucket y una vista web del PR `develop` → principal.
- Más habitaciones (satélites), front-end de Telegram, calendario y correo.

## Licencia

[MIT](LICENSE) © Manuel Nortes
