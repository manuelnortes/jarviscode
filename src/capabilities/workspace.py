"""Capacidad de workspace: estado de los proyectos y notas por voz.

Da a Jarvis acceso al workspace de desarrollo del usuario (la carpeta con todos
sus proyectos) para responder cosas como "¿en qué me quedé con X?", "¿qué tengo
pendiente?" o "¿hay algo sin subir?", y le deja apuntar notas rápidas en una
sección **Inbox** de un ``TODO.md`` versionado en git.

Diseño (Hito 8):
  - **Herramientas propias en vez de Read/Bash genéricos.** Cada pregunta típica
    se responde con UNA llamada: por voz, cada ronda de herramientas son segundos
    de espera.
  - **Lectura** sobre el workspace montado en solo lectura. Los working copies se
    consultan con ``GIT_OPTIONAL_LOCKS=0`` para que ``git status`` no intente
    escribir el índice (fallaría en un montaje ro).
  - **Escritura acotada**: solo las líneas del Inbox marcadas con
    ``<!-- j:<id> -->`` (las que creó Jarvis). Jarvis trabaja en su propio clon del
    repo de notas, del que es el único escritor: antes de cada cambio lo alinea
    con el remoto (``reset --hard @{u}``), aplica el cambio, hace commit y push.
    Si el push se rechaza (otro equipo empujó entre medias) se repite una vez.
    Nunca se reescribe historial: cada corrección es un commit nuevo.

Config por entorno:
  JARVIS_WORKSPACE     Carpeta del workspace (ro). Sin ella la capacidad no se registra.
  JARVIS_GIT_ROOT      Carpeta con los bare repos (``<proyecto>.git``), para la
                       actividad reciente de todos los equipos. Opcional.
  JARVIS_NOTES_REMOTE  Remoto git del repo de notas. Sin él no hay herramientas de notas.
  JARVIS_NOTES_DIR     Clon de trabajo de las notas (se crea solo).
  JARVIS_NOTES_FILE    Fichero de notas dentro del repo (por defecto TODO.md).
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import subprocess
import threading
import unicodedata
from datetime import date
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server, tool

# Cabecera de la sección donde Jarvis apunta. Si no existe, se crea antes de la
# primera sección "## " del fichero de notas.
INBOX_HEADING = "## 📥 Inbox (notas de Jarvis)"
_INBOX_BLURB = (
    "> Notas dictadas a Jarvis. Repasarlas y moverlas a su sitio "
    "(PLAN.md del proyecto o una sección de abajo)."
)

# Marcador de id al final de cada nota. Es un comentario HTML: invisible al
# renderizar el Markdown, pero permite a Jarvis localizar SUS líneas.
_ID_RE = re.compile(r"<!--\s*j:([0-9a-f]{4,8})\s*-->")

# Recortes para no inundar el contexto (y la respuesta hablada).
_MAX_SECTION_CHARS = 3500
_MAX_TASKS = 30
_MAX_COMMITS_PER_REPO = 10

# Serializa las escrituras: dos notas a la vez sobre el mismo clon se pisarían.
_notes_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
def _workspace() -> Path | None:
    """Carpeta del workspace, o None si la capacidad no está configurada."""
    raw = os.getenv("JARVIS_WORKSPACE", "").strip()
    return Path(raw) if raw and Path(raw).is_dir() else None


def _git_root() -> Path | None:
    """Carpeta de bare repos, o None si no está configurada."""
    raw = os.getenv("JARVIS_GIT_ROOT", "").strip()
    return Path(raw) if raw and Path(raw).is_dir() else None


def _notes_remote() -> str:
    """Remoto del repo de notas ('' si las notas están desactivadas)."""
    return os.getenv("JARVIS_NOTES_REMOTE", "").strip()


def _notes_dir() -> Path:
    """Clon de trabajo de las notas (en Docker, dentro del volumen de datos)."""
    default = Path(__file__).resolve().parents[2] / "data" / "notes"
    return Path(os.getenv("JARVIS_NOTES_DIR", str(default)))


def _notes_file() -> Path:
    """Ruta del fichero de notas dentro del clon."""
    return _notes_dir() / os.getenv("JARVIS_NOTES_FILE", "TODO.md")


def is_enabled() -> bool:
    """True si hay workspace configurado (condición para registrar la capacidad)."""
    return _workspace() is not None


def notes_enabled() -> bool:
    """True si además están configuradas las notas (herramientas note_*)."""
    return bool(_notes_remote())


# --------------------------------------------------------------------------- #
# Helpers de git y de texto (BLOQUEANTES — se llaman vía asyncio.to_thread)
# --------------------------------------------------------------------------- #
def _git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    """Ejecuta git y devuelve stdout.

    ``safe.directory=*`` porque los repos montados pueden tener otro dueño que el
    proceso, y ``GIT_OPTIONAL_LOCKS=0`` para no escribir el índice en montajes ro.
    """
    env = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Jarvis",
        "GIT_AUTHOR_EMAIL": "jarvis@localhost",
        "GIT_COMMITTER_NAME": "Jarvis",
        "GIT_COMMITTER_EMAIL": "jarvis@localhost",
    }
    proc = subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def _norm(name: str) -> str:
    """Normaliza un nombre para compararlo: sin acentos, minúsculas, solo alfanumérico.

    Por voz el nombre llega como lo transcribe el STT ("pokelab", "Poke Lab"…).
    """
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", plain.lower())


def _projects() -> dict[str, Path]:
    """Proyectos del workspace: carpetas (hasta 2 niveles) con PLAN.md o README.md.

    Returns:
        Dict nombre relativo (p. ej. "blog", "juegos/snake") → ruta.
    """
    ws = _workspace()
    found: dict[str, Path] = {}
    if ws is None:
        return found
    for top in sorted(ws.iterdir()):
        if not top.is_dir() or top.name.startswith("."):
            continue
        if (top / "PLAN.md").exists() or (top / "README.md").exists():
            found[top.name] = top
        for sub in sorted(top.iterdir()):
            if sub.is_dir() and not sub.name.startswith((".", "_")) and (sub / "PLAN.md").exists():
                found[f"{top.name}/{sub.name}"] = sub
    return found


def _find_project(name: str) -> tuple[str, Path] | None:
    """Busca un proyecto por nombre aproximado: exacto, luego por la última parte, luego substring."""
    target = _norm(name)
    projects = _projects()
    if not target:
        return None
    for key, path in projects.items():
        if _norm(key) == target or _norm(key.split("/")[-1]) == target:
            return key, path
    for key, path in projects.items():
        if target in _norm(key) or _norm(key.split("/")[-1]) in target:
            return key, path
    return None


def _unknown_project(name: str) -> str:
    """Mensaje para un proyecto no encontrado, con la lista para que el modelo reintente."""
    return f"No encuentro el proyecto '{name}'. Proyectos disponibles: {', '.join(_projects())}."


def _section(text: str, title_re: str) -> str | None:
    """Extrae la sección Markdown cuyo título casa con ``title_re`` (hasta el siguiente título de nivel ≤)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#+)\s+(.*)", line)
        if m and re.search(title_re, m.group(2), re.IGNORECASE):
            level = len(m.group(1))
            out = []
            for nxt in lines[i + 1:]:
                h = re.match(r"^(#+)\s", nxt)
                if h and len(h.group(1)) <= level:
                    break
                out.append(nxt)
            return "\n".join(out).strip()
    return None


