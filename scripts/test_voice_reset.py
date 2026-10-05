"""Smoke-test del protocolo de reset de sesión del WS /voice (Sub-hito 3.6.1).

Valida los tres disparadores de `session_reset` SIN cargar Whisper/Piper ni llamar
al SDK: se inyectan un STT y un TTS falsos y se ejercita el endpoint con el
TestClient de Starlette (WebSocket en proceso).

Casos:
  1. Frase de cierre ("gracias") → transcript + FAREWELL + done + session_reset(ended).
  2. Inactividad (timeout=0) → en el siguiente utterance_start, session_reset(timeout).
  3. Mensaje reset_session → session_reset(manual).
  4. Downgrade real (voz): con un núcleo falso que rastrea instancias y simula el
     ratchet-up, se comprueba que tras un reset por cierre el siguiente turno lo
     atiende un núcleo NUEVO y `done.model` vuelve a Haiku (no se queda en Sonnet).
  5. Path de TEXTO (/ws): mismo downgrade que el caso 4 pero por el endpoint de
     texto, más el reset manual (botón +). Es el path de la UI web y donde el
     reset NO estaba cableado hasta el Sub-hito 3.6.1.

Uso:
    python -m scripts.test_voice_reset
"""

from __future__ import annotations

import json
import sys

import numpy as np
from fastapi.testclient import TestClient

from src.core import voice_ws
from src.core.routing import MODEL_HAIKU, MODEL_SONNET
from src.core.session import FAREWELL

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# PCM de relleno por encima del mínimo para que la utterance se procese (zeros: el
# STT es falso y no mira el audio, solo importa superar _MIN_UTTERANCE_BYTES).
_PCM = b"\x00" * (voice_ws._MIN_UTTERANCE_BYTES + 2000)


class _FakeSTT:
    """STT falso: devuelve siempre el transcript configurado."""

    def __init__(self, transcript: str) -> None:
        self.transcript = transcript

    def transcribe(self, audio: np.ndarray) -> str:  # noqa: ARG002 - ignora el audio
        return self.transcript


class _FakeTTS:
    """TTS falso: devuelve un poco de audio para que haya audio_start + bytes."""

    sample_rate = 22050

    def synthesize(self, sentence: str) -> np.ndarray:  # noqa: ARG002
        return np.zeros(256, dtype=np.float32)


class _ScriptSTT:
    """STT falso con guion: devuelve un transcript distinto por turno (en orden)."""

    def __init__(self, transcripts: list[str]) -> None:
        self._queue = list(transcripts)

    def transcribe(self, audio: np.ndarray) -> str:  # noqa: ARG002 - ignora el audio
        return self._queue.pop(0) if self._queue else ""


class _FakeCore:
    """`JarvisCore` falso que NO toca el SDK: rastrea instancias y simula el modelo.

    Cada instancia nueva arranca en Haiku (como un núcleo fresco real). `ask`
    escala a Sonnet en prompts no triviales para imitar el ratchet-up; así un
    reset (que crea una instancia nueva) demuestra el downgrade de vuelta a Haiku.
    """

    instances: list["_FakeCore"] = []

    def __init__(self, config: object | None = None) -> None:  # noqa: ARG002
        self.current_model = MODEL_HAIKU
        self.last_tools_used: list[str] = []  # lo consulta el path de texto (/ws)
        _FakeCore.instances.append(self)

    async def __aenter__(self) -> "_FakeCore":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def ask(self, prompt: str):
        # Prompt no trivial → ratchet-up a Sonnet (simulado). Trivial → Haiku.
        if "tiempo" in prompt:
            self.current_model = MODEL_SONNET
        yield f"Respuesta a: {prompt}"


def _install_models(transcript: str) -> None:
    """Inyecta STT/TTS falsos en el módulo del endpoint."""
    voice_ws.get_models = lambda: (_FakeSTT(transcript), _FakeTTS())  # type: ignore[assignment]


def _drain(ws, stop_type: str) -> list[dict]:
    """Lee mensajes del WS hasta (incluido) uno con type == stop_type.

    Los frames binarios se representan como {"type":"<binary>","len":N}.
    """
    out: list[dict] = []
    while True:
        m = ws.receive()
        if m.get("text") is not None:
            obj = json.loads(m["text"])
            out.append(obj)
            if obj.get("type") == stop_type:
                return out
        elif m.get("bytes") is not None:
            out.append({"type": "<binary>", "len": len(m["bytes"])})


