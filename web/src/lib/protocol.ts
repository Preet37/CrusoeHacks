// TypeScript mirror of PROTOCOL.md (v1 + v2 additions). Server mirror: server/protocol.py.
// Change only with agreement in AGENT_CHAT.txt, and bump PROTOCOL_VERSION.

export const PROTOCOL_VERSION = 2 // PROTOCOL.md v2.1 (adds add_3d); kept integer 2 for hello.protocol compat

// ---------------------------------------------------------------- browser -> server

export interface Hello {
  type: 'hello'
  protocol: number
  user: string
  room: string
}

export interface UserText {
  type: 'user_text'
  source?: 'speech' | 'typed'
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

export type ControlAction = 'mute_twin' | 'unmute_twin' | 'clear_board' | 'reset_session' | 'set_mode' | 'mic_off' | 'mic_on'

export interface Control {
  type: 'control'
  action: ControlAction
}

// v2
export interface DrawError {
  type: 'draw_error'
  id: string
  op: string
  error: string
}

export interface CalendarJoin {
  type: 'calendar_join'
  meeting: string
  scheduled?: string
}

export interface ConnectRepo {
  type: 'connect_repo'
  url: string
}

/** v2.2: one JPEG frame of a shared screen (browser -> server -> other browsers). */
export interface ScreenFrame {
  type: 'screen_frame'
  user?: string
  jpeg: string // base64
  w?: number
  h?: number
  ts?: number
}
/** v2.2: sharer stopped sharing. */
export interface ScreenStop {
  type: 'screen_stop'
  user?: string
}

export type ClientMessage =
  | Hello
  | UserText
  | BargeIn
  | PushToTalk
  | Control
  | DrawError
  | CalendarJoin
  | ConnectRepo
  | Draw
  | ScreenFrame
  | ScreenStop

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
  frame?: string
  op: 'add_box'
  id: string
  label: string
  x: number
  y: number
  kind: BoxKind
}
export interface ConnectOp {
  frame?: string
  op: 'connect'
  id: string
  from_id: string
  to_id: string
  label?: string
}
export interface RenameOp {
  frame?: string
  op: 'rename'
  id: string
  label: string
}
export interface RemoveOp {
  frame?: string
  op: 'remove'
  id: string
}
export interface HighlightOp {
  frame?: string
  op: 'highlight'
  ids: string[]
  on: boolean
}
export interface ClearOp {
  op: 'clear'
  frame?: string
}

