"""Capacidad de control de medios vía Google Cast (Chromecast / Google Home).

Expone herramientas en proceso (SDK MCP) para que Jarvis reproduzca y controle
audio en los altavoces Cast de la red local. Es local: sin OAuth ni nube.

Nota: Google Cast sirve como SALIDA (altavoz). El micrófono del Google Home está
bloqueado para Google Assistant y NO se puede usar como entrada de Jarvis.

Las operaciones de pychromecast son bloqueantes (red), así que se ejecutan en un
hilo aparte con asyncio.to_thread para no bloquear el bucle de eventos.
"""

from __future__ import annotations

import asyncio
import atexit

import pychromecast
from claude_agent_sdk import create_sdk_mcp_server, tool
from zeroconf import Zeroconf

# Dispositivo Cast por defecto (nombre 'friendly' tal y como aparece en la red).
DEFAULT_DEVICE = "Salón"

# Segundos de espera al descubrir un dispositivo por nombre.
_DISCOVERY_TIMEOUT = 10

# Caché de dispositivos ya conectados, por nombre.
_devices: dict[str, "pychromecast.Chromecast"] = {}

# Zeroconf compartido: debe seguir VIVO mientras haya conexiones Cast activas,
# porque el socket_client del Chromecast lo usa para resolver el host por mDNS
# (en la conexión inicial y en cada reconexión). Lo cerramos solo al salir del
# proceso (atexit).
_zconf: "Zeroconf | None" = None


def _get_zconf() -> "Zeroconf":
    """Devuelve el Zeroconf compartido, creándolo de forma perezosa."""
    global _zconf
    if _zconf is None:
        _zconf = Zeroconf()
    return _zconf


@atexit.register
def _close_zconf() -> None:
    """Cierra el Zeroconf compartido al terminar el proceso."""
    global _zconf
    if _zconf is not None:
        _zconf.close()
        _zconf = None


def _stop_discovery_keep_zconf(browser: "pychromecast.discovery.CastBrowser") -> None:
    """Para el ServiceBrowser de descubrimiento SIN cerrar el Zeroconf compartido.

    `CastBrowser.stop_discovery()` (pychromecast) cierra siempre
    `self._zc_browser.zc.close()`, incluso cuando ese Zeroconf fue pasado como
    instancia externa/compartida vía `zeroconf_instance=`. Esto rompía nuestro
    `_zconf` compartido en cuanto acababa CUALQUIER descubrimiento (p. ej. al
    conectar con un Chromecast ya cacheado en `_devices`): el objeto seguía
    vivo en Python pero cerrado por dentro, y el hilo de reconexión del
    Chromecast (que reusa ese mismo `_zconf` para resolver el host por mDNS)
    petaba con "Zeroconf instance loop must be running, was it already
    stopped?" en bucle infinito y sin backoff — 100% CPU + logs sin fin hasta
    llenar el disco (visto en producción el 2026-07-03/04).

    Replicamos aquí solo la parte segura de `stop_discovery()` (cancelar el
    ServiceBrowser de mDNS y el host_browser) sin tocar el Zeroconf.
    """
    zc_browser = browser._zc_browser  # noqa: SLF001 (no hay API pública equivalente)
    if zc_browser is not None:
        try:
            zc_browser.cancel()
        except RuntimeError:
            # Lanza si se llama desde el propio callback del servicio zeroconf.
            pass
    browser.host_browser.stop.set()
    browser.host_browser.join()


# --------------------------------------------------------------------------- #
# Helpers (bloqueantes — se llaman vía asyncio.to_thread)
# --------------------------------------------------------------------------- #
def _discover_names() -> list[str]:
    """Devuelve los nombres de los dispositivos Cast visibles en la red."""
    chromecasts, browser = pychromecast.get_chromecasts(
        timeout=_DISCOVERY_TIMEOUT, zeroconf_instance=_get_zconf()
    )
    names = [cc.cast_info.friendly_name for cc in chromecasts]
    _stop_discovery_keep_zconf(browser)
    return names