def _types(msgs: list[dict]) -> list[str]:
    return [m["type"] for m in msgs]


def main() -> int:
    print("== Smoke-test · reset de sesión del WS /voice ==\n")
    from src.core.server import app

    failures = 0

    # ── Caso 1: frase de cierre ──────────────────────────────────────────────
    voice_ws.SESSION_TIMEOUT_SECONDS = 9999  # que no salte el timeout aquí
    _install_models("gracias")
    with TestClient(app).websocket_connect("/voice") as ws:
        ws.send_json({"type": "utterance_start"})
        ws.send_bytes(_PCM)
        ws.send_json({"type": "utterance_end"})
        msgs = _drain(ws, "session_reset")
    t = _types(msgs)
    reset = next(m for m in msgs if m["type"] == "session_reset")
    farewell_ok = any(m.get("type") == "reply_text" and FAREWELL in m.get("text", "") for m in msgs)
    ok1 = (
        "transcript" in t and "done" in t
        and reset["reason"] == "ended" and farewell_ok
    )
    print(f"[1/5] Frase de cierre → {t}")
    print(f"      reason={reset['reason']!r} · FAREWELL presente={farewell_ok} → {'OK' if ok1 else 'FALLO'}\n")
    failures += 0 if ok1 else 1

    # ── Caso 2: inactividad (timeout perezoso) ───────────────────────────────
    voice_ws.SESSION_TIMEOUT_SECONDS = 0  # cualquier hueco dispara el reset
    _install_models("")  # tras el reset mandamos una utterance vacía (transcript "")
    with TestClient(app).websocket_connect("/voice") as ws:
        ws.send_json({"type": "utterance_start"})
        # El reset por inactividad se emite ANTES de empezar a grabar.
        first = json.loads(ws.receive()["text"])
        # Cerramos un turno vacío para confirmar que la conexión sigue viva.
        ws.send_json({"type": "utterance_end"})
        rest = _drain(ws, "done")
    ok2 = first.get("type") == "session_reset" and first.get("reason") == "timeout" and "done" in _types(rest)
    print(f"[2/5] Inactividad → primero={first.get('type')}/{first.get('reason')} · luego={_types(rest)} → {'OK' if ok2 else 'FALLO'}\n")
    failures += 0 if ok2 else 1

    # ── Caso 3: reset manual (botón) ─────────────────────────────────────────
    voice_ws.SESSION_TIMEOUT_SECONDS = 9999
    _install_models("")
    with TestClient(app).websocket_connect("/voice") as ws:
        ws.send_json({"type": "reset_session"})
        msg = json.loads(ws.receive()["text"])
    ok3 = msg.get("type") == "session_reset" and msg.get("reason") == "manual"
    print(f"[3/5] reset_session → {msg} → {'OK' if ok3 else 'FALLO'}\n")
    failures += 0 if ok3 else 1

    # ── Caso 4: downgrade real a Haiku tras reset ────────────────────────────
    # Sustituimos JarvisCore por un núcleo falso (sin SDK) que rastrea instancias
    # y simula el ratchet-up. Guion de 3 turnos en una sola conexión:
    #   1) "qué tiempo hace" → ask escala a Sonnet → done.model = Sonnet.
    #   2) "gracias"         → cierre: FAREWELL + done(Sonnet) + session_reset(ended).
    #                          reset_core crea un núcleo NUEVO (Haiku).
    #   3) "2 más 2"         → trivial → done.model = Haiku  ← downgrade real.
    voice_ws.SESSION_TIMEOUT_SECONDS = 9999
    _FakeCore.instances = []
    orig_core = voice_ws.JarvisCore
    voice_ws.JarvisCore = _FakeCore  # type: ignore[assignment]
    voice_ws.get_models = lambda: (  # type: ignore[assignment]
        _ScriptSTT(["qué tiempo hace", "gracias", "2 más 2"]),
        _FakeTTS(),
    )
    try:
        with TestClient(app).websocket_connect("/voice") as ws:
            ws.send_json({"type": "utterance_start"})
            ws.send_bytes(_PCM)
            ws.send_json({"type": "utterance_end"})
            t1 = _drain(ws, "done")

            ws.send_json({"type": "utterance_start"})
            ws.send_bytes(_PCM)
            ws.send_json({"type": "utterance_end"})
            t2 = _drain(ws, "session_reset")

            ws.send_json({"type": "utterance_start"})
            ws.send_bytes(_PCM)
            ws.send_json({"type": "utterance_end"})
            t3 = _drain(ws, "done")
    finally:
        voice_ws.JarvisCore = orig_core  # type: ignore[assignment]

    m1 = next(m for m in t1 if m["type"] == "done")["model"]
    reset2 = next(m for m in t2 if m["type"] == "session_reset")["reason"]
    m3 = next(m for m in t3 if m["type"] == "done")["model"]
    ok4 = (
        m1 == MODEL_SONNET            # subió a Sonnet en el turno 1
        and reset2 == "ended"          # el cierre disparó el reset
        and m3 == MODEL_HAIKU          # tras el reset, de vuelta a Haiku
        and len(_FakeCore.instances) == 2  # el reset recicló el núcleo (no resucitó)
    )
    print(f"[4/5] Downgrade tras reset (voz) → t1.model={m1!r} · reset={reset2!r} · "
          f"t3.model={m3!r} · núcleos={len(_FakeCore.instances)} → {'OK' if ok4 else 'FALLO'}\n")
    failures += 0 if ok4 else 1

    # ── Caso 5: path de TEXTO (/ws) — botón + cierre + downgrade ──────────────
    # El path de la UI web. Mismo núcleo falso, pero parcheado en server (que es
    # donde /ws lo importa). Una conexión con 4 mensajes:
    #   1) ask "qué tiempo hace" → Sonnet.
    #   2) ask "muchas gracias"  → cierre: chunk(FAREWELL) + done + session_reset(ended).
    #   3) ask "2 más 2"         → Haiku  ← downgrade real por texto.
    #   4) reset_session         → session_reset(manual).
    from src.core import server as server_mod

    _FakeCore.instances = []
    orig_server_core = server_mod.JarvisCore
    server_mod.JarvisCore = _FakeCore  # type: ignore[assignment]
    try:
        with TestClient(app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "ask", "prompt": "qué tiempo hace"})
            x1 = _drain(ws, "done")
            ws.send_json({"type": "ask", "prompt": "muchas gracias"})
            x2 = _drain(ws, "session_reset")
            ws.send_json({"type": "ask", "prompt": "2 más 2"})
            x3 = _drain(ws, "done")
            ws.send_json({"type": "reset_session"})
            x4 = _drain(ws, "session_reset")
    finally:
        server_mod.JarvisCore = orig_server_core  # type: ignore[assignment]

    xm1 = next(m for m in x1 if m["type"] == "done")["model"]
    farewell_txt = any(m.get("type") == "chunk" and FAREWELL in m.get("text", "") for m in x2)
    xreset2 = next(m for m in x2 if m["type"] == "session_reset")["reason"]
    xm3 = next(m for m in x3 if m["type"] == "done")["model"]
    xreset4 = next(m for m in x4 if m["type"] == "session_reset")["reason"]
    ok5 = (
        xm1 == MODEL_SONNET            # subió a Sonnet
        and farewell_txt and xreset2 == "ended"   # cierre por texto: despedida + reset
        and xm3 == MODEL_HAIKU         # tras el cierre, de vuelta a Haiku
        and xreset4 == "manual"        # el botón + también recicla por texto
        and len(_FakeCore.instances) == 3  # inicial + reset(ended) + reset(manual)
    )
    print(f"[5/5] Reset por TEXTO (/ws) → t1.model={xm1!r} · cierre={xreset2!r}(FAREWELL={farewell_txt}) · "
          f"t3.model={xm3!r} · botón={xreset4!r} · núcleos={len(_FakeCore.instances)} → {'OK' if ok5 else 'FALLO'}\n")
    failures += 0 if ok5 else 1

    if failures == 0:
        print("✅ PASA: reset de sesión y downgrade a Haiku funcionan en voz Y texto.")
        return 0
    print(f"❌ FALLA: {failures} caso(s) no pasaron.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
