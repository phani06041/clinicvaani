// Capture: downsample mic to 16 kHz mono PCM16, optionally mix synthetic background noise
// (for the "noisy clinic counter" part of the demo). Reports raw mic level for local VAD/latency.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.acc = 0;
    this.out = new Int16Array(1600); // 100 ms @ 16 kHz
    this.n = 0;
    this.noise = 0;      // 0..1
    this.brown = 0;
    this.babblePhase = 0;
    this.sumSq = 0; this.cnt = 0;
    this.port.onmessage = (e) => { if (e.data.noise !== undefined) this.noise = e.data.noise; };
  }
  noiseSample() {
    // brown noise (traffic / fan rumble) + amplitude-modulated hiss (crowd babble-ish)
    const white = Math.random() * 2 - 1;
    this.brown = (this.brown + 0.02 * white) / 1.02;
    this.babblePhase += 2 * Math.PI * (3 + Math.random() * 4) / 16000;
    const babble = white * 0.35 * (0.5 + 0.5 * Math.sin(this.babblePhase));
    return (this.brown * 3.5 + babble) * this.noise * 0.6;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.sumSq += ch[i] * ch[i]; this.cnt++;
      this.acc += 1;
      if (this.acc >= this.ratio) {
        this.acc -= this.ratio;
        let s = ch[i] + (this.noise > 0 ? this.noiseSample() : 0);
        s = Math.max(-1, Math.min(1, s));
        this.out[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        if (this.n === this.out.length) {
          const rms = Math.sqrt(this.sumSq / Math.max(1, this.cnt));
          this.port.postMessage({ pcm: this.out.buffer.slice(0), rms });
          this.n = 0; this.sumSq = 0; this.cnt = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('capture-processor', CaptureProcessor);

// Player: queue of 24 kHz float chunks; "clear" flushes instantly on barge-in.
class PlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = []; this.cur = null; this.pos = 0; this.wasPlaying = false;
    this.port.onmessage = (e) => {
      if (e.data === 'clear') { this.queue = []; this.cur = null; this.pos = 0; }
      else this.queue.push(e.data);
    };
  }
  process(_in, outputs) {
    const out = outputs[0][0];
    let i = 0;
    while (i < out.length) {
      if (!this.cur) { this.cur = this.queue.shift() || null; this.pos = 0; if (!this.cur) break; }
      const take = Math.min(out.length - i, this.cur.length - this.pos);
      out.set(this.cur.subarray(this.pos, this.pos + take), i);
      i += take; this.pos += take;
      if (this.pos >= this.cur.length) this.cur = null;
    }
    for (; i < out.length; i++) out[i] = 0;
    const playing = !!(this.cur || this.queue.length);
    if (playing !== this.wasPlaying) { this.wasPlaying = playing; this.port.postMessage({ playing }); }
    return true;
  }
}
registerProcessor('player-processor', PlayerProcessor);
