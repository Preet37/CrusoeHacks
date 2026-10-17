// Twin audio player: ordered, gapless playback of server `audio` chunks with
// speechSynthesis fallback and instant stop (barge-in).
// No dependencies beyond the DOM.

// align with lib/protocol.ts
export interface AudioMsg {
  type: "audio";
  say_id: string;
  seq: number;
  format?: "wav" | string;
  sample_rate?: number;
  text: string;
  data: string | null; // base64 WAV, or null -> speak `text` with speechSynthesis
  last: boolean;
}

export type PlaybackStartCb = (sayId: string, tsMs: number) => void;
export type IdleCb = () => void;

export interface AudioPlayerOptions {
  /** If a turn is missing its next chunk for this long while nothing is playing, skip ahead / drop it. */
  stallTimeoutMs?: number;
  /** speechSynthesis voice language. */
  ttsLang?: string;
}

// ---------- shared AudioContext ----------

let sharedCtx: AudioContext | null = null;

type AudioContextCtor = new () => AudioContext;

function getContext(): AudioContext {
  if (!sharedCtx) {
    const w = window as unknown as { AudioContext?: AudioContextCtor; webkitAudioContext?: AudioContextCtor };
    const Ctor = w.AudioContext ?? w.webkitAudioContext;
    if (!Ctor) throw new Error("Web Audio API not supported");
    sharedCtx = new Ctor();
  }
  return sharedCtx;
}

/** Call from a user gesture (click/keydown) so the AudioContext may start. Safe to call repeatedly. */
export function unlockAudio(): Promise<void> {
  const ctx = getContext();
  // Prime speechSynthesis too (some browsers require a gesture-initiated call).
  try {
    if ("speechSynthesis" in window) window.speechSynthesis.getVoices();
  } catch {
    /* ignore */
  }
  if (ctx.state === "suspended") return ctx.resume().catch(() => undefined);
  return Promise.resolve();
}

