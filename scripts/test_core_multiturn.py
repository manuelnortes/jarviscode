"""Prueba no interactiva del núcleo: verifica memoria entre turnos.

Manda dos mensajes en la misma sesión; el segundo solo se puede contestar bien
si el núcleo recuerda el primero. Útil como smoke test del bucle conversacional.

Uso:
    python -m scripts.test_core_multiturn
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


async def collect(jarvis: JarvisCore, prompt: str) -> str:
    """Lanza un prompt y devuelve la respuesta completa concatenada."""
    parts: list[str] = []
    async for chunk in jarvis.ask(prompt):
        parts.append(chunk)
    return "".join(parts)


async def main() -> None:
    print("== Test núcleo · memoria multi-turno ==\n")
    async with JarvisCore() as jarvis:
        r1 = await collect(jarvis, "Recuerda este código de prueba: BRAVO-42. Solo confírmalo.")
        print(f"Turno 1 → {r1}\n")

        r2 = await collect(jarvis, "¿Cuál era el código de prueba que te dije?")
        print(f"Turno 2 → {r2}\n")

    if "BRAVO-42" in r2 or "bravo-42" in r2.lower():
        print("✅ El núcleo mantiene memoria entre turnos.")
    else:
        print("❌ El núcleo NO recordó el dato del turno 1. Revisar la sesión.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
