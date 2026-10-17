import { Component, Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { AnyServerMessage, TwinClient } from '../lib/ws'
import Board3D from './Board3D'
import { renderChartSvg } from './board/chart'
import { renderCodeSvg } from './board/code'
import { renderTableSvg } from './board/table'
import { validateScene, type Scene3D } from './board/scene3d'
import { FONT, MONO, textWidth } from './board/svgUtil'

const LazyScene = lazy(() => import('./board/threeScene'))

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
const BOX_W = 340
const BOX_H = 150
/** Board units per CSS px inside rich cards: content is laid out at rect/ZOOM and scaled up so it stays readable at fit zoom. */
const ZOOM = 1.9
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
    if (shape.op === 'sketch') {
      const strokes = sketchStrokes(shape, source)
      const pts = strokes.flat()
      if (pts.length) {
        const xs = pts.map(p => p[0]), ys = pts.map(p => p[1])
        const x = Math.min(...xs), y = Math.min(...ys)
        shapeRects.set(shape.id, { x, y, w: Math.max(1, Math.max(...xs) - x), h: Math.max(1, Math.max(...ys) - y) })
      }
      continue
    }
    const x = source.x + grid(shape.x, 500) * source.w
    const y = source.y + grid(shape.y, 500) * source.h
    const w = shape.op === 'add_box' ? BOX_W : Math.max(180, grid(shape.w, 280) * source.w)
    const h = shape.op === 'add_box' ? BOX_H : shape.op === 'add_text' ? 80 : Math.max(80, grid(shape.h, 180) * source.h)
    shapeRects.set(shape.id, { x, y, w, h })
  }
  // Fit to content (or the focused frame) with a ~6% margin.
  const focusRect = board.focus && board.shapes.get(board.focus)?.op === 'frame' ? frames.get(board.focus) : undefined
  const bounds = focusRect ? [focusRect] : [...shapeRects.values()]
  let minX = 0, minY = 0, maxX = PAGE_W, maxY = PAGE_H
  if (bounds.length) {
    minX = Math.min(...bounds.map(r => r.x)); minY = Math.min(...bounds.map(r => r.y))
    maxX = Math.max(...bounds.map(r => r.x + r.w)); maxY = Math.max(...bounds.map(r => r.y + r.h))
  }
  const margin = Math.max(40, 0.06 * Math.max(maxX - minX, maxY - minY))
  const fit: FrameRect = { x: minX - margin, y: minY - margin, w: maxX - minX + margin * 2, h: maxY - minY + margin * 2 }
  return { shapeRects, frames, fit }
}

function frameSource(board: Board, frames: Map<string, FrameRect>, shape: Shape): FrameRect {
  void board
  return frames.get(string(shape.frame)) ?? { x: 0, y: 0, w: PAGE_W, h: PAGE_H }
}

function sketchStrokes(shape: Shape, source: FrameRect): [number, number][][] {
  const raw = Array.isArray(shape.strokes) ? shape.strokes : []
  return raw.map(s => (Array.isArray(s) ? s : [])
    .filter((p): p is [number, number] => Array.isArray(p) && p.length >= 2 && Number.isFinite(+p[0]) && Number.isFinite(+p[1]))
    .map(([x, y]) => [source.x + grid(+x) * source.w, source.y + grid(+y) * source.h] as [number, number]))
    .filter(s => s.length > 0)
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
  if (output.length === limit && value.replace(/\s+/g, '').length > output.join('').replace(/\s+/g, '').length) output[limit - 1] = fitText(output[limit - 1] + '…', max * 100, 1)
  return output
}

/** Ellipsize so the string fits maxW at the given font size. */
function fitText(value: string, maxW: number, size: number, mono = false) {
  if (textWidth(value, size, mono) <= maxW) return value
  const n = Math.max(1, Math.floor(maxW / (size * (mono ? 0.61 : 0.56))) - 1)
  return value.slice(0, n).trimEnd() + '…'
}

/** Word wrap into at most `limit` lines that fit maxW; the last line is ellipsized. */
function wrapWords(value: string, maxW: number, size: number, limit: number) {
  const words = value.split(/\s+/).filter(Boolean)
  const out: string[] = []
  let current = ''
  for (let i = 0; i < words.length; i++) {
    const next = current ? `${current} ${words[i]}` : words[i]
    if (textWidth(next, size) <= maxW || !current) { current = next; continue }
    if (out.length === limit - 1) { current = words.slice(i - 0).reduce((acc, w) => `${acc} ${w}`, current); break }
    out.push(current); current = words[i]
  }
  if (current) out.push(current)
  return out.slice(0, limit).map(line => fitText(line, maxW, size))
}

const GENERIC = new Set(['index', 'route', 'page', 'layout', 'main', '__init__', 'mod', 'app', 'server', 'handler', 'config', 'utils', 'types', 'schema', 'models', 'views', 'urls'])

/** "app/api/auth/[auth0]/route.ts Owner: Preet37" -> primary "auth0 / route", secondary "app/api/auth/[auth0] · Preet37". */
function humanize(label: string): { primary: string; secondary: string } {
  let text = label.trim()
  let owner = ''
  const ownerMatch = text.match(/[\s,;(·|-]*\bowner\s*[:=]\s*([^\n)]+)\)?\s*$/i)
  if (ownerMatch) { owner = ownerMatch[1].trim(); text = text.slice(0, ownerMatch.index).trim() }
  const [first = '', ...rest] = text.split('\n').map(line => line.trim()).filter(Boolean)
  const pathLike = /^[\w@.[\]()~+-]+(\/[\w@.[\]()~+-]*)+$|^[\w@[\]()~+-]+\.[a-z0-9]{1,5}$/i.test(first)
  let primary = first || label, secondary = rest.join(' · ')
  if (pathLike) {
    const parts = first.split('/').filter(Boolean)
    const file = parts[parts.length - 1] ?? first
    const base = file.replace(/\.[a-z0-9]{1,5}$/i, '') || file
    const parent = (parts[parts.length - 2] ?? '').replace(/[[\]()]/g, '').replace(/^\.\.\.?/, '')
    primary = parent && (GENERIC.has(base.toLowerCase()) || base.length <= 4) ? `${parent} / ${base}` : base
    const dir = parts.slice(0, -1).join('/')
    secondary = [dir, secondary].filter(Boolean).join(' · ')
  }
  if (owner) secondary = [secondary, owner].filter(Boolean).join(' · ')
  return { primary, secondary }
}

