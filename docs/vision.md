# Vision — the autonomous agent manager

🇬🇧 English | [🇪🇸 Español](vision.es.md)

Vision is the **manager of Jarvis' coding agents**: a long-lived Claude Opus session that wakes up on
events, reviews the agents' pull requests against the project's plan, merges what fits into an
integration branch, answers the agents' questions and launches the next item of the plan. You only
step in for what the plan does not cover and for the final `develop` → `main` merge.

This guide is for people who want **only the agents + Vision part**, driven over HTTP (from a
terminal, Claude Code, a script…) without talking to Jarvis by voice.

## How it works

```
 you (curl / Claude Code / Jarvis voice) ──POST /vision/message──┐
                                                                 ▼
   ┌──────────────── Jarvis core (FastAPI) ────────────────┐    Vision (Opus session)
   │  /vision/message · /api/prs · GET /agents             │◄── wakes on: agent questions,
   │  merges PRs into develop (git, bare repos)            │    finished PRs, unblocked
   └──────────────┬────────────────────────────────────────┘    waits, your messages
                  │ HTTP + shared token
   ┌──────────────▼──── jarvis-worker (separate container) ┐
   │  queue · Claude Code sessions (Sonnet) · usage probe  │──► pushes agent/* branches
   │  lightweight PRs · handoffs · metrics                 │    (a git hook blocks anything else)
   └───────────────────────────────────────────────────────┘
```

1. A **task** ("encargo") covers **one** unchecked item of the project's `PLAN.md`. A code agent works on
   its own branch `agent/<id>-<slug>` cut from **`develop`** (created and synced with `main` automatically).
2. When it finishes it leaves a **lightweight PR** (title, description, diffstat). The worker runs the
   project's test suite itself on the branch tip (no tokens spent); if it fails, it hands the output back
   to the agent. Agents never edit `PLAN.md`: Vision ticks the item on `develop` after merging.
3. Vision reviews it against the plan and either **merges it into `develop`**, asks the agent for
   changes, or flags it for you. After a merge it **launches the next item** of the plan.
4. Agents' questions go to Vision first: it answers what the plan settles (or pure coordination,
   "should I wait for the other agent?") and escalates the rest to you.
5. You review and merge **`develop` → `main`** yourself. Vision never touches the main branch.

The safety rules are **in code, not in the prompt**: Vision's merge tool refuses anything that is not
the integration branch or whose suite is not green on the branch's current tip (`merge_guard`); merging or closing a PR on your
behalf (`user_merge`, `user_close`) only works during the turn of one of *your* messages; and the
worker's user can only push `agent/*` branches (`pre-receive` hook).

Vision costs nothing while idle: it polls the worker over plain HTTP every 15 s and only spends Opus
tokens when there is an event. Its session writes a **handoff per project** after 45 minutes idle,
stays alive while agents are working or queued, closes at 50 minutes otherwise, and is recycled from
the handoffs when its context grows past ~100k tokens.

## Minimal setup (no voice)

Today Vision runs **inside the Jarvis core process**, so you still start the `jarvis` container, but
with nothing else: no voice models, no other capabilities.

