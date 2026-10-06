"""API HTTP/WebSocket del núcleo de Jarvis.

Expone `JarvisCore` por red para que los front-ends (texto ahora, voz después)
sean clientes desacoplados en vez de importar el núcleo en proceso. La primitiva
principal es un WebSocket: cada conexión abre UNA sesión `JarvisCore` con memoria
multi-turno, y la respuesta se transmite por fragmentos (streaming), que es justo
lo que el front-end de voz necesitará para alimentar el TTS.

Arrancar (con el venv activado, desde el directorio del proyecto):
    python -m src.core.server

Protocolo del WebSocket (mensajes JSON):
    Cliente  -> {"type": "ask", "prompt": "..."}
                {"type": "reset_session"}               # botón "nueva conversación"
    Servidor -> {"type": "chunk",  "text":  "..."}     # 0..N por respuesta (streaming)
                {"type": "tools",  "tools": [...]}      # herramientas usadas en el turno
                {"type": "done",   "model": "..."}      # fin del turno
                {"type": "session_reset", "reason": "manual"|"ended"}  # de vuelta a Haiku
                {"type": "error",  "message": "..."}    # algo falló

Reset de sesión (Sub-hito 3.6.1): igual que en el WS de voz, el path de texto
recicla su `JarvisCore` para volver a Haiku sin recargar — por mensaje de control
``reset_session`` (botón) o al detectar una frase de cierre ("gracias"/"adiós").
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from src.capabilities import youtube as media_player
from src.capabilities.reminders import shutdown_scheduler, start_scheduler
from src.core import tts_store
from src.core.jarvis import JarvisConfig, JarvisCore
from src.core.session import FAREWELL, is_closing
from src.core.voice_ws import get_models_async, voice_endpoint

# Directorio del frontend web (UI de voz, Hito 3.6). Se sirve estático.
_WEB_DIR = Path(__file__).resolve().parents[1] / "web"

# Bind por defecto en desarrollo. Solo localhost: la API no se expone fuera hasta
# que se añada autenticación (ver PLAN.md, fuera de alcance de este frente).
# En Docker (NUC) se sobrescriben por entorno: JARVIS_HOST=0.0.0.0 + puerto del stack.
DEV_HOST = "127.0.0.1"
DEV_PORT = 8200

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ciclo de vida del servidor.

    Arranca el scheduler de recordatorios al iniciar (recarga los pendientes del
    jobstore SQLite) y lo para limpiamente al apagar. Vive en el proceso del
    servidor, siempre encendido en el NUC, así dispara aunque no haya ninguna
    sesión de chat abierta.
    """
    start_scheduler()
    # Precarga de Whisper/Piper en segundo plano: así la primera sesión de voz no
    # espera la carga (~7 s con el modelo en disco; minutos si hay que descargarlo).
    # JARVIS_STT_PRELOAD=0 la desactiva (uso solo texto: no gasta ~2 GB de RAM).
    preload = None
    if os.environ.get("JARVIS_STT_PRELOAD", "1") != "0":
        preload = asyncio.create_task(_preload_voice_models())
    try:
        yield
    finally:
        if preload is not None:
            preload.cancel()
        shutdown_scheduler()


async def _preload_voice_models() -> None:
    """Carga los modelos de voz sin bloquear el arranque; un fallo solo se registra.

    Si falla, la carga se reintentará igualmente en la primera conexión de voz.
    """
    try:
        await get_models_async()
    except Exception:
        logging.getLogger("jarvis").exception("Fallo precargando los modelos de voz.")


