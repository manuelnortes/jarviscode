/* ============================================================================
 * VoiceOrb — anillo de partículas oscilante (estilo Jarvis)
 * ----------------------------------------------------------------------------
 * Módulo vanilla, sin dependencias. Dibuja sobre un <canvas> un anillo
 * circular formado por partículas que ondulan. La amplitud de la oscilación
 * se controla en tiempo real con setLevel(0..1) — conéctalo al volumen del
 * micrófono o del TTS para que el orbe "respire" cuando el asistente habla.
 *
 * USO BÁSICO
 *   const orb = new VoiceOrb(document.querySelector('#orb'), { amp: 1.1 });
 *   orb.start();
 *   // mientras habla:
 *   orb.setLevel(0.85);
 *   // al callar:
 *   orb.setLevel(0);
 *   // al desmontar:
 *   orb.destroy();
 *
 * EL HOOK DE OSCILACIÓN (lo importante)
 *   `level` es 0..1. 0 = reposo (solo un latido suave de respiración),
 *   1 = oscilación máxima. La fórmula de amplitud es:
 *       amp = baseAmp * (0.55 + 1.0 * level) * breathing
 *   Sube `level` con la energía del audio. Ejemplo con Web Audio:
 *       const ctx = new AudioContext();
 *       const src = ctx.createMediaStreamSource(micStream);
 *       const analyser = ctx.createAnalyser(); analyser.fftSize = 512;
 *       src.connect(analyser);
 *       const buf = new Uint8Array(analyser.frequencyBinCount);
 *       function tick(){
 *         analyser.getByteTimeDomainData(buf);
 *         let sum = 0;
 *         for (const v of buf){ const x = (v-128)/128; sum += x*x; }
 *         const rms = Math.sqrt(sum / buf.length);      // 0..~0.5
 *         orb.setLevel(Math.min(1, rms * 3.5));          // ajusta el factor
 *         requestAnimationFrame(tick);
 *       }
 *       tick();
 *
 * NOTA SOBRE TAMAÑO
 *   El canvas debe tener un tamaño CSS definido (width/height por CSS o por
 *   su contenedor). El módulo gestiona devicePixelRatio internamente y
 *   reduce la cuenta de partículas automáticamente en orbes pequeños (<92px).
 * ==========================================================================*/

class VoiceOrb {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.baseAmp = opts.amp ?? 1.0;          // amplitud base del orbe
    this.color = opts.color ?? [130, 228, 236]; // [r,g,b] del cian
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.level = 0;          // 0..1, controlado por setLevel()
    this._target = 0;        // suavizado hacia level
    this._raf = null;
    this._t0 = performance.now();
  }

  /** Sube/baja la oscilación. v en 0..1 (se puede pasar de 1, se recorta). */
  setLevel(v) { this._target = Math.max(0, Math.min(1.4, +v || 0)); }

  start() {
    if (this._raf) return;
    const loop = (now) => {
      // suavizado exponencial para que el cambio de nivel no sea brusco
      this.level += (this._target - this.level) * 0.18;
      this._draw((now - this._t0) / 1000);
      this._raf = requestAnimationFrame(loop);
    };
    this._raf = requestAnimationFrame(loop);
  }

  destroy() { if (this._raf) cancelAnimationFrame(this._raf); this._raf = null; }

  _draw(t) {
    const cv = this.canvas, ctx = this.ctx, dpr = this.dpr;
    const cw = cv.clientWidth, ch = cv.clientHeight;
    if (!cw || !ch || cw > 1400 || ch > 1400) return;
    if (cv.width !== Math.round(cw * dpr)) {
      cv.width = Math.round(cw * dpr);
      cv.height = Math.round(ch * dpr);
    }
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.save();
    ctx.scale(dpr, dpr);

    const cx = cw / 2, cy = ch / 2;
    const R = Math.min(cw, ch) / 2;
    const small = R < 46;
    const breath = 0.85 + 0.15 * Math.sin(t * 0.9);
    const amp = this.baseAmp * (0.55 + 1.0 * this.level) * breath;
    const [cr, cg, cb] = this.color;

    // núcleo tenue
    ctx.globalCompositeOperation = 'source-over';
    const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
    core.addColorStop(0, `rgba(${cr},${cg},${cb},0.10)`);
    core.addColorStop(0.5, `rgba(${cr},${cg},${cb},0.03)`);
    core.addColorStop(1, `rgba(${cr},${cg},${cb},0)`);
    ctx.fillStyle = core;
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7); ctx.fill();

    // anillos guía suaves (solo en orbes grandes)
    if (!small) {
      ctx.strokeStyle = `rgba(34,211,238,0.07)`; ctx.lineWidth = 1;
      for (const rr of [0.28, 0.40, 0.52]) {
        ctx.beginPath(); ctx.arc(cx, cy, R * rr, 0, 7); ctx.stroke();
      }
    }

    // banda de partículas oscilante
    ctx.globalCompositeOperation = 'lighter';
    const ringR = R * (small ? 0.58 : 0.62);
    const strands = small ? 4 : 6;
    const per = small ? 64 : 230;
    for (let s = 0; s < strands; s++) {
      const ph = s * 1.7;
      const fr1 = 3 + (s % 3), fr2 = 5 + (s % 2), fr3 = 8;
      for (let i = 0; i < per; i++) {
        const a = (i / per) * Math.PI * 2 + s * 0.05;
        const n = Math.sin(a * fr1 + t * 0.8 + ph)
                + 0.6 * Math.sin(a * fr2 - t * 1.1 + ph * 1.3)
                + 0.4 * Math.sin(a * fr3 + t * 0.5 + ph)
                + 0.8 * Math.sin(a * 2 - t * 0.6 + ph * 0.7);
        const rnd = Math.sin(i * 12.9898 + s * 78.233) * 43758.5453;
        const jit = rnd - Math.floor(rnd);
        const rr = ringR + n * amp * R * 0.10 + (jit - 0.5) * R * 0.06;
        const x = cx + Math.cos(a) * rr, y = cy + Math.sin(a) * rr;
        const al = 0.08 + 0.20 * jit;
        ctx.fillStyle = `rgba(${cr + (jit * 70 | 0)},${cg},${cb},${al.toFixed(3)})`;
        const sz = 0.6 + jit * 1.0;
        ctx.fillRect(x, y, sz, sz);
      }
    }
    ctx.restore();
  }
}

// Export universal (ESM / CommonJS / global)
if (typeof module !== 'undefined' && module.exports) module.exports = { VoiceOrb };
if (typeof window !== 'undefined') window.VoiceOrb = VoiceOrb;
