// TypeScript mirror of PROTOCOL.md (v1). Server mirror: server/protocol.py.
// Change only with agreement in AGENT_CHAT.txt, and bump PROTOCOL_VERSION.

export const PROTOCOL_VERSION = 1

// ---------------------------------------------------------------- browser -> server

export interface Hello {
  type: 'hello'
  protocol: number
  user: string
  room: string
}

export interface UserText {
  type: 'user_text'
  speaker: string
  text: string
  final: boolean
  ts?: number
}

export interface BargeIn {
  type: 'barge_in'
  speaker?: string
  ts?: number
}

export interface PushToTalk {
  type: 'push_to_talk'
  active: boolean
  ts?: number
}

export type ControlAction = 'mute_twin' | 'unmute_twin' | 'clear_board' | 'reset_session'

export interface Control {
  type: 'control'
  action: ControlAction
}

export type ClientMessage = Hello | UserText | BargeIn | PushToTalk | Control

// ---------------------------------------------------------------- server -> browser

export type StatusState = 'listening' | 'thinking' | 'speaking' | 'hand_raised'

export interface Status {
  type: 'status'
  state: StatusState
}

export interface Transcript {
  type: 'transcript'
  speaker: string
  text: string
  ts?: number
}

export interface Audio {
  type: 'audio'
  say_id: string
  seq: number
  format: string
  sample_rate?: number
  text: string
  /** base64 WAV, or null: speak `text` with speechSynthesis instead. */
  data: string | null
  last: boolean
}

export interface StopAudio {
  type: 'stop_audio'
  /** Omitted = stop everything. */
  say_id?: string
}

export type BoxKind = 'service' | 'datastore' | 'client' | 'external' | 'note'

export interface AddBoxOp {
  op: 'add_box'
  id: string
  label: string
  x: number
  y: number
  kind: BoxKind
}
export interface ConnectOp {
  op: 'connect'
  id: string
  from_id: string
  to_id: string
  label?: string
}
export interface RenameOp {
  op: 'rename'
  id: string
  label: string
}
export interface RemoveOp {
  op: 'remove'
  id: string
}
export interface HighlightOp {
  op: 'highlight'
  ids: string[]
  on: boolean
}
export interface ClearOp {
  op: 'clear'
}

export type DrawOp = AddBoxOp | ConnectOp | RenameOp | RemoveOp | HighlightOp | ClearOp
export type Draw = { type: 'draw' } & DrawOp

export interface Box {
  id: string
  label: string
  x: number
  y: number
  kind: BoxKind
}

export interface Edge {
  id: string
  from_id: string
  to_id: string
  label?: string
}

export interface BoardSnapshot {
  type: 'board_snapshot'
  boxes: Box[]
  edges: Edge[]
}

export type AgentState = 'started' | 'done' | 'failed' | 'cancelled'

export interface AgentEvent {
  type: 'agent_event'
  event_id: string
  parent_id?: string
  agent: string
  model?: string
  state: AgentState
  detail?: string
  ts?: number
}

export interface Task {
  type: 'task'
  id: string
  title: string
  owner?: string
  source?: string
}

export interface Metric {
  type: 'metric'
  version: string
  split: string
  name: string
  value: number
}

export interface ErrorMsg {
  type: 'error'
  message: string
  recoverable: boolean
}

export type ServerMessage =
  | Status
  | Transcript
  | Audio
  | StopAudio
  | Draw
  | BoardSnapshot
  | AgentEvent
  | Task
  | Metric
  | ErrorMsg

export type ServerMessageType = ServerMessage['type']
export type ServerMessageOf<T extends ServerMessageType> = Extract<ServerMessage, { type: T }>

const SERVER_TYPES: ReadonlySet<string> = new Set<ServerMessageType>([
  'status', 'transcript', 'audio', 'stop_audio', 'draw', 'board_snapshot',
  'agent_event', 'task', 'metric', 'error',
])

/** Parse one wire frame. Returns null for anything that isn't a known server message. */
export function parseServerMessage(raw: string): ServerMessage | null {
  try {
    const data = JSON.parse(raw)
    if (data && typeof data === 'object' && SERVER_TYPES.has(data.type)) return data as ServerMessage
  } catch {
    /* ignore bad frames */
  }
  return null
}

export const nowMs = () => Date.now()
