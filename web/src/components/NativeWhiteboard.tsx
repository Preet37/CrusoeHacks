import { useEffect, useMemo, useRef, useState } from 'react'
import type { AnyServerMessage, TwinClient } from '../lib/ws'
import Board3D from './Board3D'

// A small, independent production renderer for the authoritative board stream.
// It intentionally has no tldraw dependency and does not interpret HTML or code.
type Op = Record<string, unknown> & { op: string }
type Shape = Op & { id: string }
type FrameRect = { x: number; y: number; w: number; h: number }
type Board = {
  shapes: Map<string, Shape>
  edges: Map<string, Shape>
  highlighted: Set<string>
  activeFrame?: string
  focus?: string
}

const EMPTY = (): Board => ({ shapes: new Map(), edges: new Map(), highlighted: new Set() })
const PAGE_W = 2000
const PAGE_H = 1400
const BOX_W = 260
const BOX_H = 128
const number = (value: unknown, fallback: number) => typeof value === 'number' && Number.isFinite(value) ? value : fallback
const string = (value: unknown, fallback = '') => typeof value === 'string' ? value : fallback
const grid = (value: unknown, fallback = 0) => Math.max(0, Math.min(1000, number(value, fallback))) / 1000

function apply(board: Board, message: AnyServerMessage): Board {
  if (message.type === 'board_snapshot') {
    const next = EMPTY()
    const snapshot = message as Record<string, unknown>
    const shapes = Array.isArray(snapshot.shapes) ? snapshot.shapes : []
    for (const shape of shapes) if (shape && typeof shape === 'object' && (shape as Op).op === 'frame') add(next, shape as Op)
    for (const box of Array.isArray(snapshot.boxes) ? snapshot.boxes : [])
      if (box && typeof box === 'object') add(next, { ...(box as object), op: 'add_box' } as Op)
    for (const edge of Array.isArray(snapshot.edges) ? snapshot.edges : [])
      if (edge && typeof edge === 'object') add(next, { ...(edge as object), op: 'connect' } as Op)
    for (const shape of shapes) if (shape && typeof shape === 'object' && (shape as Op).op !== 'frame') add(next, shape as Op)
    if (typeof snapshot.active_frame === 'string') next.activeFrame = snapshot.active_frame
    return next
  }
  if (message.type !== 'draw') return board
  const next: Board = {
    shapes: new Map(board.shapes), edges: new Map(board.edges),
    highlighted: new Set(board.highlighted), activeFrame: board.activeFrame, focus: board.focus,
  }
  add(next, message as Op)
  return next
}

function add(board: Board, op: Op) {
  const id = string(op.id)
  if (op.op === 'clear') {
    board.shapes.clear(); board.edges.clear(); board.highlighted.clear()
    board.activeFrame = undefined; board.focus = undefined
    return
  }
  if (op.op === 'focus') {
    if (board.shapes.has(id)) board.focus = id
    if (board.shapes.get(id)?.op === 'frame') board.activeFrame = id
    return
  }
  if (op.op === 'highlight') {
    const ids = Array.isArray(op.ids) ? op.ids : [id]
    for (const item of ids) if (typeof item === 'string') {
      if (op.on === false) board.highlighted.delete(item)
      else board.highlighted.add(item)
    }
    return
  }
  if (op.op === 'remove') {
    if (!id) return
    const removed = new Set([id])
    // A removed frame takes its descendants and their connecting arrows with it.
    let changed = true
    while (changed) {
      changed = false
      for (const shape of board.shapes.values())
        if (typeof shape.frame === 'string' && removed.has(shape.frame) && !removed.has(shape.id)) {
          removed.add(shape.id); changed = true
        }
    }
    for (const key of removed) { board.shapes.delete(key); board.highlighted.delete(key) }
    for (const [key, edge] of board.edges)
      if (removed.has(key) || removed.has(string(edge.from_id)) || removed.has(string(edge.to_id))) board.edges.delete(key)
    if (board.activeFrame && removed.has(board.activeFrame)) board.activeFrame = undefined
    if (board.focus && removed.has(board.focus)) board.focus = undefined
    return
  }
  if (op.op === 'rename') {
    const shape = board.shapes.get(id) ?? board.edges.get(id)
    if (shape) {
      const field = shape.op === 'frame' ? 'title' : shape.op === 'add_box' || shape.op === 'connect' ? 'label' : 'text'
      const updated = { ...shape, [field]: string(op.label, string(op.text)) }
      if (shape.op === 'connect') board.edges.set(id, updated)
      else board.shapes.set(id, updated)
    }
    return
  }
  if (op.op === 'update') {
    const shape = board.shapes.get(id)
    if (shape) board.shapes.set(id, { ...shape, ...op, op: shape.op })
    return
  }
  if (!id) return
  if (op.op === 'connect') { board.edges.set(id, { ...op, id }); return }
  const known = new Set(['frame', 'add_box', 'add_sticky', 'add_text', 'add_mermaid', 'add_chart', 'add_table', 'add_code', 'add_mock', 'add_3d', 'sketch'])
  if (!known.has(op.op)) return
  board.shapes.set(id, { ...op, id, frame: typeof op.frame === 'string' ? op.frame : op.op === 'frame' ? undefined : board.activeFrame })
  if (op.op === 'frame') { board.activeFrame = id; board.focus = id }
}

