# Vision — el gestor autónomo de los agentes

[🇬🇧 English](vision.md) | 🇪🇸 Español

Vision es el **gestor de los agentes programadores de Jarvis**: una sesión larga de Claude Opus que se
despierta con los eventos, revisa los pull requests de los agentes contra el plan del proyecto, fusiona en
una rama de integración lo que encaja, responde a las preguntas de los agentes y lanza el siguiente punto
del plan. Tú solo intervienes en lo que el plan no cubre y en la fusión final `develop` → `main`.

Esta guía es para quien quiera **solo la parte de agentes + Vision**, manejada por HTTP (desde una
terminal, Claude Code, un script…) sin hablar con Jarvis por voz.

## Cómo funciona

```
 tú (curl / Claude Code / voz de Jarvis) ──POST /vision/message──┐
                                                                 ▼
   ┌──────────────── Núcleo de Jarvis (FastAPI) ───────────┐    Vision (sesión de Opus)
   │  /vision/message · /api/prs · GET /agents             │◄── se despierta con: preguntas,
   │  fusiona PRs en develop (git, bare repos)             │    PRs terminados, esperas
   └──────────────┬────────────────────────────────────────┘    desbloqueadas, tus mensajes
                  │ HTTP + token compartido
   ┌──────────────▼──── jarvis-worker (contenedor aparte) ─┐
   │  cola · sesiones de Claude Code (Sonnet) · sonda uso  │──► empuja ramas agent/*
   │  PRs ligeros · handoffs · métricas                    │    (un hook de git bloquea el resto)
   └───────────────────────────────────────────────────────┘
```

1. Un **encargo** cubre **un** punto sin marcar del `PLAN.md` del proyecto. Un agente de código trabaja en
   su propia rama `agent/<id>-<slug>`, que sale de **`develop`** (se crea y se pone al día con `main` sola).
2. Al terminar deja un **PR ligero** (título, descripción, diffstat). El worker ejecuta él la suite de
   tests del proyecto sobre la punta de la rama (sin gastar tokens); si falla, le devuelve la salida al
   agente. Los agentes no editan el `PLAN.md`: Vision marca el punto en `develop` tras fusionar.
3. Vision lo revisa contra el plan y o bien **lo fusiona en `develop`**, o pide cambios al agente, o te lo
   pasa a ti. Tras fusionar, **lanza el siguiente punto** del plan.
4. Las preguntas de los agentes van primero a Vision: responde lo que el plan deja claro (o lo que es pura
   coordinación, "¿espero al otro agente?") y te escala el resto.
5. La fusión **`develop` → `main`** la revisas y la haces tú. Vision nunca toca la rama principal.

Las reglas de seguridad están **en el código, no en el prompt**: la herramienta de fusión de Vision rechaza
todo lo que no sea la rama de integración o no tenga la suite en verde sobre la punta actual de la rama (`merge_guard`); fusionar o cerrar
un PR en tu nombre (`user_merge`, `user_close`) solo funciona durante el turno de un mensaje *tuyo*; y el
usuario del worker solo puede empujar ramas `agent/*` (hook `pre-receive`).

Vision no gasta nada mientras está inactivo: sondea el worker por HTTP plano cada 15 s y solo gasta tokens
de Opus cuando hay un evento. Su sesión escribe un **handoff por proyecto** a los 45 minutos inactivo,
sigue vivo mientras haya agentes trabajando o en cola, se cierra a los 50 si no, y se recicla desde los
handoffs cuando el contexto pasa de ~100.000 tokens.

## Montaje mínimo (sin voz)

Hoy Vision vive **dentro del proceso del núcleo de Jarvis**, así que hay que levantar el contenedor
`jarvis`, pero sin nada más: sin modelos de voz y sin otras capacidades.

1. Sigue [Configurar agentes](../README.es.md#configurar-agentes) (contenedor del worker, token,
   repos, `agent_repo_setup.sh`).
2. Monta los **bare repos** en el núcleo, de solo lectura (`JARVIS_GIT_ROOT`): Vision lee de ahí cada
   `PLAN.md` (de `develop` si existe). Los repos de los agentes son los de `JARVIS_AGENT_REPOS_DIR`, que el
   compose monta con escritura en `/repos` en el núcleo y en el worker.
