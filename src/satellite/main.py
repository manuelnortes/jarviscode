"""Punto de entrada del satélite de voz ambiente.

Dos modos, seleccionados por ``JARVIS_SAT_MODE``:

* ``test-once`` (Fase 2): valida la cadena micro PS Eye → STT → Claude → Cast sin
  wake word — graba unos segundos y manda una utterance.
* ``run`` (Fase 3, por defecto): **máquina de estados con wake word**. El micro
  escucha siempre; "Hey Jarvis" abre una sesión, la VAD delimita cada frase por
  silencio, la respuesta suena por el Home del Salón (Cast) y hay multi-turno con
  ventana de seguimiento. "Hey Jarvis" mientras Jarvis habla corta la respuesta
  (barge-in, D5).

Concurrencia (modo ``run``): el callback de captura (hilo de PortAudio) empuja los
bloques a una ``asyncio.Queue`` (``mic.bind_async``); una tarea dirige la máquina
de estados consumiendo bloques y otra (``_recv_loop``) lee los mensajes del WS y
actualiza el estado compartido (``cast_start`` → duración; ``session_reset`` /
``cancelled`` → vuelta a reposo).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import numpy as np
import websockets

from src.satellite.mic import BLOCK_SIZE, SAMPLE_RATE, Microphone
from src.satellite.ws_client import VoiceClient

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_WS_URL = os.getenv("JARVIS_SAT_WS_URL", "ws://127.0.0.1:8200/voice")
_CAST_DEVICE = os.getenv("JARVIS_SAT_CAST_DEVICE", "Salón")
_MODE = os.getenv("JARVIS_SAT_MODE", "run")
_TEST_RECORD_SECONDS = float(os.getenv("JARVIS_SAT_TEST_SECONDS", "4.0"))
_TEST_WARMUP_SECONDS = 2.0  # dejar que el stream de captura se asiente

# --- Knobs de la máquina de estados (modo run) ---------------------------------
# Diagnóstico del wake: loguea scores/heartbeat en WAKE_LISTEN para tunear umbral.
_WAKE_DEBUG = os.getenv("JARVIS_SAT_WAKE_DEBUG", "1") == "1"
# Ganancia SOLO para los detectores (wake/VAD): la PS Eye capta muy flojo
# (RMS ~0.01) y sin amplificar el wake se queda en ~0.1. Se aplica aquí, no en
# mic.py, para que el audio que se streamea al STT siga CRUDO (amplificarlo lo
# clippea y Whisper transcribe basura). 1.0 = passthrough. Tunable sin rebuild.
_DETECT_GAIN = float(os.getenv("JARVIS_SAT_INPUT_GAIN", "1.0"))
_SILENCE_SECONDS = float(os.getenv("JARVIS_SAT_SILENCE_SECONDS", "0.8"))
_MAX_UTTERANCE_SECONDS = float(os.getenv("JARVIS_SAT_MAX_UTTERANCE_SECONDS", "15"))
_FOLLOWUP_SECONDS = float(os.getenv("JARVIS_SAT_FOLLOWUP_SECONDS", "8"))
# Tiempo máximo esperando a que el usuario empiece a hablar tras el wake/followup;
# evita utterances de puro silencio si el disparo fue un falso positivo.
_NO_SPEECH_ONSET_SECONDS = 6.0
# Ventana inicial de la captura en la que se transmite audio pero NO se evalúa el
# cierre por silencio: cubre el ding (que el micro oye por el Cast, con ~1-2 s de
# latencia de arranque) y la reacción del usuario, para que no se cierre la
# utterance antes de que hable.
_CAPTURE_WARMUP_SECONDS = 2.0
# Inactividad máxima en THINKING: si el núcleo no manda NINGÚN mensaje del turno
# (transcript/reply_text/cast_start/done) durante este tiempo, se asume error mudo
# y se vuelve a reposo. Es un reloj de inactividad (se rearma con cada mensaje), no
# un tope absoluto — así una respuesta lenta pero viva (STT medium + síntesis del
# WAV completo) no se aborta a mitad.
_THINKING_TIMEOUT_SECONDS = 30.0
# Margen extra sobre la duración del clip antes de dar por acabada la locución.
_SPEAKING_MARGIN_SECONDS = 0.6
# Tiempo de un bloque del micro (80 ms) — para razonar sobre silencios/deadlines.
_BLOCK_SECONDS = BLOCK_SIZE / SAMPLE_RATE
# Pre-roll: bloques recientes que se anteponen a la utterance para no cortar el
# inicio de la frase (el usuario suele empezar a hablar antes de que el wake/VAD
# dispare). ~0,5 s de audio.
_PREROLL_BLOCKS = round(0.5 / _BLOCK_SECONDS)

# Estados de la máquina (strings por legibilidad en los logs).
_WAKE_LISTEN = "WAKE_LISTEN"
_CAPTURE = "CAPTURE"
_THINKING = "THINKING"
_SPEAKING = "SPEAKING"
_FOLLOWUP = "FOLLOWUP"


# =============================================================================
# Modo test-once (Fase 2) — intacto
# =============================================================================
async def _drain_response(client: VoiceClient) -> bool:
    """Consume los mensajes del servidor hasta ``done`` y los loguea.

    Args:
        client: Cliente WS ya conectado y con la utterance enviada.

    Returns:
        True si llegó ``cast_start`` (la respuesta se casteó al Salón).
    """
    cast_started = False
    async for msg in client.messages():
        mtype = msg.get("type")
        if mtype == "transcript":
            print(f"[ws] ← transcript: {msg.get('text')!r}", flush=True)
        elif mtype == "reply_text":
            print(f"[ws] ← reply_text: {msg.get('text')!r}", flush=True)
        elif mtype == "cast_start":
            cast_started = True
            print(
                f"[ws] ← cast_start: {msg.get('duration_s'):.1f}s "
                f"(escucha la respuesta en el Salón)",
                flush=True,
            )
        elif mtype == "done":
            print(f"[ws] ← done (modelo {msg.get('model')})", flush=True)
            break
        elif mtype in ("error", "cancelled", "interrupted"):
            print(f"[ws] ← {mtype}: {msg}", flush=True)
            break
        else:
            print(f"[ws] ← {mtype}: {msg}", flush=True)
    return cast_started


async def _test_once() -> int:
    """Graba una vez del micro y valida el pipe completo micro→STT→Cast.

    Returns:
        0 si la respuesta se casteó (llegó ``cast_start``); 1 en caso contrario.
    """
    print("== Satélite Jarvis · modo test-once ==", flush=True)
    with Microphone() as mic:
        async with VoiceClient(_WS_URL, _CAST_DEVICE) as client:
            await client.hello()
            await client.wake()
            print("[sat] esperando el DING en el Salón…", flush=True)
            await asyncio.sleep(_TEST_WARMUP_SECONDS)
            print(f"[sat] grabando {_TEST_RECORD_SECONDS:.0f}s — HABLA AHORA", flush=True)
            pcm = await asyncio.to_thread(mic.record, _TEST_RECORD_SECONDS)

            await client.send_utterance(pcm)
            cast_started = await _drain_response(client)

    if cast_started:
        print("\n✅ PASA: llegó cast_start. Confirma por oído la respuesta.", flush=True)
        return 0
    print("\n❌ FALLA: no llegó cast_start.", flush=True)
    return 1


# =============================================================================
# Modo run (Fase 3) — máquina de estados con wake word
# =============================================================================
def _pcm(block: np.ndarray) -> bytes:
    """Convierte un bloque Int16 mono a bytes PCM Int16 LE."""
    return block.astype("<i2").tobytes()


def _boost(block: np.ndarray) -> np.ndarray:
    """Amplifica un bloque para el wake/VAD (con clip a rango Int16).

    Solo para los detectores: sube la señal floja de la PS Eye para que wake y
    VAD reaccionen. El clip evita el wrap-around. NO se usa en el audio que se
    envía al STT (ése va crudo, sin clip, para no ensuciar la transcripción).

    Args:
        block: Bloque Int16 mono crudo del micro.

    Returns:
        Bloque Int16 amplificado ``×_DETECT_GAIN`` y recortado a [-32768, 32767].
    """
    if _DETECT_GAIN == 1.0:
        return block
    return np.clip(block.astype(np.int32) * _DETECT_GAIN, -32768, 32767).astype("<i2")


class Satellite:
    """Máquina de estados del satélite de voz ambiente (modo ``run``).

    Ver el diagrama en ``docs/plan/hito-35-4-voz-ambiente.md`` §Arquitectura.
    Mantiene una sola conexión WS de larga vida; si se cae, el bucle exterior
    (:meth:`run`) reconstruye el cliente y vuelve a WAKE_LISTEN.
    """

    def __init__(self, mic: Microphone) -> None:
        self._mic = mic
        self._audio_q: asyncio.Queue[np.ndarray] = asyncio.Queue()
        # Pre-roll: cola circular de los últimos bloques capturados (en cualquier
        # estado), para anteponerlos a la utterance y no cortar el inicio.
        self._preroll: list[bytes] = []
        # Import perezoso de la pila ONNX (pesada): solo en modo run.
        from src.satellite.vad import SpeechGate
        from src.satellite.wake import WakeWord

        self._wake = WakeWord()
        self._gate = SpeechGate()
        # Estado compartido con _recv_loop (single-thread asyncio, sin locks).
        # `_turn_done` se activa al llegar `done`/`cancelled` (fin del turno);
        # `_cast_seen` dice si hubo respuesta hablada (cast_start) en el turno.
        self._turn_done = asyncio.Event()
        self._cast_seen = False
        # Despedida ("gracias"): session_reset llegado con el turno YA terminado →
        # cerrar la sesión (volver a WAKE_LISTEN en vez de seguir en FOLLOWUP).
        self._farewell = False
        self._cast_duration = 0.0
        self._cast_at = 0.0  # monotonic del cast_start (para el deadline de SPEAKING)
        # Último mensaje del turno recibido (transcript/reply_text/cast_start):
        # THINKING mide INACTIVIDAD contra esto, no un tope absoluto, para no
        # rendirse con respuestas lentas (STT medium + Claude + síntesis del WAV).
        self._last_activity = 0.0
        self._conn_closed = asyncio.Event()  # el recv_loop terminó (WS caído)

    # --- captura ---------------------------------------------------------------
    async def _next_block(self) -> np.ndarray:
        """Espera el próximo bloque del micro y actualiza el pre-roll."""
        block = await self._audio_q.get()
        self._preroll.append(_pcm(block))
        if len(self._preroll) > _PREROLL_BLOCKS:
            self._preroll.pop(0)
        return block

    # --- lectura del WS --------------------------------------------------------
    async def _recv_loop(self, client: VoiceClient) -> None:
        """Consume los mensajes del servidor y actualiza el estado compartido."""
        try:
            async for msg in client.messages():
                mtype = msg.get("type")
                # Cualquier mensaje de progreso del turno cuenta como "vida":
                # rearma el reloj de inactividad de THINKING (evita rendirse con
                # respuestas lentas). No incluye done/cancelled: ésos cierran.
                if mtype in ("transcript", "reply_text", "cast_start"):
                    self._last_activity = time.monotonic()
                if mtype == "transcript":
                    print(f"[ws] ← transcript: {msg.get('text')!r}", flush=True)
                elif mtype == "reply_text":
                    print(f"[ws] ← reply_text: {msg.get('text')!r}", flush=True)
                elif mtype == "cast_start":
                    # Hay respuesta hablada: guardamos duración/instante para el
                    # deadline de SPEAKING. El `done` que sigue cierra el turno.
                    self._cast_seen = True
                    self._cast_duration = float(msg.get("duration_s") or 0.0)
                    self._cast_at = time.monotonic()
                    print(f"[ws] ← cast_start: {self._cast_duration:.1f}s", flush=True)
                elif mtype == "done":
                    # Fin del turno. Si no hubo cast_start (transcripción vacía),
                    # THINKING lo verá y volverá a reposo sin colgarse.
                    self._turn_done.set()
                    print(f"[ws] ← done (modelo {msg.get('model')})", flush=True)
                elif mtype == "cancelled":
                    # "Cancela": el turno se descarta, no hay nada que hablar.
                    self._cast_seen = False
                    self._turn_done.set()
                    print("[ws] ← cancelled", flush=True)
                elif mtype == "session_reset":
                    # Dos casos: (a) reciclado idle→Haiku a mitad de turno (llega
                    # ANTES del done → turno en curso, no tocar); (b) despedida
                    # "gracias" (llega DESPUÉS del done → cerrar sesión). Se
                    # distinguen por si el turno ya terminó (`_turn_done`).
                    if self._turn_done.is_set():
                        self._farewell = True
                        print("[ws] ← session_reset (despedida → cerrar sesión)", flush=True)
                    else:
                        print("[ws] ← session_reset (reciclado del núcleo)", flush=True)
                elif mtype == "interrupted":
                    # Provocado por nuestro propio barge-in; ya lo gestionamos local.
                    print("[ws] ← interrupted", flush=True)
                elif mtype == "error":
                    print(f"[ws] ← error: {msg.get('detail')}", flush=True)
        except websockets.exceptions.WebSocketException as exc:
            print(f"[ws] recv_loop cerrado: {exc}", flush=True)
        finally:
            self._conn_closed.set()
            self._turn_done.set()  # desbloquea cualquier espera pendiente

    # --- estados ---------------------------------------------------------------
    async def _wake_listen(self, client: VoiceClient) -> str:
        """WAKE_LISTEN: solo wake word; al disparar, ding y a CAPTURE."""
        self._wake.reset()
        # Diagnóstico temporal (JARVIS_SAT_WAKE_DEBUG): heartbeat con el score
        # máximo de cada ventana + tamaño de la cola de audio (detecta si nos
        # quedamos atrás y se acumula backlog) para tunear el umbral.
        max_score = 0.0
        n = 0
        while True:
            self._raise_if_closed()
            block = await self._next_block()
            score = self._wake.process(_boost(block))
            if _WAKE_DEBUG:
                max_score = max(max_score, score)
                n += 1
                if score > 0.1:
                    print(f"[wake] score={score:.3f}", flush=True)
                if n >= 25:  # ~2 s
                    print(
                        f"[wake] heartbeat: max_score={max_score:.3f} "
                        f"cola_audio={self._audio_q.qsize()}",
                        flush=True,
                    )
                    max_score = 0.0
                    n = 0
            if self._wake.triggered(score):
                print(f"[sat] wake! score={score:.3f}", flush=True)
                await client.wake()  # ding de confirmación en el Salón
                return _CAPTURE

    async def _capture(self, client: VoiceClient, use_preroll: bool = False) -> str:
        """CAPTURE: transmite la utterance en vivo; la VAD la cierra por silencio.

        Termina por (a) silencio sostenido tras haber detectado voz, (b) tope
        máximo, o (c) sin voz durante el arranque. Durante el **warmup** inicial se
        transmite audio pero no se evalúa el cierre (deja sonar el ding sin armar
        el fin por silencio).

        Args:
            use_preroll: Antepone el pre-roll (~0,5 s previos) a la utterance. Solo
                útil cuando el disparo lo dio la propia voz (FOLLOWUP): en el wake y
                el barge-in el pre-roll es la cola de "Jarvis" y ensucia sin aportar.
        """
        # Nuevo turno: limpiamos el estado de respuesta para no leer el del turno
        # anterior en THINKING.
        self._cast_seen = False
        self._farewell = False
        self._turn_done.clear()
        self._gate.reset_states()
        await client.utterance_start()
        if use_preroll:
            for frame in list(self._preroll):  # no cortar el inicio de la frase
                await client.send_frame(frame)

        speech_seen = False
        start = time.monotonic()
        warmup_until = start + _CAPTURE_WARMUP_SECONDS
        last_speech = warmup_until  # el conteo de silencio no arranca en el warmup
        while True:
            block = await self._next_block()
            await client.send_frame(_pcm(block))  # al STT: audio CRUDO (sin gain)
            now = time.monotonic()
            is_speech = self._gate.is_speech(_boost(block))  # VAD: con gain
            if now < warmup_until:
                continue  # transmite, pero no evalúa cierre (ding + arranque)
            if is_speech:
                speech_seen = True
                last_speech = now
            if speech_seen and (now - last_speech) >= _SILENCE_SECONDS:
                break
            if not speech_seen and (now - warmup_until) >= _NO_SPEECH_ONSET_SECONDS:
                print("[sat] captura sin voz (falso positivo del wake)", flush=True)
                break
            if (now - start) >= _MAX_UTTERANCE_SECONDS:
                print("[sat] captura al tope máximo", flush=True)
                break

        await client.utterance_end()
        secs = time.monotonic() - start
        print(f"[sat] utterance cerrada ({secs:.1f}s, voz={speech_seen})", flush=True)
        return _THINKING

    async def _think(self) -> str:
        """THINKING: espera el fin del turno (``done``) drenando audio.

        Si hubo ``cast_start`` → SPEAKING; si el turno acabó sin respuesta hablada
        (transcripción vacía / cancelado / timeout de seguridad) → WAKE_LISTEN, sin
        colgarse.
        """
        self._last_activity = time.monotonic()  # arranca el reloj de inactividad
        while not self._turn_done.is_set():
            self._raise_if_closed()
            if time.monotonic() - self._last_activity >= _THINKING_TIMEOUT_SECONDS:
                print("[sat] THINKING: sin actividad del núcleo → WAKE_LISTEN", flush=True)
                return _WAKE_LISTEN
            # Drenamos el micro para que la cola no crezca mientras Jarvis piensa.
            try:
                await asyncio.wait_for(self._next_block(), timeout=0.2)
            except asyncio.TimeoutError:
                pass
        if self._cast_seen:
            return _SPEAKING
        print("[sat] turno sin respuesta hablada (vacío/cancelado) → WAKE_LISTEN", flush=True)
        return _WAKE_LISTEN

    async def _speak(self, client: VoiceClient) -> str:
        """SPEAKING: Jarvis habla por Cast; solo escuchamos wake (barge-in, D5)."""
        self._wake.reset()
        deadline = self._cast_at + self._cast_duration + _SPEAKING_MARGIN_SECONDS
        while time.monotonic() < deadline:
            self._raise_if_closed()
            try:
                block = await asyncio.wait_for(self._next_block(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            score = self._wake.process(_boost(block))
            if self._wake.triggered(score, speaking=True):
                print(f"[sat] barge-in! score={score:.3f}", flush=True)
                await client.stop()  # corta el WAV que suena en el Home
                return _CAPTURE
        if self._farewell:  # "gracias": la despedida ya sonó → cerrar sesión
            print("[sat] despedida → WAKE_LISTEN", flush=True)
            return _WAKE_LISTEN
        return _FOLLOWUP

    async def _followup(self, client: VoiceClient) -> str:
        """FOLLOWUP: ventana multi-turno; VAD directo (sin wake) reabre CAPTURE."""
        self._gate.reset_states()
        deadline = time.monotonic() + _FOLLOWUP_SECONDS
        while time.monotonic() < deadline:
            self._raise_if_closed()
            if self._farewell:  # despedida que llegó ya en FOLLOWUP → cerrar
                print("[sat] despedida (en followup) → WAKE_LISTEN", flush=True)
                return _WAKE_LISTEN
            block = await self._next_block()
            if self._gate.is_speech(block):
                print("[sat] followup: voz detectada → CAPTURE", flush=True)
                return _CAPTURE
        print("[sat] followup agotado → WAKE_LISTEN", flush=True)
        return _WAKE_LISTEN

    # --- bucle principal -------------------------------------------------------
    def _raise_if_closed(self) -> None:
        """Aborta el ciclo actual si el recv_loop detectó la conexión caída."""
        if self._conn_closed.is_set():
            raise ConnectionError("conexión WS cerrada")

    async def _fsm(self, client: VoiceClient) -> None:
        """Ejecuta la máquina de estados hasta que se caiga la conexión."""
        state = _WAKE_LISTEN
        prev = _WAKE_LISTEN
        while True:
            if state == _WAKE_LISTEN:
                nxt = await self._wake_listen(client)
            elif state == _CAPTURE:
                # Solo se antepone el pre-roll si la captura la abrió la voz del
                # seguimiento (no el wake ni el barge-in, donde es cola de "Jarvis").
                nxt = await self._capture(client, use_preroll=(prev == _FOLLOWUP))
            elif state == _THINKING:
                nxt = await self._think()
            elif state == _SPEAKING:
                nxt = await self._speak(client)
            else:  # _FOLLOWUP
                nxt = await self._followup(client)
            if nxt != state:
                print(f"[sat] {state} → {nxt}", flush=True)
            prev = state
            state = nxt

    async def run(self) -> None:
        """Bucle de conexión: mantiene la sesión y reconecta si el WS se cae."""
        loop = asyncio.get_running_loop()
        self._mic.bind_async(loop, self._audio_q)
        print(
            f"[sat] modo run — pre-roll {_PREROLL_BLOCKS} bloques "
            f"(~{_PREROLL_BLOCKS * _BLOCK_SECONDS:.1f}s), silencio {_SILENCE_SECONDS}s, "
            f"followup {_FOLLOWUP_SECONDS}s",
            flush=True,
        )
        while True:
            self._conn_closed.clear()
            try:
                async with VoiceClient(_WS_URL, _CAST_DEVICE) as client:
                    await client.hello()
                    recv_task = asyncio.create_task(self._recv_loop(client))
                    try:
                        await self._fsm(client)
                    finally:
                        recv_task.cancel()
                        try:
                            await recv_task
                        except asyncio.CancelledError:
                            pass
            except (ConnectionError, OSError, websockets.exceptions.WebSocketException) as exc:
                print(f"[sat] conexión perdida ({exc}); reconecto en 1 s…", flush=True)
                # Vaciamos la cola para no arrastrar audio viejo tras reconectar.
                while not self._audio_q.empty():
                    self._audio_q.get_nowait()
                await asyncio.sleep(1.0)


async def _run() -> int:
    """Arranca el satélite en modo servicio permanente (máquina de estados)."""
    print("== Satélite Jarvis · modo run (wake word) ==", flush=True)
    with Microphone() as mic:
        await Satellite(mic).run()
    return 0


def main() -> int:
    if _MODE == "test-once":
        return asyncio.run(_test_once())
    if _MODE == "run":
        return asyncio.run(_run())
    print(
        f"[sat] JARVIS_SAT_MODE={_MODE!r} desconocido (usa 'run' o 'test-once').",
        file=sys.stderr,
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
