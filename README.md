# Jarvis

🇬🇧 English | [🇪🇸 Español](README.es.md)

A self-hosted, Spanish-speaking **voice assistant** for the home, in the spirit of Iron Man's J.A.R.V.I.S.
The heavy reasoning runs on **Claude** through the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview),
authenticated with a regular Claude **subscription** (no pay-per-token API key). Everything else —
speech-to-text, text-to-speech, wake word, media and smart-home control — runs locally on a small
always-on server.

The core is **headless**: a FastAPI service exposing HTTP and WebSocket APIs. Voice and text are
just interchangeable front-ends talking to it.

> The assistant speaks Spanish (prompts, voice and wake-word pipeline are tuned for it). Code
> comments and internal docs are in Spanish too.

## Features

- **Claude as the brain** via the Agent SDK, billed against a Pro/Max subscription
  (`claude setup-token`).
- **Automatic model routing**: Haiku for casual chat, Sonnet for real tasks, Opus for deep
  reasoning. Within a session the model only *ratchets up*, carrying recent context when it does.
  Force one with a `!haiku` / `!sonnet` / `!opus` prefix.
- **Character**: a dry, formal, slightly ironic butler defined entirely in the system prompt
  (few-shot examples, speech-friendly output, short spoken acknowledgement before slow actions).
- **Voice pipeline**: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) STT +
  [Piper](https://github.com/rhasspy/piper) TTS, sentence-by-sentence streaming so it starts
  talking after the first sentence, Markdown/URL stripping and phonetic respelling of anglicisms.
- **Front-ends**
  - **Web voice UI** (served by the core): mic capture in the browser, audio streamed over a
    WebSocket, animated "orb", text chat, media widget. Works on desktop and mobile.
  - **Ambient satellite**: a separate container with a USB microphone, wake word
    (`hey_jarvis` from [openWakeWord](https://github.com/dscripka/openWakeWord)) + Silero VAD,
    follow-up turns, barge-in ("Hey Jarvis, cancel") and replies played on a **Google Cast** speaker.
  - Text REPL, remote WebSocket client and a desktop push-to-talk client.
- **Capabilities** (in-process MCP servers / tools)
  - Web search and fetch.
  - **Google Cast** control: play, pause, volume, seek, what's playing.
  - **Hands-free music via YouTube**: `yt-dlp` search → YouTube Mix queue → cast, with a watcher
    that chains tracks.
  - **Spotify Connect** control (when a device is active).
  - **Push notifications** via [ntfy](https://ntfy.sh).
  - **Reminders and timers** that survive restarts (APScheduler + SQLite), delivered via ntfy.
  - **Smart home** through Home Assistant's MCP server (lights by room or name).

See [docs/architecture.md](docs/architecture.md) for the design and the decisions behind it.

## Architecture at a glance

```
 Browser (web UI) ──WS /voice──┐
 Satellite (mic + wake word) ──┤        ┌──────────── Jarvis core (FastAPI) ────────────┐
 Text clients ─────WS /ws──────┴──────► │ STT (faster-whisper) → JarvisCore → TTS (Piper)│
                                        │        │ Claude Agent SDK (Haiku/Sonnet/Opus)  │
                                        │        └─ MCP tools: web · Cast · YouTube ·    │
                                        │           Spotify · ntfy · reminders · HA     │
                                        └───────────────────────────────────────────────┘
                                                 │ replies: audio stream or Google Cast
```

## Requirements

- Python 3.11+ (the Docker image uses 3.13) and Node.js (the Agent SDK drives the Claude Code CLI).
- A Claude Pro/Max subscription and a long-lived token from `claude setup-token`.
- **Do not** set `ANTHROPIC_API_KEY`: if present, the SDK bills per token and ignores the subscription.
- Optional, per capability: Google Cast speakers on the LAN, an ntfy topic, a Spotify developer
  app (Premium account), a Home Assistant instance with the MCP Server integration, a USB mic for
  the satellite.

## Quick start (Docker)

```bash
cp .env.example .env
# Fill in CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), JARVIS_USER_NAME and the capabilities you want.
docker compose up -d --build jarvis
# Web UI and API on http://<host>:8200  ·  health check: GET /health
# Port in use? Set JARVIS_PORT=<port> (and TZ if you are not in Europe/Madrid) in .env.
```

The compose file uses `network_mode: host` so Cast/mDNS discovery works. The Whisper model is
cached in a volume; reminders and the Spotify token live in another.

To also run the ambient satellite, pass the sound device through and adjust the
`JARVIS_SAT_*` variables in `docker-compose.yml` (input device, gain, Cast device, thresholds):

```bash
docker compose up -d --build
```

> The web UI has no authentication of its own: put it behind a reverse proxy with SSO
> (e.g. Authelia) or keep it on the LAN.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-voice.txt   # voice stack (STT/TTS)
cp .env.example .env                 # and fill it in

python scripts/smoke_test.py         # checks the Agent SDK answers with your subscription
python -m src.core.server            # API + web UI on 127.0.0.1:8200
python -m src.frontends.cli          # text REPL
python -m src.frontends.voice        # desktop push-to-talk
```

Piper binary and voice (`es_ES-davefx-medium`) go into `piper/` and `voices/` (git-ignored); paths
are configurable with `JARVIS_PIPER_BIN` and `JARVIS_TTS_VOICE`.

### Useful environment variables

| Variable | Default | What it controls |
|----------|---------|------------------|
| `JARVIS_USER_NAME` | `Tony` | How Jarvis addresses you |
| `JARVIS_STT_MODEL` | `small` | Whisper model (`medium` in the compose file) |
| `JARVIS_STT_DEVICE` | `cpu` | `cpu` or `cuda` |
| `JARVIS_TTS_VOICE` | local es_ES voice | Path to the Piper `.onnx` voice |
| `NTFY_BASE_URL` / `NTFY_TOPIC` / `NTFY_TOKEN` | `https://ntfy.sh` / – / – | Push notifications |
| `HA_URL` / `HA_TOKEN` | – | Home Assistant MCP endpoint; capability disabled if empty |

See [`.env.example`](.env.example) for the full list.

### Tuning model routing

[`src/core/routing.py`](src/core/routing.py) holds the model IDs, the Sonnet/Opus trigger signals
and the length threshold, all commented and editable without touching the core.

## Project layout

```
src/
├── core/          # Headless core: JarvisCore, model routing, FastAPI server, voice WS, TTS clip store
├── capabilities/  # MCP tools: Cast, YouTube, Spotify, ntfy, reminders
├── voice/         # Audio capture, STT (faster-whisper), TTS (Piper)
├── frontends/     # Text REPL, WS client, desktop push-to-talk
├── satellite/     # Ambient satellite: mic, wake word, VAD, state machine
└── web/           # Web voice UI (vanilla JS)
scripts/           # Smoke test, diagnostics and end-to-end test scripts
```

## Roadmap

- GPU offload of STT/TTS to a desktop RTX card woken on demand (Wake-on-LAN), with the always-on
  server as fallback.
- Character voices with RVC on top of Piper.
- More rooms (satellites), a Telegram front-end, calendar and e-mail.

## License

[MIT](LICENSE) © Manuel Nortes
