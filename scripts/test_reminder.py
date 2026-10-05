"""Prueba de la capacidad de recordatorios/temporizadores.

Comprueba el ciclo completo end-to-end:
  1. Programa un recordatorio a ~15 s vista usando la herramienta directamente.
  2. Lo lista (debe aparecer).
  3. Mantiene el scheduler vivo y espera a que venza → debe llegar un push (ntfy).

Y, de paso, valida el wiring vía Jarvis: pide al modelo que ponga un temporizador
y comprueba que invoca `set_reminder`.

Requiere NTFY_BASE_URL/NTFY_TOPIC en el entorno (.env), ya que el aviso se entrega
por notificación push. Manda 1-2 push reales a tu móvil.

Uso:
    python -m scripts.test_reminder
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

from src.capabilities import reminders
from src.core.jarvis import JarvisCore

load_dotenv()

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# Segundos hasta que vence el recordatorio de prueba.
_DELAY = 15


async def main() -> None:
    print("== Test capacidad · recordatorios ==\n")

    # Los objetos @tool no son invocables directamente; su función original vive
    # en .handler (lo que el SDK MCP llama). Para probar sin pasar por el modelo
    # invocamos el handler a mano.
    set_reminder = reminders.set_reminder.handler
    list_reminders = reminders.list_reminders.handler

    # 1) Programar directamente vía la herramienta.
    print(f"→ Programando un recordatorio a {_DELAY} s vista…")
    res = await set_reminder(
        {"message": "Recordatorio de prueba (test_reminder)", "in_seconds": _DELAY}
    )
    print("   " + res["content"][0]["text"])

    # 2) Listar.
    listed = await list_reminders({})
    print("\n→ Recordatorios pendientes:\n   " + listed["content"][0]["text"].replace("\n", "\n   "))

    # 3) Esperar a que venza (el scheduler ya está corriendo: lo arrancó set_reminder).
    print(f"\n→ Esperando {_DELAY + 5} s a que venza (debería llegarte un push)…")
    await asyncio.sleep(_DELAY + 5)
    remaining = reminders.get_scheduler().get_jobs()
    if not remaining:
        print("✅ El recordatorio venció y se eliminó del jobstore (revisa el push en el móvil).")
    else:
        print(f"⚠️ Aún quedan {len(remaining)} jobs pendientes; ¿no llegó a disparar?")

    # 4) Wiring vía Jarvis.
    print("\n→ Pidiendo a Jarvis que ponga un temporizador…")
    async with JarvisCore() as jarvis:
        parts: list[str] = []
        async for chunk in jarvis.ask("Ponme un temporizador de 30 segundos para revisar el horno."):
            parts.append(chunk)
        answer = "".join(parts)

    print(f"\n🤖 Jarvis: {answer}")
    print(f"Herramientas usadas: {jarvis.last_tools_used or 'ninguna'}\n")

    if any("reminders" in t for t in jarvis.last_tools_used):
        print("✅ Jarvis usó la capacidad de recordatorios correctamente.")
    else:
        print("❌ Jarvis NO invocó set_reminder. Revisar wiring MCP / allowed_tools.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
