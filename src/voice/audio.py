"""Captura de micrófono (push-to-talk) y reproducción de audio.

Prototipo Windows del Hito 3. Usa sounddevice (PortAudio) para grabar a 16 kHz
mono (lo que espera faster-whisper) y para reproducir el audio que genera Piper.

El push-to-talk es por TOGGLE de tecla: una pulsación empieza a grabar y la
siguiente la para. En Windows se usa `msvcrt` para poder esperar la tecla con
*timeout* (necesario para cerrar la sesión por inactividad). En otros sistemas
se cae a `input()` bloqueante (sin timeout); al portar al NUC (Hito 3.5) esta
parte se sustituirá por VAD/wake word, así que la dependencia de msvcrt es
aceptable para el prototipo.
"""

from __future__ import annotations

import os
import queue
import sys
import time

import numpy as np
import sounddevice as sd

# faster-whisper trabaja internamente a 16 kHz mono.
SAMPLE_RATE = 16000
CHANNELS = 1

# Dispositivo de entrada. None = el de Windows por defecto. Se puede fijar con
# JARVIS_INPUT_DEVICE (índice numérico de `sd.query_devices()` o subcadena del
# nombre). Útil si el micro por defecto no es el deseado.
_DEV_ENV = os.environ.get("JARVIS_INPUT_DEVICE")
if _DEV_ENV is None or _DEV_ENV == "":
    INPUT_DEVICE: int | str | None = None
elif _DEV_ENV.isdigit():
    INPUT_DEVICE = int(_DEV_ENV)
else:
    INPUT_DEVICE = _DEV_ENV

# Por debajo de este pico el audio se considera silencio (no se normaliza, para
# no amplificar el ruido de fondo a tope).
_NOISE_FLOOR = 0.005
# Pico objetivo al normalizar audio bajo (habla lejos del micro).
_TARGET_PEAK = 0.8

try:
    import msvcrt  # Solo Windows
    _HAS_MSVCRT = True
except ImportError:  # pragma: no cover - rama no-Windows
    _HAS_MSVCRT = False


def wait_for_key(timeout: float | None = None) -> bool:
    """Espera a que el usuario pulse una tecla.

    Args:
        timeout: Segundos máximos de espera. ``None`` espera indefinidamente.

    Returns:
        ``True`` si se pulsó una tecla; ``False`` si venció el timeout.
        En sistemas sin ``msvcrt`` (no-Windows) siempre devuelve ``True`` tras
        un ENTER, ignorando el timeout.
    """
    if not _HAS_MSVCRT:  # pragma: no cover - rama no-Windows
        input()
        return True

    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if msvcrt.kbhit():
            msvcrt.getch()  # consume la tecla que dispara la acción
            # Vacía el resto del buffer (p. ej. el \n de un ENTER, o teclas
            # extendidas que llegan en dos bytes) para no contaminar la
            # siguiente espera.
            while msvcrt.kbhit():
                msvcrt.getch()
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(0.03)


def _input_samplerate() -> int:
    """Sample rate nativo del dispositivo de entrada en uso."""
    info = sd.query_devices(INPUT_DEVICE, kind="input")
    return int(info["default_samplerate"])


def _resample(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Remuestrea audio mono por interpolación lineal."""
    if src_sr == dst_sr or audio.size == 0:
        return audio.astype(np.float32)
    n_dst = int(round(audio.size * dst_sr / src_sr))
    x_src = np.linspace(0, 1, audio.size, endpoint=False)
    x_dst = np.linspace(0, 1, n_dst, endpoint=False)
    return np.interp(x_dst, x_src, audio).astype(np.float32)


def _normalize(audio: np.ndarray) -> np.ndarray:
    """Sube el volumen del audio bajo (habla lejos del micro) sin reventar.

    Solo actúa si hay señal por encima del ruido de fondo; así no amplifica el
    silencio. Esto evita tener que hablar pegado al micrófono.
    """
    if audio.size == 0:
        return audio
    peak = float(np.abs(audio).max())
    if _NOISE_FLOOR < peak < _TARGET_PEAK:
        audio = audio * (_TARGET_PEAK / peak)
    return audio


def record_push_to_talk(target_sr: int = SAMPLE_RATE) -> np.ndarray:
    """Graba desde el micrófono hasta que se pulse una tecla para parar.

    Graba al sample rate NATIVO del dispositivo (más fiable que forzar 16 kHz),
    luego remuestrea al objetivo y normaliza el volumen. Asume que ya se avisó
    al usuario de que empiece a hablar (la pulsación de inicio la gestiona quien
    llama).

    Args:
        target_sr: Frecuencia de muestreo de salida (16 kHz para faster-whisper).

    Returns:
        Array float32 mono a ``target_sr`` (vacío si no se capturó nada).
    """
    native_sr = _input_samplerate()
    chunks: "queue.Queue[np.ndarray]" = queue.Queue()

    def _callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        chunks.put(indata.copy())

    with sd.InputStream(
        samplerate=native_sr,
        channels=CHANNELS,
        dtype="float32",
        device=INPUT_DEVICE,
        callback=_callback,
    ):
        wait_for_key()  # bloquea hasta que el usuario pulsa para parar

    frames: list[np.ndarray] = []
    while not chunks.empty():
        frames.append(chunks.get())
    if not frames:
        return np.zeros(0, dtype=np.float32)

    audio = np.concatenate(frames, axis=0).reshape(-1)
    audio = _resample(audio, native_sr, target_sr)
    return _normalize(audio)


def play(samples: np.ndarray, samplerate: int) -> None:
    """Reproduce audio por el altavoz por defecto (bloqueante).

    Args:
        samples: Array float32 mono en rango [-1, 1].
        samplerate: Frecuencia de muestreo del audio.
    """
    if samples.size == 0:
        return
    sd.play(samples, samplerate)
    sd.wait()
