"""Infraestructura de voz de Jarvis (Hito 3).

Piezas reutilizables e independientes del front-end:
  - audio: captura de micrófono (push-to-talk) y reproducción (sounddevice).
  - stt:   reconocimiento de voz con faster-whisper.
  - tts:   síntesis de voz con Piper (binario por subproceso).

El front-end de voz (src/frontends/voice.py) las orquesta junto al núcleo.
NO son "capabilities": las capabilities son herramientas que usa Claude; esto
es infraestructura de entrada/salida del front-end.
"""
