// React state for the call, built from server messages.
// v2 messages (participants, topic, brief) are typed locally until protocol.ts has them.
import { useEffect, useState } from 'react'
import type { StatusState } from './protocol'
import type { AnyServerMessage, ConnState, TwinClient } from './ws'
import type { Annotation, AnnotateItem, RemoteFrame } from '../components/ScreenStage'
import type { Contribution } from '../components/TwinTake'

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
  /** Diagrammer jobs currently running (event ids) -> Twin is sketching. */
  sketching: string[]
  /** When Twin's status became hand_raised (ms), else null. */
  handRaisedAt: number | null
  /** Latest frame someone else is sharing (screen_frame), else null. */
  remoteScreen: RemoteFrame | null
  /** Twin's live markup on the shared screen. */
  annotations: Annotation[]
  /** Latest Twin contribution ("Twin's take") for the active topic. */
  contribution: Contribution | null
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
    sketching: [],
    handRaisedAt: null,
    remoteScreen: null,
    annotations: [],
    contribution: null,
  }))

  useEffect(() => {
    if (!client) return
    set((p) => ({ ...p, conn: client.state }))
    const offState = client.onState((conn) => set((p) => ({ ...p, conn })))
    const off = client.onAny((m: AnyServerMessage) => {
      const msg = m as Record<string, unknown> & { type: string }
      switch (msg.type) {
        case 'status': {
          const st = msg.state as StatusState
          set((p) => ({
            ...p,
            twinStatus: st,
            handRaisedAt: st === 'hand_raised' ? (p.twinStatus === 'hand_raised' ? p.handRaisedAt : Date.now()) : null,
          }))
          break
        }
        case 'agent_event': {
          if (!/diagram/i.test(String(msg.agent))) break
          const id = String(msg.event_id)
          set((p) => {
            const rest = p.sketching.filter((x) => x !== id)
            return { ...p, sketching: msg.state === 'started' ? [...rest, id] : rest }
          })
          break
        }
        case 'transcript': {
          const speaker = String(msg.speaker)
          // ts = local receive time, so caption timing never depends on the server clock.
          const line: Line = { id: ++lineSeq, speaker, text: String(msg.text), ts: Date.now() }
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
        case 'screen_frame':
          if (msg.user === client.user) break
          set((p) => ({
            ...p,
            remoteScreen: { user: String(msg.user), jpeg: String(msg.jpeg), w: Number(msg.w) || 1280, h: Number(msg.h) || 800 },
          }))
          break
        case 'screen_stop':
          set((p) => (p.remoteScreen && p.remoteScreen.user === msg.user ? { ...p, remoteScreen: null, annotations: [] } : p))
          break
        case 'annotate': {
          if (msg.target && msg.target !== 'screen') break
          const ttl = Number(msg.ttl_s ?? 20)
          const a: Annotation = {
            id: String(msg.id ?? Date.now()),
            items: (msg.items as AnnotateItem[]) ?? [],
            until: ttl > 0 ? Date.now() + ttl * 1000 : null,
          }
          set((p) => ({ ...p, annotations: [...p.annotations.filter((x) => x.id !== a.id), a] }))
          break
        }
        case 'contribution':
          set((p) => ({ ...p, contribution: { ...(msg as unknown as Contribution), id: String(msg.id ?? Date.now()) } }))
          break
        case 'error':
          set((p) => ({ ...p, lastError: String(msg.message ?? 'error') }))
          break
      }
    })
    // Drop expired annotations.
    const sweep = window.setInterval(() => {
      const now = Date.now()
      set((p) =>
        p.annotations.some((a) => a.until !== null && a.until < now)
          ? { ...p, annotations: p.annotations.filter((a) => a.until === null || a.until >= now) }
          : p,
      )
    }, 1000)
    return () => {
      off()
      offState()
      window.clearInterval(sweep)
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
