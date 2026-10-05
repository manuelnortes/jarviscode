"""Capacidad de música manos libres en el salón vía YouTube + Google Cast.

Resuelve la petición del usuario ("pon música de X en el salón") sin depender de
Spotify (que no arranca en frío un Cast en reposo). Usa el Home Mini ("Salón")
como altavoz real reproduciendo audio por stream con el Default Media Receiver.

Cómo funciona la cola (modo radio):
  - `ytsearch1:<consulta>` resuelve la canción SEMILLA.
  - Se construye el Mix `RD<id>` y se expande con `--flat-playlist` → lista de
    pistas afines (mismo artista/género), NO N copias de la misma canción (que es
    lo que daría `ytsearchN`). Se recorta a N≈40 (el Mix anónimo se desvía de
    género más allá).
  - Las URLs de audio de YouTube caducan (~horas) y son por-IP, así que se
    resuelven JUST-IN-TIME (al ir a sonar), no todas de golpe.

Encadenado sin silencio (watcher):
  El Default Media Receiver NO tiene autoplay. Un watcher asyncio hace polling del
  estado del media_controller. Como el estado expone `current_time`/`duration`,
  el watcher sabe cuánto queda y PRE-RESUELVE la siguiente pista ~15-20 s antes
  del final; al terminar la actual (`idle_reason == FINISHED`), castea la URL ya
  resuelta → arranque casi instantáneo.

  Las paradas/saltos manuales se gestionan con flags de control explícitos
  (`_command` + un `asyncio.Event` para despertar al watcher al instante), NO por
  `idle_reason`, para que "para la música" no reviva la cola.

Auth: ninguna. El de-risk demostró que funciona anónimo desde el NUC (sin cookies
ni token). Las cookies de YouTube serían una mejora opcional futura (radio más
fina en el género), no un requisito.

Las llamadas de yt-dlp y pychromecast son bloqueantes (red), así que se ejecutan
en un hilo aparte con asyncio.to_thread para no bloquear el bucle de eventos.
"""

from __future__ import annotations

import asyncio

import yt_dlp
from claude_agent_sdk import create_sdk_mcp_server, tool

from src.capabilities import media_cast

# Altavoz por defecto (mismo que media_cast / spotify).
DEFAULT_DEVICE = "Salón"

# Cuántas pistas del Mix conservamos. El Mix anónimo se va de género más allá de
# ~150; las primeras decenas son las buenas.
_QUEUE_SIZE = 40

# Cada cuánto sondea el watcher el estado del reproductor, LEJOS del final de la
# pista (segundos). Cada poll es solo un GET_STATUS sobre el socket ya abierto con
# el Cast (coste de red nimio, CPU/memoria despreciables), así que un intervalo
# holgado ahorra tráfico sin penalizar nada: la reactividad que importa (cazar el
# fin de la pista) la da _POLL_INTERVAL_NEAR, no este.
_POLL_INTERVAL = 5.0

# Intervalo de sondeo REDUCIDO cerca del final de la pista. Con el poll largo (3 s)
# el watcher puede tardar hasta ese tiempo en ver el FINISHED → silencio entre
# temas; cerca del final sondeamos a este ritmo para cazarlo antes.
_POLL_INTERVAL_NEAR = 0.5

# Cuando quedan <= estos segundos de la pista actual, el watcher pasa a sondear con
# _POLL_INTERVAL_NEAR en vez de _POLL_INTERVAL (poll adaptativo).
_NEAR_END_THRESHOLD = 8.0

# Margen antes del final de la pista para pre-resolver la siguiente (segundos).
_PREFETCH_LEAD = 18.0

# Mapeo de extensión de yt-dlp → content-type para el Cast. YouTube da
# normalmente opus/webm o m4a; el Home Mini reproduce Opus/AAC/MP3.
_EXT_CONTENT_TYPE = {
    "webm": "audio/webm",
    "opus": "audio/webm",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
}

# --------------------------------------------------------------------------- #
# Estado de la cola (en memoria del proceso del server)
# --------------------------------------------------------------------------- #
# Entradas del Mix: lista de {"id": str, "title": str} (sin URLs; se resuelven al
# vuelo). `_index` es la pista que está sonando ahora.
_queue: list[dict] = []
_index: int = 0
_device: str = DEFAULT_DEVICE