app = FastAPI(title="Jarvis Core API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Sonda de vida para healthchecks (Docker, monitores).

    Returns:
        Estado del servicio.
    """
    return {"status": "ok", "service": "jarvis-core"}


@app.websocket("/ws")
async def ws_chat(websocket: WebSocket) -> None:
    """Sesión conversacional por WebSocket.

    Una conexión = una sesión `JarvisCore` con memoria entre turnos. Se mantiene
    abierta mientras el cliente esté conectado; al desconectar, se cierra el
    núcleo y se libera la conversación.

    Args:
        websocket: La conexión WebSocket entrante.
    """
    await websocket.accept()

    # El núcleo vive lo que dure la conexión (memoria multi-turno). Gestionado a
    # mano (no `async with`) para poder reciclarlo dentro de la misma conexión sin
    # cerrar el WS: el reset de sesión recrea el núcleo y vuelve a Haiku.
    jarvis = JarvisCore(JarvisConfig())
    await jarvis.__aenter__()

    async def reset_core(reason: str) -> None:
        """Cierra el núcleo actual y abre uno fresco (de vuelta a Haiku).

        Args:
            reason: Motivo (``manual`` por botón, ``ended`` por frase de cierre).
        """
        nonlocal jarvis
        await jarvis.__aexit__(None, None, None)
        jarvis = JarvisCore(JarvisConfig())
        await jarvis.__aenter__()
        await websocket.send_json({"type": "session_reset", "reason": reason})

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "JSON inválido."})
                continue

            msg_type = msg.get("type")
            if msg_type == "reset_session":
                # Botón "nueva conversación".
                await reset_core("manual")
                continue
            if msg_type != "ask" or not isinstance(msg.get("prompt"), str):
                await websocket.send_json(
                    {"type": "error", "message": "Se esperaba {'type':'ask','prompt': str}."}
                )
                continue

            prompt = msg["prompt"].strip()
            if not prompt:
                await websocket.send_json({"type": "error", "message": "Prompt vacío."})
                continue

            # Frase de cierre detectada → tras despedirse, reciclamos a Haiku.
            if await _handle_ask(websocket, jarvis, prompt):
                await reset_core("ended")
    except WebSocketDisconnect:
        # Desconexión normal del cliente: no es un error.
        return
    finally:
        await jarvis.__aexit__(None, None, None)


async def _handle_ask(websocket: WebSocket, jarvis: JarvisCore, prompt: str) -> bool:
    """Procesa un ``ask`` de texto y transmite la respuesta por fragmentos.

    Args:
        websocket: Conexión activa para responder.
        jarvis: Núcleo de la sesión (conserva la memoria de la conversación).
        prompt: Prompt del usuario, ya validado y sin espacios sobrantes.

    Returns:
        True si el prompt era una frase de cierre (el llamador debe reciclar el
        núcleo a Haiku tras este turno). False en cualquier otro caso.
    """
    # Frase de cierre ("gracias"/"adiós"…): no preguntamos a Claude; mandamos una
    # despedida corta, cerramos el turno y pedimos al llamador que recicle a Haiku.
    if is_closing(prompt):
        await websocket.send_json({"type": "chunk", "text": FAREWELL})
        await websocket.send_json({"type": "done", "model": jarvis.current_model})
        return True

    try:
        async for chunk in jarvis.ask(prompt):
            await websocket.send_json({"type": "chunk", "text": chunk})
        if jarvis.last_tools_used:
            await websocket.send_json({"type": "tools", "tools": jarvis.last_tools_used})
        await websocket.send_json({"type": "done", "model": jarvis.current_model})
    except Exception as exc:  # noqa: BLE001 - reportamos cualquier fallo al cliente
        await websocket.send_json({"type": "error", "message": str(exc)})
    return False


# Endpoint WS de voz (Hito 3.6): STT → núcleo → TTS server-side. Implementado en
# src/core/voice_ws.py para no engordar este módulo.
app.add_api_websocket_route("/voice", voice_endpoint)


# ── Control multimedia para la UI web (widget de música YouTube) ──────────────
# El estado de la música vive en src/capabilities/youtube.py (cola en memoria del
# proceso). Estos endpoints lo exponen al frontend, que hace polling de /media/state
# y manda órdenes a /media/command. Corren en el mismo event loop que el watcher.
@app.get("/media/state")
async def media_state() -> dict:
    """Estado de la música en curso (qué suena, posición, volumen, cola)."""
    return await media_player.get_state()


@app.post("/media/command")
async def media_command(payload: dict) -> dict:
    """Orden de control: pause/resume/skip/prev/stop/seek/volume.

    Args:
        payload: ``{"action": str, "value"?: number}``.
    """
    action = (payload or {}).get("action")
    if not action:
        return {"ok": False, "error": "Falta 'action'."}
    return await media_player.command(action, (payload or {}).get("value"))


# ── Clips de audio para el Google Cast (modo de salida `cast` del WS de voz) ──
# El WS de voz publica la respuesta como WAV en tts_store y castea al Home Mini la
# URL de este endpoint; el altavoz la descarga y reproduce. El ding del wake word
# vive bajo el id fijo `ding`. Va ANTES del mount de StaticFiles (como /media/*)
# para que el catch-all de "/" no lo ensombrezca.
@app.get("/tts/{clip_id}.wav")
async def tts_clip(clip_id: str) -> Response:
    """Sirve un clip WAV publicado por el modo cast (o el ding del wake word).

    Args:
        clip_id: Identificador del clip (``ding`` para el beep de confirmación).

    Returns:
        El WAV como ``audio/wav``, o 404 si no existe / ya expiró.
    """
    wav = tts_store.get(clip_id)
    if wav is None:
        return Response(status_code=404)
    return Response(content=wav, media_type="audio/wav")


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles que fuerza la revalidación de cada asset en el navegador.

    Las metas no-cache del HTML solo afectan al DOCUMENTO, no a los subrecursos
    (app.js, styles.css), que StaticFiles sirve con etag/last-modified pero sin
    Cache-Control. Sin esa cabecera el navegador reutiliza copias viejas sin
    revalidar y, tras un deploy, se queda con el JS/CSS anterior (había que forzar
    Ctrl+Shift+R). Con ``no-cache`` el navegador revalida siempre: 304 si no
    cambió (barato) o 200 con lo nuevo, así cada recarga normal trae la última
    versión.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# La UI web se sirve estática desde la raíz. IMPORTANTE: este mount va el ÚLTIMO,
# después de registrar /health, /ws y /voice, para que el catch-all de "/" no las
# ensombrezca. html=True sirve index.html en "/". Si el directorio aún no existe
# (entorno mínimo sin frontend) se omite para no romper el arranque del backend.
if _WEB_DIR.is_dir():
    app.mount("/", NoCacheStaticFiles(directory=str(_WEB_DIR), html=True), name="web")


def main() -> None:
    """Arranca el servidor con Uvicorn.

    El bind se lee de entorno (`JARVIS_HOST`/`JARVIS_PORT`) para que el contenedor
    pueda exponer la API en `0.0.0.0:<puerto del stack>`. Si no hay variables, usa
    los valores de desarrollo (solo localhost), que conservan el comportamiento previo.
    """
    import uvicorn

    host = os.getenv("JARVIS_HOST", DEV_HOST)
    port = int(os.getenv("JARVIS_PORT", str(DEV_PORT)))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
