"""Captura de audio de la PlayStation Eye para el satélite.

La PS Eye es un array de 4 micrófonos (chip OV534) que ALSA expone como una
tarjeta de captura de **4 canales** a 16/32/48 kHz. Nos quedamos con el **canal 0**
y lo reducimos a mono, que es lo que espera el contrato ``/voice`` (16 kHz mono
Int16 LE). El beamforming del array no se usa (fuera de alcance).

Diseño:
    * ``blocksize=1280`` muestras = **80 ms** por bloque a 16 kHz, la granularidad
      que openWakeWord consumirá en la Fase 3 (ahí se reutiliza este mismo stream).
    * Selección de dispositivo por substring (env ``JARVIS_SAT_INPUT_DEVICE``,
      default ``CameraB``) para no depender del índice de PortAudio, que baila.
    * Plan B si PortAudio no deja abrir 4 canales: reintento pidiendo 1 canal
      (algunos backends ALSA hacen el downmix ellos solos vía ``plughw``).
"""

from __future__ import annotations

import asyncio
import os
import queue
from collections.abc import Iterator

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000  # el contrato /voice exige 16 kHz mono
BLOCK_SIZE = 1280  # 80 ms a 16 kHz (granularidad de openWakeWord en la Fase 3)
# PortAudio expone la PS Eye como "USB Camera-B4.09.24.1: Audio (hw:0,0)"
# (distinto del "CameraB409241" que muestra `arecord`); "Camera" casa seguro.
_DEVICE_SUBSTRING = os.getenv("JARVIS_SAT_INPUT_DEVICE", "Camera")
_NATIVE_CHANNELS = 4  # canales nativos de la PS Eye


def _find_input_device(substring: str) -> int:
    """Devuelve el índice PortAudio del primer dispositivo de entrada que casa.

    Args:
        substring: Fragmento (case-insensitive) del nombre del dispositivo, p. ej.
            ``CameraB`` para la PS Eye (``CameraB409241``).

    Returns:
        Índice del dispositivo en la tabla de PortAudio.

    Raises:
        RuntimeError: Si ningún dispositivo de entrada casa con ``substring``.
    """
    needle = substring.lower()
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and needle in dev["name"].lower():
            return idx
    # Volcamos los dispositivos vistos para depurar el passthrough desde el log.
    available = [
        f"[{i}] {d['name']} (in={d['max_input_channels']})"
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]
    raise RuntimeError(
        f"No hay dispositivo de entrada que contenga {substring!r}. "
        f"Entradas disponibles: {available or '(ninguna)'}"
    )


class Microphone:
    """Stream de captura de la PS Eye reducido a mono 16 kHz.

    Uso como context manager::

        with Microphone() as mic:
            pcm = mic.record(seconds=4.0)          # bloque puntual (Fase 2)
            for block in mic.blocks():             # streaming (Fase 3)
                ...
    """

    def __init__(self, device_substring: str = _DEVICE_SUBSTRING) -> None:
        self._device = _find_input_device(device_substring)
        self._channels = _NATIVE_CHANNELS
        self._queue: queue.Queue[np.ndarray] = queue.Queue()
        # Entrega asíncrona opcional (modo `run`, FSM): si se enlaza con
        # `bind_async`, el callback empuja los bloques a una asyncio.Queue vía
        # call_soon_threadsafe en vez de a la cola síncrona.
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._async_queue: asyncio.Queue[np.ndarray] | None = None
        self._stream: sd.RawInputStream | None = None
        info = sd.query_devices(self._device)
        print(
            f"[mic] dispositivo [{self._device}] {info['name']!r} "
            f"({self._channels}ch @ {SAMPLE_RATE} Hz, bloque {BLOCK_SIZE}, crudo)",
            flush=True,
        )

    def _callback(self, indata: bytes, frames: int, time_info, status) -> None:  # noqa: ANN001
        """Callback de PortAudio: extrae el canal 0 y lo encola como Int16 mono."""
        if status:
            print(f"[mic] status: {status}", flush=True)
        # indata es un buffer intercalado de `frames` x `channels` Int16.
        # Se entrega CRUDO (sin ganancia): el STT necesita audio sin clip. La
        # amplificación para wake/VAD se aplica aguas abajo en la FSM (main.py),
        # solo en los detectores, no en el frame que se streamea al núcleo.
        interleaved = np.frombuffer(indata, dtype="<i2")
        mono = interleaved[:: self._channels].copy()  # canal 0 de cada frame
        if self._async_loop is not None and self._async_queue is not None:
            # El callback de sounddevice corre en un hilo propio; hay que cruzar al
            # loop de asyncio de forma segura para encolar el bloque.
            self._async_loop.call_soon_threadsafe(self._async_queue.put_nowait, mono)
        else:
            self._queue.put(mono)

    def __enter__(self) -> "Microphone":
        try:
            self._open(channels=self._channels)
        except sd.PortAudioError as err:
            # Plan B: algunos backends ALSA no abren 4ch; probamos 1 canal (downmix
            # del propio driver vía plughw). Ajustamos el stride del callback a 1.
            print(f"[mic] {self._channels}ch falló ({err}); reintento con 1 canal", flush=True)
            self._channels = 1
            self._open(channels=1)
        return self

    def _open(self, channels: int) -> None:
        self._channels = channels
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            device=self._device,
            channels=channels,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def bind_async(
        self, loop: asyncio.AbstractEventLoop, aqueue: asyncio.Queue[np.ndarray]
    ) -> None:
        """Redirige los bloques capturados a una ``asyncio.Queue`` (modo ``run``).

        A partir de la llamada, el callback de captura entrega cada bloque a
        ``aqueue`` en el loop ``loop`` (thread-safe), en vez de a la cola síncrona
        que usan :meth:`record` / :meth:`blocks`.

        Args:
            loop: Event loop de asyncio donde vive la máquina de estados.
            aqueue: Cola asíncrona (sin límite) que recibe los bloques Int16 mono.
        """
        self._async_loop = loop
        self._async_queue = aqueue

    def blocks(self) -> Iterator[np.ndarray]:
        """Itera indefinidamente bloques Int16 mono (``BLOCK_SIZE`` muestras)."""
        while True:
            yield self._queue.get()

    def record(self, seconds: float) -> bytes:
        """Graba ``seconds`` de audio y lo devuelve como PCM Int16 LE mono.

        Vacía primero lo acumulado en la cola para no arrastrar audio viejo del
        arranque del stream.

        Args:
            seconds: Duración a grabar.

        Returns:
            Bytes PCM Int16 LE, 16 kHz, mono.
        """
        while not self._queue.empty():  # descartar backlog previo
            self._queue.get_nowait()
        needed = int(seconds * SAMPLE_RATE)
        collected: list[np.ndarray] = []
        total = 0
        for block in self.blocks():
            collected.append(block)
            total += block.size
            if total >= needed:
                break
        pcm = np.concatenate(collected)[:needed]
        return pcm.astype("<i2").tobytes()