1. Follow [Agents setup](../README.md#agents-setup) (worker container, token, repos, `agent_repo_setup.sh`).
2. Mount the **bare repos** in the core read-only (`JARVIS_GIT_ROOT`): Vision reads each `PLAN.md` from
   there (from `develop` if it exists). The agent repos themselves are the ones in
   `JARVIS_AGENT_REPOS_DIR`, mounted writable at `/repos` in the core and the worker by the compose.
3. In `.env`:

   ```bash
   CLAUDE_CODE_OAUTH_TOKEN=...          # claude setup-token (subscription, never ANTHROPIC_API_KEY)
   JARVIS_CAPABILITIES=agents           # nothing but the agents
   JARVIS_STT_PRELOAD=0                 # do not load Whisper/Piper (~2 GB of RAM saved)
   JARVIS_AGENTS_URL=http://127.0.0.1:8113
   JARVIS_WORKER_TOKEN=<random>
   JARVIS_AGENT_REPOS_DIR=/srv/agent-repos   # one bare (or bind mount) per allowed repo
   JARVIS_GIT_ROOT=/opt/git             # where the bare repos are mounted in the core
   JARVIS_INTEGRATION_BRANCH=develop
   JARVIS_AGENTS_MAX=1                  # start with one agent at a time
   JARVIS_AGENT_USAGE_LIMIT=60          # stop launching at 60 % of the 5-hour window
   ```

4. `docker compose --profile agents up -d --build jarvis-worker jarvis`

The web UI's **PRs** panel works too (diff, merge, close), and push notifications if you set ntfy.

## Talking to Vision

Everything goes through one endpoint. Your message enters the **same session** as the agents'
events, so Vision has the full context; if it is busy with an event, your message waits its turn.

```bash
curl -s -X POST http://127.0.0.1:8200/vision/message \
  -H 'Content-Type: application/json' \
  -d '{"text": "Launch the next pending item of the plan of my-repo"}' | jq -r .reply
```

Responses: `200 {"reply": "..."}` · `400` without `text` · `503` Vision is off · `502` the turn failed
(the session closes and the next one starts from the handoffs).

Things you can ask:

| You say | Vision does |
|---|---|
| "Launch the next pending item of my-repo" | Reads the plan, writes the task with the template and starts an agent |
| "Have an agent do X in my-repo" | Same, with your request as the item |
| "Merge the PR of a3ea0d" / "Close the PR of 65aa62" | `user_merge` / `user_close` (only allowed in your turn) |
| "Don't launch agents for F3, we do it by hand" | Notes it in the agent log and respects it |
| "Resume the agents" | Resumes the ones paused by the usage limit |

Read-only endpoints (no Opus involved):

| Endpoint | Where | What |
|---|---|---|
| `GET /agents` | core | Agents and their states (`queued`, `working`, `waiting_input`, `idle`, `rate_limited`, `paused`, `done`, `merged`, `failed`, `cancelled`) |
| `GET /api/prs` · `GET /api/prs/{id}` | core | PRs, and one PR with its full diff |
| `POST /api/prs/{id}/merge` · `/close` | core | Merge or close a PR directly, bypassing Vision |
| `GET /usage` | worker | Last known usage of the 5-hour and weekly windows |
| `GET /metrics?last=50` | worker | Agents only: per-turn tokens, cost and usage before/after, with a summary |
| `GET` · `PUT /api/settings` | core | Live settings (the ⚙ panel of the web): effective value, origin and when it applies; `PUT {"KEY": value \| null}`, all or nothing |
| `GET /api/metrics?tasks=20` | core | Spend in tokens of agents and Vision, per task (launch → agent → review/merge, wall time) and per Vision turn; `$` only as a reference |

The worker listens on `127.0.0.1` only and needs the `X-Jarvis-Token` header. The core has no
authentication of its own: keep it on the LAN or behind a reverse proxy with SSO.

## Rules for driving Vision

These are the rules that made the first real project work (10 agents, 7 PRs merged by Vision, 94 tests):

1. **The plan is the source of truth.** Keep a `PLAN.md` with checkbox items (`- [ ]`) small enough for
   one agent each, and the decisions they depend on (`D1`, `D2`…). Vision only launches items from the
   plan on its own; anything else it *proposes* to you.
2. **One item per task.** Never "X + Y + Z": one agent per item; they queue and start when there is room.
   Items that depend on another PR are launched after it is merged.
3. **Real contracts.** If a task consumes an API, the plan (or the task) must give the real field names or
   a recorded real response; environment variables with their exact name from `.env.example`. The one
   agent that had to guess invented a contract and its PR was thrown away.
4. **The suite is the gate; new tests are the exception.** A PR is merged only if the existing suite is
   green on its exact commit, run by the system, not reported by the agent. Agents add no tests unless
   Vision asks for one, and then a minimal one added to the existing suite: big test files and tests that
   pin hand-edited data cost a lot of usage and catch little.
5. **`develop` is Vision's, `main` is yours.** Review `develop` → `main` by hand when a phase is done.
6. **Budget the account.** Agents, Vision and Jarvis spend from the Claude account of the token you give
   Jarvis (and so do your own Claude sessions if they use the same account).
   Keep `JARVIS_AGENTS_MAX` low at first, set `JARVIS_AGENT_USAGE_LIMIT`, and check `/api/metrics`: in our
   runs a task cost **$0.4–1**.
   Checkpoints (`JARVIS_AGENT_CHECKPOINT`, default every +5 % of the window) make long agents stop,
   estimate what is left and ask Vision to cut the scope.
7. **Say what is not for agents.** Deploys, ports, secrets and DNS stay with you; tell Vision explicitly.

### Snippet for Claude Code

To let Claude Code manage your agents through Vision, add something like this to the project's (or
your global) `CLAUDE.md`:

```markdown
## Agents (Vision)
- Vision is the only manager of the coding agents. Talk to it with
  `curl -s -X POST http://127.0.0.1:8200/vision/message -H 'Content-Type: application/json' -d '{"text": "..."}'`.
- To start work: make sure the item exists as `- [ ]` in PLAN.md (one item = one agent), then ask Vision to
  launch it. Do not launch agents directly on the worker.
- To check progress: `GET /agents` and `GET /api/prs` (no tokens spent). Ask Vision only to act.
- Never merge into `main` without the user's explicit OK; `develop` → `main` is always the user's call.
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `JARVIS_VISION` | `1` | `0` turns Vision off (agents' questions then go to you after `JARVIS_AGENT_MANAGER_TIMEOUT`) |
| `JARVIS_VISION_MODEL` | Opus | Model of the manager |
| `JARVIS_VISION_LAUNCH` | `1` | `0` = Vision reviews and answers but never launches agents |
| `JARVIS_INTEGRATION_BRANCH` | `develop` | Where autonomous work is merged; empty = disabled |
| `JARVIS_VISION_HANDOFF_AFTER` / `_DIE_AFTER` | `2700` / `3000` | Idle seconds until handoff / until the session closes |
| `JARVIS_VISION_MAX_CONTEXT` | `100000` | Context tokens after which the session is recycled from the handoffs |
| `JARVIS_VISION_DATA` | `/app/data/vision` | Handoffs per project and the action log |
| `JARVIS_AGENT_ASK_MANAGER_FIRST` | `1` | Design questions also go to Vision first (it decides whether they are yours) |
| `JARVIS_AGENT_USAGE_LIMIT` / `_7D` | `60` / `75` | Usage safeguard (%) for the 5-hour / weekly window |
| `JARVIS_USAGE_PROBE` / `_EVERY` | `1` / `120` | Real usage read with a 1-token Haiku call while agents run; if unreadable, nothing is launched |
| `JARVIS_AGENT_CHECKPOINT` | `5` | Checkpoint every +N % of the 5-hour window during a turn; `0` = off |
| `JARVIS_AGENT_USAGE_MODE` | `session` | Paused agents wait for "resume" (`session`) or resume when the window resets (`autonomous`) |

## Pending

- **Standalone Vision.** Vision lives inside the Jarvis core process today, so running it means
  starting the core (even if voice is off). The plan is to split it into its own service next to the
  worker, with its own small HTTP API, so that "agents + Vision" can be deployed without Jarvis at all.
- GitHub/Bitbucket pull requests instead of the built-in lightweight PRs.
- A web view for the `develop` → `main` PR.