# Tarea del watcher y canal de mando hacia él.
_watcher_task: "asyncio.Task | None" = None
_command: "str | None" = None  # None | "skip" | "stop"
_wake: "asyncio.Event | None" = None  # despierta al watcher sin esperar al poll


def _text(message: str) -> dict:
    """Envuelve un texto en el formato de respuesta que espera el SDK MCP."""
    return {"content": [{"type": "text", "text": message}]}


def _device_param() -> dict:
    """Esquema reutilizable para el parámetro opcional 'device'."""
    return {
        "type": "string",
        "description": f"Nombre del altavoz Google Cast. Por defecto: '{DEFAULT_DEVICE}'.",
    }


# --------------------------------------------------------------------------- #
# Helpers de yt-dlp (BLOQUEANTES — se llaman vía asyncio.to_thread)
# --------------------------------------------------------------------------- #
def _resolve_seed(query: str) -> dict:
    """Resuelve la canción semilla de una consulta (1er resultado de búsqueda).

    Returns:
        {"id": videoId, "title": título}.

    Raises:
        LookupError: Si la búsqueda no devuelve nada.
    """
    opts = {"quiet": True, "skip_download": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    entries = info.get("entries") or []
    if not entries:
        raise LookupError(f"No encontré nada en YouTube para '{query}'.")
    e = entries[0]
    return {"id": e["id"], "title": e.get("title") or query}


def _expand_mix(seed_id: str, seed_title: str) -> list[dict]:
    """Expande el Mix RD<id> a una cola de pistas afines (plano, sin audio).

    La semilla siempre encabeza la cola. Se deduplican ids y se recorta a
    _QUEUE_SIZE.
    """
    mix_url = f"https://www.youtube.com/watch?v={seed_id}&list=RD{seed_id}"
    opts = {"quiet": True, "skip_download": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        mix = ydl.extract_info(mix_url, download=False)

    queue: list[dict] = []
    seen: set[str] = set()
    for e in mix.get("entries") or []:
        if not e:
            continue
        vid = e.get("id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        queue.append({"id": vid, "title": e.get("title") or "—"})
        if len(queue) >= _QUEUE_SIZE:
            break

    # Garantiza que la semilla va primera (algunos Mixes la omiten o reordenan).
    if not queue or queue[0]["id"] != seed_id:
        queue = [{"id": seed_id, "title": seed_title}] + [
            q for q in queue if q["id"] != seed_id
        ]
        queue = queue[:_QUEUE_SIZE]
    return queue


def _resolve_audio(video_id: str) -> dict:
    """Resuelve la URL de audio directa de un vídeo (just-in-time).

    Returns:
        {"url", "content_type", "title", "thumb"}.
    """
    opts = {"quiet": True, "skip_download": True, "format": "bestaudio"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )
    ext = (info.get("ext") or "").lower()
    return {
        "url": info["url"],
        "content_type": _EXT_CONTENT_TYPE.get(ext, "audio/webm"),
        "title": info.get("title") or "—",
        "thumb": info.get("thumbnail"),
    }


def _cast_track(device: str, audio: dict) -> None:
    """Castea una pista ya resuelta al altavoz (bloqueante).

    Reutiliza la conexión Cast compartida de media_cast (mismo Zeroconf y caché).
    """
    cc = media_cast.get_device(device)
    cc.media_controller.play_media(
        audio["url"],
        audio["content_type"],
        title=audio["title"],
        thumb=audio.get("thumb"),
    )
    cc.media_controller.block_until_active(timeout=15)


def _read_status(device: str) -> dict:
    """Lee el estado fresco del reproductor (bloqueante)."""
    cc = media_cast.get_device(device)
    cc.media_controller.update_status()
    st = cc.media_controller.status
    return {
        "state": st.player_state,
        "idle_reason": st.idle_reason,
        "current": st.adjusted_current_time or st.current_time or 0,
        "duration": st.duration or 0,
    }


def _stop_cast(device: str) -> None:
    """Detiene la reproducción (bloqueante)."""
    try:
        media_cast.get_device(device).media_controller.stop()
    except Exception:
        pass  # ya estaba parado / sin sesión


# --------------------------------------------------------------------------- #
# Watcher: encadena la cola con pre-resolución anticipada
# --------------------------------------------------------------------------- #
async def _watcher_loop() -> None:
    """Vigila la pista actual y encadena la siguiente del Mix sin silencio.

    Asume que la pista en `_index` YA está sonando (la lanzó youtube_play). No
    re-castea la actual: espera a que termine (o a una orden manual) y avanza.
    """
    global _index, _command
    prefetched: "dict | None" = None  # audio ya resuelto de la SIGUIENTE pista
    started = False  # ¿hemos visto ya PLAYING en la pista actual?
    poll = _POLL_INTERVAL  # se acorta cerca del final de la pista (poll adaptativo)

    while True:
        # Espera al próximo poll, pero despierta antes si llega una orden.
        try:
            await asyncio.wait_for(_wake.wait(), timeout=poll)
        except asyncio.TimeoutError:
            pass
        _wake.clear()

        # --- Órdenes manuales (tienen prioridad sobre el estado del receptor) ---
        if _command == "stop":
            _command = None
            await asyncio.to_thread(_stop_cast, _device)
            return
        if _command == "skip":
            _command = None
            if not await _advance(prefetched):
                return
            prefetched = None
            started = False
            continue
        if _command == "prev":
            _command = None
            await _retreat()
            prefetched = None
            started = False
            continue

        # --- Estado del reproductor ---
        try:
            st = await asyncio.to_thread(_read_status, _device)
        except Exception:
            continue  # fallo puntual de red; reintenta en el siguiente poll

        state = st["state"]
        if state == "PLAYING":
            started = True

        remaining = st["duration"] - st["current"]

        # Poll adaptativo: cerca del final sondeamos más a menudo para cazar el
        # FINISHED con menos retardo (menos silencio entre temas). Lejos del final,
        # el intervalo largo evita machacar al receptor. El guard `duration > 0`
        # protege el arranque de pista, cuando aún no hay metadatos (duration=0).
        if state == "PLAYING" and st["duration"] > 0 and remaining <= _NEAR_END_THRESHOLD:
            poll = _POLL_INTERVAL_NEAR
        else:
            poll = _POLL_INTERVAL

        # Pre-resolución anticipada de la siguiente pista.
        if (
            state == "PLAYING"
            and st["duration"] > 0
            and remaining <= _PREFETCH_LEAD
            and prefetched is None
            and _index + 1 < len(_queue)
        ):
            try:
                prefetched = await asyncio.to_thread(
                    _resolve_audio, _queue[_index + 1]["id"]
                )
            except Exception:
                prefetched = None  # se reintenta al avanzar

        # Fin natural de la pista → avanzar.
        if started and state == "IDLE" and st["idle_reason"] == "FINISHED":
            if not await _advance(prefetched):
                return
            prefetched = None
            started = False


async def _advance(prefetched: "dict | None") -> bool:
    """Avanza a la siguiente pista de la cola y la castea.

    Args:
        prefetched: Audio ya resuelto de la siguiente pista, si lo había.

    Returns:
        True si reprodujo la siguiente; False si la cola se ha terminado.
    """
    global _index
    _index += 1
    if _index >= len(_queue):
        return False

    audio = prefetched
    if audio is None:
        try:
            audio = await asyncio.to_thread(_resolve_audio, _queue[_index]["id"])
        except Exception:
            # Pista no disponible: salta a la siguiente.
            return await _advance(None)
    await asyncio.to_thread(_cast_track, _device, audio)
    return True


async def _retreat() -> None:
    """Retrocede a la pista anterior y la castea desde el principio.

    Si ya estamos en la primera pista, no retrocede: re-castea la actual (efecto
    "volver al inicio de la canción", como un reproductor normal). Nunca termina
    la sesión.
    """
    global _index
    if _index > 0:
        _index -= 1
    try:
        audio = await asyncio.to_thread(_resolve_audio, _queue[_index]["id"])
        await asyncio.to_thread(_cast_track, _device, audio)
    except Exception:
        pass  # pista no disponible puntualmente; el watcher seguirá vivo


def _stop_watcher() -> None:
    """Cancela el watcher en curso, si lo hay."""
    global _watcher_task
    if _watcher_task is not None and not _watcher_task.done():
        _watcher_task.cancel()
    _watcher_task = None


# --------------------------------------------------------------------------- #
# Herramientas expuestas a Jarvis
# --------------------------------------------------------------------------- #
@tool(
    "youtube_play",
    "Pone música en un altavoz del salón vía YouTube (modo radio): suena la "
    "canción/artista pedido y luego temas PARECIDOS (mismo artista, género, "
    "afines), sin repetir. Úsalo para 'pon música de X en el salón'. El altavoz "
    f"por defecto es '{DEFAULT_DEVICE}'.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Qué poner: artista, canción o género. P. ej. 'Estopa' o 'rumba'.",
            },
            "device": _device_param(),
        },
        "required": ["query"],
    },
)
async def youtube_play(args: dict) -> dict:
    global _queue, _index, _device, _command, _wake, _watcher_task
    query = (args.get("query") or "").strip()
    if not query:
        return _text("Dime qué quieres escuchar.")
    device = args.get("device") or DEFAULT_DEVICE

    # Para cualquier sesión anterior antes de empezar otra.
    _stop_watcher()
    _command = None

    # Construir la cola desde el Mix (semilla → radio).
    try:
        seed = await asyncio.to_thread(_resolve_seed, query)
        queue = await asyncio.to_thread(_expand_mix, seed["id"], seed["title"])
    except Exception as exc:
        return _text(f"No pude preparar la música: {exc}")

    # Reproducir la primera pista de forma síncrona (para confirmar el título).
    try:
        first = await asyncio.to_thread(_resolve_audio, queue[0]["id"])
        await asyncio.to_thread(_cast_track, device, first)
    except Exception as exc:
        return _text(f"No pude reproducir la primera canción: {exc}")

    # Fijar estado y arrancar el watcher para encadenar el resto.
    _queue, _index, _device = queue, 0, device
    _wake = asyncio.Event()
    _watcher_task = asyncio.create_task(_watcher_loop())

    return _text(
        f"Sonando en '{device}': {first['title']}. "
        f"Y a continuación una lista de {len(queue)} temas parecidos."
    )


