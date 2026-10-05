"""Prueba de la primera capacidad: búsqueda web.

Pide algo que solo se puede contestar bien con información actual y comprueba
que Jarvis usa de verdad una herramienta web (WebSearch/WebFetch).

Uso:
    python -m scripts.test_websearch
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
    print("== Test capacidad · búsqueda web ==\n")
    prompt = (
        "Busca en la web cuál es la última versión estable de Python publicada "
        "y dime el número de versión. Cita brevemente la fuente."
    )

    async with JarvisCore() as jarvis:
        parts: list[str] = []
        async for chunk in jarvis.ask(prompt):
            parts.append(chunk)
        answer = "".join(parts)

    print(f"🤖 Jarvis: {answer}\n")
    print(f"Herramientas usadas: {jarvis.last_tools_used or 'ninguna'}\n")

    web_tools = {"WebSearch", "WebFetch"}
    if web_tools.intersection(jarvis.last_tools_used):
        print("✅ Jarvis usó la web para responder. Primera capacidad operativa.")
    else:
        print("❌ Jarvis NO usó ninguna herramienta web (respondió de memoria).")
        print("   Revisar allowed_tools / permisos del Agent SDK.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