3. En el `.env`:

   ```bash
   CLAUDE_CODE_OAUTH_TOKEN=...          # claude setup-token (suscripción, nunca ANTHROPIC_API_KEY)
   JARVIS_CAPABILITIES=agents           # solo los agentes
   JARVIS_STT_PRELOAD=0                 # no cargar Whisper/Piper (~2 GB de RAM menos)
   JARVIS_AGENTS_URL=http://127.0.0.1:8113
   JARVIS_WORKER_TOKEN=<aleatorio>
   JARVIS_AGENT_REPOS_DIR=/srv/agent-repos   # un bare (o bind mount) por repo permitido
   JARVIS_GIT_ROOT=/opt/git             # donde están montados los bare repos en el núcleo
   JARVIS_INTEGRATION_BRANCH=develop
   JARVIS_AGENTS_MAX=1                  # empieza con un agente a la vez
   JARVIS_AGENT_USAGE_LIMIT=60          # no lanzar nada a partir del 60 % de la ventana de 5 h
   ```

4. `docker compose --profile agents up -d --build jarvis-worker jarvis`

El panel **PRs** de la web también funciona (diff, fusionar, cerrar), y las notificaciones push si
configuras ntfy.

## Hablar con Vision

Todo pasa por un único endpoint. Tu mensaje entra en la **misma sesión** que los eventos de los agentes,
para que Vision tenga todo el contexto; si está atendiendo un evento, tu mensaje espera turno.

```bash
curl -s -X POST http://127.0.0.1:8200/vision/message \
  -H 'Content-Type: application/json' \
  -d '{"text": "Lanza el siguiente punto pendiente del plan de mi-repo"}' | jq -r .reply
```

Respuestas: `200 {"reply": "..."}` · `400` sin `text` · `503` Vision apagado · `502` el turno falló (la
sesión se cierra y la siguiente arranca de los handoffs).

Cosas que puedes pedirle:

| Le dices | Vision |
|---|---|
| "Lanza el siguiente punto pendiente de mi-repo" | Lee el plan, redacta el encargo con la plantilla y lanza un agente |
| "Que un agente haga X en mi-repo" | Lo mismo, con tu petición como punto |
| "Fusiona el PR de a3ea0d" / "Cierra el PR de 65aa62" | `user_merge` / `user_close` (solo en tu turno) |
| "No lances agentes para la F3, la hacemos a mano" | Lo apunta en la bitácora y lo respeta |
| "Retoma los agentes" | Retoma los pausados por el límite de uso |

Endpoints de solo lectura (sin pasar por Opus):

| Endpoint | Dónde | Qué |
|---|---|---|
| `GET /agents` | núcleo | Agentes y sus estados (`queued`, `working`, `waiting_input`, `idle`, `rate_limited`, `paused`, `done`, `merged`, `failed`, `cancelled`) |
| `GET /api/prs` · `GET /api/prs/{id}` | núcleo | PRs, y un PR con su diff completo |
| `POST /api/prs/{id}/merge` · `/close` | núcleo | Fusionar o cerrar un PR directamente, sin Vision |
| `GET /usage` | worker | Último uso conocido de las ventanas de 5 h y semanal |
| `GET /metrics?last=50` | worker | Solo agentes: tokens, coste y uso antes/después de cada turno, con resumen |
| `GET` · `PUT /api/settings` | núcleo | Ajustes en caliente (panel ⚙ de la web): valor efectivo, origen y cuándo se aplica; `PUT {"CLAVE": valor \| null}`, todo o nada |
| `GET /api/metrics?tasks=20` | núcleo | Gasto en tokens de agentes y Vision, por tarea (lanzamiento → agente → revisión/fusión, tiempo real) y por turno de Vision; el `$` solo de referencia |

El worker solo escucha en `127.0.0.1` y pide la cabecera `X-Jarvis-Token`. El núcleo no tiene
autenticación propia: déjalo en la LAN o tras un proxy inverso con SSO.

## Reglas para manejar a Vision

Son las reglas con las que salió bien el primer proyecto real (10 agentes, 7 PRs fusionados por Vision, 94 tests):

1. **El plan manda.** Ten un `PLAN.md` con puntos marcables (`- [ ]`), lo bastante pequeños para un agente
   cada uno, y las decisiones de las que dependen (`D1`, `D2`…). Por iniciativa propia, Vision solo lanza
   puntos del plan; lo demás te lo *propone*.
2. **Un punto por encargo.** Nunca "X + Y + Z": un agente por punto; se encolan y arrancan cuando hay hueco.
   Los que dependen del PR de otro se lanzan cuando ese se fusiona.
3. **Contratos reales.** Si una tarea consume una API, el plan (o el encargo) debe dar los nombres reales de
   los campos o una respuesta real grabada; las variables de entorno, con su nombre exacto del
   `.env.example`. El único agente que tuvo que adivinar se inventó el contrato y su PR se tiró.