/** Anchor on the side of `rect` that faces `toward`, used for smooth connectors. */
function anchor(rect: FrameRect, toward: FrameRect, horizontal: boolean, gap: number) {
  const cx = rect.x + rect.w / 2, cy = rect.y + rect.h / 2
  const tx = toward.x + toward.w / 2, ty = toward.y + toward.h / 2
  if (horizontal) return { x: tx >= cx ? rect.x + rect.w + gap : rect.x - gap, y: cy, nx: tx >= cx ? 1 : -1, ny: 0 }
  return { x: cx, y: ty >= cy ? rect.y + rect.h + gap : rect.y - gap, nx: 0, ny: ty >= cy ? 1 : -1 }
}

/** Smooth path through points (quadratic curves via midpoints). */
function smoothPath(points: [number, number][]) {
  if (points.length === 1) return `M${points[0][0]} ${points[0][1]} l0.5 0.5`
  let d = `M${points[0][0]} ${points[0][1]}`
  for (let i = 1; i < points.length - 1; i++) {
    const [x, y] = points[i], [nx, ny] = points[i + 1]
    d += ` Q${x} ${y} ${(x + nx) / 2} ${(y + ny) / 2}`
  }
  const last = points[points.length - 1]
  return d + ` L${last[0]} ${last[1]}`
}

const ACCENT = '#0a84ff'
const kinds: Record<string, { fill: string; stroke: string; ink: string; label: string }> = {
  service: { fill: '#f1f6ff', stroke: '#bcd3f5', ink: '#2f6fd6', label: 'Service' },
  datastore: { fill: '#effaf3', stroke: '#b6e0c5', ink: '#2c9a5a', label: 'Data store' },
  client: { fill: '#f6f1ff', stroke: '#d5c4f2', ink: '#7d4fc4', label: 'Client' },
  external: { fill: '#f6f7f9', stroke: '#c3c9d2', ink: '#6b7482', label: 'External' },
  note: { fill: '#fff8e1', stroke: '#ecd58f', ink: '#a07a12', label: 'Note' },
}
const stickyFills: Record<string, string> = {
  yellow: '#fff4bf', pink: '#ffe0ea', red: '#ffe0e0', blue: '#dcebff', 'light-blue': '#dcebff', green: '#dcf5e3',
  'light-green': '#dcf5e3', orange: '#ffe7cc', violet: '#ece3ff', purple: '#ece3ff', 'light-violet': '#ece3ff', grey: '#eef0f3', gray: '#eef0f3',
}
const sketchColors: Record<string, string> = {
  black: '#1d2430', red: '#e5484d', blue: ACCENT, green: '#30a46c', orange: '#f76b15', violet: '#8e4ec6', purple: '#8e4ec6', grey: '#8b95a3', gray: '#8b95a3', yellow: '#e2b203',
}

/** Human edits carry `by`; Twin's ops say "Twin". */
const isHuman = (shape: Shape) => typeof shape.by === 'string' && !!shape.by && shape.by.toLowerCase() !== 'twin'
const HUMAN_KIND = { fill: '#fff3e8', stroke: '#f5b88a', ink: '#d9690f', label: 'Service' }

function KindIcon({ kind, x, y, color }: { kind: string; x: number; y: number; color: string }) {
  const common = { fill: 'none', stroke: color, strokeWidth: 2, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const }
  return <g transform={`translate(${x} ${y})`}>
    <rect width="44" height="44" rx="11" fill={color} fillOpacity="0.12" />
    <g transform="translate(8 8) scale(1.4)">
      {kind === 'datastore' ? <>
        <ellipse cx="10" cy="4.5" rx="7.5" ry="3" {...common} />
        <path d="M2.5 4.5v11c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-11M2.5 10c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3" {...common} />
      </> : kind === 'client' ? <>
        <rect x="1.5" y="2.5" width="17" height="11.5" rx="2" {...common} />
        <path d="M7 18h6M10 14v4" {...common} />
      </> : kind === 'external' ? <>
        <circle cx="10" cy="10" r="8" {...common} />
        <path d="M2 10h16M10 2c2.5 2.6 2.5 13.4 0 16M10 2c-2.5 2.6-2.5 13.4 0 16" {...common} />
      </> : kind === 'note' ? <>
        <path d="M4 2.5h12v15H4z" {...common} /><path d="M7 7h6M7 10.5h6M7 14h3" {...common} />
      </> : <>
        <rect x="2" y="2.5" width="16" height="6" rx="1.8" {...common} />
        <rect x="2" y="11.5" width="16" height="6" rx="1.8" {...common} />
        <path d="M5.5 5.5h.01M5.5 14.5h.01" {...common} strokeWidth="2.6" />
      </>}
    </g>
  </g>
}

function GlowRing({ rect, radius }: { rect: FrameRect; radius: number }) {
  return <rect x={rect.x - 6} y={rect.y - 6} width={rect.w + 12} height={rect.h + 12} rx={radius + 6}
    fill="none" stroke={ACCENT} strokeWidth="3.5" filter="url(#native-glow)" pointerEvents="none" />
}

const svgUrl = (svg: string) => 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg)
type Report = (id: string, op: string, error: string) => void