@tool(
    "youtube_skip",
    "Salta a la siguiente canción de la lista de YouTube que está sonando.",
    {"type": "object", "properties": {}},
)
async def youtube_skip(args: dict) -> dict:
    global _command
    if _watcher_task is None or _watcher_task.done():
        return _text("Ahora mismo no hay ninguna lista de YouTube sonando.")
    if _index + 1 >= len(_queue):
        return _text("Es la última canción de la lista.")
    _command = "skip"
    _wake.set()
    nxt = _queue[_index + 1]["title"]
    return _text(f"Siguiente: {nxt}.")


@tool(
    "youtube_stop",
    "Para la música de YouTube y cancela la lista (modo radio). Usa esto para "
    "'para la música' cuando lo que suena viene de YouTube.",
    {"type": "object", "properties": {}},
)
async def youtube_stop(args: dict) -> dict:
    global _command
    if _watcher_task is None or _watcher_task.done():
        # No hay watcher, pero por si quedó algo sonando, lo paramos igual.
        await asyncio.to_thread(_stop_cast, _device)
        return _text("No había ninguna lista activa; he parado el altavoz por si acaso.")
    _command = "stop"
    _wake.set()
    return _text("Música parada.")


# --------------------------------------------------------------------------- #
# API programática para la UI web (widget de control multimedia)
# --------------------------------------------------------------------------- #
# El widget de la UI web controla la MISMA sesión que la voz (estado de módulo).
# Estas funciones corren en el event loop del server (uvicorn), el mismo donde
# vive el watcher, así que pueden mandarle órdenes (`_command` + `_wake`) sin
# condiciones de carrera.
def _is_active() -> bool:
    """¿Hay una sesión de música activa (watcher vivo)?"""
    return _watcher_task is not None and not _watcher_task.done()


