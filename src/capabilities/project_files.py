"""Lectura de los proyectos desde sus bare repos (Hito 9, Fase 4).

La usan Jarvis (antes de delegar: leer el plan, resolver un proyecto por nombre)
y Vision (revisar preguntas y PRs contra el plan). Se lee del bare repo
(``git show <rama>:<ruta>``) y no del workspace montado, que puede ir atrasado
respecto a lo empujado; por defecto en la rama de integración (``develop``) si
existe, donde está el trabajo autónomo más reciente.

- ``project_file``: un fichero (por defecto ``PLAN.md``) o el listado de una carpeta.
- ``project_list``: la tabla "Proyectos" del ``CLAUDE.md`` de dotclaude (nombre,
  carpeta, qué es, dónde corre) más los repos que hay en el servidor git, para
  resolver nombres aproximados ("la oficina" → ``the-office``).

Config por entorno:
  JARVIS_GIT_ROOT          Carpeta de los bare repos (``<repo>.git``).
  JARVIS_PROJECTS_SOURCE   ``<repo>:<ruta>`` del índice de proyectos (def. ``dotclaude:CLAUDE.md``).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path

from claude_agent_sdk import tool

# Tope de lo que devuelve project_file (caracteres): un PLAN.md grande cabe de sobra.
_MAX_FILE = 60_000
_REPO_RE = re.compile(r"^[\w.-]+$")


def _bare(repo: str) -> Path:
    """Ruta del bare repo de ``repo`` en ``JARVIS_GIT_ROOT``.

    Raises:
        RuntimeError: Si el nombre no es válido o el repo no existe.
    """
    root = os.getenv("JARVIS_GIT_ROOT", "").strip()
    if not _REPO_RE.match(repo) or not root:
        raise RuntimeError(f"Repo no válido: '{repo}'.")
    path = Path(root) / f"{repo}.git"
    if not path.is_dir():
        raise RuntimeError(f"No encuentro el repo '{repo}'.")
    return path


def _git(bare: Path, *args: str) -> subprocess.CompletedProcess:
    """git de solo lectura sobre el bare."""
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "--git-dir", str(bare), *args],
        capture_output=True, text=True, timeout=30,
    )


def default_ref(bare: Path) -> str:
    """Rama que lee Vision: ``develop`` si existe (trabajo autónomo), si no HEAD."""
    return "develop" if _git(bare, "rev-parse", "--verify", "-q", "refs/heads/develop").returncode == 0 else "HEAD"


def read_project_file(repo: str, path: str = "PLAN.md", ref: str = "") -> str:
    """Contenido de un fichero (o listado de una carpeta) del bare repo.

    Raises:
        RuntimeError: Si el repo, la rama o la ruta no existen.
    """
    bare = _bare(repo)
    ref = ref or default_ref(bare)
    path = path.strip().strip("/")
    spec = f"{ref}:{path}"
    kind = _git(bare, "cat-file", "-t", spec)
    if kind.returncode != 0:
        raise RuntimeError(f"No existe '{path or '/'}' en {repo}@{ref}.")
    if kind.stdout.strip() == "tree":
        listing = _git(bare, "ls-tree", "--name-only", spec).stdout.strip()
        return f"Carpeta {path or '/'} de {repo}@{ref}:\n{listing}"
    text = _git(bare, "show", spec).stdout
    if len(text) > _MAX_FILE:
        text = text[:_MAX_FILE] + f"\n… (recortado: {len(text)} caracteres en total)"
    return f"{repo}@{ref}:{path}\n{text}"


def _text(message: str) -> dict:
    """Envuelve un texto en el formato de respuesta que espera el SDK MCP."""
    return {"content": [{"type": "text", "text": message}]}


@tool(
    "project_file",
    "Lee un fichero de un proyecto (por defecto su PLAN.md) desde el repositorio central, en la rama "
    "de trabajo autónomo (develop) si existe o en la principal. Si la ruta es una carpeta, la lista. "
    "Úsala para comprobar qué dice el plan antes de responder a un agente.",
    {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Nombre del repositorio, p. ej. 'the-office'."},
            "path": {"type": "string", "description": "Ruta dentro del repo. Por defecto PLAN.md; '' = raíz."},
            "ref": {"type": "string", "description": "Rama o commit. Por defecto develop o la principal."},
        },
        "required": ["repo"],
    },
)
async def project_file(args: dict) -> dict:
    try:
        path = args.get("path")
        text = await asyncio.to_thread(
            read_project_file, args["repo"], "PLAN.md" if path is None else path, args.get("ref") or ""
        )
    except (RuntimeError, subprocess.SubprocessError) as exc:
        text = f"Error: {exc}"
    return _text(text)


def _project_list() -> str:
    """Tabla de proyectos del índice + repos disponibles en el servidor git."""
    source = os.getenv("JARVIS_PROJECTS_SOURCE", "dotclaude:CLAUDE.md")
    repo, _, path = source.partition(":")
    lines: list[str] = []
    try:
        text = read_project_file(repo, path or "CLAUDE.md", "HEAD")
        section = text.split("## Proyectos", 1)[1] if "## Proyectos" in text else ""
        section = re.split(r"^## ", section, maxsplit=1, flags=re.MULTILINE)[0]
        lines = [ln for ln in section.splitlines() if ln.startswith("|")]
    except RuntimeError as exc:
        lines = [f"(Sin índice de proyectos: {exc})"]
    root = os.getenv("JARVIS_GIT_ROOT", "").strip()
    repos = sorted(p.name[:-4] for p in Path(root).glob("*.git")) if root else []
    return ("\n".join(lines) + "\nRepos en el servidor git (nombre para project_file y agent_start): "
            + ", ".join(repos))


@tool(
    "project_list",
    "Lista de los proyectos de desarrollo del usuario (nombre, carpeta, qué es, dónde corre) y los "
    "repositorios disponibles. Úsala para saber a qué repositorio se refiere cuando nombra un proyecto, "
    "aunque sea de forma aproximada.",
    {"type": "object", "properties": {}},
)
async def project_list(args: dict) -> dict:
    return _text(await asyncio.to_thread(_project_list))


TOOLS = [project_file, project_list]
