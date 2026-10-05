/* ============================================================================
 * mic-worklet.js — AudioWorkletProcessor de captura de micrófono.
 * ----------------------------------------------------------------------------
 * Toma el audio del micro (mono, a la frecuencia del AudioContext — típicamente
 * 48 kHz), lo remuestrea a 16 kHz y lo convierte a PCM Int16 LE, que es lo que
 * espera faster-whisper en el servidor. Emite cada bloque resampleado al hilo
 * principal por `port.postMessage` (ArrayBuffer transferible).
 *
 * El downsample es lineal por interpolación sobre un ratio acumulado: simple y
 * suficiente para voz a 16 kHz (no buscamos calidad hi-fi, sino que Whisper
 * entienda). El hilo principal solo reenvía estos buffers por el WebSocket.
 * ==========================================================================*/

const TARGET_RATE = 16000;

class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // Posición fraccional de lectura sobre el flujo de entrada, para mantener
    // continuidad del resampleo entre bloques sucesivos de 128 muestras.
    this._readPos = 0;
    this._ratio = sampleRate / TARGET_RATE; // sampleRate es global en el worklet
  }

  /**
   * Procesa un bloque de audio. `inputs[0][0]` es el canal 0 (mono) en Float32.
   * @returns {boolean} true para seguir vivo.
   */
  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) return true;

    const channel = input[0];
    const ratio = this._ratio;

    // Nº de muestras de salida que caben empezando desde _readPos.
    const out = [];
    let pos = this._readPos;
    while (pos < channel.length) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const a = channel[i];
      const b = i + 1 < channel.length ? channel[i + 1] : a;
      const sample = a + (b - a) * frac; // interpolación lineal
      // Float32 [-1,1] → Int16 LE
      let s = Math.max(-1, Math.min(1, sample));
      out.push(s < 0 ? s * 0x8000 : s * 0x7fff);
      pos += ratio;
    }
    // Guardamos el desfase para el siguiente bloque (continuidad del resampleo).
    this._readPos = pos - channel.length;

    if (out.length > 0) {
      const pcm = new Int16Array(out);
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}

registerProcessor('mic-processor', MicProcessor);
