"""API HTTP del worker de agentes (la consume la capacidad ``agents`` del núcleo).

Escucha solo en localhost (el puerto se publica en 127.0.0.1) y además exige el
token compartido ``JARVIS_WORKER_TOKEN`` en la cabecera ``X-Jarvis-Token``: en
``network_mode: host`` cualquier proceso del host llega a 127.0.0.1.

Endpoints:
    GET  /health
    GET  /agents                    lista (?active=1 solo los vivos)
    GET  /usage                     último estado del límite de uso de la suscripción
    POST /agents                    {"repo": str, "task": str, "mode": "read"|"code"}
    GET  /agents/{id}
    POST /agents/{id}/message       {"text": str, "by"?: str}
    POST /agents/{id}/note          {"text": str} → bitácora del gestor
    POST /agents/{id}/escalate      la pregunta pendiente pasa al usuario
    POST /agents/{id}/cancel
    GET  /agents/{id}/merge-check   ¿se puede fusionar su PR ahora? (lo fusiona el núcleo)
    POST /agents/{id}/merged        {"base_sha", "branch_sha", "merge_sha"} tras fusionar
    POST /agents/{id}/closed        cierra su PR sin fusionar (la rama se queda)
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from src.worker.manager import manager_from_env

_TOKEN = os.getenv("JARVIS_WORKER_TOKEN", "")
manager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Crea el gestor al arrancar y cierra las sesiones al apagar."""
    global manager
    if not _TOKEN:
        raise RuntimeError("Falta JARVIS_WORKER_TOKEN: el worker no arranca sin token.")
    manager = manager_from_env()
    manager.start()
    try:
        yield
    finally:
        await manager.shutdown()


app = FastAPI(title="Jarvis worker", lifespan=lifespan)


def _auth(x_jarvis_token: str = Header(default="")) -> None:
    """Comprueba el token compartido (comparación en tiempo constante)."""
    if not secrets.compare_digest(x_jarvis_token, _TOKEN):
        raise HTTPException(status_code=401, detail="Token inválido.")


class NewAgent(BaseModel):
    """Cuerpo de POST /agents."""

    repo: str
    task: str
    mode: str = "read"


class Message(BaseModel):
    """Cuerpo de POST /agents/{id}/message."""

    text: str
    # Quién responde (p. ej. "vision"); queda en la bitácora. Vacío = Jarvis/usuario.
    by: str = ""


@app.get("/health")
async def health() -> dict:
    """Sonda de vida (sin token, para el healthcheck)."""
    return {"status": "ok", "service": "jarvis-worker"}


@app.get("/agents", dependencies=[Depends(_auth)])
async def list_agents(active: int = 0) -> list[dict]:
    """Lista de agentes, más recientes primero."""
    return manager.store.list(include_final=not active)


@app.post("/agents", dependencies=[Depends(_auth)])
async def create_agent(body: NewAgent) -> dict:
    """Encola un agente nuevo."""
    # Lectura fresca del uso antes de decidir: sin agentes vivos la sonda periódica no corre.
    await manager.refresh_usage()
    try:
        return manager.create(body.repo, body.task, body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/agents/{agent_id}", dependencies=[Depends(_auth)])
async def get_agent(agent_id: str) -> dict:
    """Detalle de un agente (incluido su último resultado)."""
    agent = manager.store.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="No existe ese agente.")
    return agent


@app.post("/agents/{agent_id}/message", dependencies=[Depends(_auth)])
async def message_agent(agent_id: str, body: Message) -> dict:
    """Mensaje a un agente (seguimiento, aclaración…)."""
    try:
        return await manager.message(agent_id, body.text, by=body.by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/agents/{agent_id}/note", dependencies=[Depends(_auth)])
async def note_agent(agent_id: str, body: Message) -> dict:
    """Apunta una decisión en la bitácora del gestor del agente."""
    try:
        return manager.note(agent_id, body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/agents/{agent_id}/escalate", dependencies=[Depends(_auth)])
async def escalate_agent(agent_id: str) -> dict:
    """El gestor pasa al usuario la pregunta pendiente de un agente."""
    try:
        return manager.escalate(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/usage", dependencies=[Depends(_auth)])
async def usage() -> dict:
    """Último estado conocido del límite de uso (utilización, reinicio)."""
    # Sin agentes vivos la sonda periódica no corre: tras un reinicio el uso salía
    # "desconocido" hasta lanzar algo. Sondea aquí si la lectura es vieja (cada probe_every s).
    await manager.refresh_usage()
    return manager.usage()


@app.get("/metrics", dependencies=[Depends(_auth)])
async def metrics(last: int = 50) -> dict:
    """Métricas por turno (coste y uso antes/después) y su resumen."""
    return manager.metrics(last)


@app.post("/agents/{agent_id}/cancel", dependencies=[Depends(_auth)])
async def cancel_agent(agent_id: str) -> dict:
    """Cancela un agente."""
    try:
        return await manager.cancel(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class Merged(BaseModel):
    """Cuerpo de POST /agents/{id}/merged (SHA para el rollback)."""

    base_sha: str
    branch_sha: str
    merge_sha: str


@app.get("/agents/{agent_id}/merge-check", dependencies=[Depends(_auth)])
async def merge_check(agent_id: str) -> dict:
    """409 con el motivo si el PR del agente no se puede fusionar ahora."""
    try:
        return manager.merge_check(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/agents/{agent_id}/merged", dependencies=[Depends(_auth)])
async def mark_merged(agent_id: str, body: Merged) -> dict:
    """El núcleo avisa de que ha fusionado el PR del agente."""
    try:
        return await manager.mark_merged(agent_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/agents/{agent_id}/closed", dependencies=[Depends(_auth)])
async def mark_closed(agent_id: str) -> dict:
    """Cierra el PR del agente sin fusionar (la rama se queda en el bare)."""
    try:
        return await manager.mark_closed(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host=os.getenv("JARVIS_WORKER_HOST", "0.0.0.0"),
                port=int(os.getenv("JARVIS_WORKER_PORT", "8113")))