def _open_tasks(text: str) -> list[str]:
    """Tareas sin marcar (``- [ ] ...``) de un Markdown, sin el marcador de id."""
    tasks = []
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]\s+\[ \]\s+(.*)", line)
        if m:
            tasks.append(m.group(1).strip())
    return tasks


def _clip(text: str, limit: int) -> str:
    """Recorta un texto largo indicándolo."""
    return text if len(text) <= limit else text[:limit].rstrip() + "\n[…recortado]"


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #
def _project_status(name: str) -> str:
    """Punto de retomada del PLAN.md de un proyecto + nº de tareas abiertas."""
    hit = _find_project(name)
    if hit is None:
        return _unknown_project(name)
    key, path = hit
    plan = path / "PLAN.md"
    if not plan.exists():
        readme = (path / "README.md").read_text(encoding="utf-8", errors="replace")
        return f"Proyecto {key}: no tiene PLAN.md. Inicio del README:\n{_clip(readme, _MAX_SECTION_CHARS)}"
    text = plan.read_text(encoding="utf-8", errors="replace")
    body = _section(text, r"retomada") or _section(text, r"estado") or text
    n_open = len(_open_tasks(text))
    for extra in sorted((path / "docs" / "plan").glob("*.md")):
        n_open += len(_open_tasks(extra.read_text(encoding="utf-8", errors="replace")))
    return f"Proyecto {key} — {n_open} tareas abiertas.\n\n{_clip(body, _MAX_SECTION_CHARS)}"


