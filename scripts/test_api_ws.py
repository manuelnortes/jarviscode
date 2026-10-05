"""Test de la API HTTP/WebSocket del núcleo.

Arranca el servidor FastAPI en el propio proceso (Uvicorn en una task), comprueba
`GET /health` y valida la MEMORIA MULTI-TURNO a través del WebSocket: en el turno 1
da un dato y en el turno 2 comprueba que Jarvis lo recuerda. Así se verifica que el
desacople núcleo<->front-end por red funciona de extremo a extremo.

Uso:
    python -m scripts.test_api_ws
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

import uvicorn
import websockets

from src.core.server import app

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# Puerto propio del test, distinto del de dev (8200), por si hay un server dev vivo.
HOST = "127.0.0.1"
PORT = 8201
WS_URL = f"ws://{HOST}:{PORT}/ws"
HEALTH_URL = f"http://{HOST}:{PORT}/health"

# Dato secreto que Jarvis debe recordar entre turnos.
SECRET_WORD = "berenjena"


async def _collect_reply(ws: websockets.WebSocketClientProtocol, prompt: str) -> str:
    """Envía un turno y devuelve el texto completo de la respuesta."""
    await ws.send(json.dumps({"type": "ask", "prompt": prompt}))
    parts: list[str] = []
    async for raw in ws:
        msg = json.loads(raw)
        if msg["type"] == "chunk":
            parts.append(msg["text"])
        elif msg["type"] == "done":
            break
        elif msg["type"] == "error":
            raise RuntimeError(f"El servidor devolvió error: {msg['message']}")
    return "".join(parts)


async def _wait_until_up(timeout: float = 15.0) -> None:
    """Espera a que el WebSocket acepte conexiones (servidor listo)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with websockets.connect(WS_URL):
                return
        except (OSError, websockets.exceptions.WebSocketException):
            await asyncio.sleep(0.25)
    raise TimeoutError("El servidor no arrancó a tiempo.")


def _check_health() -> bool:
    """Llama a GET /health y comprueba que responde ok."""
    with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("status") == "ok"


async def main() -> int:
    print("== Test · API HTTP/WebSocket del núcleo ==\n")

    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    try:
        await _wait_until_up()

        # 1) Salud del endpoint HTTP.
        health_ok = await asyncio.to_thread(_check_health)
        print(f"[1/3] GET /health → {'OK' if health_ok else 'FALLO'}")
        if not health_ok:
            return 1

        # 2) y 3) Memoria multi-turno por WebSocket (misma conexión = misma sesión).
        async with websockets.connect(WS_URL) as ws:
            r1 = await _collect_reply(
                ws, f"Recuerda esta palabra clave: {SECRET_WORD}. Solo dime 'vale'."
            )
            print(f"[2/3] Turno 1 (dar dato)  → {r1.strip()[:60]!r}")

            r2 = await _collect_reply(ws, "¿Cuál era la palabra clave que te dije?")
            print(f"[3/3] Turno 2 (recordar)  → {r2.strip()[:60]!r}")

        remembered = SECRET_WORD.lower() in r2.lower()
        print()
        if remembered:
            print("✅ PASA: la API recuerda el dato entre turnos por WebSocket.")
            return 0
        print("❌ FALLA: Jarvis no recordó la palabra clave.")
        return 1
    finally:
        server.should_exit = True
        await server_task


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
