"""Prueba de la salvaguarda de uso de los agentes (Hito 9, Fase 4, tarea (d)).

Sin red ni SDK: comprueba la lectura de las cabeceras ``anthropic-ratelimit-unified-*``
(valores reales de la prueba del 2026-10-10) y las decisiones de ``usage.assess``:
límites por ventana, ventana ya reiniciada, y que sin lectura reciente NO se lanza nada
(la salvaguarda estaba ciega porque "sin datos" contaba como 0 %).

Uso:
    python3 -m scripts.test_usage_probe
"""

from __future__ import annotations

from src.worker.usage import assess, parse_headers

# Cabeceras reales de la sonda (2026-10-10), en minúscula como las da HTTP/2.
REAL = {
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.84",
    "anthropic-ratelimit-unified-5h-reset": "1791653400",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.5",
    "anthropic-ratelimit-unified-7d-reset": "1791756000",
    "anthropic-ratelimit-unified-status": "allowed",
}
NOW = 1791650000.0  # antes de ambos reinicios


def check(name: str, got, expected) -> bool:
    """Imprime el resultado de un caso y devuelve si pasa."""
    ok = got == expected
    print(f"{'OK ' if ok else 'FALLO'} {name}" + ("" if ok else f": {got!r} != {expected!r}"))
    return ok


def main() -> int:
    """Ejecuta los casos; código de salida 1 si alguno falla."""
    w = parse_headers(REAL)
    results = [
        check("parse 5h", w["five_hour"]["utilization"], 0.84),
        check("parse 7d reset", w["seven_day"]["resets_at"], 1791756000.0),
        check("parse sin cabeceras", parse_headers({}), {}),
        # 5 h al 84 % con límite 60 → bloquea hasta su reinicio.
        check("5h por encima", assess(w, 0.6, 0.6, True, NOW, NOW, 600), ("over", 1791653400.0)),
        # Límite 5 h alto: manda la semanal (50 % < 60 %) → ok.
        check("7d por debajo", assess(w, 0.9, 0.6, True, NOW, NOW, 600), ("ok", None)),
        # Semanal al 50 % con límite 50 → bloquea hasta el reinicio semanal.
        check("7d en el límite", assess(w, 0.9, 0.5, True, NOW, NOW, 600), ("over", 1791756000.0)),
        # Ambas por encima → la hora de reinicio más tardía.
        check("ambas por encima", assess(w, 0.6, 0.4, True, NOW, NOW, 600)[1], 1791756000.0),
        # Ventana de 5 h ya reiniciada: su lectura vieja no bloquea.
        check("5h reiniciada", assess(w, 0.6, 0.6, True, NOW - 10, 1791653500.0, 10**6), ("ok", None)),
        # Sonda activa sin ninguna lectura → desconocido, no se lanza.
        check("sin lectura", assess({}, 0.6, 0.6, True, None, NOW, 600), ("unknown", None)),
        # Última lectura buena hace más de 10 min → desconocido.
        check("lectura caducada", assess({}, 0.6, 0.6, True, NOW - 601, NOW, 600), ("unknown", None)),
        # Pero si lo que se sabe ya supera el límite, se dice "over" (con su hora).
        check("caducada pero over", assess(w, 0.6, 0.6, True, NOW - 601, NOW, 600)[0], "over"),
        # Sin sonda (sin token): como antes, sin datos no bloquea.
        check("sin sonda", assess({}, 0.6, 0.6, False, None, NOW, 600), ("ok", None)),
    ]
    print(f"\n{sum(results)}/{len(results)} casos OK")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
