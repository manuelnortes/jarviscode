"""Detección de voz (VAD) para delimitar la utterance, sobre Silero VAD.

Usa el ``silero_vad.onnx`` que **ya empaqueta openWakeWord** (``openwakeword.vad``):
corre en onnxruntime, sin dependencia extra ni nada que compilar (por eso se
descartó ``pysilero-vad``, que tira de CMake).

Papel: sustituir al botón "pulsar para hablar" de la web. En el estado CAPTURE de
la máquina de estados, el satélite alimenta cada bloque del micro aquí y usa
:meth:`is_speech` para saber cuándo la frase ha terminado (silencio sostenido).

Detalle crítico de tamaños de frame:
    * El micro entrega bloques de **1280 muestras** (80 ms).
    * Silero espera longitudes **múltiplo de 480 muestras** (30 ms). 1280 NO lo es,
      así que :class:`SpeechGate` mantiene un buffer y procesa el mayor múltiplo de
      480 disponible, guardando el remanente (<480) para el siguiente bloque.

El **pre-roll** (no cortar el inicio de la frase) y el conteo de silencio para
cerrar la utterance viven en la máquina de estados de ``main.py``, que es quien
ensambla y transmite el audio; aquí solo damos el primitivo "¿hay voz en este
bloque?".
"""

from __future__ import annotations

import os

import numpy as np

_VAD_FRAME = 480  # muestras (30 ms a 16 kHz) — unidad que exige Silero
_THRESHOLD = float(os.getenv("JARVIS_SAT_VAD_THRESHOLD", "0.5"))


class SpeechGate:
    """Envoltorio de Silero VAD que decide "¿hay voz?" por bloque de 1280.

    Uso::

        gate = SpeechGate()
        gate.reset_states()                    # al abrir cada utterance
        for block in mic.blocks():
            if gate.is_speech(block):
                ...
    """

    def __init__(self) -> None:
        # Import perezoso (onnxruntime pesa) — el modo test-once no lo carga.
        from openwakeword.vad import VAD

        self._vad = VAD()
        # Remanente de muestras (<480) que no completó un frame en el bloque previo.
        self._buf = np.empty(0, dtype=np.int16)
        print("[vad] Silero VAD (openWakeWord) cargado", flush=True)

    def reset_states(self) -> None:
        """Reinicia el estado LSTM del VAD y el buffer de re-troceo.

        Se llama al **abrir cada utterance** para no arrastrar el contexto de la
        frase anterior.
        """
        self._vad.reset_states()
        self._buf = np.empty(0, dtype=np.int16)

    def is_speech(self, block: np.ndarray) -> bool:
        """Indica si el bloque (1280 muestras Int16) contiene voz.

        Acumula el bloque en el buffer, procesa el mayor múltiplo de 480 muestras
        disponible con Silero y guarda el remanente para la próxima llamada.

        Args:
            block: Bloque de audio mono Int16 a 16 kHz (típicamente 1280 muestras).

        Returns:
            ``True`` si el score medio de Silero supera el umbral configurado.
            Si aún no hay un frame completo de 480 muestras, devuelve ``False``.
        """
        self._buf = np.concatenate([self._buf, block])
        n = (len(self._buf) // _VAD_FRAME) * _VAD_FRAME
        if n == 0:
            return False
        chunk, self._buf = self._buf[:n], self._buf[n:]
        score = self._vad.predict(chunk, frame_size=_VAD_FRAME)
        return float(score) > _THRESHOLD
