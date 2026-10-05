"""Smoke test de Hito 0 para Jarvis.

Verifica tres cosas antes de construir nada encima:
  1. Que NO existe ANTHROPIC_API_KEY en el entorno (para no salirnos de la
     suscripción y caer en facturación pay-as-you-go).
  2. Que hay credenciales de suscripción (CLAUDE_CODE_OAUTH_TOKEN, o un login
     previo del CLI de Claude Code en esta máquina).
  3. Que una llamada de ida y vuelta al Agent SDK responde correctamente.

Uso:
    python scripts/smoke_test.py
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

# La consola de Windows usa cp1252 por defecto y no codifica emojis.
# Forzamos UTF-8 en la salida para que los prints no revienten.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def check_environment() -> None:
    """Comprueba el entorno antes de llamar al SDK.

    Sale con código 1 si detecta una API key (rompería el modelo de
    facturación por suscripción). Solo avisa si falta el token OAuth,
    porque el SDK podría usar un login previo del CLI.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY está definida en el entorno.")
        print("   Esto haría que el Agent SDK facture pay-as-you-go en lugar de")
        print("   usar tu suscripción. Quítala del entorno/.env antes de seguir.")
        sys.exit(1)

    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print("⚠️  No hay CLAUDE_CODE_OAUTH_TOKEN en el entorno ni en .env.")
        print("   Genera uno con:  claude setup-token")
        print("   (Si ya hiciste login del CLI en esta máquina, el SDK puede usar")
        print("    esas credenciales igualmente; seguimos para comprobarlo.)")
    else:
        print("✅ CLAUDE_CODE_OAUTH_TOKEN presente.")


async def run_query() -> bool:
    """Lanza una pregunta simple al Agent SDK y muestra la respuesta.

    Returns:
        True si Claude devolvió algún bloque de texto.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    options = ClaudeAgentOptions(
        system_prompt="Eres Jarvis. Responde en una sola frase, en español.",
        max_turns=1,
    )

    got_text = False
    async for message in query(
        prompt="Di 'hola' y confirma en una frase que estás operativo.",
        options=options,
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"🤖 Jarvis: {block.text}")
                    got_text = True
    return got_text


def main() -> None:
    load_dotenv()
    print("== Jarvis · Smoke test Hito 0 ==\n")
    check_environment()
    print("\nLanzando llamada de prueba al Agent SDK...\n")

    try:
        ok = asyncio.run(run_query())
    except Exception as exc:  # noqa: BLE001 - en un smoke test queremos ver cualquier fallo
        print(f"\n❌ La llamada falló: {type(exc).__name__}: {exc}")
        print("   Revisa la autenticación (claude setup-token) y que el CLI de")
        print("   Claude Code esté instalado y accesible en el PATH.")
        sys.exit(1)

    if ok:
        print("\n✅ Hito 0 OK: el Agent SDK responde usando tu suscripción.")
    else:
        print("\n⚠️ La llamada terminó sin texto de respuesta. Revisa la salida de arriba.")
        sys.exit(1)


if __name__ == "__main__":
    main()
