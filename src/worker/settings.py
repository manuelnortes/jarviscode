"""Ajustes en caliente (Hito 9, Fase 6, bloque 5): registro, valores y validación.

Lo usan el worker y el núcleo (vive en ``src/worker/`` porque ``Dockerfile.worker`` solo copia
esa carpeta). Los valores cambiados desde la web se guardan en ``settings.json`` (volumen
``jarvis-settings``: el núcleo lo escribe, el worker lo monta de solo lectura) y se releen
cuando cambia su ``mtime``. Precedencia en cada uso: **en caliente > entorno > defecto**.

Solo las claves del registro se pueden cambiar (lista blanca); el resto (rutas, puertos,
la sonda, la rama de integración, secretos…) sigue siendo solo de ``.env`` + reinicio.
Los porcentajes se guardan como en el ``.env`` (60 = 60 %) y se entregan como fracción.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("jarvis.settings")

# Cuándo surte efecto un cambio (lo enseña la web).
INSTANT, NEXT_AGENT, NEXT_QUESTION, NEXT_VISION = "instant", "next_agent", "next_question", "next_vision"
EFFORTS = ("low", "medium", "high")


@dataclass(frozen=True)
class Setting:
    """Un ajuste editable: tipo, rango, defecto y dónde se aplica."""

    key: str
    label: str
    kind: str  # int · float · percent · bool · enum · model
    default: object
    applies: str
    scope: str  # worker · core (qué proceso lo usa)
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = ()


_W, _C = "worker", "core"
REGISTRY: dict[str, Setting] = {s.key: s for s in (
    Setting("JARVIS_AGENTS_MAX", "Agentes trabajando a la vez", "int", 2, INSTANT, _W, 1, 4),
    Setting("JARVIS_AGENT_MODEL", "Modelo de los agentes", "model", "claude-sonnet-4-6", NEXT_AGENT, _W),
    Setting("JARVIS_AGENT_EFFORT", "Esfuerzo de los agentes", "enum", "low", NEXT_AGENT, _W, choices=EFFORTS),
    Setting("JARVIS_AGENT_IDLE_WARN", "Idle: aviso (s)", "int", 2100, INSTANT, _W, 60, 3600),
    Setting("JARVIS_AGENT_HANDOFF_AT", "Idle: handoff (s)", "int", 2700, INSTANT, _W, 60, 3600),
    Setting("JARVIS_AGENT_IDLE_TTL", "Idle: cierre de sesión (s)", "int", 3300, INSTANT, _W, 60, 3600),
    Setting("JARVIS_AGENT_TURN_TIMEOUT", "Tope de trabajo por turno (s)", "int", 1800, INSTANT, _W, 60, 7200),
    Setting("JARVIS_AGENT_MANAGER_TIMEOUT", "Espera al gestor (s)", "int", 900, NEXT_QUESTION, _W, 30, 3600),
    Setting("JARVIS_AGENT_ASK_MANAGER_FIRST", "Preguntas de diseño primero a Vision", "bool", True,
            NEXT_QUESTION, _W),
    Setting("JARVIS_AGENT_USAGE_LIMIT", "Límite de uso 5 h (%)", "percent", 60, INSTANT, _W, 1, 95),
    Setting("JARVIS_AGENT_USAGE_LIMIT_7D", "Límite de uso semanal (%)", "percent", 75, INSTANT, _W, 1, 95),
    Setting("JARVIS_AGENT_USAGE_MODE", "Al reiniciarse la ventana", "enum", "session", INSTANT, _W,
            choices=("session", "autonomous")),
    Setting("JARVIS_USAGE_PROBE_EVERY", "Sonda de uso cada (s)", "int", 120, INSTANT, _W, 30, 900),
    Setting("JARVIS_AGENT_CHECKPOINT", "Punto de control cada +% de 5 h", "percent", 5, INSTANT, _W, 0, 50),
    Setting("JARVIS_VISION_MODEL", "Modelo de Vision", "model", "claude-opus-5-5", NEXT_VISION, _C),
    Setting("JARVIS_VISION_EFFORT", "Esfuerzo de Vision", "enum", "medium", NEXT_VISION, _C, choices=EFFORTS),
    Setting("JARVIS_VISION_HANDOFF_AFTER", "Vision: handoff tras (s)", "int", 2700, INSTANT, _C, 300, 3600),
    Setting("JARVIS_VISION_DIE_AFTER", "Vision: cierre tras (s)", "int", 3000, INSTANT, _C, 300, 3600),
    Setting("JARVIS_VISION_MAX_CONTEXT", "Vision: reciclar con contexto de (tokens)", "int", 100_000, INSTANT, _C,
            20_000, 1_000_000),
    Setting("JARVIS_VISION_LAUNCH", "Vision puede lanzar agentes", "bool", True, INSTANT, _C),
    Setting("JARVIS_WEB_SESSION_TIMEOUT_SECONDS", "Sesión de la web (s)", "int", 90, INSTANT, _C, 10, 7200),
    Setting("JARVIS_AGENTS_SESSION_TIMEOUT_SECONDS", "Sesión de la web con agentes (s)", "int", 3600, INSTANT, _C,
            10, 7200),
)}

# Pares que deben ir en orden estricto (menor < mayor), con el valor efectivo.
_ORDER = (
    ("JARVIS_AGENT_IDLE_WARN", "JARVIS_AGENT_HANDOFF_AT"),
    ("JARVIS_AGENT_HANDOFF_AT", "JARVIS_AGENT_IDLE_TTL"),
    ("JARVIS_VISION_HANDOFF_AFTER", "JARVIS_VISION_DIE_AFTER"),
)


# --------------------------------------------------------------------------- #
# Conversión
# --------------------------------------------------------------------------- #
def parse(setting: Setting, raw: object) -> object:
    """Valor en las unidades del ``.env`` (60 = 60 %), validado contra tipo y rango.

    Raises:
        ValueError: Con el motivo, si no vale.
    """
    kind = setting.kind
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "si", "sí", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError("debe ser sí o no")
    if kind in ("enum", "model"):
        text = str(raw).strip()
        if kind == "enum" and text not in setting.choices:
            raise ValueError(f"debe ser uno de: {', '.join(setting.choices)}")
        if kind == "model" and not text.startswith("claude-"):
            raise ValueError("debe empezar por 'claude-'")
        return text
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("debe ser un número") from None
    if isinstance(raw, bool):
        raise ValueError("debe ser un número")
    if kind == "percent" and 0 < value < 1:
        value *= 100  # el .env admite también "0.85"
    if kind == "int":
        if value != int(value):
            raise ValueError("debe ser un número entero")
        value = int(value)
    if setting.min is not None and value < setting.min or setting.max is not None and value > setting.max:
        raise ValueError(f"fuera de rango ({setting.min:g}–{setting.max:g})")
    return value


def internal(setting: Setting, value: object) -> object:
    """De las unidades del ``.env`` a las del código (porcentaje → fracción)."""
    return value / 100 if setting.kind == "percent" else value  # type: ignore[operator]


# --------------------------------------------------------------------------- #
# Fichero en caliente
# --------------------------------------------------------------------------- #
class HotSettings:
    """``settings.json`` con relectura por ``mtime`` (barato: un ``stat`` por uso)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float | None = None
        self._values: dict[str, object] = {}

    def overrides(self) -> dict[str, object]:
        """Valores en caliente válidos (los inválidos o desconocidos se ignoran con aviso)."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime, self._values = None, {}
            return {}
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.exception("Ajustes: no puedo leer %s; sigo con el entorno", self.path)
                raw = {}
            values = {}
            for key, value in (raw if isinstance(raw, dict) else {}).items():
                try:
                    values[key] = parse(REGISTRY[key], value)
                except (KeyError, ValueError):
                    log.warning("Ajustes: ignoro %s=%r", key, value)
            self._values = values
        return dict(self._values)

    def write(self, values: dict[str, object]) -> None:
        """Escritura atómica (fichero temporal + rename en la misma carpeta)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(values, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


_hot = HotSettings(Path(os.getenv("JARVIS_SETTINGS_FILE", "/settings/settings.json")))


def hot() -> HotSettings:
    """El fichero de ajustes de este proceso."""
    return _hot


def env_value(setting: Setting) -> tuple[object, str]:
    """Valor del entorno (o el defecto) en unidades del ``.env``, y su origen."""
    raw = os.getenv(setting.key, "")
    if raw.strip():
        try:
            return parse(setting, raw), "env"
        except ValueError:
            log.warning("Ajustes: %s=%r del entorno no vale; uso el defecto", setting.key, raw)
    return setting.default, "default"


def get(key: str, fallback: object) -> object:
    """Valor efectivo en unidades del código: en caliente si lo hay; si no, ``fallback``.

    ``fallback`` es el valor que el proceso leyó del entorno al arrancar (ya convertido).
    """
    overrides = _hot.overrides()
    if key in overrides:
        return internal(REGISTRY[key], overrides[key])
    return fallback


class Hot:
    """Descriptor: atributo de instancia que consulta el valor en caliente en cada lectura.

    ``self.model = x`` guarda el valor del entorno; ``self.model`` devuelve el de
    ``settings.json`` si lo hay. Así no hay que tocar cada uso del atributo.
    """

    def __init__(self, key: str) -> None:
        self.key = key

    def __set_name__(self, owner, name: str) -> None:
        self.attr = f"_env_{name}"

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return get(self.key, getattr(obj, self.attr))

    def __set__(self, obj, value) -> None:
        setattr(obj, self.attr, value)


# --------------------------------------------------------------------------- #
# Para la API
# --------------------------------------------------------------------------- #
def describe(scope: str) -> list[dict]:
    """Ajustes de un ``scope`` con su valor efectivo y su origen (``hot``/``env``/``default``)."""
    overrides = _hot.overrides()
    out = []
    for s in REGISTRY.values():
        if s.scope != scope:
            continue
        env, origin = env_value(s)
        hot_value = overrides.get(s.key)
        out.append({
            "key": s.key, "label": s.label, "kind": s.kind, "min": s.min, "max": s.max,
            "choices": list(s.choices), "default": s.default, "applies": s.applies, "scope": s.scope,
            "env": env, "hot": hot_value, "value": env if hot_value is None else hot_value,
            "origin": origin if hot_value is None else "hot",
        })
    return out


def validate(changes: dict, current: dict[str, object]) -> tuple[dict[str, object], dict[str, str]]:
    """Aplica ``changes`` (``{CLAVE: valor | None}``) a los valores en caliente actuales.

    Args:
        changes: ``None`` en una clave = quitar el valor en caliente (volver al entorno).
        current: Valor efectivo de cada clave SIN los cambios (para las reglas de orden).

    Returns:
        (nuevos valores en caliente, errores por clave). Con errores no se aplica nada.
    """
    hot_values = _hot.overrides()
    errors: dict[str, str] = {}
    effective = dict(current)
    for key, raw in (changes or {}).items():
        setting = REGISTRY.get(key)
        if setting is None:
            errors[key] = "clave desconocida o no editable en caliente"
            continue
        if raw is None:
            hot_values.pop(key, None)
            effective[key] = env_value(setting)[0]
            continue
        try:
            hot_values[key] = effective[key] = parse(setting, raw)
        except ValueError as exc:
            errors[key] = str(exc)
    for low, high in _ORDER:
        if low in effective and high in effective and not effective[low] < effective[high]:
            bad = high if high in (changes or {}) else low
            errors.setdefault(bad, f"{REGISTRY[low].label} debe ser menor que {REGISTRY[high].label}")
    return hot_values, errors


def audit(path: Path, old: dict, new: dict) -> None:
    """Una línea por cambio aplicado (valor viejo → nuevo; ``None`` = sin valor en caliente)."""
    keys = sorted(set(old) | set(new))
    changed = {k: [old.get(k), new.get(k)] for k in keys if old.get(k) != new.get(k)}
    if not changed:
        return
    log.info("Ajustes cambiados: %s", changed)
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": round(time.time(), 1), "changed": changed}, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("Ajustes: no se pudo escribir la auditoría")
