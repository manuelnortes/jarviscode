"""Front-end de voz de Jarvis (prototipo Windows, push-to-talk).

Cierra el bucle de voz:
    micrófono → STT (faster-whisper) → JarvisCore.ask() → TTS (Piper) → altavoz

Lógica de sesión (Hito 3, ver PLAN §7):
  - Un "ciclo de sesión" = una instancia de JarvisCore (memoria multi-turno y
    ratchet-up de modelos funcionan dentro de la sesión).
  - La sesión se CIERRA por:
      (a) palabra de cierre ("gracias", "adiós", "hasta luego"…), o
      (b) inactividad: no pulsar para hablar en JARVIS_VOICE_TIMEOUT_SECONDS.
  - Al cerrarse, la siguiente interacción crea un JarvisCore NUEVO (modelo
    fresco, clasificado desde cero). El wake word (Hito 4) solo sustituirá el
    disparo por tecla; el resto de la lógica de sesión ya queda montada aquí.

Uso (venv activado, desde la raíz del proyecto):
    python -m src.frontends.voice
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

from src.core.jarvis import JarvisCore
from src.core.session import FAREWELL, is_cancel, is_closing
from src.voice import audio
from src.voice.stt import WhisperSTT
from src.voice.tts import PiperTTS

# Separadores de frase para hablar en streaming (empezar a hablar antes de que
# Claude termine toda la respuesta).
_SENTENCE_END = re.compile(r"[.!?…\n]+")

# La consola de Windows usa cp1252 por defecto; forzamos UTF-8 para los emojis.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# Segundos de inactividad tras una respuesta antes de cerrar la sesión.
VOICE_TIMEOUT_SECONDS = float(os.environ.get("JARVIS_VOICE_TIMEOUT_SECONDS", "8"))

# `is_closing`, `CLOSING_PHRASES` y `FAREWELL` viven ahora en src/core/session.py
# (compartidos con el endpoint web). Se importan arriba.


async def _ask_and_speak(core: JarvisCore, prompt: str, tts: PiperTTS) -> None:
    """Pregunta a Claude y habla la respuesta por FRASES, en streaming.

    Solapa la generación con la síntesis: un productor consume el stream de
    texto y va metiendo frases completas en una cola; un consumidor las habla
    según llegan. Así Jarvis empieza a hablar tras la primera frase, en vez de
    esperar a toda la respuesta (menos latencia percibida).
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def producer() -> None:
        buffer = ""
        async for chunk in core.ask(prompt):
            print(chunk, end="", flush=True)
            buffer += chunk
            # Saca todas las frases completas que haya en el buffer.
            while (match := _SENTENCE_END.search(buffer)) is not None:
                sentence = buffer[: match.end()].strip()
                buffer = buffer[match.end():]
                if sentence:
                    await queue.put(sentence)
        if buffer.strip():
            await queue.put(buffer.strip())
        await queue.put(None)  # centinela de fin

    async def consumer() -> None:
        while (sentence := await queue.get()) is not None:
            await asyncio.to_thread(tts.speak, sentence)

    print("Jarvis > ", end="", flush=True)
    await asyncio.gather(producer(), consumer())
    print()


def _print_meta(core: JarvisCore) -> None:
    """Imprime el modelo usado y las herramientas, como hace el CLI de texto."""
    meta: list[str] = []
    if core.current_model:
        meta.append(core.current_model.removeprefix("claude-"))
    if core.last_tools_used:
        meta.append("tools: " + ", ".join(core.last_tools_used))
    if meta:
        print(f"  ({' · '.join(meta)})")


async def run() -> None:
    """Bucle principal del front-end de voz."""
    print("== Jarvis · voz (push-to-talk) ==")
    print("Cargando modelos de voz (la primera vez se descarga el STT)…")
    stt = WhisperSTT()
    tts = PiperTTS()
    print(f"  STT: faster-whisper '{stt.model_name}'  ·  TTS: Piper ({tts.sample_rate} Hz)")
    print()
    print("Pulsa una tecla para hablar; púlsala otra vez para parar de grabar.")
    print(f"Cierra la sesión diciendo 'gracias'/'adiós' o esperando {VOICE_TIMEOUT_SECONDS:.0f}s en silencio.")
    print("Ctrl+C para salir del programa.\n")

    core: JarvisCore | None = None  # None = no hay sesión abierta

    try:
        while True:
            # ── Disparo: abrir sesión nueva o continuar la abierta ──────────
            if core is None:
                print("· Pulsa una tecla para hablar con Jarvis…")
                audio.wait_for_key(None)  # espera indefinida para iniciar sesión
                core = JarvisCore()
                await core.__aenter__()
            else:
                print(
                    f"· (sesión abierta) pulsa para seguir · "
                    f"{VOICE_TIMEOUT_SECONDS:.0f}s de silencio cierran la sesión…"
                )
                started = await asyncio.to_thread(
                    audio.wait_for_key, VOICE_TIMEOUT_SECONDS
                )
                if not started:
                    print("· Sesión cerrada por inactividad.\n")
                    await core.__aexit__(None, None, None)
                    core = None
                    continue

            # ── Grabar y transcribir ───────────────────────────────────────
            print("🔴 Grabando… pulsa una tecla para parar.")
            audio_data = await asyncio.to_thread(audio.record_push_to_talk)
            print("· Transcribiendo…")
            text = await asyncio.to_thread(stt.transcribe, audio_data)

            if not text:
                print("· (no he captado nada, repite)\n")
                continue
            print(f"Tú > {text}")

            # ── ¿Cancelación al vuelo? ("Cancela") ──────────────────────────
            # Se descarta el transcript sin mandarlo al modelo y se vuelve a
            # escuchar; la sesión sigue abierta (no se cierra el núcleo).
            if is_cancel(text):
                print("✋ Cancelado.\n")
                continue

            # ── ¿Cierre de sesión por palabra clave? ────────────────────────
            if is_closing(text):
                print(f"Jarvis > {FAREWELL}")
                await asyncio.to_thread(tts.speak, FAREWELL)
                await core.__aexit__(None, None, None)
                core = None
                print()
                continue

            # ── Pensar (Claude) y responder por voz (frases en streaming) ───
            await _ask_and_speak(core, text, tts)
            _print_meta(core)
            print()

    except (KeyboardInterrupt, EOFError):
        print("\nHasta luego.")
    finally:
        if core is not None:
            await core.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(run())
