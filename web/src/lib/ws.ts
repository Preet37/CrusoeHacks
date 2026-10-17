// WebSocket client for the Twin server: typed send/receive, auto-reconnect, event emitter.
// With ?mock=1 it talks to lib/mockServer.ts (if present) instead of a real server.
import {
  PROTOCOL_VERSION,
  parseServerMessage,
  type ClientMessage,
  type ServerMessage,
  type ServerMessageOf,
  type ServerMessageType,
} from './protocol'

export type ConnState = 'connecting' | 'open' | 'closed'

/** Any message from the server, including v2 types protocol.ts may not know yet. */
export type AnyServerMessage = ServerMessage | { type: string; [k: string]: unknown }
/** Any message to the server, including v2 additions. */
export type AnyClientMessage = ClientMessage | { type: string; [k: string]: unknown }

export interface TwinClient {
  readonly room: string
  readonly user: string
  readonly mock: boolean
  readonly state: ConnState
  send(msg: AnyClientMessage): void
  on<T extends ServerMessageType>(type: T, cb: (msg: ServerMessageOf<T>) => void): () => void
  on(type: string, cb: (msg: AnyServerMessage) => void): () => void
  onAny(cb: (msg: AnyServerMessage) => void): () => void
  onState(cb: (state: ConnState) => void): () => void
  close(): void
}

/** Server base URL. VITE_SERVER_URL (http(s) or ws(s)) wins; else same host, port 8000. */
export function serverWsBase(): string {
  const env = (import.meta.env.VITE_SERVER_URL as string | undefined)?.trim()
  if (env) return env.replace(/^http/, 'ws').replace(/\/+$/, '')
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  // Vite dev ports talk to a separate server on :8000. Anywhere else (server serving web/dist,
  // or a public tunnel) the web app and /ws share one origin.
  const devPorts = new Set(['5173', '5174', '5180'])
  if (!devPorts.has(location.port)) return `${proto}://${location.host}`
  return `${proto}://${location.hostname || 'localhost'}:8000`
}

export function isMockMode(): boolean {
  return new URLSearchParams(location.search).get('mock') === '1'
}

type MockStart = (
  emit: (m: ServerMessage) => void,
  onClientMessage?: (handler: (m: AnyClientMessage) => void) => void,
) => () => void

// Optional module: missing file just means no mock server yet.
const mockModules = import.meta.glob('./mockServer.ts')

export function createClient(opts: { room: string; user: string; mock?: boolean }): TwinClient {
  const { room, user } = opts
  const mock = opts.mock ?? isMockMode()
  const typed = new Map<string, Set<(m: AnyServerMessage) => void>>()
  const any = new Set<(m: AnyServerMessage) => void>()
  const stateCbs = new Set<(s: ConnState) => void>()
  const pending: string[] = []
  let state: ConnState = 'connecting'
  let ws: WebSocket | null = null
  let closed = false
  let retry = 0
  let retryTimer: number | undefined
  let stopMock: (() => void) | null = null
  let mockInbox: ((m: AnyClientMessage) => void) | null = null

  const setState = (s: ConnState) => {
    if (s === state) return
    state = s
    stateCbs.forEach((cb) => cb(s))
  }

  const dispatch = (msg: AnyServerMessage) => {
    typed.get(msg.type)?.forEach((cb) => cb(msg))
    any.forEach((cb) => cb(msg))
  }

  const connect = () => {
    if (closed) return
    setState('connecting')
    const url = `${serverWsBase()}/ws?room=${encodeURIComponent(room)}&user=${encodeURIComponent(user)}`
    const sock = new WebSocket(url)
    ws = sock
    sock.onopen = () => {
      retry = 0
      setState('open')
      sock.send(JSON.stringify({ type: 'hello', protocol: PROTOCOL_VERSION, user, room }))
      while (pending.length) sock.send(pending.shift()!)
    }
    sock.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return
      const known = parseServerMessage(ev.data)
      if (known) return dispatch(known)
      try {
        const raw = JSON.parse(ev.data)
        if (raw && typeof raw.type === 'string') dispatch(raw)
      } catch {
        /* ignore */
      }
    }
    sock.onclose = () => {
      if (ws !== sock) return
      ws = null
      setState('closed')
      if (closed) return
      const delay = Math.min(5000, 300 * 2 ** retry++)
      retryTimer = window.setTimeout(connect, delay)
    }
    sock.onerror = () => sock.close()
  }

  const startMock = async () => {
    const loader = mockModules['./mockServer.ts']
    if (!loader) {
      console.warn('[ws] ?mock=1 but lib/mockServer.ts does not exist yet')
      setState('open')
      return
    }
    const mod = (await loader()) as { startMockServer?: MockStart }
    if (!mod.startMockServer) {
      console.warn('[ws] mockServer.ts has no startMockServer export')
      setState('open')
      return
    }
    if (closed) return
    setState('open')
    stopMock = mod.startMockServer(dispatch, (h) => (mockInbox = h))
  }

  if (mock) void startMock()
  else connect()

  const client: TwinClient = {
    room,
    user,
    mock,
    get state() {
      return state
    },
    send(msg) {
      if (mock) {
        mockInbox?.(msg)
        return
      }
      const text = JSON.stringify(msg)
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(text)
      // Interim text is only useful live; queue everything else for reconnect.
      else if (!(msg.type === 'user_text' && (msg as { final?: boolean }).final === false)) {
        pending.push(text)
        if (pending.length > 50) pending.shift()
      }
    },
    on(type: string, cb: (msg: never) => void) {
      let set = typed.get(type)
      if (!set) typed.set(type, (set = new Set()))
      const fn = cb as (m: AnyServerMessage) => void
      set.add(fn)
      return () => set!.delete(fn)
    },
    onAny(cb) {
      any.add(cb)
      return () => any.delete(cb)
    },
    onState(cb) {
      stateCbs.add(cb)
      return () => stateCbs.delete(cb)
    },
    close() {
      closed = true
      window.clearTimeout(retryTimer)
      stopMock?.()
      ws?.close()
      ws = null
      setState('closed')
    },
  }
  return client
}