function ErrorCard({ rect, title, error }: { rect: FrameRect; title: string; error: string }) {
  const size = Math.max(22, Math.min(28, rect.w / 16))
  return <g>
    <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} rx="12" fill="#fff6f5" stroke="#f3c1bd" strokeWidth="1.5" />
    <text x={rect.x + 18} y={rect.y + 18 + size} fontSize={size + 1} fontWeight="600" fill="#b42318" fontFamily={FONT}>{fitText(title, rect.w - 36, size + 1)}</text>
    <text fontSize={size - 2} fill="#7a2e26" fontFamily={FONT}>
      {lines(error, Math.max(12, Math.floor((rect.w - 36) / ((size - 2) * 0.56))), Math.max(1, Math.floor((rect.h - 60) / (size + 2)))).map((line, index) =>
        <tspan key={index} x={rect.x + 18} y={rect.y + 26 + size * 2 + 6 + index * (size + 2)}>{line}</tspan>)}
    </text>
  </g>
}

function ImageCard({ rect, svg, pad = 0, card = false }: { rect: FrameRect; svg: string; pad?: number; card?: boolean }) {
  return <g>
    <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} rx="12" fill={card ? '#fff' : 'none'} stroke={card ? '#e2e6ec' : 'none'} filter="url(#native-shadow)" />
    <image href={svgUrl(svg)} x={rect.x + pad} y={rect.y + pad} width={Math.max(1, rect.w - pad * 2)} height={Math.max(1, rect.h - pad * 2)} preserveAspectRatio="xMidYMid meet" />
  </g>
}

/** chart / table / code via the shared pure SVG builders. */
function SyncRich({ shape, rect, report }: { shape: Shape; rect: FrameRect; report: Report }) {
  const result = useMemo(() => {
    try {
      if (shape.op === 'add_chart') return { svg: renderChartSvg(shape, rect.w / ZOOM, rect.h / ZOOM).svg, card: true, pad: 0 }
      if (shape.op === 'add_table') return { svg: renderTableSvg(shape, rect.w / ZOOM * 1.2).svg, card: false, pad: 0 }
      return { svg: renderCodeSvg(shape, rect.w / ZOOM, rect.h / ZOOM).svg, card: false, pad: 0 }
    } catch (error) {
      return { error: error instanceof Error ? error.message : String(error) }
    }
  }, [shape, rect.w, rect.h])
  const error = 'error' in result ? result.error ?? '' : ''
  useEffect(() => { if (error) report(shape.id, shape.op, error) }, [error, report, shape.id, shape.op])
  if ('error' in result) return <ErrorCard rect={rect} title={`Couldn't render ${shape.op.replace('add_', '')}`} error={error} />
  return <ImageCard rect={rect} svg={result.svg} card={result.card} pad={result.pad} />
}

type MermaidResult = { svg: string; w: number; h: number } | { error: string }
const mermaidCache = new Map<string, Promise<MermaidResult>>()
function renderMermaidCached(code: string) {
  let pending = mermaidCache.get(code)
  if (!pending) {
    pending = import('./board/mermaid').then(module => module.renderMermaidSvg(code))
      .catch(error => ({ error: (error instanceof Error ? error.message : String(error)).split('\n').slice(0, 3).join(' ') }))
    mermaidCache.set(code, pending)
    if (mermaidCache.size > 60) mermaidCache.delete(mermaidCache.keys().next().value as string)
  }
  return pending
}

function MermaidShape({ shape, rect, report }: { shape: Shape; rect: FrameRect; report: Report }) {
  const code = string(shape.code).trim()
  const [state, setState] = useState<{ code: string; result: MermaidResult } | null>(null)
  useEffect(() => {
    let live = true
    if (!code) return
    renderMermaidCached(code).then(result => {
      if (!live) return
      setState({ code, result })
      if ('error' in result) report(shape.id, 'add_mermaid', result.error.slice(0, 300))
    })
    return () => { live = false }
  }, [code, report, shape.id])
  const result = state?.code === code ? state.result : undefined
  if (!code) return <ErrorCard rect={rect} title="Empty diagram" error="add_mermaid needs code" />
  if (result && 'error' in result) return <ErrorCard rect={rect} title="Couldn't render diagram" error={result.error} />
  if (!result) return <g>
    <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} rx="12" fill="#fff" stroke="#e2e6ec" filter="url(#native-shadow)" />
    <text x={rect.x + rect.w / 2} y={rect.y + rect.h / 2 + 6} textAnchor="middle" fontSize="28" fill="#8b95a3" fontFamily={FONT}>Rendering diagram…</text>
  </g>
  return <ImageCard rect={rect} svg={result.svg} card pad={Math.min(18, rect.w * 0.04)} />
}

function sanitizeHtml(html: unknown) {
  let value = String(html ?? '')
  value = value.replace(/<script\b[\s\S]*?<\/script\s*>/gi, '').replace(/<script\b[^>]*>/gi, '')
  value = value.replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '')
  return value.slice(0, 4096)
}

const MOCK_BASE = `<style>html,body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;font-size:14px;color:#1d2430}body{padding:12px}input,button,select,textarea{font:inherit}</style>`

function TitleBar({ title, badge }: { title: string; badge?: string }) {
  return <div style={{ height: 30, flex: '0 0 30px', display: 'flex', alignItems: 'center', gap: 6, padding: '0 12px', background: '#f4f5f7', borderBottom: '1px solid rgba(0,0,0,.07)', font: `600 13px ${FONT}`, color: '#3a4352' }}>
    {badge ? <span style={{ fontSize: 10, fontWeight: 700, color: '#fff', background: ACCENT, borderRadius: 4, padding: '1px 5px' }}>{badge}</span> : <>
      <span style={{ width: 9, height: 9, borderRadius: 9, background: '#ff5f57' }} />
      <span style={{ width: 9, height: 9, borderRadius: 9, background: '#febc2e' }} />
      <span style={{ width: 9, height: 9, borderRadius: 9, background: '#28c840' }} />
    </>}
    <span style={{ marginLeft: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{title}</span>
  </div>
}

function HtmlCard({ rect, children, zoom = ZOOM }: { rect: FrameRect; children: ReactNode; zoom?: number }) {
  return <g>
    <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} rx="12" fill="#fff" stroke="#e2e6ec" filter="url(#native-shadow)" />
    <foreignObject x={rect.x} y={rect.y} width={rect.w} height={rect.h} style={{ pointerEvents: 'none' }}>
      <div style={{ width: rect.w / zoom, height: rect.h / zoom, transform: `scale(${zoom})`, transformOrigin: '0 0', borderRadius: 12 / zoom, overflow: 'hidden', display: 'flex', flexDirection: 'column', background: '#fff', boxSizing: 'border-box', border: '1px solid #e2e6ec' }}>
        {children}
      </div>
    </foreignObject>
  </g>
}

class SceneBoundary extends Component<{ id: string; report: Report; children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null }
  static getDerivedStateFromError(error: unknown) { return { error: error instanceof Error ? error.message : String(error) } }
  componentDidCatch(error: unknown) { this.props.report(this.props.id, 'add_3d', (error instanceof Error ? error.message : String(error)).slice(0, 300)) }
  render() {
    if (this.state.error) return <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#b42318', font: `13px ${FONT}`, padding: 12, textAlign: 'center' }}>3D failed: {this.state.error.slice(0, 90)}</div>
    return this.props.children
  }
}