def _todo_list(project: str | None) -> str:
    """Tareas abiertas de un proyecto, o del TODO general (Inbox incluido, con ids)."""
    if project:
        hit = _find_project(project)
        if hit is None:
            return _unknown_project(project)
        key, path = hit
        files = [path / "PLAN.md", *sorted((path / "docs" / "plan").glob("*.md"))]
        tasks = []
        for f in files:
            if f.exists():
                tasks += _open_tasks(f.read_text(encoding="utf-8", errors="replace"))
        if not tasks:
            return f"Proyecto {key}: no hay tareas abiertas."
        shown = [f"- {_clip(t, 200)}" for t in tasks[:_MAX_TASKS]]
        more = f"\n(y {len(tasks) - _MAX_TASKS} más)" if len(tasks) > _MAX_TASKS else ""
        return f"Proyecto {key} — {len(tasks)} tareas abiertas:\n" + "\n".join(shown) + more

    # TODO general: el clon de notas es la copia más fresca (incluye lo que Jarvis
    # acaba de apuntar); sin notas, el TODO.md del workspace.
    if notes_enabled():
        _sync_notes()
        text = _notes_file().read_text(encoding="utf-8")
    else:
        candidates = [p / "TODO.md" for p in _projects().values()] + [_workspace() / "TODO.md"]
        existing = [p for p in candidates if p.exists()]
        if not existing:
            return "No hay fichero TODO.md en el workspace."
        text = existing[0].read_text(encoding="utf-8")

    out = []
    for block in re.split(r"(?m)^(?=## )", text):
        title = block.splitlines()[0].lstrip("# ").strip() if block.startswith("## ") else ""
        if not title:
            continue
        if title == INBOX_HEADING.lstrip("# ").strip():
            notes = [line for line in block.splitlines() if _ID_RE.search(line)]
            out.append("Inbox (id entre corchetes):")
            out += [f"- [{_ID_RE.search(n).group(1)}] {_note_text(n)}" for n in notes] or ["- (vacío)"]
            continue
        tasks = _open_tasks(block)
        if tasks:
            out.append(f"{title}:")
            out += [f"- {_clip(t, 200)}" for t in tasks]
    return "\n".join(out) if out else "No hay tareas pendientes."


def _recent_activity(days: int) -> str:
    """Commits de los últimos días en todos los bare repos (= lo empujado desde cualquier equipo)."""
    root = _git_root()
    if root is None:
        return "No hay carpeta de repos configurada (JARVIS_GIT_ROOT)."
    out = []
    for repo in sorted(root.glob("*.git")):
        log = _git(
            "--git-dir", str(repo), "log", "--all", f"--since={days}.days",
            "--date=short", "--format=%ad · %s", f"-{_MAX_COMMITS_PER_REPO}",
            check=False,
        ).strip()
        if log:
            out.append(f"{repo.stem}:\n" + "\n".join(f"- {line}" for line in log.splitlines()))
    return "\n\n".join(out) if out else f"Sin commits en los últimos {days} días."


def _unpushed() -> str:
    """Working copies del workspace con cambios sin commitear o commits sin subir."""
    ws = _workspace()
    out = []
    repos = [ws] if (ws / ".git").exists() else []
    repos += [p for p in sorted(ws.iterdir()) if (p / ".git").exists()]
    for repo in repos:
        name = repo.name
        dirty = _git("status", "--porcelain", cwd=repo, check=False).strip().splitlines()
        ahead = _git("rev-list", "--count", "@{u}..HEAD", cwd=repo, check=False).strip()
        parts = []
        if dirty:
            parts.append(f"{len(dirty)} ficheros sin commitear")
        if ahead and ahead != "0":
            parts.append(f"{ahead} commits sin subir")
        if parts:
            out.append(f"- {name}: {', '.join(parts)}")
    if not out:
        return "Todo está commiteado y subido en el workspace del servidor."
    return "Pendiente en el workspace del servidor:\n" + "\n".join(out)


# --------------------------------------------------------------------------- #
# Notas (Inbox)
# --------------------------------------------------------------------------- #
def _sync_notes() -> None:
    """Clona el repo de notas si falta y lo alinea con el remoto.

    ``reset --hard`` es seguro: Jarvis es el único escritor de este clon y todo lo
    suyo ya está empujado (se empuja en cada cambio).
    """
    d = _notes_dir()
    if not (d / ".git").exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", _notes_remote(), str(d))
        return
    _git("fetch", "origin", cwd=d)
    _git("reset", "--hard", "@{u}", cwd=d)


