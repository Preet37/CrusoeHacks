// Echo guard, browser side (ARCH 1E.3): Twin's voice from the laptop speaker leaks into the mic,
// and speech recognition then "hears" Twin. We remember what Twin is saying and ignore recognized
// text that mostly matches it, so Twin doesn't interrupt itself or pollute the transcript.

const WINDOW_MS = 3500
const MATCH_RATIO = 0.8
// Filler words never count as a match (a follow-up question shares them with anything Twin said).
const STOP = new Set(['the','to','me','of','and','a','an','is','it','this','that','what','how','in','on','for','you','we','i','so','do','can','be','are','with','about','our','my','your','like','just','but','or','if','at','as','from','there','then','okay','ok','yeah','um','uh','its',"it's","i'm",'was','were','will','would','should','could'])

function words(text: string): string[] {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9' ]+/g, ' ')
    .split(/\s+/)
    .filter((w) => w.length > 1 && !STOP.has(w))
}

export class EchoGuard {
  private recent: { words: Set<string>; until: number }[] = []

  /** Remember a sentence Twin is about to say (call when audio arrives). */
  add(text: string, now = Date.now()) {
    const w = words(text)
    if (!w.length) return
    // Keep it "live" for a while after it plays (playback length is roughly 350 ms/word).
    this.recent.push({ words: new Set(w), until: now + w.length * 350 + WINDOW_MS })
    if (this.recent.length > 8) this.recent.shift()
  }

  /** Extend the live window of everything Twin said (call while audio is still playing). */
  touch(now = Date.now()) {
    for (const r of this.recent) r.until = Math.max(r.until, now + WINDOW_MS / 2)
  }

  clear() {
    this.recent = []
  }

  /** True if `text` looks like Twin's own voice coming back through the mic. */
  isEcho(text: string, now = Date.now()): boolean {
    this.recent = this.recent.filter((r) => r.until > now)
    if (!this.recent.length) return false
    const w = words(text)
    if (!w.length) return true // pure noise while Twin talks
    const pool = new Set<string>()
    for (const r of this.recent) r.words.forEach((x) => pool.add(x))
    const hits = w.filter((x) => pool.has(x)).length
    if (w.length <= 2) return hits === w.length
    return hits / w.length >= MATCH_RATIO
  }

  /** Words in `text` that Twin did not say (used to decide a real barge-in). */
  novelWords(text: string): string[] {
    const pool = new Set<string>()
    for (const r of this.recent) r.words.forEach((x) => pool.add(x))
    return words(text).filter((x) => !pool.has(x))
  }
}