function ThreeDShape({ shape, rect, report }: { shape: Shape; rect: FrameRect; report: Report }) {
  const parsed = useMemo((): { scene: Scene3D } | { error: string } => {
    try { return { scene: validateScene(shape.scene) } } catch (error) { return { error: error instanceof Error ? error.message : String(error) } }
  }, [shape.scene])
  const error = 'error' in parsed ? parsed.error : ''
  useEffect(() => { if (error) report(shape.id, 'add_3d', error) }, [error, report, shape.id])
  const title = string(shape.title, '3D model')
  if ('error' in parsed) return <ErrorCard rect={rect} title={`Couldn't render ${title}`} error={error} />
  return <HtmlCard rect={rect}>
    <TitleBar title={title} badge="3D" />
    <div style={{ flex: 1, position: 'relative', minHeight: 0, background: 'linear-gradient(180deg,#fbfcfe,#eef1f6)' }}>
      <SceneBoundary id={shape.id} report={report}>
        <Suspense fallback={<div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#8b95a3', font: `13px ${FONT}` }}>Loading 3D…</div>}>
          <LazyScene scene={parsed.scene} interactive={false} />
        </Suspense>
      </SceneBoundary>
    </div>
  </HtmlCard>
}

function BoxShape({ shape, rect, highlighted }: { shape: Shape; rect: FrameRect; highlighted: boolean }) {
  const kind = kinds[string(shape.kind, 'service')] ? string(shape.kind, 'service') : 'service'
  const color = isHuman(shape) ? { ...HUMAN_KIND, label: kinds[kind].label } : kinds[kind]
  const full = string(shape.label, shape.id)
  const { primary, secondary } = humanize(full)
  const textX = rect.x + 78, textW = rect.w - 78 - 18
  const primarySize = 28, secondarySize = 21, kindSize = 15
  const primaryLines = wrapWords(primary, textW, primarySize, 2)
  const block = kindSize + primaryLines.length * 33 + (secondary ? 28 : 0)
  let y = rect.y + (rect.h - block) / 2 + kindSize
  return <g data-testid="native-box" data-id={shape.id}>
    <title>{full}</title>
    {highlighted && <GlowRing rect={rect} radius={12} />}
    <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} rx="12" fill={color.fill}
      stroke={highlighted ? ACCENT : color.stroke} strokeWidth={highlighted ? 2 : 1.25}
      strokeDasharray={kind === 'external' ? '7 5' : undefined} filter="url(#native-shadow)" />
    {kind === 'datastore' && <path d={`M${rect.x + 1} ${rect.y + 14} Q${rect.x + rect.w / 2} ${rect.y + 30} ${rect.x + rect.w - 1} ${rect.y + 14}`} fill="none" stroke={color.stroke} strokeWidth="1.25" />}
    <KindIcon kind={kind} x={rect.x + 20} y={rect.y + rect.h / 2 - 22} color={color.ink} />
    <text fontFamily={FONT}>
      <tspan x={textX} y={y} fontSize={kindSize} fontWeight="600" fill={color.ink} letterSpacing="0.06em">{color.label.toUpperCase()}</tspan>
      {primaryLines.map((line, index) => <tspan key={index} x={textX} y={(y += 33)} fontSize={primarySize} fontWeight="650" fill="#1d2430">{line}</tspan>)}
      {secondary && <tspan x={textX} y={y + 28} fontSize={secondarySize} fill="#6b7482">{fitText(secondary, textW, secondarySize)}</tspan>}
    </text>
  </g>
}

