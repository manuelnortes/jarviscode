"""Prueba de la capacidad de notificaciones push (ntfy).

Hace dos comprobaciones:
  1. Publicación directa (sin modelo): valida que NTFY_BASE_URL/NTFY_TOPIC están
     bien y que el servidor acepta el aviso. Manda UN push real a tu móvil.
  2. Vía Jarvis: pide al modelo que envíe una notificación y comprueba que invoca
     la herramienta `notify_user`.

Requiere las variables NTFY_BASE_URL y NTFY_TOPIC en el entorno (.env).

Uso:
    python -m scripts.test_notify
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

from src.capabilities import notify
from src.core.jarvis import JarvisCore

load_dotenv()

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


async def main() -> None:
    print("== Test capacidad · notificaciones (ntfy) ==\n")

    # 1) Publicación directa.
    print("→ Publicación directa en ntfy…")
    await notify.publish(
        "Prueba directa desde test_notify.",
        title="Jarvis · test",
        priority="default",
        tags=["white_check_mark"],
    )
    print("✅ Publicación directa aceptada por el servidor ntfy.\n")

    # 2) Vía Jarvis.
    print("→ Pidiendo a Jarvis que envíe una notificación…")
    async with JarvisCore() as jarvis:
        parts: list[str] = []
        async for chunk in jarvis.ask(
            "Mándame una notificación al móvil que diga: 'Jarvis operativo'."
        ):
            parts.append(chunk)
        answer = "".join(parts)

    print(f"\n🤖 Jarvis: {answer}")
    print(f"Herramientas usadas: {jarvis.last_tools_used or 'ninguna'}\n")

    if any("notify" in t for t in jarvis.last_tools_used):
        print("✅ Jarvis usó la capacidad de notificaciones (ntfy) correctamente.")
    else:
        print("❌ Jarvis NO invocó notify_user. Revisar wiring MCP / allowed_tools.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