4. **La suite es la puerta; los tests nuevos, la excepción.** Un PR solo se fusiona si la suite existente
   está en verde sobre su commit exacto, ejecutada por el sistema y no declarada por el agente. Los agentes
   no añaden tests salvo que Vision pida uno, y entonces mínimo y dentro de la suite existente: los ficheros
   de tests enormes y los que fijan datos editados a mano gastan mucho uso y detectan poco.
5. **`develop` es de Vision, `main` es tuya.** Revisa `develop` → `main` a mano al cerrar cada fase.
6. **Presupuesta la cuenta.** Agentes, Vision y Jarvis gastan de la cuenta de Claude del token que le
   des a Jarvis (y tus propias sesiones de Claude también, si usan la misma cuenta). Empieza
   con `JARVIS_AGENTS_MAX` bajo, fija `JARVIS_AGENT_USAGE_LIMIT` y mira `/api/metrics`: en nuestras pruebas un
   encargo costó **0,4–1 $**.
   Los puntos de control (`JARVIS_AGENT_CHECKPOINT`, por defecto cada +5 % de la ventana) hacen que los
   agentes largos paren, estimen lo que les falta y pidan a Vision que les recorte el alcance.
7. **Di qué no es para agentes.** Deploys, puertos, secretos y DNS son cosa tuya; díselo a Vision explícitamente.

### Fragmento para Claude Code

Para que Claude Code gestione tus agentes a través de Vision, añade algo así al `CLAUDE.md` del proyecto
(o al global):

```markdown
## Agentes (Vision)
- Vision es el único gestor de los agentes programadores. Se le habla con
  `curl -s -X POST http://127.0.0.1:8200/vision/message -H 'Content-Type: application/json' -d '{"text": "..."}'`.
- Para empezar trabajo: comprobar que el punto existe como `- [ ]` en el PLAN.md (un punto = un agente) y
  pedirle a Vision que lo lance. No lanzar agentes directamente en el worker.
- Para ver cómo van: `GET /agents` y `GET /api/prs` (no gastan tokens). A Vision solo se le pide actuar.
- Nunca fusionar en `main` sin el OK explícito del usuario; `develop` → `main` lo decide siempre él.
```

## Configuración

| Variable | Por defecto | Para qué |
|---|---|---|
| `JARVIS_VISION` | `1` | `0` apaga Vision (las preguntas de los agentes te llegan tras `JARVIS_AGENT_MANAGER_TIMEOUT`) |
| `JARVIS_VISION_MODEL` | Opus | Modelo del gestor |
| `JARVIS_VISION_LAUNCH` | `1` | `0` = Vision revisa y responde pero nunca lanza agentes |
| `JARVIS_INTEGRATION_BRANCH` | `develop` | Dónde se fusiona el trabajo autónomo; vacía = desactivada |
| `JARVIS_VISION_HANDOFF_AFTER` / `_DIE_AFTER` | `2700` / `3000` | Segundos inactivo hasta el handoff / hasta cerrar la sesión |
| `JARVIS_VISION_MAX_CONTEXT` | `100000` | Tokens de contexto a partir de los que la sesión se recicla desde los handoffs |
| `JARVIS_VISION_DATA` | `/app/data/vision` | Handoffs por proyecto y registro de acciones |
| `JARVIS_AGENT_ASK_MANAGER_FIRST` | `1` | Las preguntas de diseño también van antes a Vision (él decide si son tuyas) |
| `JARVIS_AGENT_USAGE_LIMIT` / `_7D` | `60` / `75` | Salvaguarda de uso (%) de la ventana de 5 h / semanal |
| `JARVIS_USAGE_PROBE` / `_EVERY` | `1` / `120` | Lectura del uso real con una llamada de 1 token a Haiku mientras hay agentes; si no se puede leer, no se lanza nada |
| `JARVIS_AGENT_CHECKPOINT` | `5` | Punto de control cada +N % de la ventana de 5 h durante un turno; `0` = apagado |
| `JARVIS_AGENT_USAGE_MODE` | `session` | Los pausados esperan a "retoma" (`session`) o se retoman al reiniciarse la ventana (`autonomous`) |

## Pendiente

- **Vision independiente.** Hoy Vision vive dentro del proceso del núcleo de Jarvis, así que para usarlo
  hay que levantar el núcleo (aunque sea sin voz). El plan es separarlo en un servicio propio junto al
  worker, con su propia API HTTP pequeña, para poder desplegar "agentes + Vision" sin Jarvis.
- PRs de GitHub/Bitbucket en vez de los PR ligeros propios.
- Una vista en la web para el PR `develop` → `main`.
