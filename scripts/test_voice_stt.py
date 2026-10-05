"""Prueba manual de STT: graba del micrófono y transcribe con faster-whisper.

Uso (venv activado, desde la raíz del proyecto):
    python -m scripts.test_voice_stt
"""

from __future__ import annotations

import sys

from src.voice import audio
from src.voice.stt import WhisperSTT

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def main() -> None:
    print("Cargando modelo STT (la primera vez se descarga)…")
    stt = WhisperSTT()
    print(f"Modelo: {stt.model_name}  ·  idioma: {stt.language}")

    input("Pulsa ENTER y empieza a hablar… ")
    print("🔴 Grabando… pulsa cualquier tecla para parar.")
    data = audio.record_push_to_talk()
    print(f"· {data.size} muestras capturadas. Transcribiendo…")
    text = stt.transcribe(data)
    print(f"Transcripción: {text!r}")


if __name__ == "__main__":
    main()