def _note_text(line: str) -> str:
    """Texto legible de una línea de nota (sin casilla ni marcador de id)."""
    text = re.sub(r"^\s*[-*]\s+\[[ x]\]\s+", "", line)
    return _ID_RE.sub("", text).strip()


def _clean(text: str) -> str:
    """Una sola línea y sin comentarios HTML (no debe poder falsear un id)."""
    return " ".join(text.replace("<!--", "").replace("-->", "").split())


def _format_note(text: str, project: str | None, note_id: str) -> str:
    """Línea de nota: ``- [ ] FECHA · proyecto · texto <!-- j:id -->``."""
    parts = [date.today().isoformat()]
    if project:
        parts.append(_clean(project))
    parts.append(_clean(text))
    return f"- [ ] {' · '.join(parts)} <!-- j:{note_id} -->"


def _inbox_bounds(lines: list[str]) -> tuple[int, int]:
    """Índices [inicio, fin) del cuerpo del Inbox; crea la sección si no existe."""
    try:
        start = lines.index(INBOX_HEADING)
    except ValueError:
        first = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
        lines[first:first] = [INBOX_HEADING, "", _INBOX_BLURB, ""]
        start = first
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return start + 1, end


def _mutate_notes(change, message: str) -> str:
    """Aplica ``change(lines) -> resultado`` al fichero de notas, con commit y push.

    Si el push se rechaza porque otro equipo empujó entre medias, se realinea con
    el remoto y se reaplica el cambio una vez.
    """
    with _notes_lock:
        for attempt in range(2):
            _sync_notes()
            path = _notes_file()
            lines = path.read_text(encoding="utf-8").splitlines()
            result = change(lines)  # puede lanzar ValueError (id inexistente…)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            d = _notes_dir()
            _git("commit", "-am", message, cwd=d)
            try:
                _git("push", "origin", "HEAD", cwd=d)
                return result
            except RuntimeError:
                if attempt == 1:
                    raise
    raise RuntimeError("unreachable")


def _note_add(text: str, project: str | None) -> str:
    """Añade una nota al final del Inbox y devuelve su id."""
    note_id = secrets.token_hex(2)

    def change(lines: list[str]) -> str:
        start, end = _inbox_bounds(lines)
        # Inserta tras la última línea no vacía del Inbox (deja el hueco antes de la siguiente sección).
        pos = end
        while pos > start and not lines[pos - 1].strip():
            pos -= 1
        new = [_format_note(text, project, note_id)]
        # Pegada a la cita del encabezado, la lista quedaría DENTRO de la cita (Markdown).
        if lines[pos - 1].startswith(">"):
            new.insert(0, "")
        lines[pos:pos] = new
        return note_id

    _mutate_notes(change, f"jarvis: nota {note_id}")
    return note_id


def _find_note(lines: list[str], note_id: str) -> int:
    """Índice de la nota con ese id dentro del Inbox (ValueError si no está)."""
    start, end = _inbox_bounds(lines)
    for i in range(start, end):
        m = _ID_RE.search(lines[i])
        if m and m.group(1) == note_id.lower().strip():
            return i
    raise ValueError(f"No hay ninguna nota con id {note_id} en el Inbox.")


def _note_edit(note_id: str, text: str, project: str | None) -> str:
    """Reescribe una nota conservando su id; devuelve la línea nueva legible."""

    def change(lines: list[str]) -> str:
        i = _find_note(lines, note_id)
        lines[i] = _format_note(text, project, note_id.lower().strip())
        return _note_text(lines[i])

    return _mutate_notes(change, f"jarvis: corrige nota {note_id}")


def _note_delete(note_id: str) -> str:
    """Borra una nota del Inbox; devuelve su texto."""

    def change(lines: list[str]) -> str:
        i = _find_note(lines, note_id)
        return _note_text(lines.pop(i))

    return _mutate_notes(change, f"jarvis: borra nota {note_id}")


# --------------------------------------------------------------------------- #
# Herramientas expuestas a Jarvis
# --------------------------------------------------------------------------- #
def _text(message: str) -> dict:
    """Envuelve un texto en el formato de respuesta que espera el SDK MCP."""
    return {"content": [{"type": "text", "text": message}]}


