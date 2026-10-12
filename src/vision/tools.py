"""Herramientas de Vision (servidor MCP en proceso).

Vision reutiliza las herramientas de lectura y bitácora de la capacidad de
agentes y las de lectura de proyectos, y añade las suyas:

- ``vision_answer``: responde a la pregunta de un agente dejando constancia de
  que la respondió Vision (la bitácora la marca con ``(vision)``).
- ``project_file`` / ``project_list`` (de ``src/capabilities/project_files.py``):
  leer el plan y los ficheros de un proyecto desde su bare repo.

Revisión de PRs (tarea (b)):

- ``pr_diff``: el diff del PR de un agente.
- ``vision_merge``: fusiona un PR **solo** en la rama de integración (``develop``)
  y **solo** con la suite en verde (la ejecuta el runner sobre la punta de la rama).
  La guarda está en el código, no en el prompt: Vision nunca toca la rama principal.
- ``vision_plan_edit``: el plan lo actualiza Vision, no los agentes: reemplazo
  exacto en ``PLAN.md`` o ``docs/plan/*.md``, solo en la rama de integración.
- ``vision_flag``: un PR (o algo) necesita al usuario: bitácora + aviso al móvil.

Gestión del plan (tareas (h) e (i)):

- ``vision_start``: lanza un agente de código para UN punto del plan (``plan_item``
  obligatorio). Se apaga con ``JARVIS_VISION_LAUNCH=0``.
- ``vision_propose``: idea o desvío fuera del plan, sin agente: registro + aviso; decide el usuario.
- ``vision_wait``: el agente A espera a que se fusione el trabajo de F; al fusionarse,
  el bucle de Vision lanza el evento DESBLOQUEO.

Órdenes del usuario (Fase 5, Vision único gestor; Jarvis solo hace de puente):

- ``agent_message`` / ``agent_cancel`` (de la capacidad de agentes): instrucciones,
  respuestas a preguntas escaladas, retomar pausados y cancelar.
- ``user_merge``: fusiona un PR en cualquier base, pero **solo** durante el turno de
  un mensaje del usuario (guarda en código: la orden del usuario es la confirmación).
- ``user_close``: cierra un PR sin fusionar (la rama se queda), con la misma guarda.

Vision NO tiene ``agent_start`` (solo ``vision_start``, ceñido al plan) ni ``pr_merge``.
"""

from __future__ import annotations

import os

from claude_agent_sdk import create_sdk_mcp_server, tool

from src.capabilities import agents
from src.capabilities.project_files import project_file, project_list
from src.vision import metrics as vision_metrics
from src.worker import settings

SERVER_NAME = "vision"
# Tope del diff que se le pasa a Vision (caracteres).
_MAX_DIFF = 60_000


@tool(
    "vision_answer",
    "Manda un mensaje a un agente: la respuesta a su pregunta pendiente (queda en su bitácora como "
    "respuesta de Vision), los cambios que pides a su PR si terminó, o el aviso de que continúe tras un "
    "bloqueo (si sigue en idle retoma la misma sesión con la caché caliente; si ya cerró, retoma desde su "
    "handoff). Mensaje concreto y autocontenido; si se apoya en el plan, cita la sección o decisión.",
    {
        "type": "object",
        "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
        "required": ["id", "text"],
    },
)
async def vision_answer(args: dict) -> dict:
    async def go() -> str:
        agent = await agents._call(
            "POST", f"/agents/{args['id']}/message", json={"text": args["text"], "by": "vision"}
        )
        return f"Respuesta entregada. Agente {agent['id']}: {agent['state']}."

    return await agents._run(go())