function base64ToArrayBuffer(b64: string): ArrayBuffer {
  const comma = b64.indexOf(",");
  const clean = b64.startsWith("data:") && comma >= 0 ? b64.slice(comma + 1) : b64;
  const bin = atob(clean);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

function decode(ctx: AudioContext, b64: string): Promise<AudioBuffer> {
  const ab = base64ToArrayBuffer(b64);
  // Promise form + callback form for older Safari.
  return new Promise<AudioBuffer>((resolve, reject) => {
    const p = ctx.decodeAudioData(ab, resolve, reject);
    if (p && typeof p.then === "function") p.then(resolve, reject);
  });
}

// ---------- internal state ----------

interface Chunk {
  msg: AudioMsg;
  decoded: Promise<AudioBuffer> | null; // null for TTS chunks
}

interface Turn {
  sayId: string;
  chunks: Map<number, Chunk>;
  nextSeq: number;
  lastSeq: number | null;
  started: boolean;
  stopped: boolean;
  sources: Set<AudioBufferSourceNode>;
}

export class AudioPlayer {
  private queue: Turn[] = [];
  private turns = new Map<string, Turn>();
  /** say_ids that were stopped or have finished; late chunks for them are ignored. */
  private retired = new Set<string>();
  private playing = new Set<AudioBufferSourceNode>();
  private nextStart = 0;
  private speaking = false;
  private speakResolve: (() => void) | null = null;

  private loopRunning = false;
  private wakeResolve: (() => void) | null = null;
  private pendingWake = false;
  private active = false;

  private startCbs: PlaybackStartCb[] = [];
  private idleCbs: IdleCb[] = [];

  private readonly stallTimeoutMs: number;
  private readonly ttsLang: string;

  constructor(opts: AudioPlayerOptions = {}) {
    this.stallTimeoutMs = opts.stallTimeoutMs ?? 8000;
    this.ttsLang = opts.ttsLang ?? "en-US";
  }

  // ----- public API -----

  enqueue(msg: AudioMsg): void {
    if (this.retired.has(msg.say_id)) return;
    let turn = this.turns.get(msg.say_id);
    if (!turn) {
      turn = {
        sayId: msg.say_id,
        chunks: new Map(),
        nextSeq: 0,
        lastSeq: null,
        started: false,
        stopped: false,
        sources: new Set(),
      };
      this.turns.set(msg.say_id, turn);
      this.queue.push(turn);
    }
    if (msg.seq < turn.nextSeq || turn.chunks.has(msg.seq)) return; // duplicate
    if (msg.last) turn.lastSeq = msg.seq;

    let decoded: Promise<AudioBuffer> | null = null;
    if (msg.data !== null && msg.data !== "") {
      try {
        decoded = decode(getContext(), msg.data); // decode eagerly, schedule in order
        decoded.catch(() => undefined); // avoid unhandled rejection; handled in loop
      } catch (e) {
        decoded = Promise.reject(e instanceof Error ? e : new Error(String(e)));
        decoded.catch(() => undefined);
      }
    }
    turn.chunks.set(msg.seq, { msg, decoded });
    this.active = true;
    this.wake();
    void this.loop();
  }

  /** Stop everything instantly and forget all queued audio. */
  stopAll(): void {
    for (const t of this.queue) this.retire(t);
    this.queue = [];
    for (const src of this.playing) this.killSource(src);
    this.playing.clear();
    this.cancelSpeech();
    this.nextStart = 0;
    this.wake();
  }

  /** Stop one turn (playing or queued). Late chunks for it are ignored. */
  stop(sayId: string): void {
    const turn = this.turns.get(sayId);
    if (!turn) {
      this.retired.add(sayId);
      return;
    }
    const wasHead = this.queue[0] === turn;
    this.retire(turn);
    this.queue = this.queue.filter((t) => t !== turn);
    for (const src of turn.sources) {
      this.killSource(src);
      this.playing.delete(src);
    }
    turn.sources.clear();
    if (wasHead) {
      this.cancelSpeech();
      this.nextStart = 0;
    }
    this.wake();
  }

  /** True while audio/TTS is playing or a Twin turn is still queued/in progress. */
  isPlaying(): boolean {
    return this.playing.size > 0 || this.speaking || this.queue.length > 0;
  }

  onPlaybackStart(cb: PlaybackStartCb): () => void {
    this.startCbs.push(cb);
    return () => {
      this.startCbs = this.startCbs.filter((c) => c !== cb);
    };
  }

  onIdle(cb: IdleCb): () => void {
    this.idleCbs.push(cb);
    return () => {
      this.idleCbs = this.idleCbs.filter((c) => c !== cb);
    };
  }

  // ----- internals -----

  private retire(turn: Turn): void {
    turn.stopped = true;
    turn.chunks.clear();
    this.retired.add(turn.sayId);
    this.turns.delete(turn.sayId);
  }

  private killSource(src: AudioBufferSourceNode): void {
    src.onended = null;
    try {
      src.stop();
    } catch {
      /* already stopped */
    }
    try {
      src.disconnect();
    } catch {
      /* ignore */
    }
  }

  private cancelSpeech(): void {
    if (this.speaking || this.speakResolve) {
      try {
        window.speechSynthesis.cancel();
      } catch {
        /* ignore */
      }
    }
    this.speaking = false;
    const r = this.speakResolve;
    this.speakResolve = null;
    if (r) r();
  }

  private wake(): void {
    const r = this.wakeResolve;
    this.wakeResolve = null;
    if (r) r();
    else this.pendingWake = true;
  }

  /** Resolves true on wake(), false on timeout. */
  private wait(timeoutMs?: number): Promise<boolean> {
    if (this.pendingWake) {
      this.pendingWake = false;
      return Promise.resolve(true);
    }
    return new Promise<boolean>((resolve) => {
      let timer: number | undefined;
      this.wakeResolve = () => {
        if (timer !== undefined) clearTimeout(timer);
        resolve(true);
      };
      if (timeoutMs !== undefined) {
        timer = window.setTimeout(() => {
          this.wakeResolve = null;
          resolve(false);
        }, timeoutMs);
      }
    });
  }

  private isStale(turn: Turn): boolean {
    return turn.stopped || this.queue[0] !== turn;
  }

  /** Wait until all scheduled WebAudio sources have ended. */
  private async drain(turn: Turn): Promise<void> {
    while (this.playing.size > 0 && !this.isStale(turn)) await this.wait();
  }

  private emitStart(sayId: string, ts: number): void {
    for (const cb of this.startCbs) {
      try {
        cb(sayId, ts);
      } catch (e) {
        console.error("[player] onPlaybackStart handler error", e);
      }
    }
  }

  private emitIdle(): void {
    for (const cb of this.idleCbs) {
      try {
        cb();
      } catch (e) {
        console.error("[player] onIdle handler error", e);
      }
    }
  }

  private async loop(): Promise<void> {
    if (this.loopRunning) return;
    this.loopRunning = true;
    try {
      for (;;) {
        const turn = this.queue[0];
        if (!turn) {
          if (this.playing.size === 0 && !this.speaking) {
            if (this.active) {
              this.active = false;
              this.emitIdle();
            }
            break;
          }
          await this.wait();
          continue;
        }

        // Turn complete: let its audio finish, then move to the next turn.
        if (turn.lastSeq !== null && turn.nextSeq > turn.lastSeq) {
          await this.drain(turn);
          if (!this.isStale(turn)) {
            this.queue.shift();
            this.turns.delete(turn.sayId);
            this.retired.add(turn.sayId);
          }
          continue;
        }

        const chunk = turn.chunks.get(turn.nextSeq);
        if (!chunk) {
          const woke = await this.wait(this.stallTimeoutMs);
          if (!woke && !this.isStale(turn) && this.playing.size === 0 && !this.speaking) {
            this.handleStall(turn);
          }
          continue;
        }
        turn.chunks.delete(turn.nextSeq);
        turn.nextSeq++;

        if (chunk.decoded) {
          let buf: AudioBuffer | null = null;
          try {
            buf = await chunk.decoded;
          } catch (e) {
            console.warn("[player] decode failed, falling back to speechSynthesis", e);
          }
          if (this.isStale(turn)) continue;
          if (buf) {
            this.schedule(turn, buf);
            continue;
          }
        }
        // TTS fallback (data === null or decode failed): wait for prior audio, then speak.
        await this.drain(turn);
        if (this.isStale(turn)) continue;
        await this.speak(turn, chunk.msg.text);
      }
    } finally {
      this.loopRunning = false;
    }
  }

  /** Next chunk never arrived: skip the gap if later chunks exist, else drop the turn. */
  private handleStall(turn: Turn): void {
    const seqs = [...turn.chunks.keys()].sort((a, b) => a - b);
    if (seqs.length > 0) {
      console.warn(`[player] ${turn.sayId}: seq ${turn.nextSeq} missing, skipping to ${seqs[0]}`);
      turn.nextSeq = seqs[0];
    } else {
      console.warn(`[player] ${turn.sayId}: stalled at seq ${turn.nextSeq}, dropping turn`);
      this.queue.shift();
      this.turns.delete(turn.sayId);
      this.retired.add(turn.sayId);
    }
  }

  private schedule(turn: Turn, buf: AudioBuffer): void {
    const ctx = getContext();
    if (ctx.state === "suspended") void ctx.resume().catch(() => undefined);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    const now = ctx.currentTime;
    const startAt = Math.max(now, this.nextStart);
    this.nextStart = startAt + buf.duration;
    src.onended = () => {
      this.playing.delete(src);
      turn.sources.delete(src);
      try {
        src.disconnect();
      } catch {
        /* ignore */
      }
      if (this.playing.size === 0) this.nextStart = 0;
      this.wake();
    };
    this.playing.add(src);
    turn.sources.add(src);
    src.start(startAt);

    if (!turn.started) {
      turn.started = true;
      const fire = (): void => {
        if (turn.stopped) return;
        const delayMs = Math.max(0, (startAt - ctx.currentTime) * 1000);
        this.emitStart(turn.sayId, Date.now() + delayMs);
      };
      if (ctx.state === "running") fire();
      else {
        const onState = (): void => {
          if (ctx.state === "running") {
            ctx.removeEventListener("statechange", onState);
            fire();
          }
        };
        ctx.addEventListener("statechange", onState);
      }
    }
  }

  private speak(turn: Turn, text: string): Promise<void> {
    const clean = text.trim();
    if (!clean || !("speechSynthesis" in window)) return Promise.resolve();
    return new Promise<void>((resolve) => {
      let done = false;
      let safety: number | undefined;
      const finish = (): void => {
        if (done) return;
        done = true;
        if (safety !== undefined) clearTimeout(safety);
        if (this.speakResolve === finish) this.speakResolve = null;
        this.speaking = false;
        this.wake();
        resolve();
      };
      const u = new SpeechSynthesisUtterance(clean);
      u.lang = this.ttsLang;
      const voice = window.speechSynthesis.getVoices().find((v) => v.lang === this.ttsLang);
      if (voice) u.voice = voice;
      u.onstart = () => {
        if (!turn.started && !turn.stopped) {
          turn.started = true;
          this.emitStart(turn.sayId, Date.now());
        }
      };
      u.onend = finish;
      u.onerror = finish;
      this.speaking = true;
      this.speakResolve = finish;
      // Chrome sometimes never fires onend; guard with a generous timeout.
      safety = window.setTimeout(finish, 5000 + clean.length * 120);
      window.speechSynthesis.speak(u);
    });
  }
}
