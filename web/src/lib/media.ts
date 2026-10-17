// Local camera + mic for the join preview and the user's own tile.
import { useEffect, useRef, useState } from 'react'

export interface LocalMedia {
  stream: MediaStream | null
  camOn: boolean
  micOn: boolean
  error: string | null
  /** 0..1 mic input level, updated ~20x per second while the mic is on. */
  level: number
  setCamOn(on: boolean): void
  setMicOn(on: boolean): void
}

/** Keeps one MediaStream; toggling the camera stops the track so the camera light turns off. */
export function useLocalMedia(initial: { cam: boolean; mic: boolean }): LocalMedia {
  const [stream, setStream] = useState<MediaStream | null>(null)
  const [camOn, setCamOn] = useState(initial.cam)
  const [micOn, setMicOn] = useState(initial.mic)
  const [error, setError] = useState<string | null>(null)
  const [level, setLevel] = useState(0)
  const streamRef = useRef<MediaStream | null>(null)

  // (Re)acquire when the camera or mic turns on; stop tracks when turned off.
  useEffect(() => {
    let cancelled = false
    const cur = streamRef.current
    const hasVideo = !!cur?.getVideoTracks().some((t) => t.readyState === 'live')
    const hasAudio = !!cur?.getAudioTracks().some((t) => t.readyState === 'live')

    if (!camOn && cur) cur.getVideoTracks().forEach((t) => { t.stop(); cur.removeTrack(t) })
    if (!micOn && cur) cur.getAudioTracks().forEach((t) => { t.stop(); cur.removeTrack(t) })

    const needVideo = camOn && !hasVideo
    const needAudio = micOn && !hasAudio
    if (!needVideo && !needAudio) {
      setStream(cur ? new MediaStream(cur.getTracks()) : null)
      return
    }
    navigator.mediaDevices
      ?.getUserMedia({
        video: needVideo ? { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' } : false,
        audio: needAudio ? { echoCancellation: true, noiseSuppression: true, autoGainControl: true } : false,
      })
      .then((s) => {
        if (cancelled) return s.getTracks().forEach((t) => t.stop())
        const merged = streamRef.current ?? new MediaStream()
        s.getTracks().forEach((t) => merged.addTrack(t))
        streamRef.current = merged
        setStream(new MediaStream(merged.getTracks()))
        setError(null)
      })
      .catch((e: Error) => {
        if (cancelled) return
        setError(e.name === 'NotAllowedError' ? 'Camera or mic permission denied' : e.message)
        if (needVideo) setCamOn(false)
        if (needAudio) setMicOn(false)
      })
    return () => {
      cancelled = true
    }
  }, [camOn, micOn])

  // Mic level meter.
  useEffect(() => {
    const track = stream?.getAudioTracks()[0]
    if (!track || !micOn) {
      setLevel(0)
      return
    }
    const ctx = new AudioContext()
    const src = ctx.createMediaStreamSource(new MediaStream([track]))
    const an = ctx.createAnalyser()
    an.fftSize = 512
    src.connect(an)
    const buf = new Uint8Array(an.fftSize)
    const id = window.setInterval(() => {
      an.getByteTimeDomainData(buf)
      let sum = 0
      for (const v of buf) sum += ((v - 128) / 128) ** 2
      setLevel(Math.min(1, Math.sqrt(sum / buf.length) * 4))
    }, 50)
    return () => {
      window.clearInterval(id)
      void ctx.close()
    }
  }, [stream, micOn])

  // Stop everything on unmount.
  useEffect(() => () => streamRef.current?.getTracks().forEach((t) => t.stop()), [])

  return { stream, camOn, micOn, error, level, setCamOn, setMicOn }
}

/** Attach a stream to a <video> element. */
export function useVideoRef(stream: MediaStream | null) {
  const ref = useRef<HTMLVideoElement | null>(null)
  useEffect(() => {
    const v = ref.current
    if (v && v.srcObject !== stream) v.srcObject = stream
  }, [stream])
  return ref
}

export function initials(name: string) {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  return ((parts[0]?.[0] ?? '?') + (parts[1]?.[0] ?? '')).toUpperCase()
}

const AVATAR_COLORS = ['#ff9f0a', '#30d158', '#bf5af2', '#ff375f', '#64d2ff', '#ffd60a', '#5e5ce6', '#ac8e68']
export function avatarColor(name: string) {
  let h = 0
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0
  return AVATAR_COLORS[h % AVATAR_COLORS.length]
}
