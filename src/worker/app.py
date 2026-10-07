"""API HTTP del worker de agentes (la consume la capacidad ``agents`` del núcleo).

Escucha solo en localhost (el puerto se publica en 127.0.0.1) y además exige el
token compartido ``JARVIS_WORKER_TOKEN`` en la cabecera ``X-Jarvis-Token``: en
``network_mode: host`` cualquier proceso del host llega a 127.0.0.1.

Endpoints:
    GET  /health
    GET  /agents                    lista (?active=1 solo los vivos)
    POST /agents                    {"repo": str, "task": str}
    GET  /agents/{id}
    POST /agents/{id}/message       {"text": str}
    POST /agents/{id}/cancel
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


class Message(BaseModel):
    """Cuerpo de POST /agents/{id}/message."""

    text: str


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
    try:
        return manager.create(body.repo, body.task)
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
        return await manager.message(agent_id, body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/agents/{agent_id}/cancel", dependencies=[Depends(_auth)])
async def cancel_agent(agent_id: str) -> dict:
    """Cancela un agente."""
    try:
        return await manager.cancel(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host=os.getenv("JARVIS_WORKER_HOST", "0.0.0.0"),
                port=int(os.getenv("JARVIS_WORKER_PORT", "8113")))
