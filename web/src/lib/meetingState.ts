// React state for the call, built from server messages.
// v2 messages (participants, topic, brief) are typed locally until protocol.ts has them.
import { useEffect, useState } from 'react'
import type { StatusState } from './protocol'
import type { AnyServerMessage, ConnState, TwinClient } from './ws'

export interface Person {
  name: string
  kind: 'human' | 'ai'
  speaking: boolean
}

export interface Line {
  id: number
  speaker: string
  text: string
  ts: number
  interim?: boolean
}

export interface Topic {
  topic_id: string
  title: string
}

export interface Brief {
  title: string
  predictions: string[]
  ts?: number
}

export interface MeetingState {
  conn: ConnState
  twinStatus: StatusState
  twinMuted: boolean
  people: Person[]
  lines: Line[]
  /** Latest interim caption per speaker (cleared when their final arrives). */
  interim: Record<string, string>
  /** Speakers with recent interim/final speech, name -> last ms. */
  lastSpoke: Record<string, number>
  topic: Topic | null
  brief: Brief | null
  lastError: string | null
}

const MAX_LINES = 200
let lineSeq = 0

export function useMeeting(client: TwinClient | null): MeetingState & {
  addLocalInterim: (speaker: string, text: string) => void
  setTwinMuted: (m: boolean) => void
} {
  const [s, set] = useState<MeetingState>(() => ({
    conn: client?.state ?? 'connecting',
    twinStatus: 'listening',
    twinMuted: false,
    people: [],
    lines: [],
    interim: {},
    lastSpoke: {},
    topic: null,
    brief: null,
    lastError: null,
  }))

  useEffect(() => {
    if (!client) return
    set((p) => ({ ...p, conn: client.state }))
    const offState = client.onState((conn) => set((p) => ({ ...p, conn })))
    const off = client.onAny((m: AnyServerMessage) => {
      const msg = m as Record<string, unknown> & { type: string }
      switch (msg.type) {
        case 'status':
          set((p) => ({ ...p, twinStatus: msg.state as StatusState }))
          break
        case 'transcript': {
          const speaker = String(msg.speaker)
          const line: Line = { id: ++lineSeq, speaker, text: String(msg.text), ts: Number(msg.ts ?? Date.now()) }
          set((p) => {
            const interim = { ...p.interim }
            delete interim[speaker]
            return {
              ...p,
              lines: [...p.lines, line].slice(-MAX_LINES),
              interim,
              lastSpoke: { ...p.lastSpoke, [speaker]: Date.now() },
            }
          })
          break
        }
        case 'participants':
          set((p) => ({ ...p, people: (msg.people as Person[]) ?? [] }))
          break
        case 'topic':
          if (msg.active !== false)
            set((p) => ({ ...p, topic: { topic_id: String(msg.topic_id), title: String(msg.title ?? '') } }))
          break
        case 'brief':
          set((p) => ({
            ...p,
            brief: { title: String(msg.title ?? ''), predictions: (msg.predictions as string[]) ?? [], ts: msg.ts as number },
          }))
          break
        case 'error':
          set((p) => ({ ...p, lastError: String(msg.message ?? 'error') }))
          break
      }
    })
    return () => {
      off()
      offState()
    }
  }, [client])

  return {
    ...s,
    addLocalInterim: (speaker, text) =>
      set((p) => ({
        ...p,
        interim: { ...p.interim, [speaker]: text },
        lastSpoke: { ...p.lastSpoke, [speaker]: Date.now() },
      })),
    setTwinMuted: (twinMuted) => set((p) => ({ ...p, twinMuted })),
  }
}