def _get_device(name: str) -> "pychromecast.Chromecast":
    """Conecta (o reutiliza la conexión cacheada) con un dispositivo por nombre."""
    cc = _devices.get(name)
    if cc is not None:
        return cc

    chromecasts, browser = pychromecast.get_listed_chromecasts(
        friendly_names=[name], timeout=_DISCOVERY_TIMEOUT, zeroconf_instance=_get_zconf()
    )
    if not chromecasts:
        _stop_discovery_keep_zconf(browser)
        raise LookupError(f"No encontré ningún dispositivo Cast llamado '{name}'.")

    cc = chromecasts[0]
    cc.wait()  # establece la conexión (necesita el zeroconf compartido vivo)
    _stop_discovery_keep_zconf(browser)
    _devices[name] = cc
    return cc


# Alias públicos compartidos con la capacidad youtube.py: reutiliza la MISMA
# conexión Cast (mismo Zeroconf y la caché de dispositivos `_devices`) en vez de
# abrir una segunda al mismo Home Mini.
get_device = _get_device
get_shared_zeroconf = _get_zconf


def _guess_content_type(url: str) -> str:
    """Adivina el content-type a partir de la extensión de la URL."""
    u = url.lower()
    mapping = {
        ".mp3": "audio/mpeg",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".m3u8": "application/vnd.apple.mpegurl",
    }
    for ext, ctype in mapping.items():
        if u.endswith(ext):
            return ctype
    return "audio/mpeg"


def _text(message: str) -> dict:
    """Envuelve un texto en el formato de respuesta que espera el SDK MCP."""
    return {"content": [{"type": "text", "text": message}]}


def _device_param() -> dict:
    """Esquema reutilizable para el parámetro opcional 'device'."""
    return {
        "type": "string",
        "description": f"Nombre del dispositivo Cast. Por defecto: '{DEFAULT_DEVICE}'.",
    }


# --------------------------------------------------------------------------- #
# Herramientas expuestas a Jarvis
# --------------------------------------------------------------------------- #
@tool(
    "cast_list_devices",
    "Lista los altavoces / dispositivos Google Cast disponibles en la red local.",
    {"type": "object", "properties": {}},
)
async def cast_list_devices(args: dict) -> dict:
    names = await asyncio.to_thread(_discover_names)
    return _text("Dispositivos Cast: " + (", ".join(names) if names else "ninguno"))


@tool(
    "cast_play_url",
    "Reproduce una URL de audio (stream o fichero) en un altavoz Google Cast.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL del audio o stream a reproducir."},
            "device": _device_param(),
        },
        "required": ["url"],
    },
)
async def cast_play_url(args: dict) -> dict:
    url = args["url"]
    device = args.get("device") or DEFAULT_DEVICE

    def _play() -> None:
        cc = _get_device(device)
        cc.media_controller.play_media(url, _guess_content_type(url))
        cc.media_controller.block_until_active(timeout=_DISCOVERY_TIMEOUT)

    await asyncio.to_thread(_play)
    return _text(f"Reproduciendo en '{device}'.")


@tool(
    "cast_pause",
    "Pausa la reproducción en un altavoz Google Cast.",
    {"type": "object", "properties": {"device": _device_param()}},
)
async def cast_pause(args: dict) -> dict:
    device = args.get("device") or DEFAULT_DEVICE
    await asyncio.to_thread(lambda: _get_device(device).media_controller.pause())
    return _text(f"Pausado en '{device}'.")


@tool(
    "cast_resume",
    "Reanuda la reproducción pausada en un altavoz Google Cast.",
    {"type": "object", "properties": {"device": _device_param()}},
)
async def cast_resume(args: dict) -> dict:
    device = args.get("device") or DEFAULT_DEVICE
    await asyncio.to_thread(lambda: _get_device(device).media_controller.play())
    return _text(f"Reanudado en '{device}'.")


@tool(
    "cast_stop",
    "Detiene la reproducción en un altavoz Google Cast.",
    {"type": "object", "properties": {"device": _device_param()}},
)
async def cast_stop(args: dict) -> dict:
    device = args.get("device") or DEFAULT_DEVICE
    await asyncio.to_thread(lambda: _get_device(device).media_controller.stop())
    return _text(f"Detenido en '{device}'.")


@tool(
    "cast_set_volume",
    "Ajusta el volumen (0-100) de un altavoz Google Cast.",
    {
        "type": "object",
        "properties": {
            "level": {
                "type": "integer",
                "description": "Volumen de 0 a 100.",
                "minimum": 0,
                "maximum": 100,
            },
            "device": _device_param(),
        },
        "required": ["level"],
    },
)
async def cast_set_volume(args: dict) -> dict:
    level = max(0, min(100, int(args["level"])))
    device = args.get("device") or DEFAULT_DEVICE
    await asyncio.to_thread(lambda: _get_device(device).set_volume(level / 100.0))
    return _text(f"Volumen de '{device}' al {level}%.")


