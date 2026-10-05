"""Diagnóstico de captura de micrófono + STT.

Graba una ventana fija de audio (sin push-to-talk) y reporta si entra señal de
verdad y si Whisper la transcribe. Aísla si el problema es la captura o el STT.

Uso (venv activado, desde la raíz del proyecto):
    python -m scripts.diag_audio
"""

from __future__ import annotations

import sys

import numpy as np
import sounddevice as sd

from src.voice.stt import WhisperSTT

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

SECONDS = 4


def _resample(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return audio.astype(np.float32)
    n_dst = int(round(audio.size * dst_sr / src_sr))
    x_src = np.linspace(0, 1, audio.size, endpoint=False)
    x_dst = np.linspace(0, 1, n_dst, endpoint=False)
    return np.interp(x_dst, x_src, audio).astype(np.float32)


def _stats(audio: np.ndarray) -> str:
    if audio.size == 0:
        return "VACÍO"
    peak = float(np.abs(audio).max())
    rms = float(np.sqrt(np.mean(audio**2)))
    return f"{audio.size} muestras · peak={peak:.4f} · rms={rms:.5f}"


def record_window(samplerate: int) -> np.ndarray:
    """Graba SECONDS segundos al samplerate dado (mono float32)."""
    print(f"\n🔴 Grabando {SECONDS}s a {samplerate} Hz… habla AHORA.")
    audio = sd.rec(
        int(SECONDS * samplerate), samplerate=samplerate, channels=1, dtype="float32"
    )
    sd.wait()
    return audio.reshape(-1)


def main() -> None:
    dev = sd.query_devices(kind="input")
    native_sr = int(dev["default_samplerate"])
    print(f"Micro por defecto: {dev['name']}")
    print(f"Sample rate nativo: {native_sr} Hz")

    print("\nCargando STT…")
    stt = WhisperSTT()

    # Prueba 1: grabar al SR nativo (lo más fiable) y resamplear a 16k.
    input(f"\n[Prueba 1 · SR nativo {native_sr}] Pulsa ENTER y di una frase clara… ")
    a_native = record_window(native_sr)
    print("Señal capturada:", _stats(a_native))
    a16 = _resample(a_native, native_sr, 16000)
    seg, _ = stt.model.transcribe(a16, language=stt.language, vad_filter=True)
    print("Transcripción (vad ON) :", repr("".join(s.text for s in seg).strip()))
    seg, _ = stt.model.transcribe(a16, language=stt.language, vad_filter=False)
    print("Transcripción (vad OFF):", repr("".join(s.text for s in seg).strip()))

    # Prueba 2: grabar forzando 16k (como hace el código actual).
    input(f"\n[Prueba 2 · forzado 16000] Pulsa ENTER y repite la frase… ")
    a_forced = record_window(16000)
    print("Señal capturada:", _stats(a_forced))
    seg, _ = stt.model.transcribe(a_forced, language=stt.language, vad_filter=False)
    print("Transcripción (vad OFF):", repr("".join(s.text for s in seg).strip()))

    print("\n== Conclusión ==")
    print("Si la Prueba 1 transcribe bien y la 2 no (o rms casi 0): el fallo es")
    print("forzar 16 kHz en el micro → hay que grabar al SR nativo y resamplear.")
    print("Si ninguna captura señal (rms ~0): el micro/dispositivo es el problema.")
    print("Si captura señal pero vad ON da vacío y vad OFF bien: desactivar VAD.")


if __name__ == "__main__":
    main()
