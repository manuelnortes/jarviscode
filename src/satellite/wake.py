"""Wake word ("Hey Jarvis") para el satélite, sobre openWakeWord.

Usa el modelo preentrenado ``hey_jarvis`` (ONNX) de openWakeWord: sin
entrenamiento propio, corre en onnxruntime (nada que compilar). El modelo espera
**PCM Int16 a 16 kHz** y rinde mejor alimentándolo en bloques de **1280 muestras
(80 ms)**, que es justo el ``BLOCK_SIZE`` de :mod:`src.satellite.mic` — así cada
bloque del micro se convierte en un ``predict`` exacto, sin re-trocear.

Detalles que importan:
    * La **clave** del dict que devuelve ``predict`` puede ser ``hey_jarvis`` o
      ``hey_jarvis_v0.1`` según versión del paquete → se lee en runtime (la que
      contenga ``jarvis``), nunca se hardcodea.
    * Tras un disparo se aplica un **periodo refractario** (~2 s) y un
      ``model.reset()`` para no re-disparar con la misma palabra ni con la cola
      del buffer de predicción.
    * Dos umbrales: uno normal (WAKE_LISTEN/FOLLOWUP) y otro **más alto para el
      estado SPEAKING** (barge-in), de modo que la propia voz de Jarvis (si dice
      "Jarvis") no auto-dispare la interrupción.
"""

from __future__ import annotations

import os
import time

import numpy as np

# Umbrales configurables por entorno (ver tabla de knobs en el plan del hito).
_THRESHOLD = float(os.getenv("JARVIS_SAT_WAKE_THRESHOLD", "0.5"))
_THRESHOLD_SPEAKING = float(os.getenv("JARVIS_SAT_WAKE_THRESHOLD_SPEAKING", "0.7"))
# Ventana tras un disparo en la que ignoramos nuevos disparos (evita el doble
# trigger con la misma palabra y con la cola del buffer de predicción).
_REFRACTORY_SECONDS = 2.0


class WakeWord:
    """Detector de "Hey Jarvis" que consume bloques de 1280 muestras Int16.

    Uso::

        wake = WakeWord()
        for block in mic.blocks():             # block: np.ndarray int16 (1280,)
            score = wake.process(block)
            if wake.triggered(score):
                ...  # wake detectado

    En el estado SPEAKING se pasa ``speaking=True`` a :meth:`triggered` para
    exigir el umbral más alto (barge-in).
    """

    def __init__(self) -> None:
        # Import perezoso: openwakeword tira de onnxruntime, que es pesado de
        # importar; así el modo test-once (Fase 2) no lo carga.
        import openwakeword
        from openwakeword.model import Model

        # openWakeWord 0.4.x empaqueta los .onnx preentrenados en el propio
        # paquete (nada que descargar) y `Model` toma **rutas completas** por
        # `wakeword_model_paths` (el viejo `wakeword_models=[nombre]` +
        # `inference_framework` desaparecieron; el framework se infiere de la
        # extensión .onnx). Resolvemos la ruta del modelo `hey_jarvis`.
        path = next(
            (p for p in openwakeword.get_pretrained_model_paths() if "jarvis" in p.lower()),
            None,
        )
        if path is None:
            raise RuntimeError(
                "No encuentro el modelo 'hey_jarvis' entre los preentrenados de "
                f"openWakeWord: {openwakeword.get_pretrained_model_paths()}"
            )
        self._model = Model(wakeword_model_paths=[path])
        self._key: str | None = None  # clave real del dict, fijada en el 1er predict
        self._last_trigger = 0.0  # monotonic del último disparo (refractario)
        print(f"[wake] modelo openWakeWord cargado: {path.rsplit('/', 1)[-1]}", flush=True)

    def process(self, block: np.ndarray) -> float:
        """Devuelve el score del wake para un bloque de 1280 muestras Int16.

        Args:
            block: Bloque de audio mono Int16 a 16 kHz (idealmente 1280 muestras).

        Returns:
            Score del wake word en [0, 1].
        """
        scores = self._model.predict(block)
        if self._key is None:
            # La clave exacta depende de la versión del paquete → se descubre en
            # runtime y se cachea. Log único para depurar desde `docker logs`.
            self._key = next((k for k in scores if "jarvis" in k.lower()), None)
            print(f"[wake] keys del modelo: {list(scores.keys())} → uso {self._key!r}", flush=True)
            if self._key is None:
                raise RuntimeError(
                    f"El modelo no expone ninguna key con 'jarvis': {list(scores.keys())}"
                )
        return float(scores[self._key])

    def triggered(self, score: float, speaking: bool = False) -> bool:
        """Decide si ``score`` cuenta como disparo, respetando el refractario.

        Al disparar, resetea el buffer de predicción del modelo y arranca el
        periodo refractario.

        Args:
            score: Score devuelto por :meth:`process`.
            speaking: ``True`` si estamos en el estado SPEAKING (barge-in), donde
                se exige el umbral más alto.

        Returns:
            ``True`` si es un disparo válido (fuera del refractario y sobre umbral).
        """
        threshold = _THRESHOLD_SPEAKING if speaking else _THRESHOLD
        now = time.monotonic()
        if now - self._last_trigger < _REFRACTORY_SECONDS:
            return False
        if score < threshold:
            return False
        self._last_trigger = now
        self._model.reset()  # limpia el buffer para no re-disparar con la cola
        return True

    def reset(self) -> None:
        """Limpia el buffer de predicción (p. ej. al cambiar de estado)."""
        self._model.reset()
