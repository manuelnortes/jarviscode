"""Configuración y lógica del enrutado de modelos de Jarvis.

Edita las constantes de este fichero para ajustar qué modelo se usa en cada
situación, sin tocar el código del núcleo.

Niveles:
  Haiku  — respuestas rápidas: chat casual, medios, consultas de estado.
  Sonnet — tareas reales: búsquedas, explicaciones, escritura, preguntas con contexto.
  Opus   — razonamiento profundo: análisis detallados, planificación, "por qué" complejos.

Invocación explícita (ver README.md):
  Prefija el prompt con !haiku, !sonnet u !opus para forzar un modelo concreto.
"""

from __future__ import annotations

# ── IDs de modelo ──────────────────────────────────────────────────────────────
# Actualiza aquí si Anthropic lanza versiones nuevas.
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_OPUS   = "claude-opus-4-8"

# Modelo usado cuando el prompt no encaja con ninguna señal explícita.
DEFAULT_MODEL = MODEL_HAIKU

# Umbral de palabras: prompts más largos que esto suben a Sonnet aunque no
# contengan señales específicas (una pregunta larga rara vez es trivial).
SONNET_WORD_THRESHOLD = 8

# ── Señales de Sonnet ──────────────────────────────────────────────────────────
# Añade o quita entradas para afinar cuándo usar Sonnet.
# Se buscan como substrings en el prompt (en minúsculas).
SONNET_SIGNALS: frozenset[str] = frozenset([
    # Búsqueda / investigación
    "busca", "buscar", "encuentra", "investiga",
    # Explicación básica
    "explica", "explícame", "explicame",
    # Escritura
    "escribe", "redacta", "crea", "genera",
    # Transformación de texto
    "traduce", "resume", "resumeme", "corrige",
    # Comparación / análisis ligero
    "compara", "diferencia entre", "ventajas de",
    # Preguntas de conocimiento
    "qué es", "que es", "quién es", "quien es",
    "cómo se", "como se", "cuándo fue", "cuando fue",
    "por qué", "por que",
    "qué significa", "que significa",
    "cómo funciona", "como funciona",
])

# ── Señales de Opus ────────────────────────────────────────────────────────────
# Opus se reserva para razonamiento profundo y análisis extensos.
# Añade frases que indiquen que el usuario quiere una respuesta muy elaborada.
OPUS_SIGNALS: frozenset[str] = frozenset([
    "razona", "razoname", "razona sobre", "razona por qué", "razona por que",
    "analiza en detalle", "análisis profundo", "análisis detallado",
    "análisis completo", "analiza a fondo", "analiza en profundidad",
    "explica en detalle", "explícame en detalle", "explicame en detalle",
    "explica en profundidad", "explica detalladamente",
    "planifica", "diseña la arquitectura", "diseña el sistema",
    "ayúdame a pensar", "ayudame a pensar",
    "reflexiona sobre", "reflexiona acerca de",
])

# ── Prefijos de invocación explícita ──────────────────────────────────────────
# El usuario puede forzar un modelo añadiendo uno de estos prefijos al inicio
# del prompt. El prefijo se elimina antes de enviar el mensaje a Claude.
# Ejemplo: "!opus analiza este código" → usa Opus, envía "analiza este código".
MODEL_PREFIXES: dict[str, str] = {
    "!haiku":  MODEL_HAIKU,
    "!sonnet": MODEL_SONNET,
    "!opus":   MODEL_OPUS,
}


# ── Prioridad de modelos (para ratchet-up) ────────────────────────────────────
# Cuanto más alto el número, más capaz el modelo. Solo se sube, nunca se baja.
MODEL_PRIORITY: dict[str, int] = {
    MODEL_HAIKU:  0,
    MODEL_SONNET: 1,
    MODEL_OPUS:   2,
}


def classify_model(prompt: str) -> str:
    """Elige el modelo según la complejidad inferida del prompt.

    Orden de prioridad: Opus > Sonnet > umbral de longitud > Haiku (defecto).

    Args:
        prompt: El mensaje del usuario tal cual (sin prefijo de modelo).

    Returns:
        ID de modelo: MODEL_OPUS, MODEL_SONNET o MODEL_HAIKU.
    """
    p = prompt.lower().strip()

    # Razonamiento profundo — mayor prioridad
    if any(s in p for s in OPUS_SIGNALS):
        return MODEL_OPUS

    # Tarea real con capacidad moderada
    if any(s in p for s in SONNET_SIGNALS):
        return MODEL_SONNET

    # Prompts largos sin señales: probablemente no son triviales
    if len(prompt.split()) > SONNET_WORD_THRESHOLD:
        return MODEL_SONNET

    return DEFAULT_MODEL


def parse_model_prefix(prompt: str) -> tuple[str | None, str]:
    """Detecta y extrae un prefijo de modelo del inicio del prompt.

    Permite forzar un modelo concreto independientemente del enrutado automático.
    La detección no distingue mayúsculas/minúsculas.

    Args:
        prompt: El mensaje original, posiblemente con prefijo.

    Returns:
        Tupla ``(model_id, prompt_limpio)``. Si no hay prefijo, ``model_id`` es
        ``None`` y ``prompt_limpio`` es el prompt original sin modificar.
    """
    stripped = prompt.strip()
    lower = stripped.lower()
    for prefix, model in MODEL_PREFIXES.items():
        if lower.startswith(prefix + " ") or lower == prefix:
            return model, stripped[len(prefix):].strip()
    return None, prompt
