"""Endpoint WebSocket de voz (`/voice`) para la UI web multi-dispositivo.

Orquesta el bucle de voz **server-side** reutilizando los módulos del prototipo:
    PCM del navegador → STT (faster-whisper) → JarvisCore.ask() → TTS (Piper)
    → PCM de vuelta al navegador (que lo reproduce por su propio altavoz).

El audio I/O ocurre en el cliente (navegador): el NUC solo transcribe, piensa y
sintetiza. Así esquivamos pasar ALSA/PulseAudio al contenedor (ver Hito 3.5).

Sesión (decisión Hito 3.6): **una `JarvisCore` por conexión WS**, multi-turno
mientras el cliente no cierre — mismo patrón que `/ws`. En push-to-talk el botón
delimita cada utterance.

Reset de sesión (Sub-hito 3.6.1): como una conexión = un núcleo de larga vida, el
ratchet-up de modelo nunca baja (tras subir a Opus te quedas en Opus hasta
recargar). Para volver a Haiku **sin recargar ni tocar el micro/WS**, se recicla
la `JarvisCore` en tres casos: (a) inactividad perezosa (al llegar el siguiente
``utterance_start`` si pasó más de ``JARVIS_WEB_SESSION_TIMEOUT_SECONDS``), (b)
frase de cierre ("gracias"/"adiós"…), (c) mensaje de control ``reset_session``
(botón "nueva conversación"). El reset emite ``session_reset`` al cliente.

Barge-in / interrupción (Hito 3.6.2): el turno se procesa en una **tarea async
cancelable**, no en línea, para que el bucle de mensajes siga vivo mientras Jarvis
habla. Así puede atender en caliente: ``stop`` (botón "Para de hablar"), un nuevo
``utterance_start`` (pulsar el micro corta la respuesta en curso = barge-in) y
``speak`` (botón "Repetir", re-sintetiza un texto ya conocido sin volver a pensar).

Contrato (ver docs/plan/hito-ui-web.md):
    Cliente  -> {"type":"utterance_start"}
                <frames binarios PCM Int16 LE, 16 kHz mono>   (mientras habla)
                {"type":"utterance_end"}
                {"type":"stop"}                                (botón "Para de hablar")
                {"type":"speak","text":"..."}                  (botón "Repetir": re-sintetiza)
                {"type":"reset_session"}                       (botón nueva conversación)
    Servidor -> {"type":"transcript","text":"..."}
                {"type":"cancelled","text":"..."}                  (dijo "Cancela": descartado, sin turno)
                {"type":"interrupted"}                             (barge-in: silencia el audio encolado)
                {"type":"reply_text","text":"...","model":"..."}   (0..N, por frase)
                {"type":"audio_start","sample_rate":22050}         (antes de cada PCM)
                <frames binarios PCM Int16 LE mono al sample_rate anunciado>
                {"type":"done","model":"..."}
                {"type":"session_reset","reason":"timeout"|"ended"|"manual"}
                {"type":"error","detail":"..."}

Modo de salida `cast` (Hito 3.5, satélite del salón) — extensiones retro-compatibles:
    Cliente  -> {"type":"hello","output":"cast","device":"Salón"}  (tras conectar; activa cast)
                {"type":"wake"}                                     (castea el beep de confirmación)
    Servidor -> {"type":"cast_start","duration_s":X}                (en vez de audio_start + PCM)
En modo cast NO se manda PCM al cliente: la respuesta completa se acumula, se
concatena en un WAV, se publica en tts_store y se castea al Home Mini. El resto de
mensajes (transcript/reply_text/done/session_reset/cancelled/interrupted) se
siguen enviando igual (el satélite los usa para su máquina de estados). Sin
`hello`, el comportamiento es el de siempre (streaming PCM al cliente).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

from src.capabilities import media_cast
from src.core import tts_store
from src.core.jarvis import JarvisCore
from src.core.session import FAREWELL, is_cancel, is_closing
from src.voice.stt import WhisperSTT
from src.voice.tts import PiperTTS

# Logger del bucle de voz: instrumenta el ciclo por turno (start/bytes/end/STT)
# para diagnosticar pérdidas de turno. Desactivado por defecto: la traza INFO por
# turno solo se emite si JARVIS_VOICE_DEBUG está activo (1/true/yes/on). Los
# WARNING (desincronización: frame sin grabar, doble start) se ven SIEMPRE, porque
# señalan un fallo real aunque no estemos depurando.
# uvicorn no muestra logs de loggers de app por defecto, así que le enganchamos un
# handler propio a stdout.
VOICE_DEBUG = os.environ.get("JARVIS_VOICE_DEBUG", "").lower() in ("1", "true", "yes", "on")
log = logging.getLogger("jarvis.voice")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [voice] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO if VOICE_DEBUG else logging.WARNING)
    log.propagate = False

# Cronometraje por turno (audio · STT+RTF · Claude TTFT · 1ª voz · TTS · total).
# Independiente del VOICE_DEBUG verboso: usa su propio logger a INFO para poder
# leerlo en los logs del contenedor (`docker logs jarvis`) sin activar toda la
# traza de diagnóstico. Se apaga con JARVIS_VOICE_TIMING=0. El coste es nulo (solo
# lecturas de time.monotonic()); no altera el comportamiento del bucle de voz.
TIMING = os.environ.get("JARVIS_VOICE_TIMING", "1").lower() in ("1", "true", "yes", "on")
tlog = logging.getLogger("jarvis.timing")
if not tlog.handlers:
    _th = logging.StreamHandler()
    _th.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [timing] %(message)s"))
    tlog.addHandler(_th)
    tlog.setLevel(logging.INFO if TIMING else logging.WARNING)
    tlog.propagate = False

# Separadores de frase para trocear el stream de Claude y sintetizar/enviar antes
# de tener la respuesta entera (misma idea que src/frontends/voice.py).
_SENTENCE_END = re.compile(r"[.!?…\n]+")

# Enlaces markdown [texto](url) y URLs sueltas. Los puntos de la URL ("www.tiempo.
# com") engañan a _SENTENCE_END y trocean la URL/el enlace por la mitad → la
# limpieza de TTS ya no los reconoce y Piper acaba deletreándolos. Se usa en dos
# sitios: para PROTEGERLOS al trocear (no romperlos) y para detectar cuándo aún
# están llegando incompletos por el stream (para esperar a tenerlos enteros).
_PROTECT = re.compile(r"\[[^\]]*\]\([^)]*\)|(?:https?://|www\.)\S+")

# Construcciones que pueden estar aún llegando por el stream y NO deben trocearse
# todavía (faltaría su cierre): un enlace markdown a medio formar o una URL al
# final del buffer que quizá siga creciendo en el siguiente chunk.
_INCOMPLETE = [
    re.compile(r"\[[^\]]*$"),                # '[' sin cerrar ']'
    re.compile(r"\[[^\]]*\]\([^)]*$"),       # '](' sin cerrar ')'
    re.compile(r"(?:https?://|www\.)\S*$"),  # URL pegada al final del buffer
]

# Frecuencia de muestreo del audio de entrada (lo fija el contrato: 16 kHz mono).
_INPUT_SAMPLE_RATE = 16000

# Duración mínima de una utterance para molestarse en transcribir. Por debajo de
# esto casi siempre es un pulsar-soltar accidental o un end prematuro: Whisper
# tiende a *alucinar* despedidas ("gracias", "hasta luego"…) sobre semisilencio,
# lo que hacía que Jarvis se despidiera solo. Mejor cerrar el turno en seco.
_MIN_UTTERANCE_SECONDS = 0.4
_MIN_UTTERANCE_BYTES = int(_INPUT_SAMPLE_RATE * _MIN_UTTERANCE_SECONDS) * 2  # Int16

# Inactividad (s) tras la cual el siguiente turno recicla el núcleo y vuelve a
# Haiku. Var propia para web: la del CLI (JARVIS_VOICE_TIMEOUT_SECONDS=8) es
# demasiado corta. Comprobación PEREZOSA (no hay timers de fondo): solo se mira al
# llegar el próximo utterance_start.
SESSION_TIMEOUT_SECONDS = float(os.environ.get("JARVIS_WEB_SESSION_TIMEOUT_SECONDS", "90"))

# Modelos de voz cargados una sola vez y compartidos por todas las conexiones:
# faster-whisper y Piper son caros de inicializar y son thread-safe para nuestro
# uso (transcribe/synthesize se llaman serializados por turno vía to_thread).
_stt: WhisperSTT | None = None
_tts: PiperTTS | None = None


def get_models() -> tuple[WhisperSTT, PiperTTS]:
    """Devuelve las instancias singleton de STT y TTS, creándolas en el primer uso.

    Returns:
        Par ``(stt, tts)`` listo para usar.
    """
    global _stt, _tts
    if _stt is None:
        _stt = WhisperSTT()
    if _tts is None:
        _tts = PiperTTS()
    return _stt, _tts


# Serializa la primera carga de modelos entre conexiones concurrentes.
_models_lock = asyncio.Lock()


async def get_models_async() -> tuple[WhisperSTT, PiperTTS]:
    """Versión no bloqueante de :func:`get_models` para usar desde el event loop.

    Crear WhisperSTT es lento (en una instalación nueva incluye descargar ~1,5 GB
    del modelo); hecho en el event loop congelaba TODO el servidor (chat de texto,
    /health…) mientras duraba. Se carga en un hilo, y el lock evita que dos
    conexiones simultáneas carguen Whisper dos veces (el doble de RAM: ver el
    incidente del 2026-10-06 en el PLAN).

    Returns:
        Par ``(stt, tts)`` listo para usar.
    """
    if _stt is not None and _tts is not None:
        return _stt, _tts
    async with _models_lock:
        if _stt is not None and _tts is not None:  # los cargó quien tenía el lock
            return _stt, _tts
        t0 = time.monotonic()
        models = await asyncio.to_thread(get_models)
        tlog.info("modelos de voz cargados en %.1fs", time.monotonic() - t0)
        return models


def _pcm16_to_float32(raw: bytes) -> np.ndarray:
    """Convierte PCM crudo Int16 LE en float32 normalizado [-1, 1] para Whisper.

    Args:
        raw: Bytes de PCM Int16 little-endian (mono).

    Returns:
        Array float32 mono. Vacío si ``raw`` está vacío o es impar.
    """
    if not raw or len(raw) < 2:
        return np.zeros(0, dtype=np.float32)
    pcm = np.frombuffer(raw, dtype="<i2")
    return pcm.astype(np.float32) / 32768.0


def _float32_to_pcm16(samples: np.ndarray) -> bytes:
    """Convierte audio float32 [-1, 1] en PCM Int16 LE para mandar al navegador.

    Args:
        samples: Array float32 mono (salida de PiperTTS.synthesize).

    Returns:
        Bytes PCM Int16 little-endian.
    """
    if samples.size == 0:
        return b""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _hold_incomplete(buffer: str) -> tuple[str, str]:
    """Separa el buffer en (estable, pendiente) reteniendo construcciones a medias.

    Si al final del buffer hay un enlace markdown o una URL que todavía se está
    recibiendo, se retiene desde donde empieza para no trocearlo. El resto es
    seguro de procesar ya.

    Args:
        buffer: Texto acumulado del stream de Claude.

    Returns:
        ``(estable, pendiente)`` — ``pendiente`` se reinyecta en el buffer.
    """
    cut = len(buffer)
    for pat in _INCOMPLETE:
        if (m := pat.search(buffer)) is not None:
            cut = min(cut, m.start())
    return buffer[:cut], buffer[cut:]


def _iter_sentences(buffer: str) -> tuple[list[str], str]:
    """Extrae las frases completas de ``buffer`` y devuelve el resto sin terminar.

    Protege enlaces markdown y URLs (sus puntos no son fin de frase) enmascarándolos
    antes de trocear y restaurándolos después, para no partir una URL en pedazos.

    Args:
        buffer: Texto acumulado del stream de Claude (sin construcciones a medias).

    Returns:
        ``(frases_completas, resto)`` — ``resto`` es el trozo aún sin cierre.
    """
    holders: list[str] = []

    def _mask(m: re.Match) -> str:
        holders.append(m.group(0))
        return f"\x00{len(holders) - 1}\x00"

    masked = _PROTECT.sub(_mask, buffer)

    def _unmask(s: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda m: holders[int(m.group(1))], s)

    sentences: list[str] = []
    while (match := _SENTENCE_END.search(masked)) is not None:
        sentence = masked[: match.end()].strip()
        masked = masked[match.end():]
        if sentence:
            sentences.append(_unmask(sentence))
    return sentences, _unmask(masked)


async def _cast_wav(device: str, url: str) -> None:
    """Castea un WAV (por URL) a un dispositivo Google Cast, sin bloquear el loop.

    Reutiliza la conexión Cast cacheada de media_cast (misma que usa youtube.py).

    Args:
        device: Nombre 'friendly' del dispositivo Cast (p. ej. "Salón").
        url: URL del WAV alcanzable por el Home Mini (LAN).
    """
    def _play() -> None:
        media_cast.get_device(device).media_controller.play_media(url, "audio/wav")

    await asyncio.to_thread(_play)


async def _cast_stop(device: str) -> None:
    """Detiene lo que suene en el dispositivo Cast (corta el WAV en curso).

    Idempotente: parar un dispositivo en reposo es inocuo. Se usa en barge-in /
    ``stop`` para cortar la respuesta que está sonando en el Home Mini.

    Args:
        device: Nombre 'friendly' del dispositivo Cast.
    """
    def _stop() -> None:
        media_cast.get_device(device).media_controller.stop()

    await asyncio.to_thread(_stop)


async def _finish_cast(
    websocket: WebSocket, tts: PiperTTS, device: str, pcm_chunks: list[bytes]
) -> None:
    """Concatena el PCM acumulado del turno, lo castea al Salón y avisa al satélite.

    Publica el WAV en tts_store, castea su URL al Home Mini y emite ``cast_start``
    con la duración (para que el satélite sepa cuánto durará el habla).

    Args:
        websocket: Conexión activa (para el ``cast_start``).
        tts: Sintetizador Piper (aporta el sample_rate del PCM).
        device: Dispositivo Cast de salida.
        pcm_chunks: Trozos de PCM Int16 LE mono, una por frase, en orden.
    """
    pcm = b"".join(pcm_chunks)
    if not pcm:
        return
    clip_id = tts_store.publish_pcm16(pcm, tts.sample_rate)
    url = f"{tts_store.get_base_url()}/tts/{clip_id}.wav"
    await _cast_wav(device, url)
    duration_s = (len(pcm) // 2) / tts.sample_rate  # 2 bytes/muestra, mono
    await websocket.send_json({"type": "cast_start", "duration_s": duration_s})


async def voice_endpoint(websocket: WebSocket) -> None:
    """Sesión de voz por WebSocket (push-to-talk).

    Una conexión = una sesión `JarvisCore` con memoria multi-turno. Acumula los
    frames PCM entre ``utterance_start`` y ``utterance_end``, transcribe, pregunta
    al núcleo y devuelve texto + audio Piper por frases.

    Args:
        websocket: Conexión WebSocket entrante (ruta `/voice`).
    """
    await websocket.accept()
    if _stt is None or _tts is None:
        # Primera conexión tras arrancar (o precarga aún en curso): avisar al cliente
        # para que no parezca colgado. Lo que mande mientras tanto queda en cola y se
        # procesa al terminar la carga.
        await websocket.send_json({"type": "voice_loading"})
        stt, tts = await get_models_async()
        await websocket.send_json({"type": "voice_ready"})
    else:
        stt, tts = _stt, _tts

    # Buffer de la utterance en curso (PCM Int16 LE del micro del cliente).
    utterance = bytearray()
    recording = False
    turn = 0  # contador de turnos para correlacionar logs de diagnóstico

    # Modo de salida de la conexión (Hito 3.5). Por defecto "stream" (PCM al
    # cliente = UI web, comportamiento intacto). El satélite del salón manda un
    # `hello` con output="cast" tras conectar y la respuesta va por Google Cast.
    output_mode = "stream"
    cast_device: str | None = None
    log.info("VOICE conexión abierta")

    # Núcleo de larga vida de la conexión, gestionado a mano (no `async with`)
    # para poder reciclarlo dentro de la misma conexión sin cerrar el WS.
    jarvis = JarvisCore()
    await jarvis.__aenter__()
    # Reloj monótono de la última interacción; gobierna el reset por inactividad.
    last_activity = time.monotonic()

    # Tarea que procesa el turno en curso (STT→núcleo→TTS) o una repetición. Vive
    # en paralelo al bucle de mensajes para que `stop`/barge-in puedan cortarla.
    speaking_task: asyncio.Task | None = None

    async def stop_speaking(notify: bool) -> None:
        """Cancela la tarea de habla en curso (respuesta o repetición), si la hay.

        Corta también la petición a Claude en vuelo (la ``CancelledError`` aborta el
        ``async for`` del stream), ahorrando tokens.

        Args:
            notify: Si True, emite ``interrupted`` para que el cliente silencie el
                audio ya encolado (caso barge-in al pulsar el micro). El botón
                "Para de hablar" no lo necesita: el cliente ya se silenció solo.
        """
        nonlocal speaking_task
        task = speaking_task
        speaking_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # En modo cast el habla suena en el Home Mini, no en una tarea viva (el
        # turno acaba en cuanto se castea el WAV). Para cortar de verdad hay que
        # parar el media controller del Cast. Idempotente si ya está en reposo.
        if output_mode == "cast" and cast_device:
            try:
                await _cast_stop(cast_device)
            except Exception as exc:  # noqa: BLE001 - no romper el barge-in por el Cast
                log.warning("VOICE cast stop falló: %s", exc)
        if notify:
            await websocket.send_json({"type": "interrupted"})

    async def run_turn(raw_pcm: bytes) -> None:
        """Procesa un turno completo como tarea cancelable (antes era en línea).

        Encapsula STT → núcleo → TTS y el reciclaje a Haiku si el transcript era una
        frase de cierre. Si se cancela (barge-in/stop), la ``CancelledError`` se
        propaga y ``stop_speaking`` la absorbe.

        Args:
            raw_pcm: PCM Int16 LE 16 kHz mono de la utterance.
        """
        nonlocal last_activity
        reset_requested = await _handle_utterance(
            websocket, jarvis, stt, tts, raw_pcm, turn, output_mode, cast_device
        )
        last_activity = time.monotonic()
        if reset_requested:
            await reset_core("ended")
            last_activity = time.monotonic()

    async def reset_core(reason: str) -> None:
        """Cierra el núcleo actual y abre uno fresco (de vuelta a Haiku).

        No toca el micro ni el WebSocket: solo recicla la ``JarvisCore`` (memoria
        borrada + modelo reseteado). Avisa al cliente con ``session_reset``.

        Args:
            reason: Motivo del reset (``timeout``/``ended``/``manual``).
        """
        nonlocal jarvis
        log.info("VOICE session_reset (%s) tras %d turnos", reason, turn)
        await jarvis.__aexit__(None, None, None)
        jarvis = JarvisCore()
        await jarvis.__aenter__()
        await websocket.send_json({"type": "session_reset", "reason": reason})

    try:
        while True:
            message = await websocket.receive()

            # `receive()` de bajo nivel entrega la desconexión como un mensaje
            # (no como excepción): hay que detectarla y salir, o el siguiente
            # receive() lanzaría RuntimeError.
            if message["type"] == "websocket.disconnect":
                log.info("VOICE desconexión (tras %d turnos)", turn)
                return

            # Frames binarios: PCM del micro mientras se mantiene el botón.
            if (data := message.get("bytes")) is not None:
                if recording:
                    utterance.extend(data)
                else:
                    # Frame recibido sin estar grabando: señal de desincronización
                    # (el end adelantó al start, o frames de un turno anterior).
                    log.warning(
                        "VOICE frame de %d B IGNORADO (recording=False)", len(data)
                    )
                continue

            text = message.get("text")
            if text is None:
                continue

            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "error", "detail": "JSON de control inválido."}
                )
                continue

            msg_type = msg.get("type")
            if msg_type == "hello":
                # Handshake del satélite: activa el modo de salida por Google Cast.
                # Sin `hello`, la conexión sigue en modo stream (UI web, intacto).
                if msg.get("output") == "cast":
                    output_mode = "cast"
                    cast_device = msg.get("device") or media_cast.DEFAULT_DEVICE
                    log.info("VOICE hello: output=cast device=%r", cast_device)
                continue
            if msg_type == "wake":
                # Wake word detectado en el satélite: castea el beep de confirmación
                # ("te escucho" + calienta la sesión Cast). Solo en modo cast.
                if output_mode == "cast" and cast_device:
                    url = f"{tts_store.get_base_url()}/tts/{tts_store.DING_ID}.wav"
                    try:
                        await _cast_wav(cast_device, url)
                    except Exception as exc:  # noqa: BLE001 - el ding no es crítico
                        log.warning("VOICE cast del ding falló: %s", exc)
                continue
            if msg_type == "utterance_start":
                # Barge-in: si Jarvis está respondiendo, pulsar el micro corta la
                # respuesta en curso (y la petición a Claude) y manda `interrupted`
                # para que el cliente silencie el audio ya encolado. (En modo cast
                # el barge-in llega por `stop`, no por aquí: cuando se abre este
                # utterance el turno anterior ya terminó y no hay nada que cortar.)
                if speaking_task is not None and not speaking_task.done():
                    await stop_speaking(notify=True)
                # Reset PEREZOSO por inactividad: si la conexión lleva mucho
                # parada, reciclamos a Haiku ANTES de procesar esta utterance.
                idle = time.monotonic() - last_activity
                if idle > SESSION_TIMEOUT_SECONDS:
                    log.info("VOICE inactividad %.0fs > %.0fs", idle, SESSION_TIMEOUT_SECONDS)
                    await reset_core("timeout")
                    last_activity = time.monotonic()
                if recording:
                    log.warning("VOICE utterance_start con recording ya True")
                utterance = bytearray()
                recording = True
                turn += 1
                log.info("VOICE turno %d: utterance_start", turn)
            elif msg_type == "utterance_end":
                secs = len(utterance) / (_INPUT_SAMPLE_RATE * 2)
                log.info(
                    "VOICE turno %d: utterance_end · %d B (%.2fs) · recording=%s",
                    turn, len(utterance), secs, recording,
                )
                recording = False
                # Defensivo: el barge-in del start ya debería haber cortado nada,
                # pero si quedara una tarea viva la paramos antes del nuevo turno.
                if speaking_task is not None and not speaking_task.done():
                    await stop_speaking(notify=False)
                # El turno se procesa en una TAREA aparte: así el bucle sigue libre
                # para atender `stop`/barge-in mientras Jarvis piensa y habla. El
                # reciclaje a Haiku por frase de cierre lo hace `run_turn`.
                speaking_task = asyncio.create_task(run_turn(bytes(utterance)))
                utterance = bytearray()
                last_activity = time.monotonic()
            elif msg_type == "stop":
                # Botón "Para de hablar" / barge-in del satélite: corta la respuesta
                # en curso. El cliente ya silenció su audio local, así que no hace
                # falta `interrupted`. En modo cast corta el WAV del Home Mini aunque
                # el turno ya haya terminado (por eso también entra si no hay tarea).
                if (speaking_task is not None and not speaking_task.done()) or output_mode == "cast":
                    log.info("VOICE stop manual (botón/barge-in)")
                    await stop_speaking(notify=False)
                    last_activity = time.monotonic()
            elif msg_type == "speak":
                # Botón "Repetir": re-sintetiza un texto YA conocido por el cliente
                # (sin pensar ni abrir turno). Cancelable como cualquier habla.
                if speaking_task is not None and not speaking_task.done():
                    await stop_speaking(notify=False)
                speaking_task = asyncio.create_task(
                    _speak_text(websocket, jarvis, tts, msg.get("text") or "")
                )
                last_activity = time.monotonic()
            elif msg_type == "reset_session":
                # Botón "nueva conversación" del cliente. Corta cualquier habla viva
                # antes de reciclar: el núcleo que está usando va a desaparecer.
                if speaking_task is not None and not speaking_task.done():
                    await stop_speaking(notify=False)
                await reset_core("manual")
                last_activity = time.monotonic()
            else:
                await websocket.send_json(
                    {"type": "error", "detail": f"Tipo desconocido: {msg_type!r}."}
                )
    except WebSocketDisconnect:
        # Desconexión normal del cliente: no es un error.
        log.info("VOICE WebSocketDisconnect (tras %d turnos)", turn)
    finally:
        # Corta una posible respuesta en vuelo antes de cerrar el núcleo que usa.
        if speaking_task is not None and not speaking_task.done():
            speaking_task.cancel()
            try:
                await speaking_task
            except asyncio.CancelledError:
                pass
        await jarvis.__aexit__(None, None, None)


async def _handle_utterance(
    websocket: WebSocket,
    jarvis: JarvisCore,
    stt: WhisperSTT,
    tts: PiperTTS,
    raw_pcm: bytes,
    turn: int,
    output_mode: str = "stream",
    cast_device: str | None = None,
) -> bool:
    """Procesa una utterance completa: STT → núcleo → TTS por frases.

    En modo ``stream`` (UI web) cada frase se sintetiza y su PCM se manda al
    cliente. En modo ``cast`` (satélite del salón) el PCM de todas las frases se
    acumula, se concatena en un solo WAV, se publica y se castea al Home Mini
    (un WAV por respuesta evita los huecos de castear frase a frase).

    Args:
        websocket: Conexión activa para responder.
        jarvis: Núcleo de la sesión (memoria multi-turno).
        stt: Transcriptor Whisper compartido.
        tts: Sintetizador Piper compartido.
        raw_pcm: PCM Int16 LE 16 kHz mono acumulado durante la utterance.
        turn: Número de turno de la conexión (solo para etiquetar el cronometraje).
        output_mode: ``"stream"`` (PCM al cliente) o ``"cast"`` (WAV al Google Cast).
        cast_device: Dispositivo Cast de salida en modo cast (p. ej. "Salón").

    Returns:
        True si el transcript era una frase de cierre (el llamador debe reciclar
        el núcleo a Haiku tras esta utterance). False en cualquier otro caso.
    """
    is_cast = output_mode == "cast"
    # t0 marca el inicio del procesamiento del turno (justo tras recibir el audio):
    # el "total" y la "1ª voz" se miden desde aquí, que es lo que el usuario percibe
    # como "tarda en responderme" desde que suelta el botón.
    audio_secs = len(raw_pcm) / (_INPUT_SAMPLE_RATE * 2)
    t0 = time.monotonic()

    # Utterance demasiado corta (pulsación accidental / end prematuro): cerramos
    # sin transcribir para no provocar alucinaciones de despedida en Whisper.
    if len(raw_pcm) < _MIN_UTTERANCE_BYTES:
        log.info("VOICE utterance demasiado corta (%d B) → turno vacío", len(raw_pcm))
        await websocket.send_json({"type": "transcript", "text": ""})
        await websocket.send_json({"type": "done", "model": jarvis.current_model})
        return False

    audio = _pcm16_to_float32(raw_pcm)
    t_stt = time.monotonic()
    transcript = await asyncio.to_thread(stt.transcribe, audio)
    stt_secs = time.monotonic() - t_stt
    log.info("VOICE transcript: %r", transcript)

    def _emit_timing(
        outcome: str,
        *,
        ttft: float | None = None,
        first_voice: float | None = None,
        tts_secs: float = 0.0,
    ) -> None:
        """Vuelca una línea de cronometraje del turno (si JARVIS_VOICE_TIMING).

        Args:
            outcome: Desenlace del turno (ok/cancelado/vacío/cierre/error).
            ttft: Segundos hasta el primer fragmento de texto de Claude.
            first_voice: Segundos desde t0 hasta despachar el primer audio (lo que
                el usuario espera hasta oír algo).
            tts_secs: Segundos totales de síntesis Piper del turno.
        """
        if not TIMING:
            return
        total = time.monotonic() - t0
        # RTF (real-time factor): STT / duración del audio. >1 = Whisper tarda más
        # que lo que dura tu frase → ahí está el cuello de botella.
        rtf = stt_secs / audio_secs if audio_secs else 0.0
        parts = [
            f"turno {turn}",
            f"audio {audio_secs:.1f}s",
            f"STT {stt_secs:.2f}s (RTF {rtf:.1f}x)",
        ]
        if ttft is not None:
            parts.append(f"Claude TTFT {ttft:.2f}s")
        if first_voice is not None:
            parts.append(f"1ª voz {first_voice:.2f}s")
        parts.append(f"TTS {tts_secs:.2f}s")
        parts.append(f"total {total:.2f}s")
        parts.append(f"modelo {jarvis.current_model}")
        parts.append(outcome)
        tlog.info(" · ".join(parts))

    # Cancelación al vuelo ("Cancela"): se descarta el transcript sin pensar ni
    # hablar y sin reciclar el núcleo. Emitimos `cancelled` (no el `transcript`
    # normal) para que la UI no abra un turno en el historial: solo avisa y vuelve
    # a reposo. La sesión sigue intacta.
    if is_cancel(transcript):
        log.info("VOICE cancelación: %r → descartado", transcript)
        await websocket.send_json({"type": "cancelled", "text": transcript})
        _emit_timing("cancelado")
        return False

    await websocket.send_json({"type": "transcript", "text": transcript})
    if not transcript:
        # No se captó nada: cerramos el turno sin pensar/hablar.
        await websocket.send_json({"type": "done", "model": jarvis.current_model})
        _emit_timing("vacío")
        return False

    # Frase de cierre ("gracias"/"adiós"…): no preguntamos a Claude; decimos una
    # despedida corta, cerramos el turno y pedimos al llamador que recicle a Haiku.
    if is_closing(transcript):
        log.info("VOICE frase de cierre: %r → despedida + reset", transcript)
        synth, pcm = await _speak_sentence(
            websocket, jarvis, tts, FAREWELL, emit=not is_cast
        )
        if is_cast and cast_device:
            await _finish_cast(websocket, tts, cast_device, [pcm])
        await websocket.send_json({"type": "done", "model": jarvis.current_model})
        _emit_timing("cierre", tts_secs=synth)
        return True

    tts_total = 0.0
    first_chunk_at: float | None = None  # primer fragmento de texto de Claude
    first_audio_at: float | None = None  # primer audio despachado (o cast_start)
    # En modo cast se acumula el PCM de todas las frases para castear un WAV único.
    cast_pcm: list[bytes] = []
    try:
        buffer = ""
        t_ask = time.monotonic()
        async for chunk in jarvis.ask(transcript):
            if first_chunk_at is None:
                first_chunk_at = time.monotonic()
            buffer += chunk
            # Retén enlaces/URLs que aún están llegando; trocea solo lo estable.
            stable, pending = _hold_incomplete(buffer)
            sentences, leftover = _iter_sentences(stable)
            buffer = leftover + pending
            for sentence in sentences:
                synth, pcm = await _speak_sentence(
                    websocket, jarvis, tts, sentence, emit=not is_cast
                )
                tts_total += synth
                if is_cast:
                    cast_pcm.append(pcm)
                elif first_audio_at is None:
                    first_audio_at = time.monotonic()
        # Última frase sin signo de cierre (ya con la URL/enlace completos).
        if buffer.strip():
            synth, pcm = await _speak_sentence(
                websocket, jarvis, tts, buffer.strip(), emit=not is_cast
            )
            tts_total += synth
            if is_cast:
                cast_pcm.append(pcm)
            elif first_audio_at is None:
                first_audio_at = time.monotonic()

        # Modo cast: la respuesta entera se castea de una vez. La "1ª voz" que
        # percibe el usuario es el instante del cast_start (no hubo audio antes).
        if is_cast and cast_device:
            await _finish_cast(websocket, tts, cast_device, cast_pcm)
            first_audio_at = time.monotonic()

        await websocket.send_json({"type": "done", "model": jarvis.current_model})
        _emit_timing(
            "ok",
            ttft=(first_chunk_at - t_ask) if first_chunk_at is not None else None,
            first_voice=(first_audio_at - t0) if first_audio_at is not None else None,
            tts_secs=tts_total,
        )
    except Exception as exc:  # noqa: BLE001 - reportamos cualquier fallo al cliente
        await websocket.send_json({"type": "error", "detail": str(exc)})
        _emit_timing("error", tts_secs=tts_total)

    return False


async def _speak_text(
    websocket: WebSocket, jarvis: JarvisCore, tts: PiperTTS, text: str
) -> None:
    """Re-sintetiza y emite un texto ya conocido (botón "Repetir"), sin pensar.

    No llama a Claude ni abre turno: solo TTS. Trocea por frases para que el audio
    arranque antes y para que un `stop`/barge-in pueda cortar entre frases. Reemite
    ``reply_text`` (la UI lo muestra sin crear turno, porque no llega ``transcript``).

    Args:
        websocket: Conexión activa.
        jarvis: Núcleo (solo para etiquetar el modelo en el texto/done).
        tts: Sintetizador Piper compartido.
        text: Texto a repetir (la respuesta que el cliente ya tenía).
    """
    clean = text.strip()
    if not clean:
        return
    sentences, leftover = _iter_sentences(clean)
    for sentence in sentences:
        await _speak_sentence(websocket, jarvis, tts, sentence)
    if leftover.strip():
        await _speak_sentence(websocket, jarvis, tts, leftover.strip())
    await websocket.send_json({"type": "done", "model": jarvis.current_model})


async def _speak_sentence(
    websocket: WebSocket,
    jarvis: JarvisCore,
    tts: PiperTTS,
    sentence: str,
    emit: bool = True,
) -> tuple[float, bytes]:
    """Sintetiza una frase con Piper; en modo stream la manda al cliente.

    Siempre emite el ``reply_text`` (texto de la frase, que el satélite y la UI
    usan). El PCM se envía al cliente solo si ``emit`` es True (modo stream); en
    modo cast se devuelve para acumularlo y castear un WAV único al final.

    Args:
        websocket: Conexión activa.
        jarvis: Núcleo (para etiquetar el modelo en el texto).
        tts: Sintetizador Piper compartido.
        sentence: Frase ya completa a sintetizar.
        emit: Si True, manda ``audio_start`` + PCM al cliente (modo stream). Si
            False, solo sintetiza y devuelve el PCM (modo cast).

    Returns:
        ``(synth_secs, pcm)`` — segundos de síntesis Piper (para el cronometraje) y
        el PCM Int16 LE mono de la frase (vacío si quedó vacía tras limpiarla).
    """
    await websocket.send_json(
        {"type": "reply_text", "text": sentence, "model": jarvis.current_model}
    )
    t_synth = time.monotonic()
    samples = await asyncio.to_thread(tts.synthesize, sentence)
    synth_secs = time.monotonic() - t_synth
    pcm = _float32_to_pcm16(samples)
    if emit and pcm:
        await websocket.send_json({"type": "audio_start", "sample_rate": tts.sample_rate})
        await websocket.send_bytes(pcm)
    return synth_secs, pcm
