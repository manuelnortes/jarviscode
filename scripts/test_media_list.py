"""Prueba (no intrusiva) de la capacidad de medios.

Pide a Jarvis que liste los altavoces Cast. NO reproduce audio, así que no hace
ruido en casa. Valida que el servidor MCP de medios está cableado y que Jarvis
puede invocar sus herramientas.

Uso:
    python -m scripts.test_media_list
"""

from __future__ import annotations

import asyncio
import sys

from src.core.jarvis import JarvisCore

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


async def main() -> None:
    print("== Test capacidad · medios (listar altavoces) ==\n")
    async with JarvisCore() as jarvis:
        parts: list[str] = []
        async for chunk in jarvis.ask("¿Qué altavoces Google Cast tengo disponibles ahora mismo?"):
            parts.append(chunk)
        answer = "".join(parts)

    print(f"🤖 Jarvis: {answer}\n")
    print(f"Herramientas usadas: {jarvis.last_tools_used or 'ninguna'}\n")

    used_media = any("media" in t for t in jarvis.last_tools_used)
    if used_media:
        print("✅ Jarvis usó la capacidad de medios (Google Cast) correctamente.")
    else:
        print("❌ Jarvis NO invocó la herramienta de medios. Revisar wiring MCP / allowed_tools.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
