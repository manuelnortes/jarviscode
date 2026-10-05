"""Almacén efímero de clips de audio para servirlos por HTTP al Google Cast.

El modo de salida ``cast`` del WS de voz (Hito 3.5) no manda el audio al cliente:
lo publica aquí como un WAV y castea al Home Mini la URL ``/tts/<id>.wav``. El
altavoz descarga esa URL una vez y reproduce; el clip no se reutiliza, así que se
purga a los ``_TTL_SECONDS`` para no acumular respuestas viejas en memoria.

Vive en su propio módulo (no en ``server.py``) porque ``server.py`` ya importa
``voice_ws.py``; si el almacén estuviera allí, ``voice_ws`` no podría publicar sin
un import circular. Ambos importan de aquí.

El **ding** de confirmación del wake word es la excepción: se genera una vez al
importar y se guarda permanente bajo el id ``"ding"`` (se reusa en cada wake).
"""

from __future__ import annotations

import io
import math
import os
import socket
import time
import uuid
import wave

import numpy as np

# Segundos que un clip vive en memoria antes de purgarse. Solo para liberar RAM:
# el Cast pide la URL una vez (<1 s) y no vuelve. 120 s es margen de sobra.
_TTL_SECONDS = 120.0

# id fijo del ding: permanente, no expira (se reusa en cada wake).
DING_ID = "ding"

# clip_id -> (wav_bytes, expiry_monotonic | None). expiry None = permanente (ding).
_clips: dict[str, tuple[bytes, float | None]] = {}


def _pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Envuelve PCM Int16 LE mono en un contenedor WAV en memoria.

    Args:
        pcm: Bytes de PCM Int16 little-endian, mono.
        sample_rate: Frecuencia de muestreo del PCM (Hz).

    Returns:
        Bytes de un fichero WAV (RIFF) listo para servir como ``audio/wav``.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # Int16
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def _purge_expired() -> None:
    """Elimina los clips cuya expiración ya pasó (purga perezosa, sin timers)."""
    now = time.monotonic()
    for clip_id in [
        cid for cid, (_, exp) in _clips.items() if exp is not None and exp <= now
    ]:
        _clips.pop(clip_id, None)


def publish_pcm16(pcm: bytes, sample_rate: int) -> str:
    """Publica un clip de PCM como WAV y devuelve su id para construir la URL.

    Args:
        pcm: PCM Int16 LE mono (respuesta completa ya concatenada).
        sample_rate: Frecuencia de muestreo del PCM (Hz).

    Returns:
        ``clip_id`` con el que formar ``<base>/tts/<clip_id>.wav``.
    """
    _purge_expired()
    clip_id = uuid.uuid4().hex
    _clips[clip_id] = (_pcm16_to_wav(pcm, sample_rate), time.monotonic() + _TTL_SECONDS)
    return clip_id


def get(clip_id: str) -> bytes | None:
    """Devuelve los bytes WAV de un clip, o None si no existe o ya expiró.

    Args:
        clip_id: Identificador devuelto por ``publish_pcm16`` (o ``DING_ID``).

    Returns:
        Bytes del WAV, o None.
    """
    _purge_expired()
    entry = _clips.get(clip_id)
    return entry[0] if entry is not None else None


def _make_ding(sample_rate: int = 22050) -> bytes:
    """Genera el beep de confirmación del wake word ("habla ahora").

    Doble tono ascendente (G5 → C6) más fuerte y largo que un pitido simple: a
    distancia de salón y con el volumen del Home un beep corto y flojo pasaba
    desapercibido, así que un patrón "bi-bip" es mucho más fácil de reconocer.

    Args:
        sample_rate: Frecuencia de muestreo del WAV generado.

    Returns:
        Bytes WAV del ding.
    """

    def _beep(freq: float, duration: float, amp: float) -> np.ndarray:
        n = int(sample_rate * duration)
        t = np.arange(n, dtype=np.float32) / sample_rate
        tone = np.sin(2 * math.pi * freq * t)
        # Envolvente attack + release corta para que no chasquee al empezar/cortar.
        env = np.ones(n, dtype=np.float32)
        edge = max(1, int(sample_rate * 0.01))  # 10 ms de rampa a cada lado
        env[:edge] = np.linspace(0.0, 1.0, edge, dtype=np.float32)
        env[-edge:] = np.linspace(1.0, 0.0, edge, dtype=np.float32)
        return tone * env * amp

    gap = np.zeros(int(sample_rate * 0.05), dtype=np.float32)  # 50 ms de silencio
    signal = np.concatenate(
        [_beep(784.0, 0.14, 0.9), gap, _beep(1047.0, 0.18, 0.9)]
    )
    samples = (signal * 32767.0).astype("<i2").tobytes()
    return _pcm16_to_wav(samples, sample_rate)


# Ding permanente (expiry None): generado una vez al importar el módulo.
_clips[DING_ID] = (_make_ding(), None)


def get_base_url() -> str:
    """URL base alcanzable por LAN desde la que el Cast descargará los clips.

    Prioriza ``JARVIS_TTS_BASE_URL`` (p. ej. ``http://192.168.1.10:8200``). Si no
    está, autodetecta la IP local abriendo un socket UDP hacia 8.8.8.8 (no envía
    nada; solo fuerza a la pila a elegir la interfaz de salida) y usa el puerto de
    ``JARVIS_PORT``. El Home Mini no resuelve ``127.0.0.1``, por eso necesitamos la
    IP de la LAN.

    Returns:
        URL base sin barra final, p. ej. ``http://192.168.1.10:8200``.
    """
    if (base := os.getenv("JARVIS_TTS_BASE_URL")):
        return base.rstrip("/")
    port = os.getenv("JARVIS_PORT", "8200")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    finally:
        sock.close()
    return f"http://{ip}:{port}"
