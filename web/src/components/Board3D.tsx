// 3D view of the architecture board (RX-11): clients on top, services middle, data at the bottom,
// particles flowing along edges. Keeps its own model from the same client messages as Whiteboard.
import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react'
import type { BoxKind } from '../lib/protocol'
import type { AnyServerMessage, TwinClient } from '../lib/ws'
import { currentArchitecture } from './Whiteboard'
import type { Edge3D, Node3D } from './board3dScene'

const Scene = lazy(() => import('./board3dScene'))

const KINDS: BoxKind[] = ['service', 'datastore', 'client', 'external', 'note']
const asKind = (k: unknown): BoxKind => (KINDS.includes(k as BoxKind) ? (k as BoxKind) : 'service')
const num = (v: unknown, d: number) => (typeof v === 'number' && Number.isFinite(v) ? v : d)

interface Model {
  nodes: Map<string, Node3D>
  edges: Map<string, Edge3D>
  frames: Map<string, number>
  activeFrame?: string
}

function frameOrder(m: Model, f: unknown): number {
  const id = typeof f === 'string' ? f : m.activeFrame
  if (!id) return 0
  if (!m.frames.has(id)) m.frames.set(id, m.frames.size)
  return m.frames.get(id)!
}

function applyOp(m: Model, op: Record<string, unknown>) {
  switch (op.op) {
    case 'frame':
      frameOrder(m, op.id)
      m.activeFrame = String(op.id)
      break
    case 'focus':
      if (m.frames.has(String(op.id))) m.activeFrame = String(op.id)
      break
    case 'add_box': {
      const id = String(op.id)
      const prev = m.nodes.get(id)
      m.nodes.set(id, {
        id,
        label: String(op.label ?? id),
        kind: asKind(op.kind),
        x: num(op.x, 500),
        order: frameOrder(m, op.frame),
        highlighted: prev?.highlighted ?? false,
      })
      break
    }
    case 'connect': {
      const from = String(op.from_id), to = String(op.to_id)
      const id = String(op.id ?? `e_${from}_${to}`)
      m.edges.set(id, { id, from, to, highlighted: m.edges.get(id)?.highlighted ?? false })
      break
    }
    case 'rename': {
      const n = m.nodes.get(String(op.id))
      if (n) m.nodes.set(n.id, { ...n, label: String(op.label ?? op.text ?? n.label) })
      break
    }
    case 'remove': {
      const id = String(op.id)
      m.nodes.delete(id)
      m.edges.delete(id)
      for (const [eid, e] of m.edges) if (e.from === id || e.to === id) m.edges.delete(eid)
      break
    }
    case 'highlight': {
      const ids = Array.isArray(op.ids) ? (op.ids as unknown[]).map(String) : [String(op.id)]
      const on = op.on !== false
      for (const id of ids) {
        const n = m.nodes.get(id)
        if (n) m.nodes.set(id, { ...n, highlighted: on })
        const e = m.edges.get(id)
        if (e) m.edges.set(id, { ...e, highlighted: on })
      }
      break
    }
    case 'clear':
      m.nodes.clear()
      m.edges.clear()
      m.frames.clear()
      m.activeFrame = undefined
      break
  }
}

function seed(): Model {
  const m: Model = { nodes: new Map(), edges: new Map(), frames: new Map() }
  try {
    const arch = currentArchitecture()
    for (const b of arch.boxes) m.nodes.set(b.id, { id: b.id, label: b.label, kind: b.kind, x: b.x, order: 0, highlighted: b.highlighted })
    for (const e of arch.edges) m.edges.set(e.id, { id: e.id, from: e.from, to: e.to, highlighted: e.highlighted })
  } catch (e) {
    console.warn('[board3d] seed failed', e)
  }
  return m
}

export default function Board3D({ client, onClose }: { client: TwinClient; onClose?: () => void }) {
  const model = useRef<Model>(null as unknown as Model)
  if (!model.current) model.current = seed()
  const [ver, setVer] = useState(0)
  const [visible, setVisible] = useState(true)
  const wrap = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const bump = () => setVer((v) => v + 1)
    const offDraw = client.on('draw', (msg: AnyServerMessage) => {
      applyOp(model.current, msg as Record<string, unknown>)
      bump()
    })
    const offSnap = client.on('board_snapshot', (msg: AnyServerMessage) => {
      const s = msg as { boxes?: Record<string, unknown>[]; edges?: Record<string, unknown>[]; shapes?: Record<string, unknown>[]; active_frame?: string }
      const m: Model = { nodes: new Map(), edges: new Map(), frames: new Map() }
      for (const sh of s.shapes ?? []) if (sh.op === 'frame') applyOp(m, sh)
      for (const b of s.boxes ?? []) applyOp(m, { ...b, op: 'add_box' })
      for (const e of s.edges ?? []) applyOp(m, { ...e, op: 'connect' })
      model.current = m
      bump()
    })
    return () => {
      offDraw()
      offSnap()
    }
  }, [client])

  // Render only while on screen and the tab is visible.
  useEffect(() => {
    const el = wrap.current
    let inView = true
    const update = () => setVisible(inView && document.visibilityState === 'visible')
    const io = el ? new IntersectionObserver(([e]) => { inView = e.isIntersecting; update() }) : undefined
    if (el && io) io.observe(el)
    document.addEventListener('visibilitychange', update)
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose?.()
    window.addEventListener('keydown', onKey)
    return () => {
      io?.disconnect()
      document.removeEventListener('visibilitychange', update)
      window.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const { nodes, edges } = useMemo(
    () => ({ nodes: [...model.current.nodes.values()], edges: [...model.current.edges.values()] }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ver],
  )

  return (
    <div
      ref={wrap}
      style={{
        position: 'absolute',
        inset: 0,
        background: 'radial-gradient(120% 90% at 50% 20%, #1c1d2b 0%, #101116 55%, #0b0c0e 100%)',
        overflow: 'hidden',
        animation: 'twin3dIn 320ms cubic-bezier(0.2, 0.8, 0.2, 1)',
      }}
    >
      <style>{'@keyframes twin3dIn{from{opacity:0;transform:scale(.985)}to{opacity:1;transform:none}}'}</style>
      <Suspense fallback={<div style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', color: 'rgba(245,245,247,.6)', font: '500 13px var(--font, system-ui)' }}>Loading 3D view…</div>}>
        <Scene nodes={nodes} edges={edges} active={visible} />
      </Suspense>
      {nodes.length === 0 && (
        <div style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', pointerEvents: 'none', color: 'rgba(245,245,247,.55)', font: '500 14px var(--font, system-ui)' }}>
          No architecture yet. Boxes Twin draws will appear here in 3D.
        </div>
      )}
      <div style={{ position: 'absolute', left: 14, bottom: 12, display: 'flex', gap: 12, pointerEvents: 'none', font: '500 11px var(--font, system-ui)', color: 'rgba(245,245,247,.6)' }}>
        {([['Client', '#a78bfa'], ['Service', '#0a84ff'], ['Data store', '#30d158'], ['External', '#8e8e93']] as const).map(([l, c]) => (
          <span key={l} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <span style={{ width: 8, height: 8, borderRadius: 3, background: c }} />
            {l}
          </span>
        ))}
      </div>
    </div>
  )
}