function geometry(board: Board) {
  const frames = new Map<string, FrameRect>()
  const rootFrames: FrameRect[] = []
  const shapeRects = new Map<string, FrameRect>()
  const pending = [...board.shapes.values()].filter(shape => shape.op === 'frame')
  // Parent frames are always emitted before children by the board protocol.
  for (let pass = 0; pass < pending.length + 1 && pending.length; pass++) {
    let progressed = false
    for (let i = 0; i < pending.length;) {
      const shape = pending[i]
      const parentId = string(shape.frame)
      if (parentId && !frames.has(parentId) && board.shapes.has(parentId)) { i++; continue }
      const parent = frames.get(parentId)
      const source = parent ?? { x: 0, y: 0, w: PAGE_W, h: PAGE_H }
      const rect = {
        x: source.x + grid(shape.x) * source.w,
        y: source.y + grid(shape.y) * source.h,
        w: Math.max(0.05, grid(shape.w, 1000)) * source.w,
        h: Math.max(0.05, grid(shape.h, 1000)) * source.h,
      }
      if (!parent) {
        // Independent topic frames get separate areas, matching the existing board.
        for (let n = 0; n < 50 && rootFrames.some(other => rect.x < other.x + other.w + 200 && rect.x + rect.w + 200 > other.x && rect.y < other.y + other.h && rect.y + rect.h > other.y); n++)
          rect.x = Math.max(...rootFrames.map(other => other.x + other.w + 200))
        rootFrames.push(rect)
      }
      frames.set(shape.id, rect); shapeRects.set(shape.id, rect)
      pending.splice(i, 1); progressed = true
    }
    if (!progressed) break
  }
  for (const shape of board.shapes.values()) {
    if (shape.op === 'frame') continue
    const parent = frames.get(string(shape.frame))
    const source = parent ?? { x: 0, y: 0, w: PAGE_W, h: PAGE_H }
    const x = source.x + grid(shape.x, 500) * source.w
    const y = source.y + grid(shape.y, 500) * source.h
    const w = shape.op === 'add_box' ? BOX_W : Math.max(180, grid(shape.w, 280) * source.w)
    const h = shape.op === 'add_box' ? BOX_H : Math.max(80, grid(shape.h, 180) * source.h)
    shapeRects.set(shape.id, { x, y, w, h })
  }
  const bounds = [...shapeRects.values()]
  const minX = Math.min(0, ...bounds.map(r => r.x)) - 48
  const minY = Math.min(0, ...bounds.map(r => r.y)) - 48
  const maxX = Math.max(PAGE_W, ...bounds.map(r => r.x + r.w)) + 48
  const maxY = Math.max(PAGE_H, ...bounds.map(r => r.y + r.h)) + 48
  return { shapeRects, frames, viewBox: `${minX} ${minY} ${maxX - minX} ${maxY - minY}` }
}

