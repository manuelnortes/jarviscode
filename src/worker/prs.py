"""Pull requests de los agentes.

``PRProvider`` es la interfaz; ``LocalPRProvider`` la implementación "PR ligero"
(D5): la rama del agente se empuja al bare repo y el PR es un registro en SQLite
(título, descripción, diffstat, resultado de tests) que Jarvis resume y avisa.

TODO (D5): ``GitHubPRProvider`` / ``BitbucketPRProvider`` — mismo contrato:
empujar la rama al remoto de la forja y abrir/actualizar el PR con su API.
"""

from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path


def git(workdir: Path, *args: str, check: bool = True) -> str:
    """Ejecuta git en ``workdir`` y devuelve stdout.

    Raises:
        RuntimeError: Con el stderr de git si falla y ``check`` es True.
    """
    proc = subprocess.run(
        ["git", *args], cwd=workdir, capture_output=True, text=True, timeout=120
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def commits_ahead(workdir: Path, base: str) -> int:
    """Commits de la rama actual que no están en ``origin/<base>``."""
    return int(git(workdir, "rev-list", "--count", f"origin/{base}..HEAD") or 0)


class PRProvider(ABC):
    """Publica el trabajo de un agente como pull request."""

    @abstractmethod
    def publish(self, workdir: Path, agent: dict, pr: dict) -> dict:
        """Empuja la rama y crea o actualiza el PR.

        Args:
            workdir: Clon de trabajo del agente (en su rama).
            agent: Registro del agente (``branch``, ``base_branch``, ``task``…).
            pr: Datos del PR que dio el agente (``title``, ``body``); pueden faltar.
                Los tests no: los ejecuta el runner (``run_suite``).

        Returns:
            Campos a guardar en el registro del agente (``pr_*``).
        """


class LocalPRProvider(PRProvider):
    """PR ligero: rama en el bare repo + registro en SQLite."""

    def publish(self, workdir: Path, agent: dict, pr: dict) -> dict:
        """Empuja ``agent['branch']`` a origin y devuelve los campos del PR.

        El push es forzado a propósito: la rama es solo del agente (puede haber
        reescrito su historia con un amend) y el hook del bare impide que el
        worker toque nada fuera de ``agent/*``.
        """
        branch, base = agent["branch"], agent["base_branch"]
        git(workdir, "push", "--force", "origin", f"HEAD:refs/heads/{branch}")
        diffstat = git(workdir, "diff", "--stat", f"origin/{base}...HEAD")
        title = (pr.get("title") or agent.get("pr_title") or agent["task"].splitlines()[0])[:120]
        return {
            "pr_status": "open",
            "pr_title": title,
            "pr_body": pr.get("body") or agent.get("pr_body") or "",
            "pr_diffstat": diffstat[-4000:],
        }


# Variables que la suite no necesita y no debe ver (corre código del repo).
_SUITE_HIDDEN_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "JARVIS_WORKER_TOKEN")
_SUITE_TIMEOUT = 600


def run_suite(workdir: Path) -> tuple[int, str]:
    """Ejecuta la suite del proyecto en el clon del agente (``.venv/bin/pytest``).

    El verde de un PR sale de aquí, no de lo que diga el agente. Si falta el
    ``.venv`` se crea con ``requirements.txt`` (+ pytest).

    Returns:
        ``(passed, output)``: 1 en verde (o sin tests que recoger: un repo sin
        suite no bloquea), 0 si falla, no arranca o se pasa de tiempo; y el final
        de la salida.
    """
    env = {k: v for k, v in os.environ.items() if k not in _SUITE_HIDDEN_ENV}
    venv = workdir / ".venv"

    def run(*cmd: str, timeout: int = _SUITE_TIMEOUT) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=timeout)

    try:
        if not (venv / "bin" / "pytest").exists():
            made = run("python", "-m", "venv", str(venv))
            reqs = ["-r", "requirements.txt"] if (workdir / "requirements.txt").exists() else []
            if made.returncode == 0:
                made = run(str(venv / "bin" / "pip"), "install", "-q", *reqs, "pytest")
            if made.returncode != 0:
                return 0, f"No se pudo preparar el entorno de tests:\n{(made.stderr or made.stdout)[-3000:]}"
        proc = run(str(venv / "bin" / "pytest"), "-q", "-p", "no:cacheprovider")
    except subprocess.TimeoutExpired:
        return 0, f"La suite superó {_SUITE_TIMEOUT} s y se cortó."
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 5:  # pytest: no ha recogido ningún test
        return 1, "El repo no tiene tests (pytest no recogió ninguno)."
    return int(proc.returncode == 0), output[-4000:]
