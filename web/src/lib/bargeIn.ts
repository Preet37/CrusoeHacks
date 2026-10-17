// Twin barge-in glue: user starts talking while Twin plays -> stop locally FIRST, then tell server.
// No dependencies beyond the DOM. Types are structural so this compiles standalone.

// align with lib/protocol.ts
export interface BargeInMsg {
  type: "barge_in";
  speaker: string;
  ts: number;
}
// align with lib/protocol.ts
export interface UserTextMsg {
  type: "user_text";
  speaker: string;
  text: string;
  final: boolean;
  ts: number;
}
// align with lib/protocol.ts
export interface StopAudioMsg {
  type: "stop_audio";
  say_id?: string | null;
}

export type OutgoingMsg = BargeInMsg | UserTextMsg;

/** Subset of AudioPlayer used here. */
export interface PlayerLike {
  isPlaying(): boolean;
  stopAll(): void;
  stop(sayId: string): void;
}

/** Same shape as SpeechHandlers in speech.ts. */
export interface SpeechEvents {
  onInterim?: (text: string) => void;
  onFinal?: (text: string) => void;
  onSpeechStart?: () => void;
  onError?: (err: { code: string; message: string; fatal: boolean }) => void;
}

export interface BargeInOptions {
  /** Also send `user_text` for interim (final=false) and final (final=true) results. Default false. */
  sendUserText?: boolean;
}

/**
 * Wrap speech handlers so that onSpeechStart triggers barge-in.
 * Returns handlers to pass to `speech.start(...)`; your own handlers still run.
 */
export function wireBargeIn(
  events: SpeechEvents,
  player: PlayerLike,
  send: (msg: OutgoingMsg) => void,
  speaker: string,
  opts: BargeInOptions = {},
): SpeechEvents {
  return {
    onSpeechStart: () => {
      if (player.isPlaying()) {
        player.stopAll(); // must happen before anything else
        send({ type: "barge_in", speaker, ts: Date.now() });
      }
      events.onSpeechStart?.();
    },
    onInterim: (text) => {
      if (opts.sendUserText) send({ type: "user_text", speaker, text, final: false, ts: Date.now() });
      events.onInterim?.(text);
    },
    onFinal: (text) => {
      if (opts.sendUserText) send({ type: "user_text", speaker, text, final: true, ts: Date.now() });
      events.onFinal?.(text);
    },
    onError: (err) => events.onError?.(err),
  };
}

/** Handle server `stop_audio`: stop one say_id, or everything if omitted. */
export function handleStopAudio(player: PlayerLike, msg: StopAudioMsg): void {
  if (msg.say_id) player.stop(msg.say_id);
  else player.stopAll();
}

/** Minimal client surface (matches TwinClient in ws.ts). */
export interface ClientLike {
  send(msg: { type: string; [k: string]: unknown }): void;
  on(type: string, cb: (msg: any) => void): () => void; // eslint-disable-line @typescript-eslint/no-explicit-any
}

export interface SpeechLike {
  start(h: SpeechEvents): void;
  stop(): void;
}

export interface EnqueuePlayer extends PlayerLike {
  enqueue(msg: any): void; // eslint-disable-line @typescript-eslint/no-explicit-any
}

/**
 * One-call wiring: server audio -> player, stop_audio -> player, mic -> user_text,
 * human speech start while Twin plays -> stopAll() then barge_in. Returns detach().
 */
export function attachBargeIn(
  client: ClientLike,
  player: EnqueuePlayer,
  speech: SpeechLike,
  user: string,
  extra: SpeechEvents = {},
): () => void {
  const offAudio = client.on("audio", (m) => player.enqueue(m));
  const offStop = client.on("stop_audio", (m) => handleStopAudio(player, m as StopAudioMsg));
  speech.start(wireBargeIn(extra, player, (m) => client.send(m as unknown as { type: string }), user, { sendUserText: true }));
  return () => {
    offAudio();
    offStop();
    speech.stop();
  };
}
