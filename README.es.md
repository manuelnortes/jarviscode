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
                                        │           workspace                            │
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
├── capabilities/  # Tools MCP: Cast, YouTube, Spotify, ntfy, recordatorios, workspace
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
- Más habitaciones (satélites), front-end de Telegram, calendario y correo.

## Licencia

[MIT](LICENSE) © Manuel Nortes