function NativeShape({ shape, rect, highlighted, report, source }: { shape: Shape; rect: FrameRect; highlighted: boolean; report: Report; source: FrameRect }) {
  if (shape.op === 'frame') return (
    <g data-testid="native-frame" data-id={shape.id}>
      {highlighted && <GlowRing rect={rect} radius={22} />}
      <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} rx="22" fill="#f7f8fb" stroke={highlighted ? ACCENT : '#e1e5ec'} strokeWidth={highlighted ? 2.5 : 1.5} />
      <text x={rect.x + 36} y={rect.y + 58} fontSize="32" fontWeight="600" fill="#2b3442" fontFamily={FONT}>{fitText(string(shape.title, shape.id), rect.w - 72, 32)}</text>
    </g>
  )
  if (shape.op === 'add_box') return <BoxShape shape={shape} rect={rect} highlighted={highlighted} />
  const body = (() => {
    switch (shape.op) {
      case 'add_mermaid': return <MermaidShape shape={shape} rect={rect} report={report} />
      case 'add_chart': case 'add_table': case 'add_code': return <SyncRich shape={shape} rect={rect} report={report} />
      case 'add_3d': return <ThreeDShape shape={shape} rect={rect} report={report} />
      case 'add_mock': return <HtmlCard rect={rect} zoom={1.45}>
        <TitleBar title={string(shape.title, 'UI mockup')} />
        <iframe sandbox="" srcDoc={MOCK_BASE + sanitizeHtml(shape.html)} title={string(shape.title, 'mock')}
          style={{ border: 0, width: '100%', flex: 1, background: '#fff', pointerEvents: 'none' }} />
      </HtmlCard>
      case 'add_sticky': {
        const text = string(shape.text)
        const size = Math.max(26, Math.min(34, rect.w / 9))
        const wrapped = lines(text, Math.max(8, Math.floor((rect.w - 36) / (size * 0.56))), Math.max(1, Math.floor((rect.h - 32) / (size * 1.3))))
        const top = rect.y + (rect.h - wrapped.length * size * 1.3) / 2 + size
        return <g>
          <rect x={rect.x} y={rect.y} width={rect.w} height={rect.h} rx="10" fill={stickyFills[string(shape.color, 'yellow')] ?? stickyFills.yellow} stroke="rgba(0,0,0,.06)" filter="url(#native-shadow)" />
          <text fontSize={size} fontWeight="500" fill="#2b2a24" fontFamily={FONT}>
            {wrapped.map((line, index) => <tspan key={index} x={rect.x + 22} y={top + index * size * 1.3}>{line}</tspan>)}
          </text>
        </g>
      }
      case 'add_text': {
        const sizes: Record<string, number> = { s: 26, m: 32, l: 44, xl: 56 }
        const size = sizes[string(shape.size, 'm')] ?? 32
        return <text x={rect.x} y={rect.y + size} fontSize={size} fontWeight={size >= 44 ? 700 : 500} fill="#1d2430" fontFamily={FONT}>
          {lines(string(shape.text), Math.max(10, Math.floor(Math.max(rect.w, 600) / (size * 0.56))), 4).map((line, index) => <tspan key={index} x={rect.x} dy={index ? size * 1.25 : 0}>{line}</tspan>)}
        </text>
      }
      case 'sketch': {
        const color = sketchColors[string(shape.color, 'black').toLowerCase()] ?? sketchColors.black
        return <g fill="none" stroke={color} strokeWidth={shape.hl ? 16 : 4} strokeLinecap="round" strokeLinejoin="round" opacity={shape.hl ? 0.4 : 0.85}>
          {sketchStrokes(shape, source).map((points, index) => <path key={index} d={smoothPath(points)} />)}
        </g>
      }
      default: return null
    }
  })()
  return <g data-testid="native-shape" data-id={shape.id} data-op={shape.op}>
    {highlighted && <GlowRing rect={rect} radius={12} />}
    {body}
  </g>
}

function Edge({ edge, from, to, highlighted }: { edge: Shape; from: FrameRect; to: FrameRect; highlighted: boolean }) {
  const dx = (to.x + to.w / 2) - (from.x + from.w / 2), dy = (to.y + to.h / 2) - (from.y + from.h / 2)
  const horizontal = Math.abs(dx) / Math.max(1, from.w + to.w) >= Math.abs(dy) / Math.max(1, from.h + to.h)
  const a = anchor(from, to, horizontal, 4), b = anchor(to, from, horizontal, 10)
  const reach = Math.max(40, Math.min(220, (horizontal ? Math.abs(b.x - a.x) : Math.abs(b.y - a.y)) * 0.5))
  const c1 = { x: a.x + a.nx * reach, y: a.y + a.ny * reach }, c2 = { x: b.x + b.nx * reach, y: b.y + b.ny * reach }
  const mid = { x: (a.x + 3 * c1.x + 3 * c2.x + b.x) / 8, y: (a.y + 3 * c1.y + 3 * c2.y + b.y) / 8 }
  const label = string(edge.label)
  const stroke = highlighted ? ACCENT : '#8a95a6'
  const pillText = fitText(label, 360, 22)
  const pillW = textWidth(pillText, 22) + 30
  return <g data-testid="native-edge" data-id={edge.id}>
    <path d={`M${a.x} ${a.y} C${c1.x} ${c1.y} ${c2.x} ${c2.y} ${b.x} ${b.y}`} fill="none" stroke={stroke} strokeWidth={highlighted ? 4 : 3}
      markerEnd={highlighted ? 'url(#native-arrowhead-hi)' : 'url(#native-arrowhead)'} filter={highlighted ? 'url(#native-glow)' : undefined} />
    {label && <g>
      <title>{label}</title>
      <rect x={mid.x - pillW / 2} y={mid.y - 19} width={pillW} height="38" rx="19" fill="#fff" stroke={highlighted ? ACCENT : '#dfe3ea'} />
      <text x={mid.x} y={mid.y + 7.5} fontSize="22" fontWeight="500" fill="#3a4352" textAnchor="middle" fontFamily={FONT}>{pillText}</text>
    </g>}
  </g>
}

type Tool = 'select' | 'pen' | 'highlighter' | 'sticky' | 'text' | 'box' | 'arrow' | 'eraser'
const TOOLS: { id: Tool; label: string; icon: string }[] = [
  { id: 'select', label: 'Select / pan', icon: '↖' },
  { id: 'pen', label: 'Pen', icon: '✎' },
  { id: 'highlighter', label: 'Highlighter', icon: '░' },
  { id: 'sticky', label: 'Sticky note', icon: '▤' },
  { id: 'text', label: 'Text', icon: 'T' },
  { id: 'box', label: 'Box', icon: '□' },
  { id: 'arrow', label: 'Arrow (click two boxes)', icon: '↗' },
  { id: 'eraser', label: 'Eraser', icon: '⌫' },
]
type Pending = { kind: 'sticky' | 'text' | 'box'; sx: number; sy: number; bx: number; by: number }
const clampGrid = (value: number) => Math.max(0, Math.min(1000, Math.round(value)))
const AUTO_FIT_PAUSE_MS = 5000

