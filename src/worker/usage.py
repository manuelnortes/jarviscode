"""Uso real de la suscripción para la salvaguarda de los agentes (Hito 9, Fase 4, tarea (d)).

Por qué una sonda: los ``RateLimitEvent`` del CLI solo traen la utilización cerca de
su propio aviso (daban ``five_hour: 0 %`` con el uso real al 62 %), así que la
salvaguarda estaba ciega. El endpoint de uso de la cuenta (el de ``/usage``) limita
mucho la frecuencia (429 con ``retry-after`` de ~1 h). Lo que sí funciona: cada
respuesta de la API trae el uso en las cabeceras ``anthropic-ratelimit-unified-*``
(la misma fuente que el statusline de Claude Code), así que basta una llamada
mínima a Haiku (1 token de salida, ~25 en total) cada pocos minutos.

Solo biblioteca estándar: la imagen del worker no trae httpx, y así se prueba sin el SDK.
"""

from __future__ import annotations

import json
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
PROBE_MODEL = "claude-haiku-4-5-20251001"
# Las peticiones con token OAuth de la suscripción tienen que presentarse como Claude Code.
_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."

# Ventanas que se leen de las cabeceras → nombre que usan los RateLimitEvent y GET /usage.
WINDOWS = {"5h": "five_hour", "7d": "seven_day"}


def parse_headers(headers) -> dict[str, dict]:
    """Ventanas de uso a partir de las cabeceras ``anthropic-ratelimit-unified-*``.

    Args:
        headers: Cabeceras de la respuesta (cualquier objeto con ``.get``, sin distinguir
            mayúsculas como las de urllib, o un dict con las claves en minúscula).

    Returns:
        ``{"five_hour": {"utilization": 0.84, "resets_at": 1791653400.0, "status": "allowed"}, ...}``
        Solo las ventanas cuya utilización venga en las cabeceras.
    """
    windows: dict[str, dict] = {}
    for key, name in WINDOWS.items():
        prefix = f"anthropic-ratelimit-unified-{key}-"
        raw = headers.get(prefix + "utilization")
        if raw is None:
            continue
        reset = headers.get(prefix + "reset")
        windows[name] = {
            "utilization": float(raw),
            "resets_at": float(reset) if reset else None,
            "status": headers.get(prefix + "status"),
        }
    return windows


def probe(token: str, timeout: float = 30) -> dict[str, dict]:
    """Llamada mínima a la API con el token OAuth y lectura del uso de sus cabeceras.

    Bloqueante (urllib): llamarla con ``asyncio.to_thread``.

    Raises:
        OSError: Fallo de red o HTTP (``urllib.error.HTTPError`` es subclase).
        ValueError: La respuesta no trae el uso en las cabeceras.
    """
    body = {
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": "hi"}],
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        windows = parse_headers(resp.headers)
    if not windows:
        raise ValueError("la respuesta no trae las cabeceras de uso")
    return windows


def limit_for(window: str, limit_5h: float, limit_7d: float) -> float:
    """Límite que aplica a una ventana: las semanales (seven_day*) el suyo, el resto el de 5 h."""
    return limit_7d if window.startswith("seven_day") else limit_5h


def assess(
    windows: dict[str, dict],
    limit_5h: float,
    limit_7d: float,
    probing: bool,
    probed_at: float | None,
    now: float,
    stale_after: float,
) -> tuple[str, float | None]:
    """Decisión de la salvaguarda.

    Args:
        windows: Últimas ventanas conocidas.
        limit_5h / limit_7d: Límites (0–1).
        probing: Si la sonda está activa. Sin token no lo está y se decide solo con lo
            que haya (los RateLimitEvent), como antes.
        probed_at: Hora de la última sonda buena (None si aún no hubo ninguna).
        now: Hora actual.
        stale_after: Segundos a partir de los que la última lectura no vale.

    Returns:
        ``("ok", None)``, ``("over", hora de reinicio más tardía de las ventanas que bloquean)``
        o ``("unknown", None)``: con la sonda activa y sin lectura reciente no se lanza
        nada (no saber el uso no es estar al 0 %).
    """
    resets = [
        w.get("resets_at") for name, w in windows.items()
        if w.get("utilization") is not None
        and w["utilization"] >= limit_for(name, limit_5h, limit_7d)
        and (not w.get("resets_at") or w["resets_at"] > now)
    ]
    if resets:
        known = [r for r in resets if r]
        return "over", (max(known) if known else None)
    if probing and (probed_at is None or now - probed_at > stale_after):
        return "unknown", None
    return "ok", None
