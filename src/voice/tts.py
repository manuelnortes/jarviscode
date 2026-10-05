"""Síntesis de voz (TTS) con Piper, vía su binario por subproceso.

Usamos el binario de Piper (no el paquete pip `piper-tts`) por dos motivos:
  1. Robustez en Windows: evita los problemas de wheels de `piper-phonemize`.
  2. Paridad Windows/NUC: el mismo enfoque (un binario invocado por subproceso)
     servirá igual al portar la voz a Docker (Hito 3.5).

El binario recibe el texto por stdin y, con ``--output_raw``, devuelve PCM crudo
(int16 mono) por stdout. La frecuencia de muestreo se lee del fichero
``<voz>.onnx.json`` (campo ``audio.sample_rate``).

Configuración por entorno:
  JARVIS_PIPER_BIN  Ruta al ejecutable de Piper (default: ./piper/piper.exe).
  JARVIS_TTS_VOICE  Ruta al modelo .onnx de la voz (default: voz es_ES local).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import numpy as np

# OJO: `src.voice.audio` importa `sounddevice` (PortAudio), que solo hace falta
# para reproducir en local (método `speak`). El servidor web hace TTS server-side
# y manda el PCM al navegador, así que NO debe arrastrar PortAudio. Por eso el
# import de `audio` es perezoso (dentro de `speak`), no en cabecera: así la imagen
# Docker headless del NUC no necesita libportaudio2.


# Reescritura fonética para voces Piper en español.
# Las voces es_* leen el inglés con fonética castellana: 'j' → jota, 'h' → muda,
# 'w' → b/gu, clusters consonánticos anglosajones → pronunciaciones raras.
# Este mapa corrige las palabras más habituales en el contexto del homelab.
# NUNCA aplicar al texto que se muestra o loguea; solo antes de sintetizar.
_ANGLICISM_MAP: dict[str, str] = {
    "jarvis":    "Yarvis",      # j → Y (sonido inglés /dʒ/)
    "homelab":   "jómlab",      # h muda → j para aproximar /h/
    "docker":    "dóker",
    "router":    "rúter",
    "backup":    "bakap",
    "deploy":    "deplói",      # oy → oi (diptongo español)
    "proxmox":   "próxmox",     # acento en sílaba tónica
    "streaming": "estríming",   # añade e- inicial, ea → í
    "script":    "escrip",      # cluster str- + pt final difíciles
    "prompt":    "prómp",       # pt final mudo en español
    "wake":      "uéik",        # w → u
    "word":      "uórd",        # w → u
    "wifi":      "uífi",        # w → u
    "server":    "sérver",
}


def _apply_anglicism_map(text: str) -> str:
    """Sustituye anglicismos por su grafía fonética española antes de sintetizar."""
    for word, respelling in _ANGLICISM_MAP.items():
        text = re.sub(rf"\b{re.escape(word)}\b", respelling, text, flags=re.I)
    return text


def clean_for_speech(text: str) -> str:
    """Quita el formato Markdown y corrige anglicismos para que Piper suene bien.

    Sin esto, "**21:18**" se pronunciaría "asterisco asterisco 21:18…" y
    "Jarvis" sonaría con jota castellana en lugar del sonido inglés /dʒ/.

    Args:
        text: Texto (posiblemente con Markdown) generado por Claude.

    Returns:
        Texto plano con grafía fonética, apto para sintetizar.
    """
    # Bloques y código inline `...` → contenido sin las comillas.
    text = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", text)
    # Enlaces [texto](url) → solo el texto.
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # URLs sueltas (no envueltas en enlace markdown) → fuera del habla. Piper se
    # lía deletreándolas ("hache-te-te-pe-ese, dos puntos, barra, barra…"). En
    # pantalla SÍ se siguen mostrando; esto solo afecta al audio. Se hace DESPUÉS
    # de resolver [texto](url) para no tocar las URLs ya envueltas en un enlace.
    text = re.sub(r"(?:https?://|www\.)\S+", "", text)
    # Espacios dobles que dejan las URLs eliminadas.
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Pie de fuentes ("Fuentes: ...", "Referencias: ...", "Sources: ...") → fuera
    # del habla. El prompt ya lo desincentiva, pero si al modelo se le escapa no
    # debe leerse. Como el habla se trocea por frases, el pie llega como frase
    # propia y el ancla ^ (re.M) lo caza entero. En pantalla SÍ se muestra
    # (mismo criterio que las URLs: esto solo afecta al audio).
    text = re.sub(r"^\s*(?:fuentes?|referencias?|sources?)\s*:.*$", "", text, flags=re.I | re.M)
    # Énfasis: ** * __ _ ~~ → fuera.
    text = re.sub(r"(\*\*|\*|__|_|~~)", "", text)
    # Encabezados (# ..) y citas (> ..) al inicio de línea.
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)
    # Viñetas (- * +) al inicio de línea.
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    # Reescritura fonética de anglicismos.
    text = _apply_anglicism_map(text)
    return text.strip()

# Raíz del proyecto (jarvis/), para resolver los defaults de las descargas locales.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

PIPER_BIN = os.environ.get(
    "JARVIS_PIPER_BIN", str(_PROJECT_ROOT / "piper" / "piper.exe")
)
TTS_VOICE = os.environ.get(
    "JARVIS_TTS_VOICE",
    str(_PROJECT_ROOT / "voices" / "es_ES" / "es_ES-davefx-medium.onnx"),
)


class PiperTTS:
    """Sintetizador Piper reutilizable.

    Lee el sample rate de la voz una sola vez al construirse. El binario se
    invoca por cada frase (Piper arranca en ~0.2 s, despreciable frente a la
    latencia de red de Claude).
    """

    def __init__(self, piper_bin: str = PIPER_BIN, voice: str = TTS_VOICE) -> None:
        """Inicializa el sintetizador.

        Args:
            piper_bin: Ruta al ejecutable de Piper.
            voice: Ruta al modelo .onnx de la voz (debe existir su .onnx.json).

        Raises:
            FileNotFoundError: Si no existe el binario o el config de la voz.
        """
        self.piper_bin = piper_bin
        self.voice = voice

        if not Path(piper_bin).exists():
            raise FileNotFoundError(
                f"No se encuentra el binario de Piper en '{piper_bin}'. "
                "Descárgalo (ver README) o ajusta JARVIS_PIPER_BIN."
            )
        config_path = voice + ".json"
        if not Path(config_path).exists():
            raise FileNotFoundError(
                f"No se encuentra el config de la voz en '{config_path}'. "
                "Descarga el .onnx y su .onnx.json (ver README) o ajusta JARVIS_TTS_VOICE."
            )
        with open(config_path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
        self.sample_rate = int(config.get("audio", {}).get("sample_rate", 22050))

    def synthesize(self, text: str) -> np.ndarray:
        """Convierte texto en audio float32 mono listo para reproducir.

        Args:
            text: Texto a sintetizar.

        Returns:
            Array float32 mono en rango [-1, 1] (vacío si el texto está vacío).
        """
        text = clean_for_speech(text)
        if not text:
            return np.zeros(0, dtype=np.float32)

        proc = subprocess.run(
            [self.piper_bin, "--model", self.voice, "--output_raw"],
            input=text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        pcm = np.frombuffer(proc.stdout, dtype=np.int16)
        # int16 → float32 normalizado para sounddevice.
        return pcm.astype(np.float32) / 32768.0

    def speak(self, text: str) -> None:
        """Sintetiza y reproduce el texto por el altavoz por defecto (bloqueante).

        Args:
            text: Texto a decir.
        """
        from src.voice import audio as _audio  # import perezoso: ver nota arriba

        samples = self.synthesize(text)
        _audio.play(samples, self.sample_rate)
