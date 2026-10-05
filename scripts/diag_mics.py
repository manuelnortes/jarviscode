"""Escáner de micrófonos: graba un instante de CADA dispositivo de entrada y
reporta el nivel de señal, para encontrar cuál capta de verdad.

Mientras corre, HABLA o haz ruido continuamente.

Uso (venv activado, desde la raíz del proyecto):
    python -m scripts.diag_mics
"""

from __future__ import annotations

import sys

import numpy as np
import sounddevice as sd

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

SECONDS = 1.2


def main() -> None:
    print("Escaneando micrófonos. ¡HABLA o haz ruido sin parar!\n")
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()

    results = []
    for idx, dev in enumerate(devices):
        if dev["max_input_channels"] < 1:
            continue
        sr = int(dev["default_samplerate"]) or 44100
        api = hostapis[dev["hostapi"]]["name"]
        try:
            audio = sd.rec(
                int(SECONDS * sr),
                samplerate=sr,
                channels=1,
                dtype="float32",
                device=idx,
            )
            sd.wait()
            audio = audio.reshape(-1)
            rms = float(np.sqrt(np.mean(audio**2)))
            peak = float(np.abs(audio).max())
            flag = "  <-- ¡SEÑAL!" if rms > 0.005 else ""
            results.append((rms, idx, dev["name"], api, peak))
            print(f"[{idx:2d}] {api:12s} rms={rms:.5f} peak={peak:.4f}  {dev['name'][:40]}{flag}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{idx:2d}] {api:12s} ERROR: {exc}")

    print("\n== Mejores candidatos (más señal) ==")
    for rms, idx, name, api, peak in sorted(results, reverse=True)[:5]:
        print(f"  device={idx}  rms={rms:.5f}  [{api}]  {name[:45]}")
    print("\nUsa el índice del que tenga señal con JARVIS_INPUT_DEVICE (lo añado al código).")


if __name__ == "__main__":
    main()
