"""Prueba manual de TTS: sintetiza una frase con Piper y la reproduce.

Uso (venv activado, desde la raíz del proyecto):
    python -m scripts.test_voice_tts
"""

from __future__ import annotations

import sys

from src.voice.tts import PiperTTS

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def main() -> None:
    tts = PiperTTS()
    print(f"Voz: {tts.voice}")
    print(f"Sample rate: {tts.sample_rate} Hz")
    frase = "Hola. Soy Jarvis. La síntesis de voz funciona correctamente."
    print(f"Reproduciendo: {frase!r}")
    tts.speak(frase)
    print("OK — si has oído la frase, el TTS funciona.")


if __name__ == "__main__":
    main()
