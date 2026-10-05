"""Cliente WebSocket del contrato ``/voice`` para el satélite.

Habla exactamente el mismo protocolo que la UI web, activando el modo de salida
Cast con el ``hello``. Envuelve la conexión y expone helpers para el handshake,
el envío de una utterance (frames PCM entre ``utterance_start`` y ``utterance_end``)
y la escucha de la respuesta del servidor.

Contrato (cliente → servidor):
    {"type":"hello","output":"cast","device":"Salón"}   # activa modo cast
    {"type":"wake"}                                      # (Fase 3) castea el ding
    {"type":"utterance_start"} · <bytes PCM Int16 LE 16 kHz> ... · {"type":"utterance_end"}

Servidor → cliente (modo cast): transcript · reply_text · cast_start · done ·
    session_reset · cancelled · interrupted · error.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import websockets

# Tamaño de cada frame binario que mandamos. El servidor acumula hasta
# utterance_end, así que el troceo es indiferente; ~1 s por frame va sobrado.
_FRAME_BYTES = 32000  # 1 s de PCM Int16 mono a 16 kHz

# Reintentos de conexión: al arrancar el stack, `depends_on` solo garantiza que
# el contenedor `jarvis` ha arrancado, no que ya escuche en 8200 (tarda unos
# segundos en quedar healthy). Reintentamos para absorber esa carrera.
_CONNECT_RETRIES = 30
_CONNECT_BACKOFF_S = 1.0


class VoiceClient:
    """Conexión de larga vida al endpoint ``/voice`` en modo Cast."""

    def __init__(self, url: str, device: str) -> None:
        self._url = url
        self._device = device
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def __aenter__(self) -> "VoiceClient":
        # max_size=None: no limitar el tamaño de los mensajes entrantes.
        # Reintento con backoff mientras el núcleo termina de arrancar.
        last_err: OSError | None = None
        for attempt in range(1, _CONNECT_RETRIES + 1):
            try:
                self._ws = await websockets.connect(self._url, max_size=None)
                return self
            except (OSError, websockets.exceptions.InvalidStatus) as err:
                last_err = err  # type: ignore[assignment]
                if attempt == 1 or attempt % 5 == 0:
                    print(
                        f"[ws] esperando al núcleo en {self._url} "
                        f"(intento {attempt}/{_CONNECT_RETRIES})…",
                        flush=True,
                    )
                await asyncio.sleep(_CONNECT_BACKOFF_S)
        raise RuntimeError(
            f"No se pudo conectar a {self._url} tras {_CONNECT_RETRIES} intentos"
        ) from last_err

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    @property
    def _sock(self) -> websockets.WebSocketClientProtocol:
        if self._ws is None:
            raise RuntimeError("VoiceClient usado fuera del context manager")
        return self._ws

    async def hello(self) -> None:
        """Handshake: activa el modo de salida Cast en el servidor."""
        await self._sock.send(
            json.dumps({"type": "hello", "output": "cast", "device": self._device})
        )
        print(f"[ws] → hello output=cast device={self._device!r}", flush=True)

    async def wake(self) -> None:
        """Pide al servidor que castee el ding de confirmación (Fase 3)."""
        await self._sock.send(json.dumps({"type": "wake"}))
        print("[ws] → wake", flush=True)

    async def send_utterance(self, pcm: bytes) -> None:
        """Envía una utterance completa (start · frames PCM · end).

        Atajo del modo test-once (Fase 2): manda todo el audio ya grabado de una
        vez. La máquina de estados (modo ``run``) usa en cambio el trío
        :meth:`utterance_start` / :meth:`send_frame` / :meth:`utterance_end` para
        transmitir en vivo según la VAD va capturando.

        Args:
            pcm: PCM Int16 LE, 16 kHz, mono.
        """
        await self._sock.send(json.dumps({"type": "utterance_start"}))
        for i in range(0, len(pcm), _FRAME_BYTES):
            await self._sock.send(pcm[i : i + _FRAME_BYTES])
        await self._sock.send(json.dumps({"type": "utterance_end"}))
        secs = len(pcm) / (16000 * 2)
        print(f"[ws] → utterance ({secs:.1f}s de audio)", flush=True)

    async def utterance_start(self) -> None:
        """Abre una utterance en streaming (marca el inicio de la captura)."""
        await self._sock.send(json.dumps({"type": "utterance_start"}))

    async def send_frame(self, pcm: bytes) -> None:
        """Envía un fragmento de audio PCM en vivo dentro de una utterance abierta.

        Args:
            pcm: PCM Int16 LE, 16 kHz, mono. El servidor acumula hasta el
                ``utterance_end``, así que el troceo es indiferente.
        """
        await self._sock.send(pcm)

    async def utterance_end(self) -> None:
        """Cierra la utterance en streaming (dispara STT → núcleo → Cast)."""
        await self._sock.send(json.dumps({"type": "utterance_end"}))

    async def stop(self) -> None:
        """Corta la respuesta en curso (barge-in).

        En modo cast el servidor para el WAV que suena en el Home aunque el turno
        ya haya terminado, así que sirve para interrumpir a Jarvis mientras habla.
        """
        await self._sock.send(json.dumps({"type": "stop"}))
        print("[ws] → stop (barge-in)", flush=True)

    async def messages(self) -> AsyncIterator[dict]:
        """Itera los mensajes JSON entrantes (ignora binarios inesperados)."""
        async for raw in self._sock:
            if isinstance(raw, bytes):
                # En modo cast el servidor no manda PCM; si llega, lo señalamos.
                print(f"[ws] ← <binario {len(raw)} B> (inesperado en modo cast)", flush=True)
                continue
            yield json.loads(raw)
