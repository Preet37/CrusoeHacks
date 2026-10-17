// Wires the call's audio: server audio -> player, mic -> speech recognition -> user_text,
// barge-in (stop locally first, then tell the server), echo guard, Twin mute, push-to-talk.
import { useEffect, useRef, useState } from 'react'
import { handleStopAudio, type StopAudioMsg } from './bargeIn'
import { EchoGuard } from './echoGuard'
import { AudioPlayer, type AudioMsg } from './player'
import { Speech } from './speech'
import type { TwinClient } from './ws'

export type AudioMode = 'speakers' | 'headphones'

export interface CallAudio {
  speechSupported: boolean
  speechError: string | null
  /** Recognition is running (mic on and supported). */
  listening: boolean
  mode: AudioMode
  setMode(m: AudioMode): void
  /** Count of recognized lines dropped as Twin's own echo (for the debug chip). */
  echoDropped: number
}

const MODE_KEY = 'twin.audioMode'

export function useCallAudio(opts: {
  client: TwinClient | null
  user: string
  micOn: boolean
  twinMuted: boolean
  onInterim(text: string): void
  onFinal(text: string): void
}): CallAudio {
  const { client, user, micOn, twinMuted } = opts
  const [speechError, setSpeechError] = useState<string | null>(null)
  const [echoDropped, setEchoDropped] = useState(0)
  const [mode, setModeState] = useState<AudioMode>(() => {
    try {
      return (localStorage.getItem(MODE_KEY) as AudioMode) || 'speakers'
    } catch {
      return 'speakers'
    }
  })
  const playerRef = useRef<AudioPlayer | null>(null)
  const guardRef = useRef(new EchoGuard())
  const cbRef = useRef(opts)
  cbRef.current = opts
  const mutedRef = useRef(twinMuted)
  mutedRef.current = twinMuted

  if (!playerRef.current) playerRef.current = new AudioPlayer()
  const speechSupported = Speech.isSupported()

  // Server audio -> player (and remember Twin's words for the echo guard).
  useEffect(() => {
    if (!client) return
    const player = playerRef.current!
    const guard = guardRef.current
    const offAudio = client.on('audio', (m) => {
      const msg = m as unknown as AudioMsg
      guard.add(msg.text)
      if (!mutedRef.current) player.enqueue(msg)
    })
    const offStop = client.on('stop_audio', (m) => handleStopAudio(player, m as unknown as StopAudioMsg))
    const offStart = player.onPlaybackStart(() => guard.touch())
    const keepAlive = window.setInterval(() => player.isPlaying() && guard.touch(), 1000)
    return () => {
      offAudio()
      offStop()
      offStart()
      window.clearInterval(keepAlive)
      player.stopAll()
    }
  }, [client])

  // Muting Twin stops whatever it's saying right now.
  useEffect(() => {
    if (twinMuted) playerRef.current?.stopAll()
  }, [twinMuted])

  // Mic -> speech recognition -> user_text + barge-in.
  useEffect(() => {
    if (!client || !micOn || !speechSupported) return
    const player = playerRef.current!
    const guard = guardRef.current
    // With speakers we wait for actual words so Twin's own voice can't trigger a barge-in.
    const speech = new Speech({ earlyTrigger: mode === 'headphones' ? 'speech' : 'interim' })
    let bargedThisUtterance = false

    const bargeIn = () => {
      if (bargedThisUtterance || !player.isPlaying()) return
      bargedThisUtterance = true
      player.stopAll() // stop locally FIRST
      client.send({ type: 'barge_in', speaker: user, ts: Date.now() })
    }

    speech.start({
      onSpeechStart: () => {
        bargedThisUtterance = false
        if (mode === 'headphones') bargeIn()
      },
      onInterim: (text) => {
        if (mode === 'speakers' && guard.isEcho(text)) return
        if (mode === 'speakers' && player.isPlaying() && guard.novelWords(text).length < 2) return
        bargeIn()
        client.send({ type: 'user_text', speaker: user, text, final: false, ts: Date.now() })
        cbRef.current.onInterim(text)
      },
      onFinal: (text) => {
        bargedThisUtterance = false
        if (!text.trim()) return
        if (guard.isEcho(text)) {
          setEchoDropped((n) => n + 1)
          cbRef.current.onInterim('')
          return
        }
        client.send({ type: 'user_text', speaker: user, text, final: true, ts: Date.now() })
        cbRef.current.onFinal(text)
      },
      onError: (e) => setSpeechError(e.fatal ? e.message : null),
    })
    setSpeechError(null)
    return () => speech.stop()
  }, [client, user, micOn, mode, speechSupported])

  return {
    speechSupported,
    speechError: speechSupported ? speechError : 'Speech recognition needs Chrome',
    listening: micOn && speechSupported && !speechError,
    mode,
    setMode: (m) => {
      setModeState(m)
      try {
        localStorage.setItem(MODE_KEY, m)
      } catch {
        /* ignore */
      }
    },
    echoDropped,
  }
}
