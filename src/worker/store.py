"""Persistencia del runner de agentes (SQLite, stdlib).

Una tabla ``agents`` con el estado de cada agente. Las operaciones son pocas y
pequeñas, así que se usan conexiones síncronas cortas: no compensa un ORM ni un
driver async para esto.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

# Estados en los que el agente sigue "vivo" (cuenta para el TTL largo de la
# conversación y para "¿cómo van los agentes?").
ACTIVE_STATES = ("queued", "working", "waiting_input", "idle", "rate_limited")
# Estados finales: el runner ya no hará nada más con ellos.
FINAL_STATES = ("done", "merged", "failed", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    task        TEXT NOT NULL,
    mode        TEXT NOT NULL,
    state       TEXT NOT NULL,
    substate    TEXT,
    waiting_on  TEXT,
    question    TEXT,
    result      TEXT,
    error       TEXT,
    branch      TEXT,
    cost_usd    REAL DEFAULT 0,
    turns       INTEGER DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    idle_since  REAL
)
"""

# Columnas que se pueden actualizar con ``update`` (evita SQL con nombres arbitrarios).
_UPDATABLE = {
    "state", "substate", "waiting_on", "question", "result", "error", "branch",
    "cost_usd", "turns", "idle_since", "task",
}


class AgentStore:
    """Acceso a la tabla de agentes."""

    def __init__(self, path: Path) -> None:
        """Abre (o crea) la base de datos.

        Args:
            path: Ruta del fichero SQLite.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._lock = threading.Lock()
        with self._conn() as db:
            db.execute(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        """Conexión nueva con filas como dict."""
        db = sqlite3.connect(self._path)
        db.row_factory = sqlite3.Row
        return db

    def create(self, agent_id: str, repo: str, task: str, mode: str) -> dict:
        """Registra un agente nuevo en estado ``queued``."""
        now = time.time()
        with self._lock, self._conn() as db:
            db.execute(
                "INSERT INTO agents (id, repo, task, mode, state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (agent_id, repo, task, mode, now, now),
            )
        return self.get(agent_id)

    def update(self, agent_id: str, **fields) -> dict:
        """Actualiza campos de un agente (y su ``updated_at``)."""
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"Campos no actualizables: {bad}")
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn() as db:
            db.execute(f"UPDATE agents SET {cols} WHERE id = ?", (*fields.values(), agent_id))
        return self.get(agent_id)

    def get(self, agent_id: str) -> dict | None:
        """Un agente por id, o None."""
        with self._conn() as db:
            row = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return dict(row) if row else None

    def list(self, include_final: bool = True, limit: int = 50) -> list[dict]:
        """Agentes más recientes primero (los activos siempre se incluyen)."""
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM agents ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        agents = [dict(r) for r in rows]
        if not include_final:
            agents = [a for a in agents if a["state"] in ACTIVE_STATES]
        return agents

    def fail_orphans(self) -> int:
        """Marca como ``failed`` los agentes vivos de una ejecución anterior.

        Las sesiones del CLI no sobreviven a un reinicio del worker: un agente que
        figuraba trabajando ya no tiene proceso detrás.
        """
        marks = ",".join("?" for _ in ACTIVE_STATES)
        with self._lock, self._conn() as db:
            cur = db.execute(
                f"UPDATE agents SET state = 'failed', error = 'El worker se reinició.',"
                f" updated_at = ? WHERE state IN ({marks})",
                (time.time(), *ACTIVE_STATES),
            )
        return cur.rowcount
