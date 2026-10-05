# === Imagen del núcleo headless de Jarvis ===
#
# Base Debian slim (NO alpine, decisión del PLAN): pychromecast/zeroconf/cryptography
# y el CLI de Node de Claude Code son mucho más fiables sobre glibc que sobre musl.
#
# El claude-agent-sdk NO llama a la API directamente: lanza el CLI de Claude Code
# (Node) como subproceso. Por eso la imagen necesita Node + el CLI instalados,
# no solo el `pip install`.
FROM python:3.13-slim

# Node.js 20 (desde NodeSource) para el CLI de Claude Code, y curl para añadir el repo.
# Limpiamos las listas de apt al final para no inflar la imagen.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg tzdata \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencias Python primero (capa cacheable mientras requirements.txt no cambie).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Runtime JS (deno) para yt-dlp: la extracción de YouTube sin runtime JS está
# deprecada ("some formats may be missing"). deno es el que yt-dlp busca por
# defecto en el PATH. Se descomprime con el módulo zipfile de Python (ya está en
# la imagen) para no instalar `unzip`.
RUN curl -fsSL -o /tmp/deno.zip \
      "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip" \
    && python -m zipfile -e /tmp/deno.zip /usr/local/bin/ \
    && chmod +x /usr/local/bin/deno \
    && rm /tmp/deno.zip

# --- Pila de voz server-side (Hito 3.6: /voice hace STT+TTS en el NUC) ---
# Subconjunto de requirements-voice.txt: SOLO lo que necesita el servidor.
# Se omite `sounddevice` (PortAudio) a propósito: el audio I/O ocurre en el
# navegador del cliente, no en el contenedor (ver nota en src/voice/tts.py).
RUN pip install --no-cache-dir faster-whisper numpy

# Binario de Piper (TTS por subproceso, no por pip) para Linux x86_64.
# Se extrae a /app/piper/ (binario + sus libs y espeak-ng-data con rpath $ORIGIN).
ARG PIPER_VERSION=2023.11.14-2
RUN curl -fsSL -o /tmp/piper.tar.gz \
      "https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/piper_linux_x86_64.tar.gz" \
    && tar -xzf /tmp/piper.tar.gz -C /app \
    && rm /tmp/piper.tar.gz

# Voz es_ES-davefx-medium (la congelada del proyecto) + su config .onnx.json.
RUN mkdir -p /app/voices/es_ES \
    && curl -fsSL -o /app/voices/es_ES/es_ES-davefx-medium.onnx \
      "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium/es_ES-davefx-medium.onnx" \
    && curl -fsSL -o /app/voices/es_ES/es_ES-davefx-medium.onnx.json \
      "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium/es_ES-davefx-medium.onnx.json"

# Rutas de la voz dentro de la imagen (las consume src/voice/tts.py).
ENV JARVIS_PIPER_BIN=/app/piper/piper \
    JARVIS_TTS_VOICE=/app/voices/es_ES/es_ES-davefx-medium.onnx

# Código de la aplicación.
COPY src/ ./src/

# El contenedor expone la API en todas las interfaces (host-net en el NUC).
# El puerto real lo fija el compose vía JARVIS_PORT.
ENV JARVIS_HOST=0.0.0.0 \
    JARVIS_PORT=8200 \
    PYTHONUNBUFFERED=1

EXPOSE 8200

CMD ["python", "-m", "src.core.server"]
