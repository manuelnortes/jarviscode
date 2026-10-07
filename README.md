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
  - **Workspace**: the state of your dev projects by voice, plus dictated notes committed to git.
  - **Background agents**: delegate tasks to Claude agents that work on a repo in an isolated
    container while you keep talking; Jarvis tracks them and notifies you when they finish.

Each capability can be switched on or off: see [Capabilities](#capabilities).

See [docs/architecture.md](docs/architecture.md) for the design and the decisions behind it.

## Architecture at a glance

```
 Browser (web UI) ──WS /voice──┐
 Satellite (mic + wake word) ──┤        ┌──────────── Jarvis core (FastAPI) ────────────┐
 Text clients ─────WS /ws──────┴──────► │ STT (faster-whisper) → JarvisCore → TTS (Piper)│
                                        │        │ Claude Agent SDK (Haiku/Sonnet/Opus)  │
                                        │        └─ MCP tools: web · Cast · YouTube ·    │
                                        │           Spotify · ntfy · reminders · HA ·   │
                                        │           workspace · agents                  │
                                        └───────────────────────────────────────────────┘
                                                 │ replies: audio stream or Google Cast
```

## Requirements

- Python 3.11+ (the Docker image uses 3.13) and Node.js (the Agent SDK drives the Claude Code CLI).
- About **3 GB of free RAM** with the default Whisper `medium` model (measured: ~2 GB with the
  voice models loaded, plus ~0.35 GB per open session; `small` needs noticeably less).
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

## Capabilities

Every capability is optional. A capability is loaded only if it passes **three filters**:

1. It is listed in `JARVIS_CAPABILITIES` (comma-separated). Empty or `all` = all of them.
   Example, music and notes only: `JARVIS_CAPABILITIES=media,youtube,workspace`.
2. Its `enable_*` flag in `JarvisConfig` is on (default; for scripts and tests).
3. Its configuration is present. Capabilities that depend on an external service **switch
   themselves off** when their variables are missing, so Jarvis never offers a tool that would fail.

The system prompt only describes the active capabilities. Web search is always on.

| Name | What it does | Needs | Off by itself when |
|---|---|---|---|
| `media` | Google Cast: play a URL, pause, volume, seek, what's playing | Cast speakers on the LAN (`network_mode: host`) | – |
| `youtube` | Hands-free music: "play X in the living room" → YouTube Mix radio on a Cast speaker | `media` | `media` is off |
| `spotify` | Spotify Connect control (only when a device is already active) | Spotify developer app + Premium; `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, token from `python -m scripts.spotify_auth` | client ID/secret empty |
| `notify` | Push notifications to your phone | An ntfy topic: `NTFY_TOPIC` (+ `NTFY_BASE_URL`, `NTFY_TOKEN`) | `NTFY_TOPIC` empty |
| `reminders` | Reminders and timers that survive restarts | `notify` (they are delivered through it) | `NTFY_TOPIC` empty |
| `homeassistant` | Lights by room or name | Home Assistant with the MCP Server integration; `HA_URL`, `HA_TOKEN` | URL or token empty |
| `agents` | Background Claude agents on your repos: "have an agent review X", "how are they doing?", "what did it find?" (read-only in this version) | The `jarvis-worker` container; see below | `JARVIS_AGENTS_URL` empty |
| `workspace` | Your dev projects by voice: "where did I leave X?", pending tasks, recent commits, anything not pushed; dictated notes in an *Inbox* | Your projects folder mounted read-only; see below | `JARVIS_WORKSPACE` empty |

### Workspace setup

Copy `docker-compose.override.example.yml` to `docker-compose.override.yml` (git-ignored; Docker
Compose merges it automatically) and set the paths of your machine. Then, in `.env`:

| Variable | Example | Purpose |
|---|---|---|
| `JARVIS_WORKSPACE` | `/workspace` | Projects folder (read-only). Each project is a folder with a `PLAN.md` or `README.md`; "where did I leave X?" reads the *resume point* / *status* section of its `PLAN.md` |
| `JARVIS_GIT_ROOT` | `/opt/git` | Optional. Folder with bare repos (`<project>.git`) for "what was done this week?" across all your machines |
| `JARVIS_NOTES_REMOTE` | `/opt/git/notes.git` | Optional. Git repo for notes. Without it the capability is read-only |
| `JARVIS_NOTES_FILE` | `TODO.md` | File inside that repo. Jarvis adds an **Inbox** section and only ever touches its own lines there |

Notes are committed and **pushed automatically** as `Jarvis <jarvis@localhost>`, one commit per
change (add, fix, delete), never rewriting history. Jarvis repeats what it wrote; say "no, it was…"
to correct it.

### Agents setup

Agents run in a **separate container**, `jarvis-worker` (Compose profile `agents`), so they never see
the core's `.env`, your projects folder or other tokens: only the Claude token, their own clones and
the bare repos you allow. It listens on `127.0.0.1` only and requires a shared token. Each agent is a
Claude Code session (Sonnet by default) working on its own clone of the repo.

In this version agents are **read-only**: they read the code and search the web, then report back.
Writing code on branches with lightweight pull requests is the next step (see Roadmap).

1. In `.env`: `JARVIS_AGENTS_URL=http://127.0.0.1:8113`, a random `JARVIS_WORKER_TOKEN` and
   `JARVIS_AGENT_REPOS=<repo>[,<repo>…]`.
2. In `docker-compose.override.yml`, mount each allowed bare repo at `/repos/<repo>.git` for the
   `jarvis-worker` service (see `docker-compose.override.example.yml`).
3. `docker compose --profile agents up -d --build jarvis-worker`, then rebuild the core.

| Variable | Default | Purpose |
|---|---|---|
| `JARVIS_AGENTS_MAX` | `2` | Agents **working** at the same time; more wait in a queue |
| `JARVIS_AGENT_MODEL` | `claude-sonnet-4-6` | Model of the agents |
| `JARVIS_AGENT_IDLE_TTL` | `3600` | Seconds a finished agent keeps its session (and the warm prompt cache) for follow-ups |
| `JARVIS_AGENTS_SESSION_TIMEOUT_SECONDS` | `3600` | Voice session timeout while it is managing live agents (instead of 90 s) |
| `JARVIS_WORKER_MEM_LIMIT` | `4g` | RAM cap of the worker (~0.35 GB per live agent session) |

Agent states (`GET /agents`): `queued`, `working`, `idle` (finished, session kept for follow-ups),
`done`, `failed`, `cancelled`. You get a push notification (ntfy) when an agent finishes or fails.
Ask "how are the agents doing?", "what did it find?", "tell it to also…" or "cancel it".

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
| `JARVIS_MEM_LIMIT` | `4g` | RAM cap of the Docker container (no swap); raise it for larger Whisper models |
| `JARVIS_STT_PRELOAD` | `1` | Load the voice models at startup; `0` saves ~2 GB if you only use text |
| `JARVIS_STT_DEVICE` | `cpu` | `cpu` or `cuda` |
| `JARVIS_TTS_VOICE` | local es_ES voice | Path to the Piper `.onnx` voice |
| `JARVIS_CAPABILITIES` | all | Capabilities to load, comma-separated; each one's own variables are in [Capabilities](#capabilities) |

See [`.env.example`](.env.example) for the full list.

### Tuning model routing

[`src/core/routing.py`](src/core/routing.py) holds the model IDs, the Sonnet/Opus trigger signals
and the length threshold, all commented and editable without touching the core.

## Project layout

```
src/
├── core/          # Headless core: JarvisCore, model routing, FastAPI server, voice WS, TTS clip store
├── capabilities/  # MCP tools: Cast, YouTube, Spotify, ntfy, reminders, workspace, agents
├── worker/        # Agent runner (separate container): queue, Claude sessions, states
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
- Agents that write code: branches, tests and lightweight pull requests (GitHub/Bitbucket later).
- More rooms (satellites), a Telegram front-end, calendar and e-mail.

## License

[MIT](LICENSE) © Manuel Nortes
