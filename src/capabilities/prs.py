"""PRs de los agentes vistos desde el núcleo: diff y fusión (Hito 9, Fase 3).

Los PR ligeros (D5) viven en el worker (registro en SQLite) y en el bare repo
(la rama ``agent/*``). La fusión la hace el núcleo y no el worker: el hook del
bare solo deja al UID del worker empujar ramas ``agent/*`` (D12), y así debe
seguir, porque ese UID es también el de los agentes.

Cómo se fusiona (``merge``):
  1. El worker confirma que el agente está parado (``merge-check``).
  2. Clon temporal del bare, ``merge --no-ff`` de la rama sobre la base. Si hay
     conflicto no se toca nada: se le puede pedir al agente que rebase.
  3. En el mismo commit se borra el ``<rama>.md`` del handoff (D10): es estado
     del agente, no del proyecto; queda en SQLite y en el historial de la rama.
  4. Push de la base y borrado de la rama. Nada se pierde: el merge commit tiene
     como 1.er padre la base anterior y como 2.º la punta de la rama, y los tres
     SHA se guardan en el worker para el rollback.

Rama de integración (Fase 4, tarea (b)): el trabajo autónomo va a ``develop``
(``JARVIS_INTEGRATION_BRANCH``), nunca a la principal. ``ensure_integration``
la crea si falta y le trae la principal antes de lanzar un agente de código;
Vision fusiona ahí los PR alineados con el plan, y el usuario aprueba al final un
único PR ``develop`` → principal.

Requisitos: los bare de los agentes con escritura en ``JARVIS_AGENT_GIT_ROOT`` (en Docker,
``/repos``: la misma carpeta que ve el worker, Fase 6 bloque 5). Si no está, ``JARVIS_GIT_ROOT``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from src.capabilities import agents

# Tope del diff que se manda a la web (un PR de agente no debería acercarse).
_MAX_DIFF = 400_000
# Una fusión a la vez: dos a la vez sobre el mismo bare se pisarían el push.
_merge_lock = threading.Lock()


def _bare(repo: str) -> Path:
    """Ruta del bare repo de ``repo``.

    Raises:
        RuntimeError: Si no hay carpeta de repos o el repo no existe.
    """
    # Primero la carpeta de los agentes (con escritura); si no, la de todos los repos.
    for var in ("JARVIS_AGENT_GIT_ROOT", "JARVIS_GIT_ROOT"):
        root = os.getenv(var, "").strip()
        if root and (Path(root) / f"{repo}.git").is_dir():
            return Path(root) / f"{repo}.git"
    raise RuntimeError(f"No encuentro el repo '{repo}' (JARVIS_AGENT_GIT_ROOT / JARVIS_GIT_ROOT).")


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    """Ejecuta git (identidad de Jarvis) y devuelve stdout.

    Raises:
        RuntimeError: Con el stderr de git si falla y ``check`` es True.
    """
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Jarvis",
        "GIT_AUTHOR_EMAIL": "jarvis@localhost",
        "GIT_COMMITTER_NAME": "Jarvis",
        "GIT_COMMITTER_EMAIL": "jarvis@localhost",
    }
    proc = subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def integration_branch() -> str:
    """Rama de integración del trabajo autónomo ('' = desactivada)."""
    return os.getenv("JARVIS_INTEGRATION_BRANCH", "develop").strip()


def branch_tip(repo: str, branch: str) -> str:
    """Commit al que apunta ``branch`` en el bare ('' si no existe)."""
    return _git("--git-dir", str(_bare(repo)), "rev-parse", "--verify", "--quiet",
                f"refs/heads/{branch}", check=False)


def _main_branch(bare: Path) -> str:
    """Rama principal del bare (a la que apunta su HEAD)."""
    return _git("--git-dir", str(bare), "symbolic-ref", "--short", "HEAD")


def _ensure_integration(repo: str) -> str:
    """Crea la rama de integración si falta y le trae la principal.

    Returns:
        Qué se hizo, en una frase ('' si nada).

    Raises:
        RuntimeError: Si traer la principal da conflicto (el bare no cambia).
    """
    branch = integration_branch()
    if not branch:
        return ""
    bare = _bare(repo)
    main = _main_branch(bare)
    if branch == main:
        return ""
    if not _branch_exists(bare, branch):
        _git("--git-dir", str(bare), "branch", branch, main)
        return f"Creada la rama {branch} desde {main}."
    behind = int(_git("--git-dir", str(bare), "rev-list", "--count", f"{branch}..{main}"))
    if not behind:
        return ""
    with _merge_lock, tempfile.TemporaryDirectory(prefix="jarvis-sync-") as tmp:
        work = Path(tmp) / "repo"
        _git("clone", "--quiet", "--branch", branch, str(bare), str(work))
        before = _git("rev-parse", "HEAD", cwd=work)
        _git("merge", "--no-edit", "-m", f"Merge {main} en {branch} (Jarvis)", f"origin/{main}",
             cwd=work, check=False)
        conflicts = _git("diff", "--name-only", "--diff-filter=U", cwd=work)
        if conflicts:
            raise RuntimeError(
                f"Conflicto al traer {main} a {branch} en: " + ", ".join(conflicts.splitlines())
                + ". Tiene que resolverlo el usuario."
            )
        _git("push", "--quiet", f"--force-with-lease={branch}:{before}", "origin", f"HEAD:refs/heads/{branch}",
             cwd=work)
    return f"Traídos {behind} commits de {main} a {branch}."


async def ensure_integration(repo: str) -> str:
    """Versión async de ``_ensure_integration`` (git bloqueante en un hilo)."""
    return await asyncio.to_thread(_ensure_integration, repo)


def _branch_exists(bare: Path, branch: str) -> bool:
    """True si la rama sigue en el bare."""
    return bool(_git("--git-dir", str(bare), "branch", "--list", branch))


# --------------------------------------------------------------------------- #
# Lectura (BLOQUEANTE — vía asyncio.to_thread)
# --------------------------------------------------------------------------- #
def _diff(agent: dict) -> str:
    """Diff completo del PR de un agente.

    Abierto: ``base...rama`` en el bare. Fusionado: lo que metió el merge
    (``merge^1..merge``), porque la rama ya no existe.
    """
    bare = _bare(agent["repo"])
    if agent.get("pr_status") == "merged" and agent.get("pr_merge_sha"):
        sha = agent["pr_merge_sha"]
        out = _git("--git-dir", str(bare), "diff", f"{sha}^1", sha)
    elif agent.get("branch") and _branch_exists(bare, agent["branch"]):
        out = _git("--git-dir", str(bare), "diff", f"{agent['base_branch']}...{agent['branch']}")
    else:
        return ""
    if len(out) > _MAX_DIFF:
        out = out[:_MAX_DIFF] + "\n\n[… diff recortado …]"
    return out


# --------------------------------------------------------------------------- #
# Fusión (BLOQUEANTE — vía asyncio.to_thread)
# --------------------------------------------------------------------------- #
def _merge(agent: dict, by: str = "Jarvis") -> dict:
    """Fusiona la rama del agente en su base y borra la rama.

    Returns:
        ``base_sha``, ``branch_sha`` y ``merge_sha`` (para el rollback) y
        ``branch_deleted``.

    Raises:
        RuntimeError: Si hay conflicto o falla git; en ese caso el bare no cambia.
    """
    bare = _bare(agent["repo"])
    branch, base = agent["branch"], agent["base_branch"]
    with _merge_lock, tempfile.TemporaryDirectory(prefix="jarvis-merge-") as tmp:
        work = Path(tmp) / "repo"
        _git("clone", "--quiet", "--branch", base, str(bare), str(work))
        _git("fetch", "--quiet", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=work)
        base_sha = _git("rev-parse", "HEAD", cwd=work)
        branch_sha = _git("rev-parse", f"origin/{branch}", cwd=work)
        if int(_git("rev-list", "--count", f"HEAD..origin/{branch}", cwd=work)) == 0:
            raise RuntimeError("La rama no tiene cambios respecto a la base.")

        merged = _git("merge", "--no-ff", "--no-commit", f"origin/{branch}", cwd=work, check=False)
        conflicts = _git("diff", "--name-only", "--diff-filter=U", cwd=work)
        if conflicts:
            raise RuntimeError(
                "Conflicto al fusionar en: " + ", ".join(conflicts.splitlines())
                + ". Pídele al agente que actualice su rama con la base."
            )
        if not _git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=work, check=False):
            raise RuntimeError(f"No se pudo preparar la fusión: {merged}")

        # Handoff del agente (D10): fuera de la base, solo si la base no lo tenía.
        handoff = f"{branch}.md"
        if (work / handoff).exists() and not _git(
            "ls-tree", "--name-only", base_sha, "--", handoff, cwd=work
        ):
            _git("rm", "--quiet", "-f", "--", handoff, cwd=work)

        title = agent.get("pr_title") or agent["task"].splitlines()[0]
        message = (
            f"Merge PR del agente {agent['id']}: {title}\n\n"
            f"Rama {branch} ({branch_sha[:10]}), fusionada por {by}.\n"
            f"Rollback: git revert -m 1 <este commit>, o volver a {base_sha[:10]}."
        )
        _git("commit", "--quiet", "-m", message, cwd=work)
        merge_sha = _git("rev-parse", "HEAD", cwd=work)
        # Push de la base con lease: si alguien empujó entre medias, falla sin pisar nada.
        _git("push", "--quiet", f"--force-with-lease={base}:{base_sha}", "origin", f"HEAD:refs/heads/{base}",
             cwd=work)

        # La rama ya está contenida en el merge: borrarla no pierde nada. Si falla,
        # la fusión sigue siendo válida (solo queda una rama de más).
        deleted = True
        try:
            _git("push", "--quiet", "origin", "--delete", branch, cwd=work)
        except RuntimeError:
            deleted = False
    return {"base_sha": base_sha, "branch_sha": branch_sha, "merge_sha": merge_sha,
            "branch_deleted": deleted}


# --------------------------------------------------------------------------- #
# API async (la usan la herramienta pr_merge y las rutas /api/prs)
# --------------------------------------------------------------------------- #
async def list_prs() -> list[dict]:
    """Agentes con PR (abierto, fusionado o cerrado), más recientes primero."""
    return [a for a in await agents.list_agents() if a.get("pr_status")]


async def diff(agent_id: str) -> dict:
    """PR de un agente con su diff completo."""
    agent = await agents.get_agent(agent_id)
    if not agent.get("pr_status"):
        raise RuntimeError(f"El agente {agent_id} no tiene PR.")
    return {**agent, "diff": await asyncio.to_thread(_diff, agent)}


async def merge(agent_id: str, by: str = "Jarvis") -> dict:
    """Fusiona el PR de un agente y lo marca como ``merged`` en el worker.

    Returns:
        El agente actualizado más ``branch_deleted``.

    Raises:
        RuntimeError: Si no se puede fusionar (estado, conflicto, git…).
    """
    agent = await agents.merge_check(agent_id)
    shas = await asyncio.to_thread(_merge, agent, by)
    deleted = shas.pop("branch_deleted")
    updated = await agents.mark_merged(agent_id, shas)
    return {**updated, "branch_deleted": deleted}


def _plan_edit(repo: str, path: str, old: str, new: str, message: str) -> str:
    """Sustituye ``old`` (una sola aparición) por ``new`` en un .md del plan, en la rama de integración.

    Solo ``PLAN.md`` y ``docs/plan/*.md``; nunca la rama principal (el plan lo
    actualiza Vision en develop; a la principal llega con el PR final del usuario).

    Returns:
        El commit nuevo.

    Raises:
        RuntimeError: Ruta no permitida, sin rama de integración, texto ausente o
            repetido, o fallo de git (el bare no cambia).
    """
    norm = Path(path).as_posix()
    if ".." in Path(norm).parts or not (
        norm == "PLAN.md" or (norm.startswith("docs/plan/") and norm.endswith(".md"))
    ):
        raise RuntimeError("Solo se puede editar PLAN.md o un .md de docs/plan/.")
    branch = integration_branch()
    bare = _bare(repo)
    if not branch or not _branch_exists(bare, branch):
        raise RuntimeError(f"El repo '{repo}' no tiene rama de integración: el plan no se toca.")
    with _merge_lock, tempfile.TemporaryDirectory(prefix="jarvis-plan-") as tmp:
        work = Path(tmp) / "repo"
        _git("clone", "--quiet", "--branch", branch, str(bare), str(work))
        target = work / norm
        if not target.is_file():
            raise RuntimeError(f"No existe {norm} en {branch}.")
        text = target.read_text(encoding="utf-8")
        found = text.count(old) if old else 0
        if found != 1:
            raise RuntimeError(
                f"El texto a sustituir aparece {found} veces en {norm}: tiene que aparecer exactamente una "
                "(cópialo literal del fichero actual, con más contexto si se repite)."
            )
        before = _git("rev-parse", "HEAD", cwd=work)
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        _git("commit", "--quiet", "-am", f"docs(plan): {message.strip() or 'actualiza el plan'}", cwd=work)
        _git("push", "--quiet", f"--force-with-lease={branch}:{before}", "origin", f"HEAD:refs/heads/{branch}",
             cwd=work)
        return _git("rev-parse", "HEAD", cwd=work)


async def plan_edit(repo: str, path: str, old: str, new: str, message: str) -> str:
    """Versión async de ``_plan_edit`` (la usa la herramienta ``vision_plan_edit``)."""
    return await asyncio.to_thread(_plan_edit, repo, path, old, new, message)


async def close(agent_id: str) -> dict:
    """Cierra el PR de un agente sin fusionar (redundante o descartado).

    No toca el bare: la rama se queda y su punta se guarda en ``pr_branch_sha``.

    Raises:
        RuntimeError: Si no tiene PR abierto o el agente sigue trabajando.
    """
    return await agents.mark_closed(agent_id)


def summary(agent: dict) -> str:
    """Resumen del PR para pedir confirmación (título, tests, cambios)."""
    tests = {1: "los tests pasan", 0: "LOS TESTS FALLAN"}.get(agent.get("pr_tests_passed"), "sin tests informados")
    return (
        f"PR del agente {agent['id']} en {agent['repo']} ({agent.get('branch')} → {agent.get('base_branch')}): "
        f"{agent.get('pr_title')}. {tests.capitalize()}.\nCambios:\n{agent.get('pr_diffstat') or ''}"
    )