async def _run(fn, *args) -> dict:
    """Ejecuta un helper bloqueante en un hilo y convierte errores en texto para el modelo."""
    try:
        return _text(await asyncio.to_thread(fn, *args))
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return _text(f"Error: {exc}")


_PROJECT_PROP = {
    "type": "string",
    "description": "Nombre del proyecto tal como lo dice el usuario (se busca de forma aproximada).",
}


@tool(
    "project_status",
    "Estado de un proyecto de desarrollo del usuario: su 'punto de retomada' (en qué se quedó y "
    "cuál es la próxima acción) y cuántas tareas tiene abiertas. Úsala para '¿en qué me quedé con X?' "
    "o '¿cómo va X?'. Resume el resultado en dos o tres frases.",
    {"type": "object", "properties": {"project": _PROJECT_PROP}, "required": ["project"]},
)
async def project_status(args: dict) -> dict:
    return await _run(_project_status, args["project"])


@tool(
    "todo_list",
    "Tareas pendientes. Sin proyecto: el TODO general del usuario, incluido el Inbox de notas "
    "apuntadas por ti (cada nota con su id entre corchetes). Con proyecto: las tareas abiertas de "
    "ese proyecto.",
    {"type": "object", "properties": {"project": _PROJECT_PROP}},
)
async def todo_list(args: dict) -> dict:
    return await _run(_todo_list, args.get("project"))


@tool(
    "recent_activity",
    "Qué se ha hecho últimamente en los proyectos: commits de los últimos días en todos los "
    "repositorios (desde cualquier equipo), agrupados por proyecto.",
    {
        "type": "object",
        "properties": {"days": {"type": "integer", "description": "Días hacia atrás. Por defecto 7."}},
    },
)
async def recent_activity(args: dict) -> dict:
    return await _run(_recent_activity, int(args.get("days") or 7))


@tool(
    "unpushed",
    "Repositorios del workspace del servidor con cambios sin commitear o commits sin subir. "
    "Para '¿hay algo sin subir?'. Solo ve el servidor, no los otros equipos.",
    {"type": "object", "properties": {}},
)
async def unpushed(args: dict) -> dict:
    return await _run(_unpushed)


@tool(
    "note_add",
    "Apunta una nota o idea en el Inbox del TODO del usuario (se sincroniza a todos sus equipos). "
    "Devuelve el id de la nota: guárdalo por si te pide corregirla.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "La nota, redactada de forma clara y breve."},
            "project": {"type": "string", "description": "Proyecto al que se refiere, si lo nombra."},
        },
        "required": ["text"],
    },
)
async def note_add(args: dict) -> dict:
    result = await _run(_note_add, args["text"], args.get("project"))
    text = result["content"][0]["text"]
    if not text.startswith("Error"):
        result = _text(f"Nota apuntada con id {text}.")
    return result


@tool(
    "note_edit",
    "Corrige una nota del Inbox (por su id; si no lo sabes, búscalo antes con todo_list). "
    "Reescribe la nota entera con el texto corregido.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Id de la nota."},
            "text": {"type": "string", "description": "Texto completo corregido."},
            "project": {"type": "string", "description": "Proyecto, si lo tiene."},
        },
        "required": ["id", "text"],
    },
)
async def note_edit(args: dict) -> dict:
    return await _run(_note_edit, args["id"], args["text"], args.get("project"))


@tool(
    "note_delete",
    "Borra una nota del Inbox por su id (si no lo sabes, búscalo antes con todo_list).",
    {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
)
async def note_delete(args: dict) -> dict:
    return await _run(_note_delete, args["id"])


# --------------------------------------------------------------------------- #
# Servidor MCP en proceso
# --------------------------------------------------------------------------- #
_READ_TOOLS = [project_status, todo_list, recent_activity, unpushed]
_NOTE_TOOLS = [note_add, note_edit, note_delete]

# Nombre del servidor MCP. Las herramientas quedan como mcp__workspace__<tool>.
SERVER_NAME = "workspace"


def _tools() -> list:
    """Herramientas activas según la configuración (las de notas solo con remoto)."""
    return _READ_TOOLS + (_NOTE_TOOLS if notes_enabled() else [])


def tool_names() -> list[str]:
    """Nombres completos para la lista blanca del núcleo."""
    return [f"mcp__{SERVER_NAME}__{t.name}" for t in _tools()]


def build_workspace_server():
    """Crea el servidor MCP en proceso con las herramientas de workspace."""
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_tools())
