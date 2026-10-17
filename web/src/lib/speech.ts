// Twin speech-to-text: Web Speech API wrapper with early speech-start detection
// and auto-restart (Chrome ends recognition after silence / ~60s).
// No dependencies beyond the DOM.

// ---- Minimal ambient types (lib.dom does not ship SpeechRecognition) ----

interface SRAlternative {
  readonly transcript: string;
  readonly confidence: number;
}
interface SRResult {
  readonly isFinal: boolean;
  readonly length: number;
  [index: number]: SRAlternative;
}
interface SRResultList {
  readonly length: number;
  [index: number]: SRResult;
}
interface SREvent extends Event {
  readonly resultIndex: number;
  readonly results: SRResultList;
}
interface SRErrorEvent extends Event {
  readonly error: string; // 'no-speech' | 'aborted' | 'not-allowed' | 'network' | 'audio-capture' | ...
  readonly message: string;
}
interface SRInstance extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  maxAlternatives: number;
  onresult: ((ev: SREvent) => void) | null;
  onerror: ((ev: SRErrorEvent) => void) | null;
  onend: ((ev: Event) => void) | null;
  onstart: ((ev: Event) => void) | null;
  onsoundstart: ((ev: Event) => void) | null;
  onspeechstart: ((ev: Event) => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}
type SRCtor = new () => SRInstance;

function getCtor(): SRCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { SpeechRecognition?: SRCtor; webkitSpeechRecognition?: SRCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

// ---- Public types ----

export interface SpeechError {
  code: string; // recognition error code, e.g. 'not-allowed', 'network', or 'start-failed'
  message: string;
  fatal: boolean; // true -> recognition stopped and will not restart
}

export interface SpeechHandlers {
  onInterim?: (text: string) => void;
  onFinal?: (text: string) => void;
  onSpeechStart?: () => void;
  onError?: (err: SpeechError) => void;
}

export interface SpeechOptions {
  lang?: string;
  /**
   * Which signal may fire onSpeechStart first:
   * 'sound'  = soundstart/speechstart/interim (earliest; may trigger on speaker echo),
   * 'speech' = speechstart/interim,
   * 'interim'= first interim text only (most robust to echo).
   */
  earlyTrigger?: "sound" | "speech" | "interim";
}

const BENIGN = new Set(["no-speech", "aborted"]);
const FATAL = new Set(["not-allowed", "service-not-allowed"]);

export class Speech {
  private rec: SRInstance | null = null;
  private handlers: SpeechHandlers = {};
  private wanted = false; // true between start() and stop()
  private running = false;
  private restartTimer: number | undefined;
  private backoffMs = 250;
  private sessionStartedAt = 0;
  private utteranceStarted = false;
  private lastErrorCode = "";
  private readonly lang: string;
  private readonly earlyTrigger: "sound" | "speech" | "interim";

  constructor(opts: SpeechOptions = {}) {
    this.lang = opts.lang ?? "en-US";
    this.earlyTrigger = opts.earlyTrigger ?? "sound";
  }

  static isSupported(): boolean {
    return getCtor() !== null;
  }

  isSupported(): boolean {
    return Speech.isSupported();
  }

  isRunning(): boolean {
    return this.wanted && this.running;
  }

  start(handlers: SpeechHandlers): void {
    this.handlers = handlers;
    if (this.wanted) return;
    const Ctor = getCtor();
    if (!Ctor) {
      this.report({ code: "unsupported", message: "SpeechRecognition not supported (use Chrome)", fatal: true });
      return;
    }
    this.wanted = true;
    this.backoffMs = 250;
    this.lastErrorCode = "";
    this.rec = this.create(Ctor);
    this.launch();
  }

  stop(): void {
    this.wanted = false;
    if (this.restartTimer !== undefined) {
      clearTimeout(this.restartTimer);
      this.restartTimer = undefined;
    }
    const rec = this.rec;
    if (rec) {
      // abort() (not stop()) so buffered results are discarded, even if onstart has not fired yet.
      try {
        rec.abort();
      } catch {
        /* ignore */
      }
    }
    this.running = false;
    this.utteranceStarted = false;
  }

  // ----- internals -----

  private create(Ctor: SRCtor): SRInstance {
    const rec = new Ctor();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = this.lang;
    rec.maxAlternatives = 1;

    rec.onstart = () => {
      this.running = true;
      this.sessionStartedAt = Date.now();
    };
    if (this.earlyTrigger === "sound") rec.onsoundstart = () => this.speechStart();
    if (this.earlyTrigger !== "interim") rec.onspeechstart = () => this.speechStart();

    rec.onresult = (ev) => this.handleResult(ev);
    rec.onerror = (ev) => this.handleError(ev);
    rec.onend = () => {
      this.running = false;
      this.utteranceStarted = false;
      if (this.wanted) this.scheduleRestart();
    };
    return rec;
  }

  private launch(): void {
    const rec = this.rec;
    if (!rec || !this.wanted) return;
    try {
      rec.start();
    } catch (e) {
      // InvalidStateError if already started; otherwise retry later.
      const name = e instanceof DOMException ? e.name : "";
      if (name !== "InvalidStateError") {
        this.report({ code: "start-failed", message: e instanceof Error ? e.message : String(e), fatal: false });
        this.scheduleRestart();
      }
    }
  }

  private scheduleRestart(): void {
    if (this.restartTimer !== undefined) return;
    // Sessions that die quickly (errors, no-speech loops) back off; healthy ones restart fast.
    const lived = Date.now() - this.sessionStartedAt;
    if (lived > 5000) this.backoffMs = 250;
    const delay = this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, 5000);
    this.restartTimer = window.setTimeout(() => {
      this.restartTimer = undefined;
      if (this.wanted) this.launch();
    }, delay);
  }

  private speechStart(): void {
    if (this.utteranceStarted) return;
    this.utteranceStarted = true;
    this.handlers.onSpeechStart?.();
  }

  private handleResult(ev: SREvent): void {
    if (!this.wanted) return; // muted: never deliver late results
    this.lastErrorCode = ""; // healthy again; allow future errors to be reported
    let interim = "";
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const res = ev.results[i];
      const alt = res[0];
      if (!alt) continue;
      if (res.isFinal) {
        const text = alt.transcript.trim();
        if (!this.utteranceStarted && text) this.speechStart();
        this.utteranceStarted = false; // utterance committed; next words are a new one
        if (text) this.handlers.onFinal?.(text);
      } else {
        interim += alt.transcript;
      }
    }
    const text = interim.trim();
    if (text) {
      this.speechStart();
      this.handlers.onInterim?.(text);
    }
  }

  private handleError(ev: SRErrorEvent): void {
    const code = ev.error;
    if (BENIGN.has(code)) return; // 'end' follows; restart quietly
    if (FATAL.has(code)) {
      this.wanted = false;
      if (this.restartTimer !== undefined) {
        clearTimeout(this.restartTimer);
        this.restartTimer = undefined;
      }
      this.report({ code, message: ev.message || "Microphone permission denied", fatal: true });
      return;
    }
    // network, audio-capture, etc.: report once per streak, keep retrying with backoff.
    if (code !== this.lastErrorCode) {
      this.lastErrorCode = code;
      this.report({ code, message: ev.message || code, fatal: false });
    }
  }

  private report(err: SpeechError): void {
    if (this.handlers.onError) this.handlers.onError(err);
    else console.warn("[speech]", err.code, err.message);
  }
}
