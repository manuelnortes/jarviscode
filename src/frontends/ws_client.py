"""Front-end de texto REMOTO para Jarvis (vía WebSocket).

A diferencia de `cli.py` (que importa el núcleo en proceso), este cliente habla
con la API por WebSocket. Sirve para validar el desacople real núcleo<->front-end
y es el patrón que seguirá el front-end de voz.

Uso (con el servidor arrancado en otra terminal: `python -m src.core.server`):
    python -m src.frontends.ws_client
    python -m src.frontends.ws_client ws://127.0.0.1:8200/ws   # URL explícita
"""

from __future__ import annotations

import asyncio
import json
import sys

import websockets

# La consola de Windows usa cp1252 por defecto; forzamos UTF-8 para los emojis.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

DEFAULT_URL = "ws://127.0.0.1:8200/ws"
_EXIT_WORDS = {"salir", "exit", "quit", "adios", "adiós"}


async def _ask_and_print(ws: websockets.WebSocketClientProtocol, prompt: str) -> None:
    """Envía un turno y va imprimiendo la respuesta en streaming.

    Args:
        ws: Conexión WebSocket abierta con la API.
        prompt: Mensaje del usuario.
    """
    await ws.send(json.dumps({"type": "ask", "prompt": prompt}))
    print("Jarvis > ", end="", flush=True)

    async for raw in ws:
        msg = json.loads(raw)
        kind = msg.get("type")
        if kind == "chunk":
            print(msg["text"], end="", flush=True)
        elif kind == "tools":
            print(f"\n  (herramientas: {', '.join(msg['tools'])})", end="")
        elif kind == "done":
            break
        elif kind == "error":
            print(f"\n  [error] {msg['message']}", end="")
            break
    print("\n")


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print("== Jarvis · cliente de texto (remoto / WebSocket) ==")
    print(f"Conectando a {url} …")

    async with websockets.connect(url) as ws:
        print("Conectado. Escribe tu mensaje. Para terminar: 'salir'.\n")
        while True:
            try:
                user = (await asyncio.to_thread(input, "Tú > ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user:
                continue
            if user.lower() in _EXIT_WORDS:
                break

            await _ask_and_print(ws, user)

    print("Hasta luego.")


if __name__ == "__main__":
    asyncio.run(main())