function AnnotateToolbar({ tool, onTool }: { tool: Tool; onTool: (tool: Tool) => void }) {
  return <div data-testid="board-annotate-toolbar" onPointerDown={event => event.stopPropagation()}
    style={{
      position: 'absolute', left: 12, top: '50%', transform: 'translateY(-50%)', zIndex: 400,
      display: 'flex', flexDirection: 'column', gap: 2, padding: 4, borderRadius: 999,
      border: '1px solid rgba(0,0,0,0.1)', background: 'rgba(255,255,255,0.7)',
      backdropFilter: 'blur(20px) saturate(180%)', WebkitBackdropFilter: 'blur(20px) saturate(180%)',
      boxShadow: '0 4px 16px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.08)',
    }}>
    {TOOLS.map(t => {
      const on = tool === t.id
      return <button key={t.id} data-tool={t.id} title={t.label} aria-label={t.label} aria-pressed={on} onClick={() => onTool(t.id)}
        style={{
          width: 28, height: 28, borderRadius: 999, border: 'none', padding: 0, cursor: 'pointer',
          background: on ? ACCENT : 'transparent', color: on ? '#fff' : '#1d1d1f',
          font: '600 14px -apple-system, BlinkMacSystemFont, system-ui, sans-serif', lineHeight: '28px',
        }}>{t.icon}</button>
    })}
  </div>
}

