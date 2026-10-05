"""Reconocimiento de voz (STT) con faster-whisper.

Carga el modelo una vez (se descarga de HuggingFace en el primer uso y queda
cacheado) y transcribe arrays float32 mono a 16 kHz.

Configuración por entorno:
  JARVIS_STT_MODEL     Modelo Whisper (tiny/base/small/medium/large-v3). Default: medium.
  JARVIS_STT_DEVICE    cpu | cuda. Default: cpu (en el NUC no hay GPU CUDA).
  JARVIS_STT_COMPUTE   Tipo de cómputo de CTranslate2 (int8, int8_float16, float16…). Default: int8.
  JARVIS_STT_LANGUAGE  Idioma forzado del reconocimiento. Default: es.
  JARVIS_STT_CPU_THREADS  Nº de hilos que CTranslate2 usa por transcripción. Default: os.cpu_count()
                       (6 en el NUC). Por defecto CTranslate2 usa pocos hilos y desaprovecha la CPU.
"""

from __future__ import annotations

import os
import re
import unicodedata

import numpy as np
from faster_whisper import WhisperModel

STT_MODEL = os.environ.get("JARVIS_STT_MODEL", "medium")
STT_DEVICE = os.environ.get("JARVIS_STT_DEVICE", "cpu")
STT_COMPUTE = os.environ.get("JARVIS_STT_COMPUTE", "int8")
STT_LANGUAGE = os.environ.get("JARVIS_STT_LANGUAGE", "es")
# CTranslate2 por defecto usa pocos hilos; con env fijamos cuántos usar. Sin env,
# aprovechamos todos los núcleos disponibles (os.cpu_count(), 6 en el NUC).
STT_CPU_THREADS = int(os.environ.get("JARVIS_STT_CPU_THREADS", os.cpu_count() or 0))

def _normalize_for_match(text: str) -> str:
    """Normaliza texto para comparar contra la lista negra.

    Pasa a minúsculas, quita tildes/diacríticos y deja solo letras/números y
    espacios simples, para que "¡Suscríbete!" y "suscribete" comparen igual.

    Args:
        text: Texto a normalizar.

    Returns:
        Cadena normalizada.
    """
    nfkd = unicodedata.normalize("NFKD", text.lower())
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9\s]", " ", no_accents)
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_hallucination(text: str) -> bool:
    """Indica si ``text`` es una frase-fantasma típica de Whisper sobre silencio.

    Args:
        text: Texto (segmento o transcript completo) ya en bruto.

    Returns:
        True si tras normalizar coincide con una frase de la lista negra.
    """
    return _normalize_for_match(text) in _HALLUCINATION_PHRASES


# Frases-fantasma que faster-whisper alucina sobre audio mudo/degradado (las
# aprendió de subtítulos de YouTube). Llegan con confianza ALTA, así que ningún
# umbral de probabilidad las filtra: hay que descartarlas por texto. Se comparan
# normalizadas (minúsculas, sin tildes, sin signos ni espacios extra).
_HALLUCINATION_PHRASES = frozenset(
    _normalize_for_match(p)
    for p in (
        "suscribete",
        "suscribete al canal",
        "no olvides suscribirte",
        "dale like y suscribete",
        "gracias por ver el video",
        "gracias por ver este video",
        "gracias por ver",
        "nos vemos en el proximo video",
        "subtitulos realizados por la comunidad de amara org",
        "subtitulado por la comunidad de amara org",
        "subtitulos por la comunidad de amara org",
        "musica",
        "aplausos",
        "guau",
    )
)


class WhisperSTT:
    """Transcriptor de voz reutilizable sobre faster-whisper."""

    def __init__(
        self,
        model: str = STT_MODEL,
        device: str = STT_DEVICE,
        compute_type: str = STT_COMPUTE,
        language: str = STT_LANGUAGE,
        cpu_threads: int = STT_CPU_THREADS,
    ) -> None:
        """Carga el modelo Whisper.

        Args:
            model: Nombre del modelo (tiny/base/small/...). Se descarga si falta.
            device: 'cpu' o 'cuda'.
            compute_type: Tipo de cómputo de CTranslate2.
            language: Idioma forzado (acelera y mejora la transcripción).
            cpu_threads: Nº de hilos de CTranslate2. 0 = default interno de la librería.
        """
        self.language = language
        self.model_name = model
        self.model = WhisperModel(
            model, device=device, compute_type=compute_type, cpu_threads=cpu_threads
        )

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe audio a texto.

        Args:
            audio: Array float32 mono a 16 kHz.

        Returns:
            Texto transcrito (cadena vacía si el audio está vacío o es silencio).
        """
        if audio.size == 0:
            return ""
        # vad_filter recorta silencios al principio/final → menos alucinaciones.
        # condition_on_previous_text=False evita que Whisper arrastre/repita texto
        # previo (otra fuente habitual de alucinaciones en clips cortos).
        # compression_ratio/log_prob thresholds dejan que el propio Whisper marque
        # segmentos de baja calidad como no fiables.
        segments, _ = self.model.transcribe(
            audio,
            language=self.language,
            vad_filter=True,
            condition_on_previous_text=False,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
        )
        # Filtramos segmento a segmento:
        #  - no_speech_prob alto → Whisper cree que es silencio (alucinación).
        #  - frase en la lista negra → fantasma de subtítulos ("suscríbete"…).
        kept = [
            seg.text
            for seg in segments
            if getattr(seg, "no_speech_prob", 0.0) < 0.6 and not _is_hallucination(seg.text)
        ]
        text = "".join(kept).strip()
        # Si tras unir todo el resultado entero es una frase-fantasma, lo tiramos.
        if _is_hallucination(text):
            return ""
        return text
