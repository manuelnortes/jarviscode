"""Capacidad de control de reproducción de Spotify (vía Spotify Connect).

Permite a Jarvis reproducir y controlar música en los dispositivos Spotify
Connect del usuario. El **Google Home "Salón" aparece como un dispositivo Spotify
Connect**, así que es el destino por defecto (igual que en la capacidad de Cast).

Autenticación:
  OAuth Authorization Code con `spotipy.SpotifyOAuth`. El token se cachea en disco
  (`SPOTIFY_CACHE`) y se auto-refresca. La autorización inicial (interactiva, abre
  el navegador) se hace UNA vez con `scripts/spotify_auth.py`; aquí ya solo se lee
  el token cacheado (`open_browser=False`), por lo que el contenedor headless nunca
  intenta abrir un navegador.

  Necesita una app de Spotify Developer (Client ID/Secret + redirect URI dado de
  alta) y pide scopes de **control de reproducción**.

Config por entorno:
  SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET   Credenciales de la app.
  SPOTIFY_REDIRECT_URI                        Por defecto http://127.0.0.1:8888/callback.
  SPOTIFY_CACHE                               Ruta del token cacheado (en Docker, volumen).

Las llamadas de spotipy son bloqueantes (red), así que se ejecutan en un hilo
aparte con asyncio.to_thread para no bloquear el bucle de eventos.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import spotipy
from claude_agent_sdk import create_sdk_mcp_server, tool
from spotipy.oauth2 import SpotifyOAuth

# Dispositivo Spotify Connect por defecto (nombre tal y como aparece en Spotify).
DEFAULT_DEVICE = "Salón"

# Scopes necesarios para LEER el estado y CONTROLAR la reproducción.
_SCOPE = "user-read-playback-state user-modify-playback-state user-read-currently-playing"

# Ruta del token cacheado. En Docker se sobrescribe hacia el volumen jarvis-data.
_DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "data" / "spotify-cache"
_CACHE_PATH = os.getenv("SPOTIFY_CACHE", str(_DEFAULT_CACHE))

_client: "spotipy.Spotify | None" = None


def make_auth(open_browser: bool = False) -> SpotifyOAuth:
    """Construye el gestor OAuth de spotipy con la config del entorno.

    Args:
        open_browser: Si True (solo en el script de auth), abre el navegador para
            la autorización inicial. En el servicio se deja en False.

    Raises:
        RuntimeError: Si faltan las credenciales de la app.
    """
    cid = os.getenv("SPOTIFY_CLIENT_ID")
    secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    redirect = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    if not (cid and secret):
        raise RuntimeError(
            "Faltan SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET en el entorno (.env)."
        )
    Path(_CACHE_PATH).parent.mkdir(parents=True, exist_ok=True)
    return SpotifyOAuth(
        client_id=cid,
        client_secret=secret,
        redirect_uri=redirect,
        scope=_SCOPE,
        cache_path=_CACHE_PATH,
        open_browser=open_browser,
    )


def _get_client() -> "spotipy.Spotify":
    """Devuelve el cliente spotipy compartido (token cacheado, auto-refresco)."""
    global _client
    if _client is None:
        _client = spotipy.Spotify(auth_manager=make_auth(open_browser=False))
    return _client


def _resolve_device_id(sp: "spotipy.Spotify", name: str) -> tuple[str | None, str | None]:
    """Resuelve el id de un dispositivo Spotify Connect por nombre.

    Returns:
        Tupla (device_id, error). Si no se encuentra, device_id es None y error
        lleva un mensaje legible (con la lista de disponibles).
    """
    devices = sp.devices().get("devices", [])
    if not devices:
        return None, (
            "No hay dispositivos Spotify activos. Abre Spotify en el altavoz "
            "(o en el móvil) para que aparezca como dispositivo."
        )
    lname = name.lower()
    for dev in devices:
        if dev["name"].lower() == lname:
            return dev["id"], None
    for dev in devices:  # coincidencia parcial como respaldo
        if lname in dev["name"].lower():
            return dev["id"], None
    available = ", ".join(d["name"] for d in devices)
    return None, f"No encontré el dispositivo '{name}'. Disponibles: {available}."


def _text(message: str) -> dict:
    """Envuelve un texto en el formato de respuesta que espera el SDK MCP."""
    return {"content": [{"type": "text", "text": message}]}


def _device_param() -> dict:
    """Esquema reutilizable para el parámetro opcional 'device'."""
    return {
        "type": "string",
        "description": f"Nombre del dispositivo Spotify Connect. Por defecto: '{DEFAULT_DEVICE}'.",
    }


# --------------------------------------------------------------------------- #
# Herramientas expuestas a Jarvis
# --------------------------------------------------------------------------- #
@tool(
    "spotify_devices",
    "Lista los dispositivos Spotify Connect disponibles (altavoces, móvil, etc.).",
    {"type": "object", "properties": {}},
)
async def spotify_devices(args: dict) -> dict:
    def _list() -> str:
        devices = _get_client().devices().get("devices", [])
        if not devices:
            return "No hay dispositivos Spotify activos ahora mismo."
        return "Dispositivos Spotify: " + ", ".join(
            f"{d['name']}{' (activo)' if d.get('is_active') else ''}" for d in devices
        )

    return _text(await asyncio.to_thread(_list))


@tool(
    "spotify_play",
    "Reproduce música en Spotify. Pasa 'query' para buscar (canción/artista) o "
    "'uri' para un recurso concreto (track/álbum/playlist/artista). Suena en el "
    f"dispositivo indicado, por defecto '{DEFAULT_DEVICE}'.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Texto a buscar, p. ej. 'Bohemian Rhapsody' o 'jazz suave'.",
            },
            "uri": {
                "type": "string",
                "description": "URI de Spotify (spotify:track:..., spotify:playlist:..., etc.).",
            },
            "device": _device_param(),
        },
    },
)
async def spotify_play(args: dict) -> dict:
    query = args.get("query")
    uri = args.get("uri")
    device = args.get("device") or DEFAULT_DEVICE
    if not query and not uri:
        return _text("Indica qué reproducir: 'query' (búsqueda) o 'uri'.")

    def _play() -> str:
        sp = _get_client()
        device_id, err = _resolve_device_id(sp, device)
        if err:
            return err

        target_uri = uri
        label = uri
        if query and not uri:
            results = sp.search(q=query, type="track", limit=1)
            items = results.get("tracks", {}).get("items", [])
            if not items:
                return f"No encontré nada en Spotify para '{query}'."
            track = items[0]
            target_uri = track["uri"]
            label = f"{track['name']} — {track['artists'][0]['name']}"

        # Una pista va como lista de uris; álbum/playlist/artista como contexto.
        if target_uri.startswith("spotify:track:") or ":track:" in target_uri:
            sp.start_playback(device_id=device_id, uris=[target_uri])
        else:
            sp.start_playback(device_id=device_id, context_uri=target_uri)
        return f"Reproduciendo en '{device}': {label}."

    return _text(await asyncio.to_thread(_play))


@tool(
    "spotify_pause",
    "Pausa la reproducción de Spotify.",
    {"type": "object", "properties": {"device": _device_param()}},
)
async def spotify_pause(args: dict) -> dict:
    device = args.get("device") or DEFAULT_DEVICE

    def _pause() -> str:
        sp = _get_client()
        device_id, err = _resolve_device_id(sp, device)
        if err:
            return err
        sp.pause_playback(device_id=device_id)
        return f"Pausado en '{device}'."

    return _text(await asyncio.to_thread(_pause))


@tool(
    "spotify_resume",
    "Reanuda la reproducción pausada de Spotify.",
    {"type": "object", "properties": {"device": _device_param()}},
)
async def spotify_resume(args: dict) -> dict:
    device = args.get("device") or DEFAULT_DEVICE

    def _resume() -> str:
        sp = _get_client()
        device_id, err = _resolve_device_id(sp, device)
        if err:
            return err
        sp.start_playback(device_id=device_id)
        return f"Reanudado en '{device}'."

    return _text(await asyncio.to_thread(_resume))


@tool(
    "spotify_next",
    "Salta a la siguiente canción en Spotify.",
    {"type": "object", "properties": {"device": _device_param()}},
)
async def spotify_next(args: dict) -> dict:
    device = args.get("device") or DEFAULT_DEVICE

    def _next() -> str:
        sp = _get_client()
        device_id, err = _resolve_device_id(sp, device)
        if err:
            return err
        sp.next_track(device_id=device_id)
        return "Siguiente canción."

    return _text(await asyncio.to_thread(_next))


@tool(
    "spotify_previous",
    "Vuelve a la canción anterior en Spotify.",
    {"type": "object", "properties": {"device": _device_param()}},
)
async def spotify_previous(args: dict) -> dict:
    device = args.get("device") or DEFAULT_DEVICE

    def _prev() -> str:
        sp = _get_client()
        device_id, err = _resolve_device_id(sp, device)
        if err:
            return err
        sp.previous_track(device_id=device_id)
        return "Canción anterior."

    return _text(await asyncio.to_thread(_prev))


@tool(
    "spotify_set_volume",
    "Ajusta el volumen (0-100) de la reproducción de Spotify.",
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
async def spotify_set_volume(args: dict) -> dict:
    level = max(0, min(100, int(args["level"])))
    device = args.get("device") or DEFAULT_DEVICE

    def _vol() -> str:
        sp = _get_client()
        device_id, err = _resolve_device_id(sp, device)
        if err:
            return err
        sp.volume(level, device_id=device_id)
        return f"Volumen de '{device}' al {level}%."

    return _text(await asyncio.to_thread(_vol))


@tool(
    "spotify_now_playing",
    "Indica qué canción está sonando ahora mismo en Spotify.",
    {"type": "object", "properties": {}},
)
async def spotify_now_playing(args: dict) -> dict:
    def _now() -> str:
        pb = _get_client().current_playback()
        if not pb or not pb.get("item"):
            return "Ahora mismo no hay nada sonando en Spotify."
        item = pb["item"]
        artists = ", ".join(a["name"] for a in item.get("artists", []))
        state = "sonando" if pb.get("is_playing") else "en pausa"
        device = pb.get("device", {}).get("name", "—")
        return f"{state.capitalize()} en '{device}': {item['name']} — {artists}."

    return _text(await asyncio.to_thread(_now))


# --------------------------------------------------------------------------- #
# Servidor MCP en proceso
# --------------------------------------------------------------------------- #
_SPOTIFY_TOOLS = [
    spotify_devices,
    spotify_play,
    spotify_pause,
    spotify_resume,
    spotify_next,
    spotify_previous,
    spotify_set_volume,
    spotify_now_playing,
]

# Nombre del servidor MCP. Las herramientas quedan como mcp__spotify__<tool>.
SERVER_NAME = "spotify"

# Nombres completos para la lista blanca del núcleo.
SPOTIFY_TOOL_NAMES = [
    f"mcp__{SERVER_NAME}__spotify_devices",
    f"mcp__{SERVER_NAME}__spotify_play",
    f"mcp__{SERVER_NAME}__spotify_pause",
    f"mcp__{SERVER_NAME}__spotify_resume",
    f"mcp__{SERVER_NAME}__spotify_next",
    f"mcp__{SERVER_NAME}__spotify_previous",
    f"mcp__{SERVER_NAME}__spotify_set_volume",
    f"mcp__{SERVER_NAME}__spotify_now_playing",
]


def build_spotify_server():
    """Crea el servidor MCP en proceso con las herramientas de Spotify."""
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_SPOTIFY_TOOLS)