export default function NativeWhiteboard({ client, onHasContent }: { client: TwinClient; onHasContent?: (has: boolean) => void }) {
  const [board, setBoard] = useState<Board>(EMPTY)
  const [view3d, setView3d] = useState(() => new URLSearchParams(location.search).get('view') === '3d')
  const contentRef = useRef(onHasContent)
  const svgRef = useRef<SVGSVGElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const reported = useRef(new Set<string>())
  useEffect(() => { contentRef.current = onHasContent }, [onHasContent])
  useEffect(() => client.onBoard(message => setBoard(previous => apply(previous, message))), [client])
  useEffect(() => { contentRef.current?.(board.shapes.size > 0) }, [board.shapes.size])
  const report = useCallback<Report>((id, op, error) => {
    const key = `${id}|${op}|${error}`
    if (reported.current.has(key)) return
    reported.current.add(key)
    console.warn('[board] native render failed', op, id, error)
    try { client.send({ type: 'draw_error', id, op, error: error.slice(0, 300) }) } catch { /* ignore */ }
  }, [client])
  const { shapeRects, frames, fit } = useMemo(() => geometry(board), [board])
  // Pan/zoom: a user view that resets to fit whenever the board changes,
  // except while a human is marking up the board (then it resumes 5 s later).
  const [view, setView] = useState<FrameRect | null>(null)
  const pausedUntil = useRef(0)
  const fitDirty = useRef(false)
  const pauseTimer = useRef<number | undefined>(undefined)
  const drawing = useRef(false)
  useEffect(() => {
    if (Date.now() < pausedUntil.current || drawing.current) fitDirty.current = true
    else setView(null)
  }, [board])
  const vb = view ?? fit
  const vbRef = useRef(vb)
  vbRef.current = vb
  const pauseFit = useCallback(() => {
    pausedUntil.current = Date.now() + AUTO_FIT_PAUSE_MS
    setView(current => current ?? vbRef.current)
    window.clearTimeout(pauseTimer.current)
    const resume = () => {
      if (drawing.current || Date.now() < pausedUntil.current - 50) { pauseTimer.current = window.setTimeout(resume, 500); return }
      if (fitDirty.current) { fitDirty.current = false; setView(null) }
    }
    pauseTimer.current = window.setTimeout(resume, AUTO_FIT_PAUSE_MS)
  }, [])
  useEffect(() => () => window.clearTimeout(pauseTimer.current), [])
  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      const box = svg.getBoundingClientRect()
      const current = vbRef.current
      const scale = Math.max(current.w / box.width, current.h / box.height)
      const offsetX = (box.width * scale - current.w) / 2, offsetY = (box.height * scale - current.h) / 2
      const px = current.x - offsetX + (event.clientX - box.left) * scale
      const py = current.y - offsetY + (event.clientY - box.top) * scale
      const factor = Math.exp(Math.max(-0.5, Math.min(0.5, event.deltaY * (event.ctrlKey ? 0.01 : 0.0025))))
      setView({ x: px - (px - current.x) * factor, y: py - (py - current.y) * factor, w: current.w * factor, h: current.h * factor })
    }
    svg.addEventListener('wheel', onWheel, { passive: false })
    return () => svg.removeEventListener('wheel', onWheel)
  }, [])
  const drag = useRef<{ x: number; y: number; view: FrameRect; scale: number } | null>(null)

  // ---- Human markup tools ----
  const [tool, setToolState] = useState<Tool>('select')
  const [live, setLive] = useState<[number, number][] | null>(null)
  const stroke = useRef<[number, number][]>([])
  const [pending, setPending] = useState<Pending | null>(null)
  const [pendingText, setPendingText] = useState('')
  const [arrowFrom, setArrowFrom] = useState<string | null>(null)
  const [hover, setHover] = useState<string | null>(null)
  const seq = useRef(0)
  const setTool = (next: Tool) => { setToolState(next); setArrowFrom(null); setPending(null); setLive(null); drawing.current = false }
  const user = client.user || 'guest'
  const newId = () => {
    let slug = user.toLowerCase().replace(/[^a-z0-9]+/g, '') || 'guest'
    if (!/^[a-z]/.test(slug)) slug = 'u' + slug
    return `h_${slug.slice(0, 24)}_${Date.now().toString(36)}${(seq.current++).toString(36)}`
  }
  /** Screen -> board units, using the SVG's live view transform (viewBox + meet letterboxing). */
  const toBoard = (clientX: number, clientY: number): [number, number] | null => {
    const svg = svgRef.current
    const ctm = svg?.getScreenCTM()
    if (!svg || !ctm) return null
    const point = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse())
    return [point.x, point.y]
  }
  /** Innermost frame that contains the rect, or the root page. */
  const frameFor = (rect: FrameRect): { id?: string; src: FrameRect } => {
    let best: { id?: string; src: FrameRect } = { src: { x: 0, y: 0, w: PAGE_W, h: PAGE_H } }
    let bestArea = Infinity
    for (const [id, f] of frames)
      if (rect.x >= f.x && rect.y >= f.y && rect.x + rect.w <= f.x + f.w && rect.y + rect.h <= f.y + f.h && f.w * f.h < bestArea) {
        best = { id, src: f }; bestArea = f.w * f.h
      }
    return best
  }
  const toGrid = (x: number, y: number, src: FrameRect): [number, number] =>
    [clampGrid((x - src.x) / src.w * 1000), clampGrid((y - src.y) / src.h * 1000)]
  const sendDraw = (op: Op, local: Record<string, unknown> = {}) => {
    const message = { type: 'draw', ...op, by: user }
    try { client.send(message) } catch { /* ignore */ }
    // Optimistic: the server relays to everyone except the sender, so render it now.
    const creates = op.op !== 'remove' && op.op !== 'connect'
    setBoard(previous => apply(previous, { ...message, ...(creates && !op.frame ? { frame: '' } : {}), ...local } as AnyServerMessage))
  }
  const finishStroke = () => {
    const points = stroke.current
    drawing.current = false
    stroke.current = []
    setLive(null)
    if (!points.length) return
    const xs = points.map(p => p[0]), ys = points.map(p => p[1])
    const bbox = { x: Math.min(...xs), y: Math.min(...ys), w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys) }
    const { id: frame, src } = frameFor(bbox)
    const step = Math.max(1, Math.ceil(points.length / 480))
    const sampled = points.filter((_, index) => index % step === 0 || index === points.length - 1)
    const gridPoints = sampled.map(([x, y]) => toGrid(x, y, src))
    if (gridPoints.length === 1) gridPoints.push([...gridPoints[0]] as [number, number])
    const highlighter = tool === 'highlighter'
    sendDraw({ op: 'sketch', id: newId(), strokes: [gridPoints], color: highlighter ? 'orange' : 'blue', ...(frame ? { frame } : {}) },
      highlighter ? { hl: true } : {})
    pauseFit()
  }
  const submitPending = () => {
    const item = pending
    const text = pendingText.trim()
    setPending(null); setPendingText('')
    if (!item || !text) return
    const probe = frameFor({ x: item.bx, y: item.by, w: 1, h: 1 })
    const src = probe.src
    let x = item.bx, y = item.by
    if (item.kind === 'box') { x -= BOX_W / 2; y -= BOX_H / 2 }
    else if (item.kind === 'sticky') { x -= Math.max(180, 0.28 * src.w) / 2; y -= Math.max(80, 0.18 * src.h) / 2 }
    else y -= 24
    const [gx, gy] = toGrid(x, y, src)
    const frame = probe.id ? { frame: probe.id } : {}
    const id = newId()
    if (item.kind === 'sticky') sendDraw({ op: 'add_sticky', id, text, color: 'yellow', x: gx, y: gy, ...frame })
    else if (item.kind === 'text') sendDraw({ op: 'add_text', id, text, size: 'm', x: gx, y: gy, ...frame })
    else sendDraw({ op: 'add_box', id, label: text, kind: 'service', x: gx, y: gy, ...frame })
    pauseFit()
  }
  const targetOf = (element: EventTarget | null) => {
    const node = element instanceof Element ? element.closest('[data-id]') : null
    return node ? { id: node.getAttribute('data-id') ?? '', kind: node.getAttribute('data-testid') ?? '' } : null
  }
  const onBoardClick = (event: React.MouseEvent<SVGSVGElement>) => {
    if (tool === 'sticky' || tool === 'text' || tool === 'box') {
      const point = toBoard(event.clientX, event.clientY)
      const wrap = wrapRef.current?.getBoundingClientRect()
      if (!point || !wrap) return
      pauseFit()
      setPendingText('')
      setPending({ kind: tool, sx: event.clientX - wrap.left, sy: event.clientY - wrap.top, bx: point[0], by: point[1] })
      return
    }
    const target = targetOf(event.target)
    if (tool === 'eraser') {
      if (!target || !target.id || target.kind === 'native-frame') return
      pauseFit()
      sendDraw({ op: 'remove', id: target.id })
      return
    }
    if (tool === 'arrow') {
      if (!target || target.kind !== 'native-box') { setArrowFrom(null); return }
      pauseFit()
      if (!arrowFrom || arrowFrom === target.id) { setArrowFrom(target.id); return }
      sendDraw({ op: 'connect', id: newId(), from_id: arrowFrom, to_id: target.id })
      setArrowFrom(null)
    }
  }
  const hoverShape = hover ? board.shapes.get(hover) : undefined
  const hoverRect = hoverShape && isHuman(hoverShape) ? shapeRects.get(hoverShape.id) : undefined
  const unitsPerPx = (() => {
    const box = svgRef.current?.getBoundingClientRect()
    return box && box.width > 0 ? Math.max(vb.w / box.width, vb.h / box.height) : 1.5
  })()
  const arrowRect = arrowFrom ? shapeRects.get(arrowFrom) : undefined
  const cursor = tool === 'select' ? 'grab' : tool === 'pen' || tool === 'highlighter' ? 'crosshair' : tool === 'text' ? 'text' : tool === 'eraser' ? 'not-allowed' : 'copy'

  const frameShapes = [...board.shapes.values()].filter(shape => shape.op === 'frame')
  const otherShapes = [...board.shapes.values()].filter(shape => shape.op !== 'frame')
  const renderShape = (shape: Shape) => {
    const rect = shapeRects.get(shape.id)
    return rect && <NativeShape key={shape.id} shape={shape} rect={rect} highlighted={board.highlighted.has(shape.id)} report={report} source={frameSource(board, frames, shape)} />
  }
  return <div ref={wrapRef} style={{ position: 'relative', width: '100%', height: '100%' }}>
    <svg ref={svgRef} data-testid="native-board" data-tool={tool} role="img" aria-label="Live meeting whiteboard" viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`}
      preserveAspectRatio="xMidYMid meet" fontFamily={FONT}
      onPointerDown={event => {
        if (event.button !== 0) return
        if (tool === 'pen' || tool === 'highlighter') {
          const point = toBoard(event.clientX, event.clientY)
          if (!point) return
          pauseFit()
          drawing.current = true
          stroke.current = [point]
          setLive([point])
          event.currentTarget.setPointerCapture(event.pointerId)
          return
        }
        if (tool !== 'select') return
        const box = event.currentTarget.getBoundingClientRect()
        drag.current = { x: event.clientX, y: event.clientY, view: vb, scale: Math.max(vb.w / box.width, vb.h / box.height) }
        event.currentTarget.setPointerCapture(event.pointerId)
      }}
      onPointerMove={event => {
        if (drawing.current) {
          const point = toBoard(event.clientX, event.clientY)
          const last = stroke.current[stroke.current.length - 1]
          if (!point || (last && Math.hypot(point[0] - last[0], point[1] - last[1]) < unitsPerPx * 2)) return
          stroke.current = [...stroke.current, point]
          setLive(stroke.current)
          return
        }
        const start = drag.current
        if (!start) return
        setView({ ...start.view, x: start.view.x - (event.clientX - start.x) * start.scale, y: start.view.y - (event.clientY - start.y) * start.scale })
      }}
      onPointerUp={() => { drag.current = null; if (drawing.current) finishStroke() }}
      onPointerCancel={() => { drag.current = null; if (drawing.current) finishStroke() }}
      onPointerOver={event => { const target = targetOf(event.target); setHover(target?.id ?? null) }}
      onPointerLeave={() => setHover(null)}
      onClick={onBoardClick}
      onDoubleClick={() => { if (tool === 'select') setView(null) }}
      style={{ width: '100%', height: '100%', display: 'block', background: '#fcfcfd', borderRadius: 16, touchAction: 'none', cursor: tool === 'select' && drag.current ? 'grabbing' : cursor, userSelect: 'none' }}>
      <defs>
        <pattern id="native-dots" width="32" height="32" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1.4" fill="#e3e6eb" /></pattern>
        <marker id="native-arrowhead" viewBox="0 0 12 12" markerWidth="22" markerHeight="22" refX="9" refY="6" orient="auto-start-reverse" markerUnits="userSpaceOnUse">
          <path d="M1.5 1.5 L10.5 6 L1.5 10.5 Q3.5 6 1.5 1.5z" fill="#8a95a6" />
        </marker>
        <marker id="native-arrowhead-hi" viewBox="0 0 12 12" markerWidth="24" markerHeight="24" refX="9" refY="6" orient="auto-start-reverse" markerUnits="userSpaceOnUse">
          <path d="M1.5 1.5 L10.5 6 L1.5 10.5 Q3.5 6 1.5 1.5z" fill={ACCENT} />
        </marker>
        <filter id="native-shadow" x="-10%" y="-10%" width="120%" height="140%">
          <feDropShadow dx="0" dy="3" stdDeviation="6" floodColor="#1b2a41" floodOpacity="0.09" />
        </filter>
        <filter id="native-glow" x="-20%" y="-20%" width="140%" height="140%">
          <feDropShadow dx="0" dy="0" stdDeviation="7" floodColor={ACCENT} floodOpacity="0.55" />
        </filter>
      </defs>
      <rect x={vb.x - vb.w * 4} y={vb.y - vb.h * 4} width={vb.w * 9} height={vb.h * 9} fill="url(#native-dots)" pointerEvents="none" />
      {frameShapes.map(renderShape)}
      {[...board.edges.values()].map(edge => {
        const from = shapeRects.get(string(edge.from_id)); const to = shapeRects.get(string(edge.to_id))
        if (!from || !to) return null
        return <Edge key={edge.id} edge={edge} from={from} to={to} highlighted={board.highlighted.has(edge.id)} />
      })}
      {otherShapes.map(renderShape)}
      {arrowRect && <rect x={arrowRect.x - 8} y={arrowRect.y - 8} width={arrowRect.w + 16} height={arrowRect.h + 16} rx="18"
        fill="none" stroke="#f76b15" strokeWidth={3} strokeDasharray="10 7" pointerEvents="none" />}
      {live && <path data-testid="native-live-stroke" d={smoothPath(live)} fill="none" pointerEvents="none"
        stroke={tool === 'highlighter' ? sketchColors.orange : sketchColors.blue} strokeWidth={tool === 'highlighter' ? 16 : 4}
        strokeOpacity={tool === 'highlighter' ? 0.4 : 0.85} strokeLinecap="round" strokeLinejoin="round" />}
      {hoverRect && hoverShape && (() => {
        const size = 12 * unitsPerPx
        const label = string(hoverShape.by)
        const w = textWidth(label, size) + size * 1.4
        return <g data-testid="native-author-tag" pointerEvents="none">
          <rect x={hoverRect.x} y={hoverRect.y - size * 2.1} width={w} height={size * 1.7} rx={size * 0.85} fill="#f76b15" />
          <text x={hoverRect.x + size * 0.7} y={hoverRect.y - size * 0.82} fontSize={size} fontWeight="600" fill="#fff" fontFamily={FONT}>{label}</text>
        </g>
      })()}
    </svg>
    {!view3d && <AnnotateToolbar tool={tool} onTool={setTool} />}
    {pending && <input data-testid="board-inline-input" autoFocus value={pendingText}
      placeholder={pending.kind === 'box' ? 'Box label, Enter' : pending.kind === 'sticky' ? 'Sticky note, Enter' : 'Text, Enter'}
      onChange={event => { setPendingText(event.target.value); pauseFit() }}
      onKeyDown={event => {
        if (event.key === 'Enter') { event.preventDefault(); submitPending() }
        else if (event.key === 'Escape') { setPending(null); setPendingText('') }
      }}
      onBlur={() => { setPending(null); setPendingText('') }}
      style={{ position: 'absolute', left: pending.sx, top: pending.sy - 16, zIndex: 450, width: 200, height: 32, padding: '0 10px',
        borderRadius: 8, border: `1.5px solid ${pending.kind === 'sticky' ? '#e2b203' : ACCENT}`,
        background: pending.kind === 'sticky' ? '#fff4bf' : '#fff', color: '#1d2430', font: `500 14px ${FONT}`,
        boxShadow: '0 4px 16px rgba(0,0,0,0.14)', outline: 'none' }} />}
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
void MONO
