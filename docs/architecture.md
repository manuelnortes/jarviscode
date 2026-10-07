# Jarvis — Architecture and design decisions

🇬🇧 English | [🇪🇸 Español](arquitectura.md)

## 1. Vision

A Jarvis-style personal assistant for the home, **voice first**, with a text interface too. Heavy
reasoning runs on Claude in the cloud; everything that can run locally runs on a home server. It
must be able to **do things**, not just chat: search the web, control media and smart-home
devices, set reminders, send notifications to the phone…

Guiding principles:

- Local stuff at home; the brain in Claude.
- Reuse an already-paid Claude subscription, no double billing.
- Reusable *headless* core: voice and text are interchangeable front-ends.
- Start simple (a monolith on an always-on mini PC) and grow (GPU accelerator, satellites).

## 2. Access to Claude — the billing-defining choice

Pro/Max plans include a monthly credit that covers usage of the **Claude Agent SDK** and apps
built on it.

- The brain is built on the **Claude Agent SDK** (Python), authenticated with the
  **subscription** (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`), **not** an API key.
- **Never** set `ANTHROPIC_API_KEY` in the service environment: if present, the SDK uses it and
  bills per token, ignoring the subscription.
- Spend the credit wisely: simple tasks go to Haiku, Sonnet/Opus are reserved for real reasoning
  (see §6). The stable part of the prompt benefits from prompt caching.

## 3. Hardware and roles

Split **by role**, not "CPU here / GPU there".

| Machine | Role |
|---|---|
| **Mini PC** (Intel NUC, Proxmox + Docker) | 24/7 core: orchestration, CPU STT/TTS, wake word, capabilities |
| **Desktop PC with an RTX card** (planned) | Opportunistic AI accelerator: GPU STT and character voices |

The core needs no GPU because the brain is in the cloud. A gaming PC's GPU is used when it is on
(or woken via Wake-on-LAN); otherwise the mini PC keeps serving with its CPU models.
**Nothing breaks without a GPU.**

## 4. Stack

- **Brain / orchestration:** Claude Agent SDK (agent loop, built-in web search and fetch tools,
  MCP client, permissions, sessions).
- **API:** FastAPI — `GET /health`, `WebSocket /ws` (text), `WebSocket /voice` (voice),
  `GET/POST /media/*` (media widget), `GET /tts/{id}.wav` (clips for Cast) and the static web UI.
- **STT:** faster-whisper (CTranslate2), `medium` int8 on CPU. Measured: on CPU, `large-v3-turbo`
  was **slower** than `medium`; a bigger model only pays off on a GPU.
- **TTS:** Piper with the `es_ES-davefx-medium` voice, synthesised **sentence by sentence** while
  streaming.
- **Wake word:** openWakeWord (pre-trained `hey_jarvis`, ONNX) + Silero VAD, on CPU.
- **Capabilities:** in-process MCP servers (`@tool`), one per domain, plus Home Assistant's
  remote MCP server.
- **Containers:** Docker Compose with two services (`jarvis` and `jarvis-satellite`) in
  `network_mode: host` (required for Google Cast mDNS discovery).

## 5. Headless core + front-ends

The brain is a **headless service** with an HTTP/WebSocket API. Interaction modes are clients:

- **Web voice UI:** the browser captures the mic (AudioWorklet, 16 kHz mono PCM) and sends it over
  `/voice`; the server runs STT → core → TTS and streams the audio back on the same WebSocket. It
  also has text chat and a media widget. No built-in auth: it sits behind a reverse proxy with SSO,
  whose session cookie also covers the WebSocket handshake.
- **Ambient satellite:** a separate container with a USB mic. State machine:
  idle → wake word → listening (VAD) → answering → follow-up. The answer is played not on the
  satellite but on a **Google Cast speaker**: the server renders one WAV per reply, serves it at
  `/tts/{id}.wav` and casts it. The wake word stays active while speaking (**barge-in**:
  "Hey Jarvis, cancel").
- **Text:** local REPL, remote WebSocket client.
- **Desktop:** push-to-talk.

Consequence: adding a front-end (e.g. Telegram) is not a redesign, just another client.

### Perceived latency

The metric that matters is **time to first voice**. To reduce it: sentence-level streaming, and a
**contextual acknowledgement** ("Let me check the weather in Madrid, sir.") that Claude emits
before calling a slow tool, spoken while the tool runs. Every turn logs audio length, STT, Claude
TTFT, time to first voice and total.

## 6. Model routing

Every session starts on **Haiku**. A lightweight signal classifier (keywords + length) escalates
to **Sonnet** (searches, explanations, writing) or **Opus** (deep reasoning). Within a session the
model only goes **up** (*ratchet-up*); on escalation the last turns are injected as context.
`!haiku` / `!sonnet` / `!opus` prefixes force a model. Closing the session resets to Haiku. All
the configuration lives in `src/core/routing.py`.

## 7. Personality

**The voice gives the timbre; the system prompt gives the character.** The character exists from
day one, even with a neutral Piper voice.

- It lives in the core, so voice and text get the same persona.
- Jarvis: formal, calm, precise, dry irony, polite "usted", never effusive. The user's name is
  configurable (`JARVIS_USER_NAME`).
- Techniques: few-shot tone examples, an explicit "don't" list (never break character, no
  disclaimers or interjections), speech-oriented output (short sentences, no Markdown, lists or
  URLs, at most one source cited by name).

### Pronunciation

Piper uses espeak-ng with Spanish rules ("homelab" → "omelab"). The fix lives in **code**, not in
the prompt (that would be non-deterministic): a phonetic respelling dictionary applied **only to
the text sent to TTS** (`src/voice/tts.py`). Future improvement: custom IPA entries in espeak-ng.

### Character voices (planned)

There is no direct Spanish Jarvis/GLaDOS TTS. The self-hosted path is **Piper → RVC**: Piper
pronounces correctly and RVC applies the character's timbre on top, on the GPU. Without a GPU it
falls back to neutral Piper.

## 8. Capabilities

| Capability | Implementation |
|---|---|
| Web search | SDK built-in `WebSearch`/`WebFetch` tools |
| Google Cast | `pychromecast` with a shared Zeroconf instance: play URL, pause, volume, seek, status |
| YouTube music | `yt-dlp`: seed search → `RD<id>` Mix (real variety) → queue; an asyncio watcher chains tracks with pre-resolution and adaptive polling near the end |
| Spotify | `spotipy` (Spotify Connect). Web API limitation: it cannot cold-start an idle Cast device, so hands-free music goes through YouTube |
| Notifications | ntfy, publishing JSON (UTF-8 safe), with optional Bearer token |
| Reminders | APScheduler + SQLite started in the FastAPI lifespan; they survive restarts and are delivered via ntfy |
| Smart home | Home Assistant's MCP server (SSE + long-lived token) over the LAN; HA only exposes the lights |
| Workspace | Purpose-built tools (one call per spoken question) over a read-only projects folder and bare repos; notes go to an Inbox in a git-tracked `TODO.md`, committed and pushed per change |
| Agents | HTTP client to the `jarvis-worker` container (separate, non-root, token-protected), which runs a queue of Claude Agent SDK sessions on per-agent clones; the core polls it and notifies state changes via ntfy (or tells you on your next turn). Read or code mode: code agents work on `agent/*` branches (a UID-based `pre-receive` hook blocks anything else) and leave lightweight PRs; idle lifecycle with handoff inside the one-hour prompt cache |

Capabilities are toggled with `JARVIS_CAPABILITIES` and switch themselves off when their configuration is missing; the system prompt only describes the active ones.

The current date and time are injected on every turn so the model can compute reminders.

## 9. Security

- Agent SDK allowed-tools list: only what is needed, no bash.
- Credentials (Claude, ntfy, Spotify, HA) only in `.env`, never in code.
- Web UI behind a reverse proxy with SSO; the core port stays on the LAN.
- Home Assistant only exposes the entities Jarvis needs.

## 10. Roadmap

| Milestone | Status |
|---|---|
| 0 — Foundations (SDK on subscription, skeleton) | ✅ |
| 1 — Text core (API, multi-turn, routing) | ✅ |
| 2 — First capabilities (web + Cast) | ✅ |
| 3 — Voice (faster-whisper + Piper) | ✅ prototype |
| 3.6 — Web voice UI | ✅ |
| 3.5 + 4 — Ambient voice + wake word (satellite) | 🔄 deployed, tuning thresholds |
| 6 — Capabilities (ntfy, reminders, Spotify, YouTube) | ✅ |
| 7 — Smart home (Home Assistant) | ✅ deployed |
| 5 — GPU accelerator + character voices | 📋 planned |
| Future — satellites around the house, Telegram, calendar, e-mail | 📋 |

## 11. References

- Agent SDK with your plan: https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- Building agents with the Agent SDK: https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk
- Agent SDK + MCP: https://platform.claude.com/docs/en/agent-sdk/mcp
- Custom tools: https://platform.claude.com/docs/en/agent-sdk/custom-tools
- Piper: https://github.com/OHF-Voice/piper1-gpl
- openWakeWord: https://github.com/dscripka/openWakeWord
- espeak-ng dictionaries: https://github.com/espeak-ng/espeak-ng/blob/master/docs/dictionary.md