// ---- v2 draw ops. Every op may carry `frame` (topic id); x/y/w/h are on the 0-1000 grid.
export interface FrameOp {
  op: 'frame'
  id: string
  title: string
  x: number
  y: number
  w: number
  h: number
  frame?: string
}
export interface FocusOp {
  op: 'focus'
  id: string
  frame?: string
}
export type StickyColor = 'yellow' | 'blue' | 'green' | 'pink' | 'orange'
export interface AddStickyOp {
  op: 'add_sticky'
  id: string
  text: string
  color?: StickyColor
  x: number
  y: number
  w?: number
  h?: number
  frame?: string
}
export interface AddTextOp {
  op: 'add_text'
  id: string
  text: string
  x: number
  y: number
  size?: 's' | 'm' | 'l' | 'xl'
  w?: number
  h?: number
  frame?: string
}
export interface AddMermaidOp {
  op: 'add_mermaid'
  id: string
  code: string
  x: number
  y: number
  w?: number
  h?: number
  frame?: string
}
export type ChartKind = 'bar' | 'line' | 'pie' | 'scatter'
export interface ChartSeries {
  name: string
  /** numbers, or [x, y] pairs for scatter */
  values: number[] | [number, number][]
}
export interface AddChartOp {
  op: 'add_chart'
  id: string
  chart: ChartKind
  title?: string
  labels?: string[]
  series: ChartSeries[]
  x: number
  y: number
  w?: number
  h?: number
  frame?: string
}
export interface AddTableOp {
  op: 'add_table'
  id: string
  columns: string[]
  rows: string[][]
  x: number
  y: number
  w?: number
  h?: number
  frame?: string
}
export interface AddCodeOp {
  op: 'add_code'
  id: string
  language?: string
  code: string
  x: number
  y: number
  w?: number
  h?: number
  frame?: string
}
export interface AddMockOp {
  op: 'add_mock'
  id: string
  title?: string
  html: string
  x: number
  y: number
  w?: number
  h?: number
  frame?: string
}
/** v2.1: small interactive 3D scene (Three.js). */
export interface Scene3dObject {
  id?: string
  shape: 'box' | 'cylinder' | 'sphere' | 'cone' | 'torus' | 'plane'
  pos?: [number, number, number]
  rot?: [number, number, number] // radians
  scale?: [number, number, number]
  color?: string
  label?: string
}
export interface Scene3d {
  autoRotate?: boolean
  objects?: Scene3dObject[] // max 60
  gltf?: string
}
export interface Add3dOp {
  op: 'add_3d'
  id: string
  title?: string
  scene: Scene3d
  x: number
  y: number
  w?: number
  h?: number
  frame?: string
}
export type SketchColor = 'black' | 'red' | 'blue' | 'green' | 'orange' | 'violet'
export interface SketchOp {
  op: 'sketch'
  id: string
  strokes: [number, number][][]
  color?: SketchColor
  frame?: string
}
/** Partial update of an existing rich shape (e.g. new Mermaid code). */
export interface UpdateOp {
  op: 'update'
  id: string
  frame?: string
  [k: string]: unknown
}

export type DrawOp =
  | AddBoxOp
  | ConnectOp
  | RenameOp
  | RemoveOp
  | HighlightOp
  | ClearOp
  | FrameOp
  | FocusOp
  | AddStickyOp
  | AddTextOp
  | AddMermaidOp
  | AddChartOp
  | AddTableOp
  | AddCodeOp
  | AddMockOp
  | Add3dOp
  | SketchOp
  | UpdateOp
/** v2.2: `by` is who drew it ("Twin" or a human name); humans send the same ops. */
export type Draw = { type: 'draw'; by?: string } & DrawOp

export interface Box {
  id: string
  label: string
  x: number
  y: number
  kind: BoxKind
  frame?: string
}

export interface Edge {
  id: string
  from_id: string
  to_id: string
  label?: string
  frame?: string
}

export interface BoardSnapshot {
  type: 'board_snapshot'
  boxes: Box[]
  edges: Edge[]
  /** v2: every other shape (frames first), replayed as draw ops. */
  shapes?: Draw[]
  active_frame?: string
}

export type AgentState = 'started' | 'done' | 'failed' | 'cancelled'

export interface AgentEvent {
  type: 'agent_event'
  event_id: string
  parent_id?: string | null
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

// v2
export interface Topic {
  type: 'topic'
  topic_id: string
  title: string
  active: boolean
  changed?: boolean
}

export interface Participant {
  name: string
  kind: 'human' | 'ai'
  speaking: boolean
  muted?: boolean
}

export interface Participants {
  type: 'participants'
  people: Participant[]
}

export interface Brief {
  type: 'brief'
  title: string
  predictions: string[]
  ts?: number
}

export type ServerMessage =
  | Topic
  | Participants
  | Brief
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
  | ScreenFrame
  | ScreenStop

export type ServerMessageType = ServerMessage['type']
export type ServerMessageOf<T extends ServerMessageType> = Extract<ServerMessage, { type: T }>

const SERVER_TYPES: ReadonlySet<string> = new Set<ServerMessageType>([
  'status', 'transcript', 'audio', 'stop_audio', 'draw', 'board_snapshot',
  'agent_event', 'task', 'metric', 'error', 'topic', 'participants', 'brief',
  'screen_frame', 'screen_stop',
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
