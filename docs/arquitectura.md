# Jarvis — Arquitectura y decisiones de diseño

[🇬🇧 English](architecture.md) | 🇪🇸 Español

## 1. Visión

Asistente personal tipo Jarvis para casa, **voz primero** y con interfaz de texto. El razonamiento
pesado lo hace Claude en la nube; todo lo que pueda correr en local, corre en un servidor doméstico.
Debe poder **ejecutar tareas** (no solo conversar): buscar en la web, controlar medios, domótica,
recordatorios, avisos al móvil…

Principios rectores:

- Lo local en casa; el cerebro en Claude.
- Aprovechar una suscripción de Claude ya pagada, sin doble facturación.
- Núcleo *headless* reutilizable: voz y texto son front-ends intercambiables.
- Empezar simple (monolito en un mini-PC siempre encendido) y crecer (acelerador GPU, satélites).

## 2. Acceso a Claude — el punto que define la facturación

Los planes Pro/Max incluyen un crédito mensual que cubre el uso del **Claude Agent SDK** y de las
apps construidas sobre él.

- El cerebro se construye sobre el **Claude Agent SDK** (Python), autenticando con la
  **suscripción** (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`), **no** con API key.
- **Nunca** definir `ANTHROPIC_API_KEY` en el entorno del servicio: si existe, el SDK la usa y
  factura por token, ignorando la suscripción.
- Ser económicos con el crédito: las tareas simples van a Haiku y Sonnet/Opus se reservan para
  razonamiento de verdad (ver §6). La parte estable del prompt aprovecha el caché.

## 3. Hardware y reparto de roles

Reparto **por rol**, no "CPU aquí / GPU allá".

| Equipo | Rol |
|---|---|
| **Mini-PC** (Intel NUC, Proxmox + Docker) | Núcleo 24/7: orquestación, STT/TTS en CPU, wake word, capacidades |
| **PC de sobremesa con RTX** (planificado) | Acelerador de IA oportunista: STT en GPU y voces de personaje |

El núcleo no necesita GPU porque el cerebro está en la nube. La GPU de un PC de juego se aprovecha
cuando está encendido (o se despierta con Wake-on-LAN); si no está, el mini-PC cubre el servicio
con sus modelos en CPU. **Nada se rompe sin GPU.**

## 4. Stack

- **Cerebro / orquestación:** Claude Agent SDK (bucle de agente, herramientas integradas de
  búsqueda y lectura web, cliente MCP, permisos, sesiones).
- **API:** FastAPI — `GET /health`, `WebSocket /ws` (texto), `WebSocket /voice` (voz),
  `GET/POST /media/*` (widget multimedia), `GET /tts/{id}.wav` (clips para Cast) y la UI web estática.
- **STT:** faster-whisper (CTranslate2), `medium` int8 en CPU. Medido: en CPU, `large-v3-turbo`
  resultó **más lento** que `medium`; subir de modelo solo compensa con GPU.
- **TTS:** Piper con la voz `es_ES-davefx-medium`, sintetizando **por frases** en streaming.
- **Wake word:** openWakeWord (`hey_jarvis` preentrenado, ONNX) + VAD Silero, en CPU.
- **Capacidades:** servidores MCP en proceso (`@tool`), uno por dominio, más el servidor MCP
  remoto de Home Assistant.
- **Contenedores:** Docker Compose con dos servicios (`jarvis` y `jarvis-satellite`) en
  `network_mode: host` (necesario para el descubrimiento mDNS de Google Cast).

## 5. Núcleo headless + front-ends

El cerebro es un **servicio headless** con API HTTP/WebSocket. Los modos de interacción son clientes:

- **UI web de voz:** el navegador captura el micro (AudioWorklet, PCM 16 kHz mono) y lo envía por
  `/voice`; el servidor hace STT → núcleo → TTS y devuelve el audio por el mismo WebSocket. Tiene
  también chat de texto y un widget multimedia. Sin autenticación propia: se expone tras un proxy
  inverso con SSO, cuya cookie cubre también el handshake del WebSocket.
- **Satélite ambiente:** contenedor aparte con un micro USB. Máquina de estados:
  reposo → wake word → escucha (VAD) → respuesta → seguimiento. La respuesta no suena en el
  satélite sino en un **altavoz Google Cast**: el servidor genera un WAV por respuesta, lo publica
  en `/tts/{id}.wav` y lo castea. El wake word sigue activo mientras habla (**barge-in**:
  "Hey Jarvis, cancela").
- **Texto:** REPL local, cliente WebSocket remoto.
- **Escritorio:** push-to-talk.

Consecuencia: añadir un front-end (p. ej. Telegram) no es un rediseño, es otro cliente.

### Latencia percibida

La métrica que importa es la **"1ª voz"** (tiempo hasta que empieza a hablar). Para reducirla:
streaming por frases, y una **frase de confirmación contextual** ("Déjeme consultar el tiempo en
Madrid, señor.") que Claude emite antes de invocar una herramienta lenta y que suena mientras la
herramienta corre. Cada turno registra audio, STT, TTFT de Claude, 1ª voz y total.

## 6. Enrutado de modelos

Cada sesión empieza en **Haiku**. Un clasificador ligero de señales (palabras clave + longitud)
sube a **Sonnet** (búsquedas, explicaciones, escritura) o a **Opus** (razonamiento profundo).
Dentro de una sesión el modelo **solo sube** (*ratchet-up*); al saltar se inyectan los últimos
turnos como contexto. Prefijos `!haiku` / `!sonnet` / `!opus` para forzarlo. Al cerrar la sesión
vuelve a Haiku. Toda la configuración vive en `src/core/routing.py`.

## 7. Personalidad

**La voz da el timbre; el system prompt da el carácter.** El personaje existe desde el primer día
con una voz Piper neutra.

- Vive en el núcleo, así que voz y texto reciben el mismo personaje.
- Jarvis: formal, sereno, preciso, ironía seca, trato de usted, nunca efusivo. El nombre del
  usuario es configurable (`JARVIS_USER_NAME`).
- Técnicas: ejemplos few-shot de tono, lista explícita de "no hacer" (no romper personaje, sin
  disclaimers ni interjecciones), salida pensada para ser hablada (frases cortas, sin Markdown,
  listas ni URLs, como mucho una fuente citada por nombre).

### Pronunciación

Piper usa espeak-ng con reglas españolas ("homelab" → "omelab"). La corrección va en **código**, no
en el prompt (sería no determinista): un diccionario de reescritura fonética aplicado **solo al
texto que va al TTS** (`src/voice/tts.py`). Mejora futura: entradas IPA propias en espeak-ng.

### Voces de personaje (planificado)

No hay TTS directo de Jarvis/GLaDOS en español. La vía self-hosted es **Piper → RVC**: Piper
pronuncia bien y RVC aplica el timbre encima, en la GPU. Sin GPU, se degrada a Piper neutro.

## 8. Capacidades

| Capacidad | Implementación |
|---|---|
| Búsqueda web | Herramientas `WebSearch`/`WebFetch` del SDK |
| Google Cast | `pychromecast` con una instancia Zeroconf compartida: reproducir URL, pausa, volumen, seek, estado |
| Música por YouTube | `yt-dlp`: búsqueda semilla → Mix `RD<id>` (variedad real) → cola; un watcher asyncio encadena pistas con pre-resolución y sondeo adaptativo cerca del final |
| Spotify | `spotipy` (Spotify Connect). Limitación de la Web API: no arranca en frío un Cast en reposo, por eso la música manos libres va por YouTube |
| Notificaciones | ntfy, publicando en formato JSON (UTF-8 seguro), con token Bearer opcional |
| Recordatorios | APScheduler + SQLite, arrancado en el lifespan de FastAPI; persisten a reinicios y se entregan por ntfy |
| Domótica | Servidor MCP de Home Assistant (SSE + token de larga duración) por la LAN; HA solo expone las luces |
| Workspace | Herramientas propias (una llamada por pregunta hablada) sobre la carpeta de proyectos en solo lectura y los bare repos; las notas van a un Inbox de un `TODO.md` versionado, con commit y push por cambio |
| Agentes | Cliente HTTP del contenedor `jarvis-worker` (aparte, sin root, con token), que ejecuta una cola de sesiones del Claude Agent SDK sobre un clon por agente; el núcleo lo sondea y avisa de los cambios de estado por ntfy. Solo lectura en esta versión |

Las capacidades se activan con `JARVIS_CAPABILITIES` y se apagan solas si les falta configuración; el system prompt solo describe las activas.

La fecha y hora actuales se inyectan en cada turno para que el modelo calcule recordatorios.

## 9. Seguridad

- Lista de herramientas permitidas del Agent SDK: solo las necesarias, sin bash.
- Credenciales (Claude, ntfy, Spotify, HA) solo en `.env`, nunca en el código.
- UI web tras proxy inverso con SSO; el puerto del núcleo solo en la LAN.
- Home Assistant expone a Jarvis únicamente las entidades necesarias.

## 10. Hoja de ruta

| Hito | Estado |
|---|---|
| 0 — Fundamentos (SDK con suscripción, esqueleto) | ✅ |
| 1 — Núcleo por texto (API, multi-turno, enrutado) | ✅ |
| 2 — Primeras capacidades (web + Cast) | ✅ |
| 3 — Voz (faster-whisper + Piper) | ✅ prototipo |
| 3.6 — UI web de voz | ✅ |
| 3.5 + 4 — Voz ambiente + wake word (satélite) | 🔄 desplegado, ajustando umbrales |
| 6 — Capacidades (ntfy, recordatorios, Spotify, YouTube) | ✅ |
| 7 — Domótica (Home Assistant) | ✅ desplegado |
| 5 — Acelerador GPU + voces de personaje | 📋 planificado |
| Futuro — satélites por la casa, Telegram, calendario, correo | 📋 |

## 11. Referencias

- Agent SDK con tu plan: https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- Construir agentes con el Agent SDK: https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk
- Agent SDK + MCP: https://platform.claude.com/docs/en/agent-sdk/mcp
- Herramientas personalizadas: https://platform.claude.com/docs/en/agent-sdk/custom-tools
- Piper: https://github.com/OHF-Voice/piper1-gpl
- openWakeWord: https://github.com/dscripka/openWakeWord
- espeak-ng (diccionarios): https://github.com/espeak-ng/espeak-ng/blob/master/docs/dictionary.md
