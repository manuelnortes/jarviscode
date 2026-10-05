"""Front-end de texto (CLI) para Jarvis.

Cliente mínimo de línea de comandos sobre el núcleo headless. Sirve para probar
el bucle conversacional antes de meter la voz.

Uso (desde el directorio del proyecto, con el venv activado):
    python -m src.frontends.cli
"""

from __future__ import annotations

import asyncio
import sys

from src.core.jarvis import JarvisCore

# La consola de Windows usa cp1252 por defecto; forzamos UTF-8 para los emojis.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_EXIT_WORDS = {"salir", "exit", "quit", "adios", "adiós"}


async def main() -> None:
    print("== Jarvis · cliente de texto ==")
    print("Escribe tu mensaje. Para terminar: 'salir'.\n")

    async with JarvisCore() as jarvis:
        while True:
            try:
                # input() es bloqueante; lo lanzamos en un hilo para no
                # bloquear el bucle de eventos.
                user = (await asyncio.to_thread(input, "Tú > ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user:
                continue
            if user.lower() in _EXIT_WORDS:
                break

            print("Jarvis > ", end="", flush=True)
            async for chunk in jarvis.ask(user):
                print(chunk, end="", flush=True)
            meta: list[str] = []
            if jarvis.current_model:
                # "claude-sonnet-4-6" → "sonnet-4-6"
                meta.append(jarvis.current_model.removeprefix("claude-"))
            if jarvis.last_tools_used:
                meta.append(f"tools: {', '.join(jarvis.last_tools_used)}")
            if meta:
                print(f"\n  ({' · '.join(meta)})", end="")
            print("\n")

    print("Hasta luego.")


if __name__ == "__main__":
    asyncio.run(main())
