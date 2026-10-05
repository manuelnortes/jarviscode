"""Prueba EN VIVO de reproducción de medios (⚠️ HACE RUIDO).

Pone una radio de internet en el altavoz 'Salón' a volumen bajo durante unos
segundos y luego para. Ejecuta esto solo cuando estés listo para que suene.

Uso:
    python -m scripts.test_media_play
"""

from __future__ import annotations

import asyncio
import sys

from src.capabilities import media_cast

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# Radio de internet estable para la prueba (SomaFM Groove Salad).
TEST_STREAM = "https://ice1.somafm.com/groovesalad-128-mp3"
DEVICE = media_cast.DEFAULT_DEVICE
SECONDS = 8


async def main() -> None:
    print(f"== Prueba EN VIVO · reproducción en '{DEVICE}' ==\n")
    print(f"Volumen al 15%, reproduciendo {SECONDS}s y parando...\n")

    await media_cast.cast_set_volume.handler({"level": 15, "device": DEVICE})
    await media_cast.cast_play_url.handler({"url": TEST_STREAM, "device": DEVICE})
    print("▶️  Sonando. Deberías oír música en el Salón.")

    await asyncio.sleep(SECONDS)

    await media_cast.cast_stop.handler({"device": DEVICE})
    print("⏹️  Parado.\n✅ Prueba de reproducción completada.")


if __name__ == "__main__":
    asyncio.run(main())
