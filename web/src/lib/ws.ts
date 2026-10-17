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

const SERVER_KEY = 'twin.server'

/** Normalise http(s)/ws(s) URLs to a ws(s) base without trailing slash or /ws. */
function toWsBase(u: string) {
  return u.trim().replace(/^http/, 'ws').replace(/\/ws\/?$/, '').replace(/\/+$/, '')
}

/**
 * Server base URL, first match wins:
 *  1. ?server=<url> in the page link (saved for later visits, so invite links can carry it),
 *  2. a previously saved ?server (ignored on dev ports so local work never hits an old tunnel),
 *  3. VITE_SERVER_URL at build time,
 *  4. same origin when the page is served by the Twin server or a tunnel,
 *  5. ws://<host>:8000 on the Vite dev ports.
 */
export function serverWsBase(): string {
  const q = new URLSearchParams(location.search).get('server')
  if (q) {
    try {
      localStorage.setItem(SERVER_KEY, q)
    } catch {
      /* private mode */
    }
    return toWsBase(q)
  }
  // Vite dev server (any port) or the usual dev ports when serving a preview build.
  const onDev = import.meta.env.DEV || ['5173', '5174', '5180'].includes(location.port)
  try {
    const saved = onDev ? null : localStorage.getItem(SERVER_KEY)
    if (saved) return toWsBase(saved)
  } catch {
    /* ignore */
  }
  const env = (import.meta.env.VITE_SERVER_URL as string | undefined)?.trim()
  if (env) return toWsBase(env)
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  // Vite dev ports talk to a separate server on :8000. Anywhere else (server serving web/dist,
  // or a public tunnel) the web app and /ws share one origin.
  if (!onDev) return `${proto}://${location.host}`
  return `${proto}://${location.hostname || 'localhost'}:8000`
}

/** HTTP base of the same server (for /calendar, /metrics, ...). */
export function serverHttpBase(): string {
  return serverWsBase().replace(/^ws/, 'http')
}

/** Forget a saved ?server override (e.g. when the tunnel URL changed). */
export function clearSavedServer() {
  try {
    localStorage.removeItem(SERVER_KEY)
  } catch {
    /* ignore */
  }
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

export function createClient(opts: {
  room: string
  user: string
  mock?: boolean
  /** Company workspace + invite code; the server checks them (WORKSPACES env). */
  workspace?: string
  code?: string
}): TwinClient {
  const { room, user, workspace, code } = opts
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

  // Late subscribers (lazy panels) get the latest copy of these, one per key, so data sent at join isn't lost.
  const replay = new Map<string, Map<string, AnyServerMessage>>()
  const replayKey = (m: AnyServerMessage): string | null => {
    const r = m as Record<string, unknown>
    if (m.type === 'metric') return `${r.split}|${r.name}|${r.version}`
    if (m.type === 'task') return String(r.id)
    return null
  }
  const remember = (m: AnyServerMessage) => {
    const k = replayKey(m)
    if (k === null) return
    let bucket = replay.get(m.type)
    if (!bucket) replay.set(m.type, (bucket = new Map()))
    bucket.delete(k)
    bucket.set(k, m)
    if (bucket.size > 100) bucket.delete(bucket.keys().next().value as string)
  }

  const dispatch = (msg: AnyServerMessage) => {
    remember(msg)
    typed.get(msg.type)?.forEach((cb) => cb(msg))
    any.forEach((cb) => cb(msg))
  }

  const connect = () => {
    if (closed) return
    setState('connecting')
    const params = new URLSearchParams({ room, user })
    if (workspace) params.set('workspace', workspace)
    if (code) params.set('code', code)
    const url = `${serverWsBase()}/ws?${params}`
    const sock = new WebSocket(url)
    ws = sock
    sock.onopen = () => {
      retry = 0
      setState('open')
      sock.send(JSON.stringify({ type: 'hello', protocol: PROTOCOL_VERSION, user, room, ...(workspace ? { workspace, code: code ?? '' } : {}) }))
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
      const cached = replay.get(type)
      if (cached?.size) {
        const msgs = [...cached.values()]
        queueMicrotask(() => set!.has(fn) && msgs.forEach((m) => fn(m)))
      }
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
