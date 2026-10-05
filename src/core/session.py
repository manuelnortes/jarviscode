"""Lógica de ciclo de vida de una sesión de voz, compartida por front-ends.

Extraído de ``src/frontends/voice.py`` para que tanto el CLI de voz como el
endpoint web (`src/core/voice_ws.py`) reusen la detección de cierre y la
despedida sin que el backend tenga que importar desde ``frontends/``.

Una "sesión" agrupa varios turnos con la misma instancia de :class:`JarvisCore`
(memoria multi-turno + ratchet-up de modelo). Se cierra al detectar una palabra
de cierre o por inactividad; el reinicio crea un núcleo fresco (de vuelta a Haiku).
"""

from __future__ import annotations

import re
import unicodedata

# Frases que cierran la sesión. Se comparan en minúsculas, sin signos, al final
# del transcript (así "muchas gracias" o "vale, adiós" también cierran).
CLOSING_PHRASES = (
    "gracias",
    "adiós",
    "adios",
    "hasta luego",
    "hasta pronto",
    "hasta mañana",
    "hasta manana",
    "chao",
    "eso es todo",
)

# Despedida que dice Jarvis al cerrar la sesión por palabra de cierre.
FAREWELL = "De nada. Aquí estaré si me necesitas."

# Palabras que ABORTAN el comando que se está dictando (no cierran la sesión:
# solo se descarta el transcript y se vuelve a escuchar). Se eligió el imperativo
# "cancela" y NO el infinitivo "cancelar" a propósito: "cancelar" aparece en
# preguntas legítimas ("cómo cancelar mi suscripción") y daría falsos positivos.
# Se comparan como PALABRA COMPLETA (token), nunca como subcadena: por subcadena,
# "cancelar" contiene "cancela" y dispararía justo el falso positivo a evitar.
CANCEL_WORDS = ("cancela",)


def _tokens(text: str) -> list[str]:
    """Tokeniza ``text`` en minúsculas y sin tildes para comparar por palabra.

    Args:
        text: Texto a normalizar.

    Returns:
        Lista de tokens alfanuméricos (sin signos ni diacríticos).
    """
    nfkd = unicodedata.normalize("NFKD", text.lower())
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", no_accents)


def is_cancel(text: str) -> bool:
    """Indica si el transcript pide ABORTAR el comando en curso.

    Detecta cualquier palabra de :data:`CANCEL_WORDS` como token aislado en
    cualquier posición del transcript (no como subcadena). El llamador debe
    descartar el transcript sin mandarlo al modelo y volver a estado de escucha,
    SIN cerrar la sesión.

    Args:
        text: Texto transcrito del usuario.

    Returns:
        True si contiene una palabra de cancelación como palabra completa.
    """
    tokens = set(_tokens(text))
    return any(word in tokens for word in CANCEL_WORDS)


def is_closing(text: str) -> bool:
    """Indica si el transcript pide cerrar la sesión.

    Args:
        text: Texto transcrito del usuario.

    Returns:
        True si termina con (o es exactamente) una frase de cierre.
    """
    cleaned = text.lower().strip(" .!¡?¿,")
    return any(cleaned == p or cleaned.endswith(p) for p in CLOSING_PHRASES)