@tool(
    "cast_status",
    "Indica qué está sonando y el estado del reproductor en un altavoz Google Cast.",
    {"type": "object", "properties": {"device": _device_param()}},
)
async def cast_status(args: dict) -> dict:
    device = args.get("device") or DEFAULT_DEVICE

    def _status() -> str:
        cc = _get_device(device)
        st = cc.media_controller.status
        state = st.player_state or "desconocido"
        title = st.title or "—"
        return f"'{device}': estado={state}, título={title}, volumen={round((cc.status.volume_level or 0) * 100)}%"

    return _text(await asyncio.to_thread(_status))


@tool(
    "cast_seek",
    "Salta a un punto concreto de lo que suena (posición ABSOLUTA en segundos "
    "desde el principio). P. ej. 've al minuto 2' → 120.",
    {
        "type": "object",
        "properties": {
            "position": {
                "type": "integer",
                "description": "Segundos desde el inicio de la pista (>= 0).",
                "minimum": 0,
            },
            "device": _device_param(),
        },
        "required": ["position"],
    },
)
async def cast_seek(args: dict) -> dict:
    position = max(0, int(args["position"]))
    device = args.get("device") or DEFAULT_DEVICE

    def _seek() -> str:
        mc = _get_device(device).media_controller
        mc.update_status()
        duration = mc.status.duration or 0
        target = min(position, int(duration)) if duration else position
        mc.seek(target)
        return f"En '{device}', saltado al segundo {target}."

    return _text(await asyncio.to_thread(_seek))


@tool(
    "cast_seek_relative",
    "Adelanta o retrocede lo que suena un número de segundos respecto al punto "
    "actual. Positivo adelanta ('adelanta 30' → 30), negativo retrocede "
    "('retrocede 15' → -15).",
    {
        "type": "object",
        "properties": {
            "offset": {
                "type": "integer",
                "description": "Segundos a saltar: positivo adelanta, negativo retrocede.",
            },
            "device": _device_param(),
        },
        "required": ["offset"],
    },
)
async def cast_seek_relative(args: dict) -> dict:
    offset = int(args["offset"])
    device = args.get("device") or DEFAULT_DEVICE

    def _seek() -> str:
        mc = _get_device(device).media_controller
        mc.update_status()
        st = mc.status
        current = st.adjusted_current_time or st.current_time or 0
        duration = st.duration or 0
        target = current + offset
        target = max(0, target)
        if duration:
            target = min(target, int(duration))
        mc.seek(int(target))
        verbo = "Adelantado" if offset >= 0 else "Retrocedido"
        return f"{verbo} en '{device}' al segundo {int(target)}."

    return _text(await asyncio.to_thread(_seek))


# --------------------------------------------------------------------------- #
# Servidor MCP en proceso
# --------------------------------------------------------------------------- #
_MEDIA_TOOLS = [
    cast_list_devices,
    cast_play_url,
    cast_pause,
    cast_resume,
    cast_stop,
    cast_set_volume,
    cast_status,
    cast_seek,
    cast_seek_relative,
]

# Nombre del servidor MCP. Los nombres de herramienta para allowed_tools quedan
# como  mcp__media__<tool>.
SERVER_NAME = "media"

# Nombres completos de las herramientas, para la lista blanca del núcleo.
MEDIA_TOOL_NAMES = [
    f"mcp__{SERVER_NAME}__cast_list_devices",
    f"mcp__{SERVER_NAME}__cast_play_url",
    f"mcp__{SERVER_NAME}__cast_pause",
    f"mcp__{SERVER_NAME}__cast_resume",
    f"mcp__{SERVER_NAME}__cast_stop",
    f"mcp__{SERVER_NAME}__cast_set_volume",
    f"mcp__{SERVER_NAME}__cast_status",
    f"mcp__{SERVER_NAME}__cast_seek",
    f"mcp__{SERVER_NAME}__cast_seek_relative",
]


def build_media_server():
    """Crea el servidor MCP en proceso con las herramientas de medios."""
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_MEDIA_TOOLS)
