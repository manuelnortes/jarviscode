"""Pull requests de los agentes.

``PRProvider`` es la interfaz; ``LocalPRProvider`` la implementación "PR ligero"
(D5): la rama del agente se empuja al bare repo y el PR es un registro en SQLite
(título, descripción, diffstat, resultado de tests) que Jarvis resume y avisa.

TODO (D5): ``GitHubPRProvider`` / ``BitbucketPRProvider`` — mismo contrato:
empujar la rama al remoto de la forja y abrir/actualizar el PR con su API.
"""

from __future__ import annotations

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
            pr: Datos del PR que dio el agente (``title``, ``body``,
                ``tests_passed``, ``tests_output``); pueden faltar.

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
        tests = pr.get("tests_passed")
        return {
            "pr_status": "open",
            "pr_title": title,
            "pr_body": pr.get("body") or agent.get("pr_body") or "",
            "pr_tests_passed": None if tests is None else int(bool(tests)),
            "pr_tests_output": (pr.get("tests_output") or agent.get("pr_tests_output") or "")[-4000:],
            "pr_diffstat": diffstat[-4000:],
        }
