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

  // ── Demo: estado inicial visible ─────────────────────────────────────────────
  if (DEMO) {
    setMode("speaking", "DEMO · OSCILANDO");
    transcriptEl.textContent = "Modo demo: el orbe oscila sin backend.";
  }
})();
