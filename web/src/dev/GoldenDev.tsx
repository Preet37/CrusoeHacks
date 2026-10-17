// Dev harness: replays server/tests/golden_ops.json (canonical v2/v2.1 draw ops) into <Whiteboard>
// through a fake TwinClient and lists per-op status. Open /golden-dev.html
import { StrictMode, useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import Whiteboard from '../components/Whiteboard'
import type { AnyClientMessage, AnyServerMessage, ConnState, TwinClient } from '../lib/ws'

type Op = { type: string; op: string; id?: string; ids?: string[]; [k: string]: unknown }
interface Golden {
  fixture_version?: string
  operations: Op[]
}

const GOLDEN_REL = '../../../server/tests/golden_ops.json'
// Vite resolves the out-of-root path for us; '?url' yields '/@fs/<abs path>' which we then fetch.
const urls = import.meta.glob('../../../server/tests/golden_ops.json', { query: '?url', import: 'default', eager: true })

async function loadGolden(): Promise<Golden> {
  const u = urls[GOLDEN_REL] as string | undefined
  if (!u) throw new Error('golden_ops.json not found by import.meta.glob')
  const res = await fetch(u.split('?')[0])
  if (!res.ok) throw new Error(`fetch golden ${u}: HTTP ${res.status}`)
  return (await res.json()) as Golden
}

function createFakeClient(onSend: (m: AnyClientMessage) => void) {
  const typed = new Map<string, Set<(m: AnyServerMessage) => void>>()
  const board = new Set<(m: AnyServerMessage) => void>()
  const any = new Set<(m: AnyServerMessage) => void>()
  const client: TwinClient = {
    room: 'golden',
    user: 'dev',
    mock: true,
    get state(): ConnState {
      return 'open'
    },
    send: (m) => onSend(m),
    on(type: string, cb: (msg: never) => void) {
      let set = typed.get(type)
      if (!set) typed.set(type, (set = new Set()))
      const fn = cb as (m: AnyServerMessage) => void
      set.add(fn)
      return () => set!.delete(fn)
    },
    onBoard(cb) {
      board.add(cb)
      return () => board.delete(cb)
    },
    onAny(cb) {
      any.add(cb)
      return () => any.delete(cb)
    },
    onState(cb) {
      cb('open')
      return () => {}
    },
    close() {},
  }
  const emit = (m: AnyServerMessage) => {
    typed.get(m.type)?.forEach((cb) => cb(m))
    if (m.type === 'draw' || m.type === 'board_snapshot') board.forEach((cb) => cb(m))
    any.forEach((cb) => cb(m))
  }
  return { client, emit }
}

interface BoardLike {
  ids: Map<string, string>
  editor?: { getShape(id: string): unknown }
}
const board = () => (window as unknown as { twinBoard?: BoardLike }).twinBoard

// Ops that do not create a shape: status comes from the board state instead.
const NON_SHAPE = new Set(['clear', 'focus', 'update', 'rename', 'highlight', 'remove'])

function GoldenDev() {
  const [golden, setGolden] = useState<Golden | null>(null)
  const [loadErr, setLoadErr] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [sent, setSent] = useState<AnyClientMessage[]>([])
  const [tick, setTick] = useState(0)

  const { client, emit } = useMemo(
    () =>
      createFakeClient((m) => {
        setSent((s) => [...s, m])
        if (m.type === 'draw_error') {
          const e = m as { id?: string; op?: string; error?: string }
          setErrors((x) => ({ ...x, [`${e.op}:${e.id ?? ''}`]: String(e.error ?? 'error') }))
        }
      }),
    [],
  )

  useEffect(() => {
    loadGolden().then(setGolden, (e) => setLoadErr(String(e)))
  }, [])

  useEffect(() => {
    if (!golden) return
    const ops = golden.operations
    // Frame first if any op references one that is not defined before it.
    const defined = new Set<string>()
    const seq: Op[] = []
    for (const op of ops) {
      const f = typeof op.frame === 'string' ? op.frame : undefined
      if (f && !defined.has(f) && !ops.some((o) => o.op === 'frame' && o.id === f))
        seq.push({ type: 'draw', op: 'frame', id: f, title: f, x: 0, y: 0, w: 1000, h: 1000 }), defined.add(f)
      if (op.op === 'frame' && op.id) defined.add(op.id)
      seq.push(op)
    }
    const t = window.setTimeout(() => seq.forEach((op) => emit(op as AnyServerMessage)), 300)
    return () => window.clearTimeout(t)
  }, [golden, emit])

  useEffect(() => {
    const t = window.setInterval(() => setTick((n) => n + 1), 400)
    return () => window.clearInterval(t)
  }, [])

  const b = board()
  const status = (op: Op): { text: string; color: string } => {
    const err = errors[`${op.op}:${op.id ?? ''}`]
    if (err) return { text: `draw_error: ${err}`, color: '#c62828' }
    if (!b) return { text: 'board not mounted', color: '#888' }
    if (NON_SHAPE.has(op.op)) {
      if (op.op === 'remove') return b.ids.has(String(op.id)) ? { text: 'still present!', color: '#c62828' } : { text: 'removed', color: '#2e7d32' }
      if (op.op === 'highlight') return { text: `sent (${(op.ids ?? []).filter((i) => b.ids.has(i)).length}/${(op.ids ?? []).length} targets exist)`, color: '#555' }
      if (op.op === 'clear') return { text: 'sent', color: '#555' }
      return { text: op.id && b.ids.has(op.id) ? 'sent (target exists)' : 'sent (target missing)', color: '#555' }
    }
    const sid = op.id ? b.ids.get(op.id) : undefined
    if (!sid) return op.op === 'add_sticky' && op.id === 's_temp' ? { text: 'removed later', color: '#555' } : { text: 'NOT registered', color: '#c62828' }
    const exists = b.editor?.getShape(sid) != null
    return exists ? { text: 'rendered', color: '#2e7d32' } : { text: 'registered, no shape', color: '#e65100' }
  }

  void tick
  const otherSent = sent.filter((m) => m.type !== 'draw_error')
  return (
    <div style={{ position: 'fixed', inset: 0, display: 'flex', font: '12px system-ui' }}>
      <div style={{ flex: 1, position: 'relative' }}>
        <Whiteboard client={client} />
      </div>
      <div style={{ width: 380, overflow: 'auto', borderLeft: '1px solid #ddd', padding: 8, background: '#fafafa' }}>
        <b>golden_ops.json {golden?.fixture_version ?? ''}</b>
        {loadErr && <div style={{ color: '#c62828' }}>{loadErr}</div>}
        <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: 6 }}>
          <tbody>
            {golden?.operations.map((op, i) => {
              const s = status(op)
              return (
                <tr key={i} style={{ borderTop: '1px solid #eee', verticalAlign: 'top' }}>
                  <td style={{ color: '#999' }}>{i}</td>
                  <td>{op.op}</td>
                  <td style={{ fontFamily: 'monospace' }}>{op.id ?? (op.ids ?? []).join(',')}</td>
                  <td style={{ color: s.color }} data-status={s.text}>
                    {s.text}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {otherSent.length > 0 && (
          <div style={{ marginTop: 8, color: '#555' }}>
            other client sends: {otherSent.map((m) => m.type).join(', ')}
          </div>
        )}
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <GoldenDev />
  </StrictMode>,
)
