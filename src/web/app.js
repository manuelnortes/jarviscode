/* ============================================================================
 * app.js — Orquestación de la UI de voz de Jarvis (Hito 3.6).
 * ----------------------------------------------------------------------------
 * - Push-to-talk: captura micro (AudioWorklet → PCM Int16 16 kHz) y lo manda por
 *   el WebSocket /voice según el contrato del hito.
 * - Reproduce el audio TTS que vuelve (PCM Int16 al sample_rate anunciado) por
 *   Web Audio, encolando frases sin cortes.
 * - Oscilación del orbe: un AnalyserNode mide el RMS del audio activo (micro al
 *   ESCUCHAR, salida TTS al HABLAR) y alimenta orb.setLevel(0..1). La misma
 *   señal mueve las barras del waveform.
 * - Texto: la barra de entrada usa el WebSocket /ws (endpoint de texto que ya
 *   existe); el reply se transmite por chunks (sin audio, es texto).
 * - Modo demo (?demo en la URL): oscila el orbe sin backend ni micro, para
 *   validar la estética.
 * ==========================================================================*/

(() => {
  "use strict";

  const DEMO = new URLSearchParams(location.search).has("demo");

  // Instrumentación de diagnóstico (turno por turno) en la consola del navegador.
  // Desactivada por defecto; se enciende añadiendo `?debug` a la URL (combinable
  // con `?demo`). Útil para depurar el bucle de voz sin tocar código.
  const DBG = new URLSearchParams(location.search).has("debug");
  const dbg = (...a) => DBG && console.log("[voice]", ...a);
  let framesSent = 0; // PCM frames enviados en la utterance en curso

  // ── Referencias al DOM ────────────────────────────────────────────────────
  const app = document.getElementById("app");
  const orbCanvas = document.getElementById("orb-canvas");
  const miniOrbCanvas = document.getElementById("mini-orb-canvas");
  const transcriptEl = document.getElementById("transcript");
  const statusLine = document.getElementById("status-line");
  const statusChip = document.getElementById("status-chip");
  const clockEl = document.getElementById("clock");
  const historyList = document.getElementById("history-list");
  const historyToggle = document.getElementById("history-toggle");
  const historyNew = document.getElementById("history-new");
  const historyEl = document.getElementById("history");
  const scrim = document.getElementById("scrim");
  // Modal de detalle de turno (pregunta + respuesta + modelo).
  const modal = document.getElementById("modal");
  const modalOverlay = document.getElementById("modal-overlay");
  const modalClose = document.getElementById("modal-close");
  const modalBadge = document.getElementById("modal-badge");
  const modalRepeat = document.getElementById("modal-repeat");
  const modalQ = document.getElementById("modal-q");
  const modalA = document.getElementById("modal-a");
  const waveBars = Array.from(document.querySelectorAll(".waveform span"));
  const micBtn = document.getElementById("mic-btn");
  const stopBtn = document.getElementById("stop-btn");
  const textInput = document.getElementById("text-input");
  const inputbar = document.getElementById("inputbar");

  // ── Orbes ─────────────────────────────────────────────────────────────────
  const orb = new VoiceOrb(orbCanvas, { amp: 1.1 });
  const miniOrb = new VoiceOrb(miniOrbCanvas, { amp: 1.3 });
  orb.start();
  miniOrb.start();

  // ── Estado de la UI ────────────────────────────────────────────────────────
  const CHIP = {
    idle: "EN REPOSO",
    listening: "ESCUCHANDO",
    thinking: "PROCESANDO",
    speaking: "RESPONDIENDO",
  };

  /**
   * Cambia el modo de la app (gobierna chips, indicadores y la fuente del nivel
   * de audio que alimenta el orbe).
   * @param {'idle'|'listening'|'thinking'|'speaking'} mode
   * @param {string} [status] Texto opcional para la línea de estado (mono).
   */
  function setMode(mode, status) {
    app.dataset.mode = mode;
    statusChip.textContent = CHIP[mode] || CHIP.idle;
    statusLine.textContent = status || "";
  }

  // ── Reloj de la top bar ─────────────────────────────────────────────────────
  function tickClock() {
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");
    clockEl.textContent = `${hh}:${mm}`;
  }
  tickClock();
  setInterval(tickClock, 15000);

  // ── Historial (modelo de turnos) ─────────────────────────────────────────────
  // Un "turno" agrupa pregunta + respuesta + modelo. El historial guarda la
  // conversación completa (antes solo la pregunta); al pulsar un item se abre un
  // modal con todo el detalle.
  /** @typedef {{id:number,userText:string,assistantText:string,model:string|null,ts:Date,el:HTMLElement,badgeEl:HTMLElement}} Turn */
  /** @type {Map<number, Turn>} */
  const turns = new Map();
  /** @type {Turn|null} Turno abierto (acumula la respuesta en curso). */
  let currentTurn = null;
  let turnSeq = 0;
  // Tras un reset de sesión, el siguiente turno abre un grupo nuevo (separador).
  let pendingSeparator = false;

  /**
   * Acorta un id de modelo a su nombre comercial. claude-opus-4-8 → "Opus".
   * @param {string|null|undefined} model
   * @returns {string} "Haiku"/"Sonnet"/"Opus", o el id sin prefijo si no encaja.
   */
  function shortModel(model) {
    if (!model) return "";
    const bare = model.replace(/^claude-/, "");
    const family = bare.split("-")[0];
    if (["haiku", "sonnet", "opus"].includes(family)) {
      return family.charAt(0).toUpperCase() + family.slice(1);
    }
    return bare;
  }

  function hhmm(date) {
    return `${String(date.getHours()).padStart(2, "0")}:${String(
      date.getMinutes()
    ).padStart(2, "0")}`;
  }

  /**
   * Pinta el separador "NUEVA CONVERSACIÓN" en el tope del historial.
   *
   * Es idempotente y con guard anti-duplicado: no inserta nada si el historial
   * está vacío (no hay nada de qué separar) ni si el tope ya es un separador
   * (dos resets seguidos, o un reset seguido de startTurn que vuelve a llamar).
   */
  function insertSeparator() {
    if (!historyList.children.length) return;
    if (historyList.firstElementChild?.classList.contains("history__sep")) return;
    const sep = document.createElement("li");
    sep.className = "history__sep";
    sep.textContent = "NUEVA CONVERSACIÓN";
    historyList.prepend(sep);
  }

  /**
   * Abre un turno nuevo con la pregunta del usuario y lo pinta en el historial.
   * @param {string} userText Pregunta (texto o transcript de voz).
   * @returns {Turn}
   */
  function startTurn(userText) {
    // Si venimos de un reset que aún no llegó a pintar el separador (p. ej. el
    // reset ocurrió con el historial vacío), lo intentamos ahora. Si ya estaba
    // pintado por applySessionReset, el guard de insertSeparator lo hace no-op.
    if (pendingSeparator) insertSeparator();
    pendingSeparator = false;

    const id = ++turnSeq;
    const ts = new Date();
    const li = document.createElement("li");
    li.className = "history__item is-active";
    li.dataset.turnId = String(id);
    historyList
      .querySelectorAll(".is-active")
      .forEach((el) => el.classList.remove("is-active"));
    li.innerHTML =
      `<span class="history__item-title"></span>` +
      `<span class="history__item-meta">` +
        `<span class="history__item-time">${hhmm(ts)}</span>` +
        `<span class="badge"></span>` +
      `</span>`;
    li.querySelector(".history__item-title").textContent = userText;
    const badgeEl = li.querySelector(".badge");
    historyList.prepend(li);

    const turn = { id, userText, assistantText: "", model: null, ts, el: li, badgeEl };
    turns.set(id, turn);
    currentTurn = turn;
    return turn;
  }

  /**
   * Acumula texto de la respuesta en el turno abierto.
   * @param {string} text Fragmento.
   * @param {string} [joiner] Separador entre fragmentos: " " para voz (llegan
   *   frases completas) y "" para texto (llegan tokens parciales del stream).
   */
  function appendAssistant(text, joiner = " ") {
    if (currentTurn) {
      currentTurn.assistantText += (currentTurn.assistantText ? joiner : "") + text;
    }
  }

  /** Cierra el turno abierto fijando el modelo definitivo y pintando el badge. */
  function closeTurn(model) {
    if (!currentTurn) return;
    if (model) {
      currentTurn.model = model;
      currentTurn.badgeEl.textContent = shortModel(model);
    }
    currentTurn = null;
  }

  // ── Modal de detalle ──────────────────────────────────────────────────────────
  /** @type {Turn|null} Turno mostrado en el modal (para el botón "Repetir"). */
  let modalTurn = null;
  function openModal(turn) {
    modalTurn = turn;
    modalQ.textContent = turn.userText || "—";
    modalA.textContent = turn.assistantText || "—";
    const label = shortModel(turn.model);
    modalBadge.textContent = label;
    modalBadge.hidden = !label;
    // Solo se puede repetir si hay respuesta del asistente que sintetizar.
    modalRepeat.hidden = !(turn.assistantText && turn.assistantText.trim());
    modal.hidden = false;
  }
  function closeModal() {
    modal.hidden = true;
  }
  // Repetir en voz alta la respuesta del turno abierto (re-síntesis vía /voice).
  modalRepeat.addEventListener("click", () => {
    if (modalTurn) repeatText(modalTurn.assistantText);
  });
  historyList.addEventListener("click", (e) => {
    const li = e.target.closest(".history__item");
    if (!li) return;
    const turn = turns.get(Number(li.dataset.turnId));
    if (turn) openModal(turn);
  });
  modalClose.addEventListener("click", closeModal);
  modalOverlay.addEventListener("click", closeModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hidden) closeModal();
  });

  // Plegar/desplegar historial (panel en escritorio, bottom-sheet en móvil).
  function toggleHistory() {
    const mobile = window.matchMedia("(max-width: 760px)").matches;
    if (mobile) {
      const open = app.classList.toggle("history-open");
      scrim.hidden = !open;
    } else {
      app.classList.toggle("history-collapsed");
    }
  }
  historyToggle.addEventListener("click", toggleHistory);
  historyEl.addEventListener("click", (e) => {
    // En móvil, tocar la zona asomada (grabber o cabecera) abre/cierra el sheet.
    // Se excluye el botón "+" (nueva conversación), que tiene su propio handler.
    if (e.target.closest("#history-new")) return;
    if (
      window.matchMedia("(max-width: 760px)").matches &&
      e.target.closest(".history__grabber, .history__head")
    ) {
      toggleHistory();
    }
  });
  scrim.addEventListener("click", () => {
    app.classList.remove("history-open");
    scrim.hidden = true;
  });

  /**
   * Aplica un reset de sesión en la UI: avisa, cierra el turno en curso y marca
   * que el siguiente turno abra un grupo nuevo (separador). NO toca micro ni WS.
   * @param {string} reason "timeout"|"ended"|"manual"
   */
  function applySessionReset(reason) {
    currentTurn = null;
    // Pintamos el separador YA (no en diferido): en reset por botón/cierre no
    // hay un turno a continuación que lo dispare, así que si esperáramos a
    // startTurn quedaría invisible. pendingSeparator se mantiene como guard para
    // el caso de reset con historial aún vacío (insertSeparator es no-op ahora,
    // y startTurn lo reintentará cuando ya haya un turno encima).
    pendingSeparator = true;
    insertSeparator();
    const note = {
      timeout: "Sesión reiniciada por inactividad — de vuelta en Haiku.",
      ended: "Conversación finalizada — de vuelta en Haiku.",
      manual: "Nueva conversación — de vuelta en Haiku.",
    }[reason] || "Sesión reiniciada — de vuelta en Haiku.";
    setMode("idle", note);
  }

  // Botón "nueva conversación" (+): pide al backend reciclar el núcleo (vuelta a
  // Haiku). Hay DOS sesiones independientes (voz por /voice, texto por /ws) con su
  // propio núcleo, así que avisamos a las dos que estén abiertas; el servidor
  // responde con `session_reset` (el guard anti-duplicado del separador evita que
  // dos respuestas pinten dos marcas). Si no hay ninguna abierta, reset local.
  historyNew.addEventListener("click", () => {
    let notified = false;
    const reset = { type: "reset_session" };
    if (voiceWs && voiceWs.readyState === WebSocket.OPEN) {
      voiceWs.send(JSON.stringify(reset));
      notified = true;
    }
    if (textWs && textWs.readyState === WebSocket.OPEN) {
      textWs.send(JSON.stringify(reset));
      notified = true;
    }
    if (!notified) applySessionReset("manual");
  });

  // ── Web Audio: contexto, medidor y reproducción ─────────────────────────────
  /** @type {AudioContext} */
  let audioCtx = null;
  /** AnalyserNode de la fuente activa (micro al escuchar / TTS al hablar). */
  let micAnalyser = null;
  /** AnalyserNode permanente al final de la cadena de reproducción TTS. */
  let playAnalyser = null;
  /** Instante (en tiempo del AudioContext) donde encolar el próximo audio. */
  let playCursor = 0;
  /** Fuentes de audio TTS aún sonando/encoladas, para poder cortarlas en seco. */
  const activeSources = new Set();

  /** Crea el AudioContext en el primer gesto del usuario (política de autoplay). */
  function ensureAudio() {
    if (audioCtx) return audioCtx;
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    playAnalyser = audioCtx.createAnalyser();
    playAnalyser.fftSize = 512;
    playAnalyser.connect(audioCtx.destination);
    return audioCtx;
  }

  const _rmsBuf = new Uint8Array(256);
  /**
   * RMS (0..~0.5) de un AnalyserNode en el dominio del tiempo.
   * @param {AnalyserNode} analyser
   * @returns {number}
   */
  function rms(analyser) {
    analyser.getByteTimeDomainData(_rmsBuf);
    let sum = 0;
    for (let i = 0; i < _rmsBuf.length; i++) {
      const x = (_rmsBuf[i] - 128) / 128;
      sum += x * x;
    }
    return Math.sqrt(sum / _rmsBuf.length);
  }

  /** Pinta las barras del waveform en función del nivel (0..1). */
  function paintWave(level) {
    for (let i = 0; i < waveBars.length; i++) {
      if (level > 0.02) {
        waveBars[i].style.animation = "none";
        const jitter = 0.4 + 0.6 * Math.abs(Math.sin(i * 1.7 + performance.now() / 120));
        const h = Math.max(0.18, Math.min(1, 0.2 + level * jitter * 1.6));
        waveBars[i].style.transform = `scaleY(${h.toFixed(3)})`;
      } else {
        waveBars[i].style.animation = "";
        waveBars[i].style.transform = "";
      }
    }
  }

  // Bucle único de medición: alimenta el orbe y el waveform según el modo.
  function meterLoop() {
    let level = 0;
    const mode = app.dataset.mode;
    if (mode === "listening" && micAnalyser) {
      level = Math.min(1, rms(micAnalyser) * 3.5);
    } else if (mode === "speaking" && playAnalyser) {
      level = Math.min(1, rms(playAnalyser) * 3.5);
    } else if (DEMO) {
      // Oscilación sintética para validar la estética sin backend.
      const t = performance.now() / 1000;
      level = 0.5 + 0.45 * Math.sin(t * 2) * Math.sin(t * 0.7);
      level = Math.max(0, level);
    }
    orb.setLevel(level);
    miniOrb.setLevel(level);
    paintWave(level);
    requestAnimationFrame(meterLoop);
  }
  requestAnimationFrame(meterLoop);

  /**
   * Encola un bloque de PCM Int16 para reproducirlo sin cortes tras lo anterior.
   * @param {ArrayBuffer} buffer PCM Int16 LE mono.
   * @param {number} sampleRate SR anunciado por el servidor.
   */
  function enqueuePcm(buffer, sampleRate) {
    const ctx = ensureAudio();
    const int16 = new Int16Array(buffer);
    const audioBuffer = ctx.createBuffer(1, int16.length, sampleRate);
    const ch = audioBuffer.getChannelData(0);
    for (let i = 0; i < int16.length; i++) ch[i] = int16[i] / 32768;

    const src = ctx.createBufferSource();
    src.buffer = audioBuffer;
    src.connect(playAnalyser);

    const startAt = Math.max(ctx.currentTime, playCursor);
    src.start(startAt);
    playCursor = startAt + audioBuffer.duration;
    activeSources.add(src);

    setMode("speaking", "REPRODUCIENDO");
    // Al terminar la última frase encolada, volvemos a reposo.
    src.onended = () => {
      activeSources.delete(src);
      if (ctx.currentTime >= playCursor - 0.05) setMode("idle");
    };
  }

  /**
   * Silencia y descarta TODO el audio TTS en curso/encolado (corte en seco).
   * Idempotente. No toca el servidor: es solo el lado cliente del "stop"/barge-in.
   */
  function clearPlayback() {
    for (const s of activeSources) {
      s.onended = null; // que no dispare el setMode("idle") al pararlo a mano
      try { s.stop(); } catch { /* ya parado */ }
    }
    activeSources.clear();
    if (audioCtx) playCursor = audioCtx.currentTime; // el próximo audio arranca ya
  }

  /**
   * "Para de hablar" (botón): corta el audio local al instante y pide al servidor
   * que deje de generar/enviar (cancela también la petición a Claude en vuelo).
   */
  function stopSpeaking() {
    clearPlayback();
    if (voiceWs && voiceWs.readyState === WebSocket.OPEN) {
      voiceWs.send(JSON.stringify({ type: "stop" }));
    }
    setMode("idle", "DETENIDO");
  }
  stopBtn.addEventListener("click", stopSpeaking);

  /**
   * Repite en voz alta un texto ya conocido (botón "Repetir" del modal). Lo
   * re-sintetiza el servidor vía /voice sin volver a pensar ni abrir turno.
   * @param {string} text Respuesta a repetir.
   */
  function repeatText(text) {
    if (!text || !text.trim()) return;
    const ctx = ensureAudio(); // el click es gesto de usuario: habilita el audio
    if (ctx.state === "suspended") ctx.resume();
    clearPlayback();           // corta lo que estuviera sonando
    closeModal();
    // El servidor re-emite `reply_text` por frase: dejamos que el handler
    // reconstruya el buffer (como en una respuesta normal) para no duplicarlo.
    assistantBuf = "";
    setMode("speaking", "REPITIENDO");
    ensureVoiceWs()
      .then((ws) => ws.send(JSON.stringify({ type: "speak", text })))
      .catch(() => setMode("idle", "ERROR"));
  }

  // ── WebSocket de voz (/voice) ───────────────────────────────────────────────
  const wsBase = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
  /** @type {WebSocket} */
  let voiceWs = null;
  let pendingSampleRate = 22050; // lo fija cada audio_start
  let assistantBuf = "";

  /** Abre (o reutiliza) el WS /voice. Resuelve cuando está OPEN. */
  function ensureVoiceWs() {
    if (voiceWs && voiceWs.readyState === WebSocket.OPEN) return Promise.resolve(voiceWs);
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(`${wsBase}/voice`);
      ws.binaryType = "arraybuffer";
      ws.onopen = () => { dbg("ws OPEN"); resolve(ws); };
      ws.onerror = (e) => { dbg("ws ERROR", e); reject(e); };
      ws.onclose = (e) => {
        dbg("ws CLOSE", e.code, e.reason);
        if (voiceWs === ws) voiceWs = null;
      };
      ws.onmessage = onVoiceMessage;
      voiceWs = ws;
    });
  }

  /** Maneja mensajes (JSON de control o binario PCM) del WS /voice. */
  function onVoiceMessage(event) {
    if (typeof event.data !== "string") {
      // Frame binario: PCM de una frase de audio.
      enqueuePcm(event.data, pendingSampleRate);
      return;
    }
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    dbg("ws<-", msg.type, msg.text !== undefined ? JSON.stringify(msg.text) : "");
    switch (msg.type) {
      case "transcript":
        assistantBuf = "";
        if (msg.text) {
          transcriptEl.textContent = `“${msg.text}”`;
          startTurn(msg.text);
        }
        setMode("thinking", "PENSANDO");
        break;
      case "cancelled":
        // El usuario dijo "Cancela": se descartó el transcript. No abrimos turno
        // ni tocamos el historial; solo avisamos y volvemos a reposo.
        assistantBuf = "";
        transcriptEl.textContent = "✋ Cancelado";
        setMode("idle", "CANCELADO");
        break;
      case "interrupted":
        // Barge-in: el servidor cortó la respuesta porque empezamos a hablar.
        // Silencia el audio que aún tuviéramos encolado localmente.
        clearPlayback();
        break;
      case "reply_text":
        assistantBuf += (assistantBuf ? " " : "") + msg.text;
        transcriptEl.textContent = assistantBuf;
        appendAssistant(msg.text);
        if (app.dataset.mode === "thinking") setMode("speaking", "RESPONDIENDO");
        break;
      case "audio_start":
        pendingSampleRate = msg.sample_rate || pendingSampleRate;
        break;
      case "done":
        closeTurn(msg.model);
        // Si aún queda audio sonando, el onended de la última frase volverá a
        // reposo; si no hay nada encolado (turno vacío o repetición sin audio),
        // pasamos a reposo ya para no quedarnos colgados en "speaking".
        if (activeSources.size === 0) setMode("idle");
        break;
      case "session_reset":
        applySessionReset(msg.reason);
        break;
      case "voice_loading":
        // El servidor aún carga Whisper: lo grabado se procesará al terminar.
        if (!recording) setMode("thinking", "PREPARANDO VOZ…");
        transcriptEl.textContent = "Cargando el modelo de voz, un momento…";
        break;
      case "voice_ready":
        if (transcriptEl.textContent.startsWith("Cargando el modelo de voz")) {
          transcriptEl.textContent = "";
        }
        if (!recording && app.dataset.mode === "thinking" && statusLine.textContent === "PREPARANDO VOZ…") {
          setMode("idle");
        }
        break;
      case "error":
        setMode("idle", "ERROR");
        transcriptEl.textContent = `⚠ ${msg.detail || "Error del servidor"}`;
        break;
    }
  }

  // ── Captura de micrófono (push-to-talk) ─────────────────────────────────────
  /** @type {MediaStream} */
  let micStream = null;
  /** @type {MediaStreamAudioSourceNode} Fuente del micro en el grafo de audio. */
  let micSource = null;
  /** @type {AudioWorkletNode} */
  let micNode = null;
  let recording = false;
  // ¿Se llegó a mandar `utterance_start` por el WS? Solo entonces tiene sentido
  // mandar el `utterance_end` correspondiente (evita que el end adelante al start
  // y desincronice al servidor — ver bug del orden de mensajes).
  let utteranceStarted = false;
  // Token monotónico del arranque en curso. Si se suelta el botón mientras
  // `startRecording` aún está en sus `await`, este token cambia y el arranque
  // pendiente se aborta sin tocar la red ni desincronizar el turno.
  let startSeq = 0;

  /**
   * Libera micro/worklet/analyser. Idempotente.
   *
   * IMPORTANTE: hay que desconectar el `source`, no solo el `micNode`.
   * `micNode.disconnect()` corta las SALIDAS del nodo, pero la conexión
   * `source → micNode` (la ENTRADA) sigue viva. Si no se rompe, el worklet del
   * turno anterior permanece en el grafo y, como `port.onmessage` mira variables
   * compartidas (`recording`/`utteranceStarted`), sigue enviando frames en los
   * turnos siguientes → la captura se va acumulando turno a turno hasta que el
   * audio es todo silencio/ceros y Whisper devuelve vacío. (Ese era EL bug.)
   */
  function teardownMic() {
    if (micNode) {
      micNode.port.onmessage = null; // que no envíe más aunque siga vivo un instante
      try { micNode.disconnect(); } catch { /* ya desconectado */ }
      micNode = null;
    }
    if (micSource) {
      try { micSource.disconnect(); } catch { /* ya desconectado */ }
      micSource = null;
    }
    if (micAnalyser) {
      try { micAnalyser.disconnect(); } catch { /* ya desconectado */ }
      micAnalyser = null;
    }
    if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  }

  async function startRecording() {
    if (recording) return;
    recording = true;
    const mySeq = ++startSeq; // identifica este arranque concreto
    // Barge-in instantáneo: si Jarvis estaba hablando, callarlo YA en local (el
    // servidor lo confirma luego con `interrupted` al recibir utterance_start).
    clearPlayback();
    setMode("listening", "ESCUCHANDO");
    micBtn.classList.add("is-recording");
    dbg("startRecording seq", mySeq);

    if (DEMO) return; // en demo no tocamos micro ni red

    // ¿Sigue vigente este arranque? (false si ya se soltó el botón o empezó otro).
    const aborted = () => mySeq !== startSeq;

    try {
      const ctx = ensureAudio();
      if (ctx.state === "suspended") await ctx.resume();
      dbg("audioCtx state", ctx.state);
      await ensureVoiceWs();
      if (aborted()) return; // se soltó durante el setup → no mandamos nada

      // Micro lo más crudo posible: noiseSuppression y autoGainControl están
      // pensados para telefonía y degradan la señal para un ASR (gating de voz
      // baja, bombeo de ruido entre palabras) → Whisper acaba alucinando frases
      // de subtítulos ("suscríbete", "gracias por ver"…). echoCancellation se
      // mantiene por si el TTS solapa, pero sin ganancia/supresión automáticas.
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: false,
          autoGainControl: false,
        },
      });
      if (aborted()) { teardownMic(); return; }
      const source = ctx.createMediaStreamSource(micStream);
      micSource = source; // referencia para poder desconectarlo en teardownMic

      // Analyser para oscilar el orbe con la voz entrante.
      micAnalyser = ctx.createAnalyser();
      micAnalyser.fftSize = 512;
      source.connect(micAnalyser);

      // Worklet de captura → PCM Int16 16 kHz → WS.
      await ctx.audioWorklet.addModule("mic-worklet.js");
      if (aborted()) { teardownMic(); return; }
      micNode = new AudioWorkletNode(ctx, "mic-processor");
      micNode.port.onmessage = (e) => {
        // Solo enviamos PCM mientras este turno siga grabando y ya se abrió.
        if (recording && utteranceStarted &&
            voiceWs && voiceWs.readyState === WebSocket.OPEN) {
          voiceWs.send(e.data);
          framesSent++;
        }
      };

      // Abrimos el turno ANTES de conectar la fuente: garantiza que el
      // `utterance_start` viaja antes que cualquier frame de PCM y antes que el
      // `utterance_end` que mande stopRecording.
      framesSent = 0;
      voiceWs.send(JSON.stringify({ type: "utterance_start" }));
      utteranceStarted = true;
      source.connect(micNode);
      dbg("-> utterance_start (seq", mySeq + ", ws=" + voiceWs.readyState + ")");
      // No conectamos micNode al destino: no queremos oírnos a nosotros mismos.
    } catch (err) {
      recording = false;
      startSeq++; // invalida el arranque
      utteranceStarted = false;
      teardownMic();
      micBtn.classList.remove("is-recording");
      setMode("idle", "SIN MICRO");
      transcriptEl.textContent = "No se pudo acceder al micrófono.";
      console.error(err);
    }
  }

  function stopRecording() {
    if (!recording) return;
    recording = false;
    startSeq++; // invalida cualquier arranque aún en sus `await`
    micBtn.classList.remove("is-recording");

    if (DEMO) {
      setMode("idle");
      return;
    }

    teardownMic();

    if (utteranceStarted) {
      // Hubo `utterance_start`: cerramos el turno como toca.
      utteranceStarted = false;
      const open = voiceWs && voiceWs.readyState === WebSocket.OPEN;
      dbg("stopRecording -> utterance_end · frames=", framesSent, "wsOpen=", open);
      if (open) {
        voiceWs.send(JSON.stringify({ type: "utterance_end" }));
      }
      setMode("thinking", "PROCESANDO");
    } else {
      // El arranque no llegó a abrir turno (soltó muy rápido): nada que cerrar.
      dbg("stopRecording sin turno abierto (soltó rápido) → idle");
      setMode("idle");
    }
  }

  // Con texto escrito, el botón del micro pasa a ser "enviar" (patrón WhatsApp):
  // en móvil no hay otro botón de envío y se pulsaba el micro esperando enviar.
  function updateMicMode() {
    const hasText = textInput.value.trim() !== "";
    micBtn.classList.toggle("is-send", hasText);
    const label = hasText ? "Enviar mensaje" : "Mantén pulsado para hablar";
    micBtn.setAttribute("aria-label", label);
    micBtn.title = label;
  }
  textInput.addEventListener("input", updateMicMode);

  // Push-to-talk con pointer events (cubre ratón y táctil).
  micBtn.addEventListener("pointerdown", (e) => {
    // preventDefault también evita que el input pierda el foco (teclado móvil abierto).
    e.preventDefault();
    if (micBtn.classList.contains("is-send")) {
      inputbar.requestSubmit();
      return;
    }
    micBtn.setPointerCapture(e.pointerId);
    startRecording();
  });
  micBtn.addEventListener("pointerup", () => stopRecording());
  micBtn.addEventListener("pointercancel", () => stopRecording());

  // ── Entrada de texto (vía /ws, endpoint de texto existente) ──────────────────
  /** @type {WebSocket} */
  let textWs = null;

  function sendText(prompt) {
    transcriptEl.textContent = `“${prompt}”`;
    startTurn(prompt);
    assistantBuf = "";
    setMode("thinking", "PENSANDO");

    const send = (ws) => ws.send(JSON.stringify({ type: "ask", prompt }));

    if (textWs && textWs.readyState === WebSocket.OPEN) {
      send(textWs);
      return;
    }
    const ws = new WebSocket(`${wsBase}/ws`);
    ws.onopen = () => send(ws);
    ws.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }
      if (msg.type === "chunk") {
        assistantBuf += msg.text;
        transcriptEl.textContent = assistantBuf;
        appendAssistant(msg.text, "");
        if (app.dataset.mode === "thinking") setMode("speaking", "RESPUESTA");
      } else if (msg.type === "done") {
        closeTurn(msg.model);
        setMode("idle");
      } else if (msg.type === "session_reset") {
        // El núcleo de texto se recicló (botón + o frase de cierre): de vuelta a Haiku.
        applySessionReset(msg.reason);
      } else if (msg.type === "error") {
        setMode("idle", "ERROR");
        transcriptEl.textContent = `⚠ ${msg.message || "Error"}`;
      }
    };
    ws.onclose = () => { if (textWs === ws) textWs = null; };
    textWs = ws;
  }

  inputbar.addEventListener("submit", (e) => {
    e.preventDefault();
    const prompt = textInput.value.trim();
    if (!prompt) return;
    textInput.value = "";
    updateMicMode();
    sendText(prompt);
  });

  // ── Widget de control multimedia (música YouTube) ───────────────────────────
  // Controla la MISMA sesión que la voz: el estado vive en el backend
  // (src/capabilities/youtube.py). El widget hace polling de /media/state y manda
  // órdenes a /media/command. El chip solo se ve cuando hay música sonando.
  const npChip = document.getElementById("np-chip");
  const npChipTitle = document.getElementById("np-chip-title");
  const npPanel = document.getElementById("np-panel");
  const npClose = document.getElementById("np-close");
  const npSrc = document.getElementById("np-src");
  const npTitle = document.getElementById("np-title");
  const npNext = document.getElementById("np-next");
  const npSeek = document.getElementById("np-seek-range");
  const npCur = document.getElementById("np-cur");
  const npDur = document.getElementById("np-dur");
  const npPrev = document.getElementById("np-prev");
  const npPlayPause = document.getElementById("np-playpause");
  const npNextBtn = document.getElementById("np-next-btn");
  const npStop = document.getElementById("np-stop");
  const npVol = document.getElementById("np-vol-range");

  // Iconos del botón central (play ↔ pausa); se intercambian según el estado.
  const ICON_PLAY =
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 4l13 8-13 8z"></path></svg>';
  const ICON_PAUSE =
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1"></rect><rect x="14" y="4" width="4" height="16" rx="1"></rect></svg>';

  let mediaSeeking = false;   // el usuario está arrastrando el slider de posición
  let mediaVolSeeking = false; // … o el de volumen (no los pisamos con el poll)
  let isPlaying = false;
  let volTimer = null;

  /** Formatea segundos a "m:ss". */
  function fmtTime(s) {
    s = Math.max(0, Math.round(s || 0));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  /** Manda una orden de control y refresca pronto para reflejar el cambio. */
  async function mediaCommand(action, value) {
    try {
      await fetch("/media/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(value === undefined ? { action } : { action, value }),
      });
    } catch {
      /* sin red puntual: el siguiente poll re-sincroniza */
    }
    setTimeout(pollMedia, 250);
  }

  /** Pinta el estado de la música en el chip y el panel. */
  function renderMedia(st) {
    if (!st || !st.active) {
      npChip.hidden = true;
      npPanel.hidden = true;
      npChip.setAttribute("aria-expanded", "false");
      return;
    }
    npChip.hidden = false;
    const title = st.title || "—";
    npChipTitle.textContent = title;
    npChip.title = title; // tooltip al pasar el ratón (escritorio): qué suena
    npTitle.textContent = title;
    npSrc.textContent = `YOUTUBE · ${(st.device || "").toUpperCase()}`;
    npNext.textContent = st.next_title ? `Siguiente: ${st.next_title}` : "";

    isPlaying = st.state === "PLAYING" || st.state === "BUFFERING";
    npPlayPause.innerHTML = isPlaying ? ICON_PAUSE : ICON_PLAY;
    npPlayPause.setAttribute("aria-label", isPlaying ? "Pausar" : "Reanudar");
    npChip.classList.toggle("is-paused", !isPlaying);

    if (!mediaSeeking) {
      npSeek.max = String(Math.max(1, Math.round(st.duration || 0)));
      npSeek.value = String(Math.round(st.position || 0));
      npCur.textContent = fmtTime(st.position);
      npDur.textContent = fmtTime(st.duration);
    }
    if (!mediaVolSeeking && typeof st.volume === "number") {
      npVol.value = String(st.volume);
    }
  }

  /** Sondea el estado de la música (también refleja cambios hechos por voz). */
  async function pollMedia() {
    try {
      const res = await fetch("/media/state");
      renderMedia(await res.json());
    } catch {
      /* backend no disponible: reintenta en el siguiente tick */
    }
  }

  // Abrir/cerrar el panel desde el chip.
  npChip.addEventListener("click", () => {
    npPanel.hidden = !npPanel.hidden;
    npChip.setAttribute("aria-expanded", String(!npPanel.hidden));
    if (!npPanel.hidden) pollMedia();
  });
  npClose.addEventListener("click", () => {
    npPanel.hidden = true;
    npChip.setAttribute("aria-expanded", "false");
  });
  // Cerrar al pulsar fuera del panel (sin afectar al chip, que tiene su toggle).
  document.addEventListener("click", (e) => {
    if (npPanel.hidden) return;
    if (e.target.closest("#np-panel") || e.target.closest("#np-chip")) return;
    npPanel.hidden = true;
    npChip.setAttribute("aria-expanded", "false");
  });

  // Botones de transporte.
  npPrev.addEventListener("click", () => mediaCommand("prev"));
  npNextBtn.addEventListener("click", () => mediaCommand("skip"));
  npStop.addEventListener("click", () => {
    mediaCommand("stop");
    npPanel.hidden = true;
  });
  npPlayPause.addEventListener("click", () =>
    mediaCommand(isPlaying ? "pause" : "resume")
  );

  // Slider de posición: mientras se arrastra solo actualiza el tiempo mostrado;
  // al soltar manda el seek.
  npSeek.addEventListener("input", () => {
    mediaSeeking = true;
    npCur.textContent = fmtTime(Number(npSeek.value));
  });
  npSeek.addEventListener("change", () => {
    mediaCommand("seek", Number(npSeek.value));
    mediaSeeking = false;
  });

  // Slider de volumen: throttle en input para que vaya suave sin spamear el Cast.
  npVol.addEventListener("input", () => {
    mediaVolSeeking = true;
    if (volTimer) return;
    volTimer = setTimeout(() => {
      mediaCommand("volume", Number(npVol.value));
      volTimer = null;
    }, 180);
  });
  npVol.addEventListener("change", () => {
    if (volTimer) { clearTimeout(volTimer); volTimer = null; }
    mediaCommand("volume", Number(npVol.value));
    mediaVolSeeking = false;
  });

  // Arranca el sondeo (ligero: si no suena nada, el backend responde sin tocar
  // el Cast).
  pollMedia();
  setInterval(pollMedia, 1500);

  // ── PRs de los agentes (Hito 9, Fase 3) ──────────────────────────────────────
  // Chip en la topbar (visible solo si hay PRs) → modal con la lista → detalle con
  // descripción y diff → "Fusionar" con confirmación. La fusión la hace el núcleo
  // (POST /api/prs/{id}/merge); por voz se le pide a Vision (user_merge, misma fusión).
  const prChip = document.getElementById("pr-chip");
  const prChipCount = document.getElementById("pr-chip-count");
  const prModal = document.getElementById("pr-modal");
  const prList = document.getElementById("pr-list");
  const prDetail = document.getElementById("pr-detail");
  const prBack = document.getElementById("pr-back");
  const prHead = document.getElementById("pr-head");
  const prMerge = document.getElementById("pr-merge");
  const prDiscard = document.getElementById("pr-discard");
  const prTitle = document.getElementById("pr-title");
  const prMeta = document.getElementById("pr-meta");
  const prMsg = document.getElementById("pr-msg");
  const prBody = document.getElementById("pr-body");
  const prDiff = document.getElementById("pr-diff");
  /** @type {string|null} Agente cuyo PR se muestra en el detalle. */
  let prCurrent = null;

  /**
   * Texto corto del estado de los tests de un PR.
   * @param {object} pr Registro del agente.
   * @returns {[string, string]} Texto y clase CSS.
   */
  function prTests(pr) {
    if (pr.pr_tests_passed === 1) return ["tests OK", "pr-ok"];
    if (pr.pr_tests_passed === 0) return ["tests FALLAN", "pr-ko"];
    return ["tests sin informar", ""];
  }

  /**
   * Fecha corta (dd/mm hh:mm) de un timestamp en segundos.
   * @param {number} ts
   * @returns {string}
   */
  function prDate(ts) {
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getDate())}/${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  /**
   * Pide JSON a la API y lanza Error con el `detail` si responde con error.
   * @param {string} url
   * @param {RequestInit} [opts]
   */
  async function prFetch(url, opts) {
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
    return data;
  }

  /** Actualiza el chip (y la lista si el modal está en la vista de lista). */
  async function pollPrs() {
    let prs;
    try {
      prs = await prFetch("/api/prs");
    } catch (_) {
      return; // sin worker o sin red: el chip se queda como estaba
    }
    const open = prs.filter((p) => p.pr_status === "open").length;
    prChip.hidden = prs.length === 0;
    prChipCount.textContent = String(open);
    prChip.classList.toggle("is-empty", open === 0);
    if (!prModal.hidden && prDetail.hidden) renderPrList(prs);
  }

  /**
   * Pinta la lista de PRs (abiertos primero).
   * @param {object[]} prs
   */
  function renderPrList(prs) {
    prList.replaceChildren();
    const sorted = [...prs].sort((a, b) =>
      (a.pr_status === "open" ? 0 : 1) - (b.pr_status === "open" ? 0 : 1));
    if (!sorted.length) {
      const li = document.createElement("li");
      li.className = "pr-empty";
      li.textContent = "No hay PRs.";
      prList.append(li);
    }
    for (const pr of sorted) {
      const li = document.createElement("li");
      li.className = "pr-item" + (pr.pr_status === "open" ? "" : " is-merged");
      const title = document.createElement("div");
      title.className = "pr-item__title";
      title.textContent = pr.pr_title || pr.task;
      const meta = document.createElement("div");
      meta.className = "pr-item__meta";
      const [tests, cls] = prTests(pr);
      const t = document.createElement("span");
      t.className = cls;
      t.textContent = tests;
      const status = { merged: "FUSIONADO", closed: "CERRADO" }[pr.pr_status] || `ABIERTO · agente ${pr.state}`;
      meta.append(`${pr.repo} · ${status} · ${prDate(pr.updated_at)} · `, t);
      li.append(title, meta);
      li.addEventListener("click", () => openPrDetail(pr.id));
      prList.append(li);
    }
  }

  /**
   * Pinta un diff unificado con colores por tipo de línea.
   * @param {string} diff
   */
  function renderDiff(diff) {
    prDiff.replaceChildren();
    const frag = document.createDocumentFragment();
    for (const line of (diff || "(sin cambios)").split("\n")) {
      const span = document.createElement("span");
      if (line.startsWith("diff --git")) span.className = "d-file";
      else if (line.startsWith("@@")) span.className = "d-hunk";
      else if (line.startsWith("+") && !line.startsWith("+++")) span.className = "d-add";
      else if (line.startsWith("-") && !line.startsWith("---")) span.className = "d-del";
      span.textContent = line || " ";
      frag.append(span);
    }
    prDiff.append(frag);
  }

  /**
   * Muestra un aviso en el detalle (resultado de la fusión o error).
   * @param {string} text
   * @param {boolean} [isError]
   */
  function prNotice(text, isError = false) {
    prMsg.textContent = text;
    prMsg.classList.toggle("is-error", isError);
    prMsg.hidden = !text;
  }

  /**
   * Abre el detalle de un PR (descripción + diff).
   * @param {string} id Id del agente.
   */
  async function openPrDetail(id) {
    prCurrent = id;
    prList.hidden = true;
    prDetail.hidden = false;
    prBack.hidden = false;
    prMerge.hidden = true;
    prDiscard.hidden = true;
    prNotice("");
    prTitle.textContent = "Cargando…";
    prMeta.textContent = "";
    prBody.textContent = "";
    prDiff.replaceChildren();
    let pr;
    try {
      pr = await prFetch(`/api/prs/${encodeURIComponent(id)}`);
    } catch (err) {
      prTitle.textContent = "—";
      prNotice(err.message, true);
      return;
    }
    if (prCurrent !== id) return; // el usuario ya cambió de PR
    fillPrDetail(pr);
  }

  /**
   * Rellena el detalle con un PR ya cargado.
   * @param {object} pr Registro del agente con `diff`.
   */
  function fillPrDetail(pr) {
    prHead.textContent = { merged: "PR FUSIONADO", closed: "PR CERRADO" }[pr.pr_status] || "PR ABIERTO";
    prTitle.textContent = pr.pr_title || pr.task;
    const [tests, cls] = prTests(pr);
    const t = document.createElement("span");
    t.className = cls;
    t.textContent = tests;
    prMeta.replaceChildren(
      `${pr.repo} · ${pr.branch} → ${pr.base_branch} · agente ${pr.id} (${pr.state}) · `, t);
    if (pr.pr_status === "merged" && pr.pr_merge_sha) {
      // Datos para el rollback: antes de la rama (1.er padre) y la rama en sí (2.º).
      prMeta.append(`\nmerge ${pr.pr_merge_sha.slice(0, 10)} · base anterior ${(pr.pr_base_sha || "").slice(0, 10)}`
        + ` · punta de la rama ${(pr.pr_branch_sha || "").slice(0, 10)}`);
      prMeta.style.whiteSpace = "pre-wrap";
    }
    prBody.textContent = pr.pr_body || "—";
    renderDiff(pr.diff);
    // Solo se ofrece fusionar con el agente parado (mismo criterio que el worker).
    prMerge.hidden = !(pr.pr_status === "open" && (pr.state === "idle" || pr.state === "done"));
    // Cerrar también vale con el agente cancelado o fallido (PR huérfano).
    prDiscard.hidden = !(pr.pr_status === "open" && ["idle", "done", "cancelled", "failed"].includes(pr.state));
    if (pr.pr_status === "open" && prMerge.hidden) {
      prNotice(`El agente está "${pr.state}": espera a que termine para fusionar.`);
    }
  }

  /** Vuelve a la vista de lista. */
  function showPrList() {
    prCurrent = null;
    prDetail.hidden = true;
    prList.hidden = false;
    prBack.hidden = true;
    prMerge.hidden = true;
    prDiscard.hidden = true;
    prHead.textContent = "PRs DE LOS AGENTES";
    pollPrs();
  }

  function closePrModal() {
    prModal.hidden = true;
  }

  prChip.addEventListener("click", () => {
    prModal.hidden = false;
    showPrList();
  });
  prBack.addEventListener("click", showPrList);
  document.getElementById("pr-close").addEventListener("click", closePrModal);
  document.getElementById("pr-overlay").addEventListener("click", closePrModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !prModal.hidden) closePrModal();
  });

  prMerge.addEventListener("click", async () => {
    const id = prCurrent;
    if (!id) return;
    const failing = prMeta.querySelector(".pr-ko") ? "\n\n⚠ LOS TESTS FALLAN." : "";
    if (!window.confirm(`¿Fusionar "${prTitle.textContent}" y borrar su rama?${failing}`)) return;
    prMerge.disabled = true;
    prNotice("Fusionando…");
    try {
      await prFetch(`/api/prs/${encodeURIComponent(id)}/merge`, { method: "POST" });
      const pr = await prFetch(`/api/prs/${encodeURIComponent(id)}`);
      if (prCurrent === id) {
        fillPrDetail(pr);
        prNotice("Fusionado. La rama se ha borrado; el merge guarda la base anterior y la punta de la rama.");
      }
      pollPrs();
    } catch (err) {
      prNotice(err.message, true);
    } finally {
      prMerge.disabled = false;
    }
  });

  // Cerrar sin fusionar: para PRs redundantes o descartados. La rama se queda en el repo.
  prDiscard.addEventListener("click", async () => {
    const id = prCurrent;
    if (!id) return;
    if (!window.confirm(`¿Cerrar SIN fusionar "${prTitle.textContent}"? La rama se queda en el repo.`)) return;
    prDiscard.disabled = true;
    prNotice("Cerrando…");
    try {
      await prFetch(`/api/prs/${encodeURIComponent(id)}/close`, { method: "POST" });
      const pr = await prFetch(`/api/prs/${encodeURIComponent(id)}`);
      if (prCurrent === id) {
        fillPrDetail(pr);
        prNotice("PR cerrado sin fusionar. La rama sigue en el repo.");
      }
      pollPrs();
    } catch (err) {
      prNotice(err.message, true);
    } finally {
      prDiscard.disabled = false;
    }
  });

  // El chip se refresca cada 30 s: los PR cambian a ritmo de agentes, no de segundos.
  pollPrs();
  setInterval(pollPrs, 30000);

  // ── Ajustes en caliente (Hito 9, Fase 6, bloque 5) ──────────────────────────
  // Chip ⚙ (solo con worker de agentes) → modal con los ajustes del worker y de Vision.
  // GUARDAR manda solo los campos cambiados (PUT /api/settings, todo o nada); ↺ quita el
  // valor en caliente de un campo y vuelve al del .env.
  const setChip = document.getElementById("set-chip");
  const setModal = document.getElementById("set-modal");
  const setGroups = document.getElementById("set-groups");
  const setMsg = document.getElementById("set-msg");
  const setRepos = document.getElementById("set-repos");
  const setSave = document.getElementById("set-save");
  /** @type {Map<string, {item: object, input: HTMLElement, row: HTMLElement}>} */
  const setFields = new Map();
  const SET_APPLIES = {
    instant: "al instante",
    next_agent: "próximo agente",
    next_question: "próxima pregunta",
    next_vision: "próxima sesión de Vision",
  };
  const SET_ORIGIN = { hot: "en caliente", env: ".env", default: "por defecto" };

  /**
   * Aviso en la cabecera del modal.
   * @param {string} text
   * @param {boolean} [isError]
   */
  function setNotice(text, isError = false) {
    setMsg.textContent = text;
    setMsg.classList.toggle("is-error", isError);
    setMsg.hidden = !text;
  }

  /**
   * Valor de un ajuste como texto del control (bool → "1"/"0").
   * @param {object} item
   * @param {*} value
   */
  function setText(item, value) {
    if (item.kind === "bool") return value ? "1" : "0";
    return value === null || value === undefined ? "" : String(value);
  }

  /**
   * Control de un ajuste según su tipo.
   * @param {object} item Ajuste de la API.
   * @returns {HTMLElement}
   */
  function setInput(item) {
    let input;
    if (item.kind === "bool" || item.kind === "enum") {
      input = document.createElement("select");
      const options = item.kind === "bool" ? [["1", "sí"], ["0", "no"]] : item.choices.map((c) => [c, c]);
      for (const [value, label] of options) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = label;
        input.append(opt);
      }
    } else {
      input = document.createElement("input");
      input.type = item.kind === "model" ? "text" : "number";
      if (item.min !== null) input.min = String(item.min);
      if (item.max !== null) input.max = String(item.max);
    }
    input.value = setText(item, item.value);
    return input;
  }

  /**
   * Pinta los ajustes agrupados (worker / núcleo).
   * @param {{settings: object[], repos: string[]|null}} data
   */
  function renderSettings(data) {
    setGroups.replaceChildren();
    setFields.clear();
    const groups = [["worker", "AGENTES (WORKER)"], ["core", "VISION Y WEB (NÚCLEO)"]];
    for (const [scope, title] of groups) {
      const items = data.settings.filter((i) => i.scope === scope);
      if (!items.length) continue;
      const label = document.createElement("p");
      label.className = "modal__label";
      label.textContent = title;
      setGroups.append(label);
      for (const item of items) {
        const row = document.createElement("div");
        row.className = "set-row" + (item.origin === "hot" ? " is-hot" : "");
        const name = document.createElement("span");
        name.className = "set-row__label";
        name.textContent = item.label;
        const input = setInput(item);
        input.addEventListener("input", () => {
          row.classList.toggle("is-changed", input.value !== setText(item, item.value));
        });
        const reset = document.createElement("button");
        reset.type = "button";
        reset.className = "modal__action set-reset";
        reset.textContent = "↺";
        reset.title = `Volver al valor del .env (${setText(item, item.env)})`;
        reset.addEventListener("click", () => saveSettings({ [item.key]: null }));
        const meta = document.createElement("div");
        meta.className = "pr-meta";
        const origin = document.createElement("span");
        origin.className = item.origin === "hot" ? "set-hot" : "";
        origin.textContent = SET_ORIGIN[item.origin] || item.origin;
        const range = item.min !== null ? ` · ${item.min}–${item.max}` : "";
        meta.append(`${item.key} · `, origin,
          ` · se aplica: ${SET_APPLIES[item.applies] || item.applies}${range} · .env: ${setText(item, item.env)}`);
        row.append(name, input, reset, meta);
        setGroups.append(row);
        setFields.set(item.key, { item, input, row });
      }
    }
    if (data.repos === null) {
      setRepos.textContent = "El worker no responde: solo se muestran los ajustes del núcleo.";
    } else {
      setRepos.textContent = data.repos.length ? data.repos.join(" · ") : "Ninguno.";
    }
  }

  const setUsage = document.getElementById("set-usage");
  const USAGE_WINDOWS = [["five_hour", "5 H"], ["seven_day", "7 D"]];

  /**
   * Pinta una barra por ventana con la marca del límite de la salvaguarda.
   * @param {{windows: object, limits: object, probed_at: number|null, probe_error: string|null}} data
   *   Respuesta de GET /api/usage (utilization y limits en 0–1, fechas en epoch s).
   */
  function renderUsage(data) {
    setUsage.replaceChildren();
    for (const [key, title] of USAGE_WINDOWS) {
      const win = (data.windows || {})[key];
      const rawLimit = (data.limits || {})[key];
      const limit = rawLimit != null ? Math.round(rawLimit * 100) : null;
      const pct = win && win.utilization != null ? Math.round(win.utilization * 100) : null;
      const row = document.createElement("div");
      row.className = "usage-row";
      const name = document.createElement("span");
      name.className = "usage-row__label";
      name.textContent = title;
      const bar = document.createElement("div");
      bar.className = "usage-bar" + (pct !== null && limit != null && pct >= limit ? " is-over" : "");
      const fill = document.createElement("div");
      fill.className = "usage-bar__fill";
      fill.style.width = `${Math.min(pct ?? 0, 100)}%`;
      bar.append(fill);
      if (limit != null) {
        const mark = document.createElement("div");
        mark.className = "usage-bar__limit";
        mark.style.left = `${Math.min(limit, 100)}%`;
        mark.title = `Salvaguarda: ${limit} %`;
        bar.append(mark);
      }
      const value = document.createElement("span");
      value.className = "usage-row__pct";
      value.textContent = pct === null ? "?" : `${pct} %`;
      const meta = document.createElement("div");
      meta.className = "pr-meta";
      const resets = win && win.resets_at
        ? new Date(win.resets_at * 1000).toLocaleString("es-ES", { weekday: "short", hour: "2-digit", minute: "2-digit" })
        : "—";
      meta.textContent = `reinicio: ${resets}` + (limit != null ? ` · salvaguarda: ${limit} %` : "");
      row.append(name, bar, value, meta);
      setUsage.append(row);
    }
    const foot = document.createElement("p");
    foot.className = "pr-meta";
    const at = data.probed_at ? new Date(data.probed_at * 1000).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" }) : null;
    foot.textContent = (at ? `Leído a las ${at}` : "Sin lectura de la sonda") + (data.probe_error ? ` · error: ${data.probe_error}` : "");
    setUsage.append(foot);
  }

  /** Carga el uso aparte de los ajustes: si falla, los ajustes se ven igual. */
  async function loadUsage() {
    try {
      renderUsage(await prFetch("/api/usage"));
    } catch (err) {
      setUsage.replaceChildren();
      const p = document.createElement("p");
      p.className = "pr-meta";
      p.textContent = `Uso no disponible: ${err.message}`;
      setUsage.append(p);
    }
  }

  /** Carga los ajustes y los pinta. */
  async function loadSettings() {
    setNotice("");
    loadUsage();
    try {
      renderSettings(await prFetch("/api/settings"));
    } catch (err) {
      setNotice(err.message, true);
    }
  }

  /**
   * Manda cambios a la API. Con 422 marca el error de cada campo y no aplica nada.
   * @param {Object<string, *>} changes `{CLAVE: valor | null}`.
   */
  async function saveSettings(changes) {
    if (!Object.keys(changes).length) {
      setNotice("No hay cambios.");
      return;
    }
    setSave.disabled = true;
    try {
      const res = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      });
      const data = await res.json().catch(() => ({}));
      if (res.status === 422 && data.detail && typeof data.detail === "object") {
        for (const { row } of setFields.values()) row.classList.remove("is-error");
        for (const el of setGroups.querySelectorAll(".set-row__error")) el.remove();
        for (const [key, error] of Object.entries(data.detail)) {
          const field = setFields.get(key);
          if (!field) continue;
          field.row.classList.add("is-error");
          const p = document.createElement("div");
          p.className = "set-row__error";
          p.textContent = error;
          field.row.append(p);
        }
        setNotice("No se ha guardado nada: corrige los campos marcados.", true);
        return;
      }
      if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
      renderSettings(data);
      setNotice("Guardado.");
    } catch (err) {
      setNotice(err.message, true);
    } finally {
      setSave.disabled = false;
    }
  }

  setSave.addEventListener("click", () => {
    const changes = {};
    for (const [key, { item, input }] of setFields) {
      if (input.value === setText(item, item.value)) continue;
      if (item.kind === "bool") changes[key] = input.value === "1";
      else if (item.kind === "enum" || item.kind === "model") changes[key] = input.value.trim();
      else changes[key] = input.value === "" ? "" : Number(input.value);
    }
    saveSettings(changes);
  });

  function closeSetModal() {
    setModal.hidden = true;
  }

  setChip.addEventListener("click", () => {
    setModal.hidden = false;
    loadSettings();
  });
  document.getElementById("set-close").addEventListener("click", closeSetModal);
  document.getElementById("set-overlay").addEventListener("click", closeSetModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !setModal.hidden) closeSetModal();
  });

  // El chip solo aparece si el núcleo tiene worker de agentes (si no, /api/settings
  // solo traería los timeouts de la web, que no justifican el botón).
  prFetch("/api/settings")
    .then((data) => { setChip.hidden = data.repos === null && !data.settings.some((i) => i.scope === "worker"); })
    .catch(() => {});

  // ── Demo: estado inicial visible ─────────────────────────────────────────────
  if (DEMO) {
    setMode("speaking", "DEMO · OSCILANDO");
    transcriptEl.textContent = "Modo demo: el orbe oscila sin backend.";
  }
})();
