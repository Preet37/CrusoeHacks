// Voice activity on an echo-cancelled mic stream. Chrome's AEC removes Twin's own playback
// from this stream, so sustained energy while Twin is talking means a human is talking over it.
// Used for barge-in because speech recognition alone often hears Twin through laptop speakers.

export interface VadOptions {
  /** Called once per detected onset (after `minMs` of speech). */
  onVoice: () => void
  /** Only fire while this is true (e.g. Twin audio playing). */
  active: () => boolean
  minMs?: number
}

export async function startVad(opts: VadOptions): Promise<() => void> {
  const minMs = opts.minMs ?? 180
  let stream: MediaStream
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false },
    })
  } catch {
    return () => {}
  }
  const ctx = new AudioContext()
  const src = ctx.createMediaStreamSource(stream)
  const analyser = ctx.createAnalyser()
  analyser.fftSize = 1024
  src.connect(analyser)
  const buf = new Float32Array(analyser.fftSize)
  let floor = 0.01
  let loudSince = 0
  let fired = false
  const tick = () => {
    analyser.getFloatTimeDomainData(buf)
    let sum = 0
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i]
    const rms = Math.sqrt(sum / buf.length)
    const now = performance.now()
    const playing = opts.active()
    if (!playing) {
      floor = floor * 0.95 + Math.min(rms, 0.05) * 0.05 // learn room noise while Twin is quiet
      loudSince = 0
      fired = false
      return
    }
    const threshold = Math.max(0.02, floor * 3.5)
    if (rms > threshold) {
      if (!loudSince) loudSince = now
      if (!fired && now - loudSince >= minMs) {
        fired = true
        opts.onVoice()
      }
    } else if (now - loudSince > 400) {
      loudSince = 0
      fired = false
    }
  }
  const timer = window.setInterval(tick, 40)
  return () => {
    window.clearInterval(timer)
    stream.getTracks().forEach((t) => t.stop())
    void ctx.close()
  }
}