def _read_full_status(device: str) -> dict:
    """Lee el estado enriquecido del reproductor (media + volumen). Bloqueante."""
    cc = media_cast.get_device(device)
    cc.media_controller.update_status()
    st = cc.media_controller.status
    return {
        "state": st.player_state or "UNKNOWN",
        "position": st.adjusted_current_time or st.current_time or 0,
        "duration": st.duration or 0,
        "volume": round((cc.status.volume_level or 0) * 100),
    }


async def get_state() -> dict:
    """Devuelve el estado de la música para la UI web.

    Si no hay sesión activa devuelve ``{"active": False}`` SIN tocar el Cast (el
    widget hace polling constante; así el sondeo es barato cuando no suena nada).

    Returns:
        Estado serializable: title, índice/total, siguiente, estado del player,
        posición/duración (s) y volumen (0-100).
    """
    if not _is_active():
        return {"active": False}
    cur = _queue[_index] if 0 <= _index < len(_queue) else {}
    nxt = _queue[_index + 1] if _index + 1 < len(_queue) else None
    state = {
        "active": True,
        "device": _device,
        "title": cur.get("title", "—"),
        "index": _index,
        "total": len(_queue),
        "next_title": nxt["title"] if nxt else None,
        "has_prev": _index > 0,
    }
    try:
        state.update(await asyncio.to_thread(_read_full_status, _device))
    except Exception:
        # El Cast no responde puntualmente: devolvemos lo que sabemos de la cola.
        state.update({"state": "UNKNOWN", "position": 0, "duration": 0, "volume": 0})
    return state


