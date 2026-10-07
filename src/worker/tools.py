"""Herramientas MCP que el worker da a cada agente (Fase 2).

- ``submit_pr``: el agente declara que ha terminado su trabajo de código y deja
  los datos del PR estructurados (título, descripción, tests). El push y el
  registro del PR los hace el runner al acabar el turno, no el agente.
- ``ask``: pregunta al gestor (la conversación con Jarvis) o al usuario y
  **bloquea** hasta que llega la respuesta (D9).

Se crea un servidor por agente porque cada herramienta tiene que saber de qué
agente es la llamada.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_agent_sdk import create_sdk_mcp_server, tool

if TYPE_CHECKING:
    from src.worker.manager import AgentManager

SERVER_NAME = "agent"


def _text(message: str) -> dict:
    """Envuelve un texto en el formato de respuesta que espera el SDK MCP."""
    return {"content": [{"type": "text", "text": message}]}


def build_agent_server(manager: "AgentManager", agent_id: str, code_mode: bool):
    """Servidor MCP con las herramientas de un agente concreto.

    Args:
        manager: Gestor que recibe el PR y enruta las preguntas.
        agent_id: Agente al que pertenecen las herramientas.
        code_mode: Si True incluye ``submit_pr`` (solo los agentes de código).

    Returns:
        Tupla (servidor, nombres completos de las herramientas).
    """

    @tool(
        "ask",
        "Pregunta algo y espera la respuesta. kind='clarification' para dudas sobre la tarea (las "
        "responde el gestor, que puede pasárselas al usuario); kind='design' para decisiones de diseño "
        "que solo puede tomar el usuario. Pregunta solo si de verdad no puedes decidir razonablemente.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "La pregunta, autocontenida."},
                "kind": {"type": "string", "enum": ["clarification", "design"]},
            },
            "required": ["question", "kind"],
        },
    )
    async def ask(args: dict) -> dict:
        answer = await manager.ask(agent_id, args["question"], args.get("kind", "clarification"))
        return _text(f"Respuesta: {answer}")

    @tool(
        "submit_pr",
        "Declara terminado tu trabajo de código. Llámala al final, después de hacer commit de todo. "
        "El sistema empujará tu rama y abrirá el PR con estos datos.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Título corto del PR."},
                "body": {"type": "string", "description": "Qué cambia y por qué; cómo probarlo."},
                "tests_passed": {"type": "boolean", "description": "Si la batería de tests pasa entera."},
                "tests_output": {"type": "string", "description": "Resumen de la salida de los tests."},
            },
            "required": ["title", "body", "tests_passed"],
        },
    )
    async def submit_pr(args: dict) -> dict:
        manager.record_pr_request(agent_id, args)
        return _text("PR registrado: se publicará al terminar este turno.")

    tools = [ask, submit_pr] if code_mode else [ask]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tools)
    return server, [f"mcp__{SERVER_NAME}__{t.name}" for t in tools]
