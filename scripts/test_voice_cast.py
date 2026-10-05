"""Prueba de integración del modo de salida Cast del WS /voice (Hito 3.5, Fase 1).

Simula al satélite del salón SIN micro: se conecta al endpoint `/voice`, activa el
modo cast (`hello`), pide el beep de confirmación (`wake`) y manda una utterance
sintetizada (o un WAV pregrabado) como si fuera lo que capta el micro. Valida que:

  1. El `wake` hace sonar el ding en el dispositivo Cast.
  2. La utterance se transcribe, Claude responde y la respuesta **suena en el
     Salón** (no llega PCM por el WS; llega `cast_start` con la duración).

Pensado para correr **en el NUC** (donde Piper y la red Cast están disponibles):
    python -m scripts.test_voice_cast
o apuntando a otro servidor / dispositivo:
    python -m scripts.test_voice_cast --url ws://192.168.1.10:8200/voice --device Salón
    python -m scripts.test_voice_cast --wav grabacion_16k.wav

Requiere que el servidor esté levantado y un dispositivo Cast alcanzable. En dev
Windows el Cast puede fallar por mDNS: la validación buena es contra el NUC.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave

import numpy as np
import websockets

from src.voice.tts import PiperTTS

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_STT_SAMPLE_RATE = 16000  # el contrato /voice exige 16 kHz mono


def _resample_to_16k(samples: np.ndarray, src_rate: int) -> np.ndarray:
    """Reescala audio float32 mono a 16 kHz por interpolación lineal.

    Piper sintetiza a 22050 Hz; el WS de voz espera 16 kHz. Un remuestreo simple
    basta para que Whisper transcriba bien la frase de prueba.

    Args:
        samples: Audio float32 mono.
        src_rate: Frecuencia de muestreo de ``samples`` (Hz).

    Returns:
        Audio float32 mono a 16 kHz.
    """
    if src_rate == _STT_SAMPLE_RATE or samples.size == 0:
        return samples
    n_out = int(round(samples.size * _STT_SAMPLE_RATE / src_rate))
    x_old = np.linspace(0.0, 1.0, samples.size, dtype=np.float64)
    x_new = np.linspace(0.0, 1.0, n_out, dtype=np.float64)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def _load_wav_16k(path: str) -> bytes:
    """Carga un WAV mono y devuelve su PCM Int16 LE remuestreado a 16 kHz.

    Args:
        path: Ruta a un fichero WAV (mono, Int16).

    Returns:
        Bytes PCM Int16 LE a 16 kHz.
    """
    with wave.open(path, "rb") as wav:
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    samples = _resample_to_16k(samples, rate)
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _synthesize_16k(text: str) -> bytes:
    """Sintetiza ``text`` con Piper y lo devuelve como PCM Int16 LE a 16 kHz.

    Args:
        text: Frase a "decir" como si fuera el micro (p. ej. "¿qué hora es?").

    Returns:
        Bytes PCM Int16 LE a 16 kHz.
    """
    tts = PiperTTS()
    samples = _resample_to_16k(tts.synthesize(text), tts.sample_rate)
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


async def _run(url: str, device: str, pcm: bytes) -> int:
    """Ejecuta el diálogo cast contra el servidor y valida el resultado.

    Args:
        url: URL del WebSocket ``/voice``.
        device: Nombre del dispositivo Cast de salida.
        pcm: PCM Int16 LE 16 kHz mono de la utterance de prueba.

    Returns:
        0 si llegó ``cast_start``; 1 en caso contrario.
    """
    async with websockets.connect(url, max_size=None) as ws:
        # 1) Handshake: activar modo cast.
        await ws.send(json.dumps({"type": "hello", "output": "cast", "device": device}))
        print(f"→ hello output=cast device={device!r}")

        # 2) Wake: debe sonar el ding en el dispositivo.
        await ws.send(json.dumps({"type": "wake"}))
        print("→ wake (escucha el DING en el Salón)")
        await asyncio.sleep(2.0)

        # 3) Utterance: mandamos el PCM como si viniera del micro.
        await ws.send(json.dumps({"type": "utterance_start"}))
        for i in range(0, len(pcm), 32000):  # ~1 s por frame; el tamaño da igual
            await ws.send(pcm[i : i + 32000])
        await ws.send(json.dumps({"type": "utterance_end"}))
        secs = len(pcm) / (_STT_SAMPLE_RATE * 2)
        print(f"→ utterance_end ({secs:.1f}s de audio)")

        # 4) Leer hasta `done`; el modo cast NO manda PCM, solo `cast_start`.
        cast_started = False
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            if isinstance(raw, bytes):
                print(f"← <binario {len(raw)} B>  (¡inesperado en modo cast!)")
                continue
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "transcript":
                print(f"← transcript: {msg.get('text')!r}")
            elif mtype == "reply_text":
                print(f"← reply_text: {msg.get('text')!r}")
            elif mtype == "cast_start":
                cast_started = True
                print(f"← cast_start: duración {msg.get('duration_s'):.1f}s "
                      f"(escucha la respuesta en el Salón)")
            elif mtype == "done":
                print(f"← done (modelo {msg.get('model')})")
                break
            elif mtype in ("error", "cancelled"):
                print(f"← {mtype}: {msg}")
                break
            else:
                print(f"← {mtype}: {msg}")

    if cast_started:
        print("\n✅ PASA: llegó cast_start. Confirma por oído el ding y la respuesta.")
        return 0
    print("\n❌ FALLA: no llegó cast_start.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Prueba del modo cast del WS /voice.")
    parser.add_argument("--url", default="ws://127.0.0.1:8200/voice",
                        help="URL del WebSocket /voice.")
    parser.add_argument("--device", default="Salón",
                        help="Dispositivo Cast de salida.")
    parser.add_argument("--text", default="¿Qué hora es?",
                        help="Frase a sintetizar como utterance (si no se da --wav).")
    parser.add_argument("--wav", default=None,
                        help="WAV mono pregrabado a usar como utterance (en vez de --text).")
    args = parser.parse_args()

    print("== Prueba de integración · modo cast del WS /voice ==\n")
    if args.wav:
        print(f"Utterance desde WAV: {args.wav}")
        pcm = _load_wav_16k(args.wav)
    else:
        print(f"Utterance sintetizada con Piper: {args.text!r}")
        pcm = _synthesize_16k(args.text)

    return asyncio.run(_run(args.url, args.device, pcm))


if __name__ == "__main__":
    raise SystemExit(main())