async def command(action: str, value=None) -> dict:
    """Ejecuta una orden de control desde la UI web.

    Args:
        action: ``pause`` | ``resume`` | ``skip`` | ``prev`` | ``stop`` |
            ``seek`` (value = segundos) | ``volume`` (value = 0-100).
        value: Argumento numérico para seek/volume.

    Returns:
        ``{"ok": bool, "error"?: str}``.
    """
    global _command
    if not _is_active() and action != "stop":
        return {"ok": False, "error": "No hay música activa."}
    device = _device

    try:
        if action == "pause":
            await asyncio.to_thread(
                lambda: media_cast.get_device(device).media_controller.pause()
            )
        elif action == "resume":
            await asyncio.to_thread(
                lambda: media_cast.get_device(device).media_controller.play()
            )
        elif action == "skip":
            if _index + 1 >= len(_queue):
                return {"ok": False, "error": "Es la última canción."}
            _command = "skip"
            _wake.set()
        elif action == "prev":
            _command = "prev"
            _wake.set()
        elif action == "stop":
            if _is_active():
                _command = "stop"
                _wake.set()
            else:
                await asyncio.to_thread(_stop_cast, device)
        elif action == "seek":
            pos = max(0, int(value or 0))
            await asyncio.to_thread(
                lambda: media_cast.get_device(device).media_controller.seek(pos)
            )
        elif action == "volume":
            level = max(0, min(100, int(value or 0)))
            await asyncio.to_thread(
                lambda: media_cast.get_device(device).set_volume(level / 100.0)
            )
        else:
            return {"ok": False, "error": f"Acción desconocida: {action}"}
    except Exception as exc:  # noqa: BLE001 - se reporta al cliente
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Servidor MCP en proceso
# --------------------------------------------------------------------------- #
_YOUTUBE_TOOLS = [youtube_play, youtube_skip, youtube_stop]

# Nombre del servidor MCP. Las herramientas quedan como mcp__youtube__<tool>.
SERVER_NAME = "youtube"

# Nombres completos para la lista blanca del núcleo.
YOUTUBE_TOOL_NAMES = [
    f"mcp__{SERVER_NAME}__youtube_play",
    f"mcp__{SERVER_NAME}__youtube_skip",
    f"mcp__{SERVER_NAME}__youtube_stop",
]


def build_youtube_server():
    """Crea el servidor MCP en proceso con las herramientas de YouTube."""
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_YOUTUBE_TOOLS)