function lines(value: string, max = 26, limit = 5) {
  const output: string[] = []
  for (const paragraph of value.split('\n')) {
    let rest = paragraph.trim()
    while (rest.length > max && output.length < limit) {
      let cut = rest.lastIndexOf(' ', max)
      if (cut < max / 2) cut = Math.max(rest.lastIndexOf('/', max), max)
      output.push(rest.slice(0, cut).trim()); rest = rest.slice(cut).trim()
    }
    if (rest && output.length < limit) output.push(rest)
  }
  if (output.length === limit && value.length > output.join('').length) output[limit - 1] += '…'
  return output
}

function borderPoint(source: FrameRect, target: FrameRect, gap = 6) {
  const cx = source.x + source.w / 2, cy = source.y + source.h / 2
  const dx = target.x + target.w / 2 - cx, dy = target.y + target.h / 2 - cy
  if (!dx && !dy) return { x: cx, y: cy }
  const t = Math.min(dx ? (source.w / 2) / Math.abs(dx) : Infinity,
    dy ? (source.h / 2) / Math.abs(dy) : Infinity)
  const length = Math.hypot(dx, dy)
  return { x: cx + dx * t + dx / length * gap, y: cy + dy * t + dy / length * gap }
}

const colors: Record<string, { fill: string; stroke: string }> = {
  service: { fill: '#eaf2ff', stroke: '#4479c9' }, datastore: { fill: '#e9f8ee', stroke: '#3b9d64' },
  client: { fill: '#f1eaff', stroke: '#8b62bc' }, external: { fill: '#f2f3f5', stroke: '#818894' },
  note: { fill: '#fff7dc', stroke: '#c3992e' },
}

function NativeShape({ shape, rect, highlighted }: { shape: Shape; rect: FrameRect; highlighted: boolean }) {
  if (shape.op === 'frame') return (
    <g data-testid="native-frame" data-id={shape.id}>
      <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} rx="24" fill="#f7f9fc" fillOpacity="0.72" stroke={highlighted ? '#e94949' : '#aebed3'} strokeWidth="4" />
      <text x={rect.x + 24} y={rect.y + 34} fontSize="24" fontWeight="700" fill="#334458">{lines(string(shape.title, shape.id), 90, 1)[0]}</text>
    </g>
  )
  const isBox = shape.op === 'add_box'
  const kind = isBox ? string(shape.kind, 'service') : shape.op === 'add_sticky' ? 'note' : 'external'
  const color = colors[kind] ?? colors.external
  const title = isBox ? string(shape.label, shape.id) : shape.op === 'add_sticky' || shape.op === 'add_text' ? string(shape.text) :
    shape.op === 'add_mermaid' ? `Mermaid diagram\n${string(shape.code).slice(0, 120)}` :
    shape.op === 'add_chart' ? `${string(shape.title, 'Chart')} (${string(shape.chart)})` :
    shape.op === 'add_table' ? `Table: ${Array.isArray(shape.columns) ? shape.columns.join(' · ') : ''}` :
    shape.op === 'add_code' ? `${string(shape.language, 'Code')}\n${string(shape.code).slice(0, 120)}` :
    shape.op === 'add_mock' ? `${string(shape.title, 'UI mockup')} (preview unavailable)` :
    shape.op === 'add_3d' ? `${string(shape.title, '3D scene')} (preview unavailable)` :
    shape.op === 'sketch' ? 'Sketch' : shape.id
  const textLines = lines(title, isBox ? 24 : 35, isBox ? 5 : 7)
  return <g data-testid={isBox ? 'native-box' : 'native-shape'} data-id={shape.id}>
    <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} rx={isBox ? 20 : 12}
      fill={color.fill} stroke={highlighted ? '#e94949' : color.stroke} strokeWidth={highlighted ? 7 : 3}
      strokeDasharray={kind === 'external' ? '10 6' : undefined} />
    <text x={rect.x + 16} y={rect.y + (rect.h - textLines.length * 23) / 2 + 18}
      fill="#273445" fontSize={isBox ? 20 : 22} fontWeight={isBox ? 600 : 500}>
      {textLines.map((line, index) => <tspan key={index} x={rect.x + 16} dy={index ? 23 : 0}>{line}</tspan>)}
    </text>
  </g>
}