@tool(
    "pr_diff",
    "Diff completo del PR de un agente (contra su rama base).",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
async def pr_diff(args: dict) -> dict:
    from src.capabilities import prs

    async def go() -> str:
        data = await prs.diff(args["id"])
        text = data.get("diff") or "(sin diff: la rama no existe o no tiene cambios)"
        if len(text) > _MAX_DIFF:
            text = text[:_MAX_DIFF] + "\n… (diff recortado)"
        return f"PR {data.get('branch')} → {data.get('base_branch')}\n{text}"

    return await agents._run(go())


def merge_guard(agent: dict) -> str:
    """Motivo por el que Vision NO puede fusionar este PR ('' si puede).

    La regla dura del usuario: Vision solo fusiona en la rama de integración y con
    la suite en verde según la ejecutó el runner sobre la punta actual de la rama.
    Nunca en la principal. No exige tests nuevos.
    """
    from src.capabilities import prs

    integration = prs.integration_branch()
    if not integration or agent.get("base_branch") != integration:
        return (f"su base es '{agent.get('base_branch')}', no la rama de integración "
                f"'{integration or '(desactivada)'}': ese PR lo fusiona el usuario")
    if agent.get("pr_tests_passed") != 1:
        return "la suite no está en verde: mira la salida en su detalle y pide al agente que arregle su cambio"
    tip = prs.branch_tip(agent["repo"], agent["branch"])
    if not tip or agent.get("pr_tests_sha") != tip:
        return ("el verde es de un commit anterior a la punta de la rama: espera a que el sistema "
                "vuelva a pasar la suite (lo hace al acabar cada turno del agente)")
    return ""


@tool(
    "vision_merge",
    "Fusiona el PR de un agente en la rama de integración (develop). Solo si está alineado con el plan "
    "y los tests pasan; el sistema rechaza cualquier PR contra otra rama o sin tests en verde.",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
async def vision_merge(args: dict) -> dict:
    from src.capabilities import prs

    async def go() -> str:
        agent = await agents.get_agent(args["id"])
        reason = merge_guard(agent)
        if reason:
            return f"No fusiono: {reason}."
        merged = await prs.merge(args["id"], by="Vision")
        return f"PR fusionado en {merged['repo']}/{merged['base_branch']} (commit {merged['pr_merge_sha'][:10]})."

    return await agents._run(go())


@tool(
    "user_merge",
    "Fusiona el PR de un agente en su rama base (también la principal) cuando el usuario te lo pide en "
    "su mensaje. Solo funciona en un evento MENSAJE DE USUARIO; en los eventos de agentes usa vision_merge.",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
async def user_merge(args: dict) -> dict:
    from src.capabilities import prs
    from src.vision.session import in_user_turn

    async def go() -> str:
        if not in_user_turn():
            return "No fusiono: user_merge solo vale cuando el usuario lo pide en su mensaje."
        merged = await prs.merge(args["id"], by="Vision (a petición del usuario)")
        note = "" if merged["branch_deleted"] else " (la rama no se pudo borrar; no afecta a la fusión)"
        return (f"PR fusionado en {merged['repo']}/{merged['base_branch']}{note} "
                f"(commit {merged['pr_merge_sha'][:10]}; rollback posible).")

    return await agents._run(go())


@tool(
    "user_close",
    "Cierra SIN fusionar el PR de un agente (redundante o descartado) cuando el usuario te lo pide en su "
    "mensaje. La rama se queda en el repo. Solo funciona en un evento MENSAJE DE USUARIO.",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
async def user_close(args: dict) -> dict:
    from src.capabilities import prs
    from src.vision.session import in_user_turn

    async def go() -> str:
        if not in_user_turn():
            return "No cierro: user_close solo vale cuando el usuario lo pide en su mensaje."
        closed = await prs.close(args["id"])
        return f"PR de {closed['id']} cerrado sin fusionar; la rama {closed['branch']} sigue en {closed['repo']}."

    return await agents._run(go())


@tool(
    "vision_plan_edit",
    "Edita el plan de un proyecto en la rama de integración (develop), nunca en la principal: sustituye un "
    "texto exacto (copiado literal del fichero actual y que aparezca una sola vez) por otro y hace commit. "
    "Para marcar la casilla de un punto fusionado y anotar en una línea lo que hizo el agente. Nunca para "
    "decisiones de diseño que el usuario no haya aprobado.",
    {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "path": {"type": "string", "description": "PLAN.md (por defecto) o un .md de docs/plan/."},
            "old": {"type": "string", "description": "Texto actual, literal, que aparece una sola vez."},
            "new": {"type": "string", "description": "Texto que lo sustituye."},
            "message": {"type": "string", "description": "Resumen del commit (sin prefijo)."},
        },
        "required": ["repo", "old", "new", "message"],
    },
)
async def vision_plan_edit(args: dict) -> dict:
    from src.capabilities import prs

    async def go() -> str:
        sha = await prs.plan_edit(args["repo"], args.get("path") or "PLAN.md", args["old"], args["new"],
                                  args["message"])
        return f"Plan actualizado en {prs.integration_branch()} (commit {sha[:10]})."

    return await agents._run(go())


@tool(
    "vision_flag",
    "Algo de un agente necesita al usuario y no es una pregunta pendiente (p. ej. un PR que se desvía "
    "del plan o lo obliga a cambiar). Lo apunta en la bitácora y le avisa al móvil.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "reason": {"type": "string", "description": "Qué necesita decidir el usuario y qué opciones ves."},
        },
        "required": ["id", "reason"],
    },
)
async def vision_flag(args: dict) -> dict:
    async def go() -> str:
        agent = await agents._call("POST", f"/agents/{args['id']}/note",
                                   json={"text": f"Vision → usuario: {args['reason']}"})
        await _notify(args["reason"], title=f"Vision: {agent['repo']} necesita tu decisión")
        return "Apuntado; el usuario recibirá el aviso."

    return await agents._run(go())


async def _notify(message: str, title: str) -> None:
    """Aviso al móvil best-effort (lo importante queda en la bitácora o el registro)."""
    if os.getenv("NTFY_TOPIC", "").strip():
        from src.capabilities.notify import publish

        try:
            await publish(message[:400], title=title)
        except Exception:  # noqa: BLE001
            pass


@tool(
    "vision_start",
    "Lanza un agente (de código: parte de develop) para UN punto pendiente del plan del proyecto. Por "
    "iniciativa propia, solo puntos del plan, uno por agente; lo que no esté en el plan va a vision_propose. "
    "Si el usuario te pide un encargo concreto en su mensaje, plan_item = su petición. mode='read' solo "
    "cuando el usuario pide revisar o investigar sin tocar código. Reglas del encargo: "
    + agents.TASK_RULES,
    {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "plan_item": {"type": "string", "description": "Texto de la casilla '[ ]' del PLAN.md que cubre."},
            "task": {"type": "string", "description": "El encargo, con la plantilla de las reglas."},
            "mode": {"type": "string", "enum": ["code", "read"], "description": "Por defecto 'code'."},
        },
        "required": ["repo", "plan_item", "task"],
    },
)
async def vision_start(args: dict) -> dict:
    from src.vision.session import record_action

    async def go() -> str:
        if not settings.get("JARVIS_VISION_LAUNCH", os.getenv("JARVIS_VISION_LAUNCH", "1") != "0"):
            return "Lanzar agentes está desactivado para Vision (JARVIS_VISION_LAUNCH=0): no lo intentes más."
        if not args.get("plan_item", "").strip():
            return "Falta plan_item: Vision solo lanza puntos del plan."
        agent, sync = await agents.start_agent(args["repo"], args["task"], args.get("mode") or "code")
        await agents._call("POST", f"/agents/{agent['id']}/note",
                           json={"text": f"Lanzado por Vision · punto del plan: {args['plan_item']}"})
        record_action(args["repo"], agent["id"], args["task"], f"lanzó «{args['plan_item']}»", "LANZADO")
        vision_metrics.launched(agent["id"])
        return f"Agente {agent['id']} lanzado ({agent['state']}, rama {agent.get('branch')}). {sync}".strip()

    return await agents._run(go())