export default function NativeWhiteboard({ client, onHasContent }: { client: TwinClient; onHasContent?: (has: boolean) => void }) {
  const [board, setBoard] = useState<Board>(EMPTY)
  const [view3d, setView3d] = useState(() => new URLSearchParams(location.search).get('view') === '3d')
  const contentRef = useRef(onHasContent)
  useEffect(() => { contentRef.current = onHasContent }, [onHasContent])
  useEffect(() => client.onBoard(message => setBoard(previous => apply(previous, message))), [client])
  useEffect(() => { contentRef.current?.(board.shapes.size > 0) }, [board.shapes.size])
  const { shapeRects, viewBox } = useMemo(() => geometry(board), [board])
  const frameShapes = [...board.shapes.values()].filter(shape => shape.op === 'frame')
  const otherShapes = [...board.shapes.values()].filter(shape => shape.op !== 'frame')
  return <div style={{ position: 'relative', width: '100%', height: '100%' }}>
    <svg data-testid="native-board" role="img" aria-label="Live meeting whiteboard" viewBox={viewBox}
    preserveAspectRatio="xMidYMid meet" style={{ width: '100%', height: '100%', display: 'block', background: '#fff', borderRadius: 16, touchAction: 'none' }}>
    <defs><marker id="native-arrowhead" markerWidth="12" markerHeight="10" refX="10" refY="5" orient="auto" markerUnits="strokeWidth"><path d="M0 0 L12 5 L0 10 z" fill="#66758b" /></marker></defs>
    {frameShapes.map(shape => { const rect = shapeRects.get(shape.id); return rect && <NativeShape key={shape.id} shape={shape} rect={rect} highlighted={board.highlighted.has(shape.id)} /> })}
    {[...board.edges.values()].map(edge => {
      const from = shapeRects.get(string(edge.from_id)); const to = shapeRects.get(string(edge.to_id))
      if (!from || !to) return null
      const start = borderPoint(from, to), end = borderPoint(to, from, 10)
      const { x: x1, y: y1 } = start, { x: x2, y: y2 } = end
      return <g key={edge.id} data-testid="native-edge" data-id={edge.id}>
        <line x1={x1} y1={y1} x2={x2} y2={y2} stroke={board.highlighted.has(edge.id) ? '#e94949' : '#66758b'} strokeWidth="4" markerEnd="url(#native-arrowhead)" />
        {typeof edge.label === 'string' && edge.label && <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 8} fontSize="19" fill="#344255" textAnchor="middle">{edge.label}</text>}
      </g>
    })}
    {otherShapes.map(shape => { const rect = shapeRects.get(shape.id); return rect && <NativeShape key={shape.id} shape={shape} rect={rect} highlighted={board.highlighted.has(shape.id)} /> })}
    </svg>
    {view3d && <div style={{ position: 'absolute', inset: 0, background: '#fff' }}><Board3D client={client} onClose={() => setView3d(false)} /></div>}
    <button data-testid="board-3d-toggle" onClick={() => setView3d(value => !value)}
      title={view3d ? 'Back to 2D board' : '3D view of the architecture'}
      style={{ position: 'absolute', top: 12, right: 12, zIndex: 400, borderRadius: 999,
        border: '1px solid #d6dce5', background: 'rgba(255,255,255,.9)', color: '#273445',
        padding: '5px 14px', font: '600 12px system-ui', cursor: 'pointer' }}>
      {view3d ? '2D' : '3D'}
    </button>
  </div>
}