@tool(
    "vision_propose",
    "Propón al usuario una idea, mejora o desvío del plan que crees mejor (sin lanzar nada). Queda en el "
    "registro que Jarvis le cuenta y le llega un aviso al móvil. Hasta que decida, ese punto (y lo que "
    "dependa de él) queda bloqueado; sigue con lo demás.",
    {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "idea": {"type": "string", "description": "Qué propones, por qué y qué punto del plan bloquea."},
        },
        "required": ["repo", "idea"],
    },
)
async def vision_propose(args: dict) -> dict:
    from src.vision.session import record_action

    record_action(args["repo"], "-", "", "propuesta", f"PROPUESTA: {args['idea']}")
    await _notify(args["idea"], title=f"Vision propone ({args['repo']})")
    return agents._text("Propuesta apuntada; el usuario recibirá el aviso y decidirá.")


@tool(
    "vision_wait",
    "Apunta que el agente 'id' espera a que se fusione el trabajo del agente 'blocked_by' (ya en marcha "
    "o recién lanzado con vision_start). Mientras espera, su PR no se revisa; cuando el de blocked_by se "
    "fusione (o acabe sin fusionar) te llegará un evento DESBLOQUEO. Después, dile al agente con "
    "vision_answer que termine su turno y espere tu aviso.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "blocked_by": {"type": "string"},
            "reason": {"type": "string", "description": "Qué le falta, en una frase."},
        },
        "required": ["id", "blocked_by", "reason"],
    },
)
async def vision_wait(args: dict) -> dict:
    from src.vision.session import add_wait

    async def go() -> str:
        agent = await agents.get_agent(args["id"])
        blocker = await agents.get_agent(args["blocked_by"])
        if agent["repo"] != blocker["repo"]:
            return "Los dos agentes deben ser del mismo repositorio."
        if args["id"] == args["blocked_by"]:
            return "Un agente no puede esperarse a sí mismo."
        add_wait(args["id"], args["blocked_by"], args["reason"])
        await agents._call("POST", f"/agents/{args['id']}/note",
                           json={"text": f"Vision: espera al agente {args['blocked_by']} — {args['reason']}"})
        return f"Apuntado: {args['id']} espera a {args['blocked_by']}."

    return await agents._run(go())


_TOOLS = [
    project_file, project_list, vision_answer, pr_diff, vision_merge, vision_flag,
    vision_start, vision_propose, vision_wait, user_merge, user_close, vision_plan_edit,
    agents.agent_status, agents.agent_result, agents.agent_note, agents.agent_escalate,
    agents.agent_message, agents.agent_cancel,
]
TOOL_NAMES = [f"mcp__{SERVER_NAME}__{t.name}" for t in _TOOLS]


def build_server():
    """Servidor MCP en proceso con las herramientas de Vision."""
    # Envueltas para que las métricas sepan a qué agentes toca cada turno (coste por tarea).
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0",
                                 tools=[vision_metrics.tracked(t) for t in _TOOLS])
