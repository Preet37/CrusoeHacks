// Live whiteboard: tldraw editor that applies server draw ops on a 0-1000 grid.
// Handles boxes, arrows, rename, remove, highlight, clear, frames, focus and board_snapshot here.
// Every other op goes to renderRichOp() from ./board/index.ts (optional module, rich shapes).
import { useEffect, useRef, useState } from 'react'
import {
  Tldraw,
  createShapeId,
  toRichText,
  type Editor,
  type TLAnyShapeUtilConstructor,
  type TLShapeId,
  type TLShapePartial,
} from 'tldraw'
import 'tldraw/tldraw.css'
import type { BoxKind } from '../lib/protocol'
import type { AnyServerMessage, TwinClient } from '../lib/ws'

// Same shape as RichCtx in ./board/types.ts (kept local so a missing board/ never breaks the build).
export interface RichCtx {
  gridToPage(x: number, y: number, frame?: string): { x: number; y: number }
  sizeToPage(w: number, h: number, frame?: string): { w: number; h: number }
  shapeIdFor(id: string): TLShapeId | undefined
  register(id: string, shapeId: TLShapeId): void
  unregister(id: string): void
  frameShapeId(frameId: string): TLShapeId | undefined
  send(msg: { type: string; [k: string]: unknown }): void
}

type Op = Record<string, unknown> & { op: string }
type RichRenderer = (editor: Editor, op: Op, ctx: RichCtx) => boolean | Promise<boolean>

// Optional rich-shape plug-in. Eager so its shapeUtils are known before <Tldraw> mounts.
const richModules = import.meta.glob('./board/index.ts', { eager: true }) as Record<
  string,
  { renderRichOp?: RichRenderer; richShapeUtils?: readonly TLAnyShapeUtilConstructor[] }
>
const richMod = richModules['./board/index.ts']
const renderRichOp: RichRenderer | undefined = richMod?.renderRichOp
const richShapeUtils: readonly TLAnyShapeUtilConstructor[] = richMod?.richShapeUtils ?? []

/** Page area for the whole 0-1000 grid (no frame). */
const PAGE_W = 2000
const PAGE_H = 1400
const BOX_W = 200
const BOX_H = 90
const FRAME_GAP = 200
const APPEAR_MS = 260
const OP_GAP_MS = 120
const FIT_DEBOUNCE_MS = 300
const USER_IDLE_MS = 5000

interface KindStyle {
  geo: string
  color: string
  fill: string
  dash: string
}
const KIND_STYLE: Record<BoxKind, KindStyle> = {
  service: { geo: 'rectangle', color: 'blue', fill: 'semi', dash: 'solid' },
  datastore: { geo: 'ellipse', color: 'green', fill: 'semi', dash: 'solid' },
  client: { geo: 'oval', color: 'violet', fill: 'semi', dash: 'solid' },
  external: { geo: 'cloud', color: 'grey', fill: 'none', dash: 'dashed' },
  note: { geo: 'rectangle', color: 'yellow', fill: 'solid', dash: 'draw' },
}

interface BoxRec {
  shapeId: TLShapeId
  kind: BoxKind
}
interface EdgeRec {
  shapeId: TLShapeId
  from: string
  to: string
}
interface FrameRec {
  shapeId: TLShapeId
  x: number
  y: number
  w: number
  h: number
}

const num = (v: unknown, d: number) => (typeof v === 'number' && Number.isFinite(v) ? v : d)
const clampGrid = (v: number) => Math.max(0, Math.min(1000, v))

class BoardController {
  boxes = new Map<string, BoxRec>()
  edges = new Map<string, EdgeRec>()
  frames = new Map<string, FrameRec>()
  /** Every protocol id -> tldraw shape id (boxes, edges, frames, rich shapes). */
  ids = new Map<string, TLShapeId>()
  activeFrame: string | undefined
  private focusTarget: string | undefined
  private queue: Op[] = []
  private draining = false
  private drainTimer: number | undefined
  private fitTimer: number | undefined
  private lastUserInput = 0
  private hadContent = false
  private offEvent: () => void
  private editor: Editor
  private send: (m: { type: string; [k: string]: unknown }) => void
  private onContent: (has: boolean) => void
  private disposed = false

  constructor(
    editor: Editor,
    send: (m: { type: string; [k: string]: unknown }) => void,
    onContent: (has: boolean) => void,
  ) {
    this.editor = editor
    this.send = send
    this.onContent = onContent
    const onEvent = (info: { type: string; name: string }) => {
      if (info.type === 'wheel' || info.type === 'pinch' || (info.type === 'pointer' && info.name === 'pointer_down'))
        this.lastUserInput = Date.now()
    }
    editor.on('event', onEvent)
    this.offEvent = () => editor.off('event', onEvent)
  }

  // ------------------------------------------------------------- geometry

  private frameOf(frame?: string): FrameRec | undefined {
    if (frame && this.frames.has(frame)) return this.frames.get(frame)
    return this.activeFrame ? this.frames.get(this.activeFrame) : undefined
  }

  gridToPage(x: number, y: number, frame?: string) {
    const f = this.frameOf(frame)
    const gx = clampGrid(x) / 1000
    const gy = clampGrid(y) / 1000
    if (f) return { x: f.x + gx * f.w, y: f.y + gy * f.h }
    return { x: gx * PAGE_W, y: gy * PAGE_H }
  }

  sizeToPage(w: number, h: number, frame?: string) {
    const f = this.frameOf(frame)
    const sw = f ? f.w : PAGE_W
    const sh = f ? f.h : PAGE_H
    return { w: (Math.max(1, w) / 1000) * sw, h: (Math.max(1, h) / 1000) * sh }
  }

  ctx(): RichCtx {
    return {
      gridToPage: (x, y, f) => this.gridToPage(x, y, f),
      sizeToPage: (w, h, f) => this.sizeToPage(w, h, f),
      shapeIdFor: (id) => this.ids.get(id),
      register: (id, shapeId) => {
        const isNew = this.ids.get(id) !== shapeId
        this.ids.set(id, shapeId)
        if (isNew) this.fadeIn(shapeId)
      },
      unregister: (id) => {
        this.ids.delete(id)
      },
      frameShapeId: (f) => this.frames.get(f)?.shapeId,
      send: (m) => this.send(m),
    }
  }

  // ------------------------------------------------------------- queueing (RX-9)

  enqueue(op: Op) {
    this.queue.push(op)
    if (!this.draining) this.drain()
  }

  private drain() {
    if (this.disposed) return
    const op = this.queue.shift()
    if (!op) {
      this.draining = false
      return
    }
    this.draining = true
    const done = () => {
      this.afterChange()
      this.drainTimer = window.setTimeout(() => this.drain(), OP_GAP_MS)
    }
    try {
      const r = this.applyOp(op, true)
      if (r && typeof (r as Promise<unknown>).then === 'function') (r as Promise<unknown>).then(done, (e) => (console.warn('[whiteboard] op failed', op, e), done()))
      else done()
    } catch (e) {
      console.warn('[whiteboard] op failed', op, e)
      done()
    }
  }

  snapshot(msg: { boxes?: Op[]; edges?: Op[]; shapes?: Op[]; active_frame?: string }) {
    this.queue = []
    this.clearAll()
    const shapes = msg.shapes ?? []
    // Frames first so boxes/rich shapes can be positioned inside them.
    for (const s of shapes) if (s.op === 'frame') this.safe(() => this.applyOp(s, false))
    for (const b of msg.boxes ?? []) this.safe(() => this.addBox({ ...b, op: 'add_box' }, false))
    for (const e of msg.edges ?? []) this.safe(() => this.connect({ ...e, op: 'connect' }))
    for (const s of shapes) if (s.op !== 'frame' && s.op !== 'focus') this.safe(() => this.applyOp(s, false))
    if (msg.active_frame && this.frames.has(msg.active_frame)) {
      this.activeFrame = msg.active_frame
      this.focusTarget = msg.active_frame
    }
    this.afterChange(true)
  }

  private safe(fn: () => unknown) {
    try {
      const r = fn()
      if (r && typeof (r as Promise<unknown>).then === 'function') (r as Promise<unknown>).catch((e) => console.warn('[whiteboard]', e))
    } catch (e) {
      console.warn('[whiteboard] snapshot op failed', e)
    }
  }

  private applyOp(op: Op, animate: boolean): unknown {
    switch (op.op) {
      case 'add_box':
        return this.addBox(op, animate)
      case 'connect':
        return this.connect(op)
      case 'rename':
        return this.rename(String(op.id), String(op.label ?? op.text ?? ''))
      case 'remove':
        return this.remove(String(op.id))
      case 'highlight':
        return this.highlight(Array.isArray(op.ids) ? (op.ids as string[]) : [String(op.id)], op.on !== false)
      case 'clear':
        return this.clearAll()
      case 'frame':
        return this.frame(op)
      case 'focus':
        return this.focus(String(op.id))
      default:
        return this.rich(op)
    }
  }

  private async rich(op: Op) {
    if (!renderRichOp) {
      console.info('[whiteboard] no board/ module yet, skipping op', op.op)
      return
    }
    try {
      const handled = await renderRichOp(this.editor, op, this.ctx())
      if (!handled) console.info('[whiteboard] unhandled op', op.op)
    } catch (e) {
      this.send({ type: 'draw_error', id: String(op.id ?? ''), op: op.op, error: String(e).slice(0, 300) })
    }
  }

  // ------------------------------------------------------------- core ops

  private addBox(op: Op, animate: boolean) {
    const ed = this.editor
    const id = String(op.id)
    const kind = (typeof op.kind === "string" && op.kind in KIND_STYLE ? op.kind : "service") as BoxKind
    const style = KIND_STYLE[kind]
    const label = String(op.label ?? id)
    const frame = typeof op.frame === 'string' ? op.frame : undefined
    const p = this.gridToPage(num(op.x, 500), num(op.y, 500), frame)
    const props = {
      geo: style.geo,
      color: style.color,
      fill: style.fill,
      dash: style.dash,
      w: BOX_W,
      h: BOX_H,
      size: 's',
      font: 'sans',
      richText: toRichText(label),
    }
    const existing = this.boxes.get(id)
    if (existing && ed.getShape(existing.shapeId)) {
      ed.updateShape({ id: existing.shapeId, type: 'geo', x: p.x, y: p.y, props } as TLShapePartial)
      existing.kind = kind
      return
    }
    const { x, y } = this.freeSpot(p.x, p.y)
    const shapeId = createShapeId()
    ed.createShape({ id: shapeId, type: 'geo', x, y, opacity: animate ? 0 : 1, props } as TLShapePartial)
    this.boxes.set(id, { shapeId, kind })
    this.ids.set(id, shapeId)
    if (animate) this.fadeIn(shapeId)
  }

  /** Nudge a new box right/down until it doesn't overlap an existing box (RX-4). */
  private freeSpot(x: number, y: number) {
    const pad = 24
    const rects = [...this.boxes.values()]
      .map((b) => this.editor.getShapePageBounds(b.shapeId))
      .filter((b): b is NonNullable<typeof b> => !!b)
    const hit = (cx: number, cy: number) =>
      rects.some((r) => cx < r.maxX + pad && cx + BOX_W + pad > r.minX && cy < r.maxY + pad && cy + BOX_H + pad > r.minY)
    let cx = x
    let cy = y
    for (let i = 0; i < 8 && hit(cx, cy); i++) {
      if (i % 2 === 0) cx += BOX_W * 0.6
      else cy += BOX_H * 0.9
    }
    return { x: cx, y: cy }
  }

  private connect(op: Op) {
    const ed = this.editor
    const from = this.boxes.get(String(op.from_id))?.shapeId ?? this.ids.get(String(op.from_id))
    const to = this.boxes.get(String(op.to_id))?.shapeId ?? this.ids.get(String(op.to_id))
    if (!from || !to || !ed.getShape(from) || !ed.getShape(to)) {
      console.warn('[whiteboard] connect with unknown shape', op)
      return
    }
    const id = String(op.id ?? `e_${op.from_id}_${op.to_id}`)
    const label = String(op.label ?? '')
    const existing = this.edges.get(id)
    if (existing && ed.getShape(existing.shapeId)) {
      ed.updateShape({ id: existing.shapeId, type: 'arrow', props: { richText: toRichText(label) } } as TLShapePartial)
      return
    }
    const a = ed.getShapePageBounds(from)
    const b = ed.getShapePageBounds(to)
    if (!a || !b) return
    const shapeId = createShapeId()
    ed.createShape({
      id: shapeId,
      type: 'arrow',
      x: a.center.x,
      y: a.center.y,
      opacity: 0,
      props: {
        start: { x: 0, y: 0 },
        end: { x: b.center.x - a.center.x, y: b.center.y - a.center.y },
        richText: toRichText(label),
        color: 'grey',
        size: 's',
        font: 'sans',
      },
    } as TLShapePartial)
    ed.createBindings([bind(shapeId, from, 'start'), bind(shapeId, to, 'end')])
    this.edges.set(id, { shapeId, from: String(op.from_id), to: String(op.to_id) })
    this.ids.set(id, shapeId)
    this.fadeIn(shapeId)
  }

  private rename(id: string, label: string) {
    const ed = this.editor
    const fr = this.frames.get(id)
    if (fr) return void ed.updateShape({ id: fr.shapeId, type: 'frame', props: { name: label } } as TLShapePartial)
    const sid = this.ids.get(id)
    const shape = sid && ed.getShape(sid)
    if (shape && 'richText' in (shape.props as object))
      ed.updateShape({ id: shape.id, type: shape.type, props: { richText: toRichText(label) } } as TLShapePartial)
  }

  private remove(id: string) {
    const ed = this.editor
    const del: TLShapeId[] = []
    const sid = this.ids.get(id)
    if (sid) del.push(sid)
    if (this.boxes.has(id) || sid) {
      for (const [eid, e] of this.edges) {
        if (e.from === id || e.to === id) {
          del.push(e.shapeId)
          this.edges.delete(eid)
          this.ids.delete(eid)
        }
      }
    }
    this.boxes.delete(id)
    this.edges.delete(id)
    if (this.frames.has(id)) {
      this.frames.delete(id)
      if (this.activeFrame === id) this.activeFrame = undefined
      if (this.focusTarget === id) this.focusTarget = undefined
    }
    this.ids.delete(id)
    const existing = del.filter((s) => ed.getShape(s))
    if (existing.length) ed.deleteShapes(existing)
  }

  private highlight(ids: string[], on: boolean) {
    const ed = this.editor
    const updates: TLShapePartial[] = []
    for (const id of ids) {
      const box = this.boxes.get(id)
      if (box) {
        const base = KIND_STYLE[box.kind]
        updates.push({
          id: box.shapeId,
          type: 'geo',
          props: on ? { color: 'red', dash: 'solid', size: 'l' } : { color: base.color, dash: base.dash, size: 's' },
        } as TLShapePartial)
        continue
      }
      const edge = this.edges.get(id)
      if (edge) {
        updates.push({ id: edge.shapeId, type: 'arrow', props: { color: on ? 'red' : 'grey', size: on ? 'l' : 's' } } as TLShapePartial)
        continue
      }
      const sid = this.ids.get(id)
      const shape = sid && ed.getShape(sid)
      if (!shape) continue
      const meta = (shape.meta ?? {}) as Record<string, unknown>
      const props = shape.props as Record<string, unknown>
      if ('color' in props) {
        if (on) {
          const orig = typeof meta.twinColor === 'string' ? meta.twinColor : props.color
          updates.push({ id: shape.id, type: shape.type, meta: { ...meta, twinColor: orig }, props: { color: 'red' } } as TLShapePartial)
        } else if (typeof meta.twinColor === 'string') {
          updates.push({ id: shape.id, type: shape.type, props: { color: meta.twinColor } } as TLShapePartial)
        }
      } else {
        updates.push({ id: shape.id, type: shape.type, opacity: on ? 1 : shape.opacity } as TLShapePartial)
      }
    }
    if (updates.length) ed.updateShapes(updates)
  }

  private frame(op: Op) {
    const ed = this.editor
    const id = String(op.id)
    const title = String(op.title ?? op.label ?? id)
    const existing = this.frames.get(id)
    if (existing && ed.getShape(existing.shapeId)) {
      ed.updateShape({ id: existing.shapeId, type: 'frame', props: { name: title } } as TLShapePartial)
      this.activeFrame = id
      this.focusTarget = id
      return
    }
    // Grid coords for the frame map onto the global page area...
    let x = (clampGrid(num(op.x, 0)) / 1000) * PAGE_W
    const y = (clampGrid(num(op.y, 0)) / 1000) * PAGE_H
    const w = (Math.max(50, Math.min(1000, num(op.w, 1000))) / 1000) * PAGE_W
    const h = (Math.max(50, Math.min(1000, num(op.h, 1000))) / 1000) * PAGE_H
    // ...but topics are separate areas: shift right past any frame it would overlap.
    for (let i = 0; i < 50; i++) {
      const hit = [...this.frames.values()].find((f) => x < f.x + f.w + FRAME_GAP && x + w + FRAME_GAP > f.x && y < f.y + f.h && y + h > f.y)
      if (!hit) break
      x = hit.x + hit.w + FRAME_GAP
    }
    const shapeId = createShapeId()
    ed.createShape({ id: shapeId, type: 'frame', x, y, props: { w, h, name: title } } as TLShapePartial)
    ed.sendToBack([shapeId])
    this.frames.set(id, { shapeId, x, y, w, h })
    this.ids.set(id, shapeId)
    this.activeFrame = id
    this.focusTarget = id
  }

  private focus(id: string) {
    if (this.frames.has(id)) this.activeFrame = id
    if (!this.ids.has(id)) return
    this.focusTarget = id
    this.zoomNow(true)
  }

  clearAll() {
    const ed = this.editor
    const all = [...ed.getCurrentPageShapeIds()]
    if (all.length) ed.deleteShapes(all)
    this.boxes.clear()
    this.edges.clear()
    this.frames.clear()
    this.ids.clear()
    this.activeFrame = undefined
    this.focusTarget = undefined
  }

  // ------------------------------------------------------------- after-change: content flag + fit (RX-4)

  private afterChange(immediate = false) {
    const has = this.editor.getCurrentPageShapeIds().size > 0
    if (has !== this.hadContent) {
      this.hadContent = has
      this.onContent(has)
    }
    window.clearTimeout(this.fitTimer)
    this.fitTimer = window.setTimeout(() => this.zoomNow(false), immediate ? 30 : FIT_DEBOUNCE_MS)
  }

  private zoomNow(force: boolean) {
    if (this.disposed) return
    const ed = this.editor
    if (!force && Date.now() - this.lastUserInput < USER_IDLE_MS) return
    if (ed.getCurrentPageShapeIds().size === 0) return
    const animation = { duration: 500 }
    const target = this.focusTarget ? this.ids.get(this.focusTarget) : undefined
    const bounds = target && ed.getShape(target) ? ed.getShapePageBounds(target) : ed.getCurrentPageBounds()
    if (bounds) ed.zoomToBounds(bounds, { animation, inset: 72, targetZoom: 1 })
  }

  // ------------------------------------------------------------- animation (RX-9)

  private fadeIn(id: TLShapeId) {
    const ed = this.editor
    const shape = ed.getShape(id)
    if (!shape) return
    const target = shape.opacity > 0 ? shape.opacity : 1
    ed.run(() => ed.updateShape({ id, type: shape.type, opacity: 0 } as TLShapePartial), { history: 'ignore' })
    const start = performance.now()
    const step = () => {
      const s = ed.getShape(id)
      if (!s || this.disposed) return
      const k = Math.max(0, Math.min(1, (performance.now() - start) / APPEAR_MS))
      ed.run(() => ed.updateShape({ id, type: s.type, opacity: target * k } as TLShapePartial), { history: 'ignore' })
      if (k < 1) requestAnimationFrame(step)
    }
    requestAnimationFrame(step)
  }

  dispose() {
    this.disposed = true
    window.clearTimeout(this.fitTimer)
    window.clearTimeout(this.drainTimer)
    this.offEvent()
  }
}

function bind(arrow: TLShapeId, target: TLShapeId, terminal: 'start' | 'end') {
  return {
    type: 'arrow' as const,
    fromId: arrow,
    toId: target,
    props: { terminal, normalizedAnchor: { x: 0.5, y: 0.5 }, isExact: false, isPrecise: false, snap: 'none' as const },
  }
}

export default function Whiteboard({ client, onHasContent }: { client: TwinClient; onHasContent?: (has: boolean) => void }) {
  const [editor, setEditor] = useState<Editor | null>(null)
  const onContentRef = useRef(onHasContent)
  useEffect(() => {
    onContentRef.current = onHasContent
  }, [onHasContent])

  // Subscribe immediately (before the editor mounts) and buffer messages until it does.
  const pendingRef = useRef<AnyServerMessage[]>([])
  const boardRef = useRef<BoardController | null>(null)

  useEffect(() => {
    const handle = (m: AnyServerMessage) => {
      const board = boardRef.current
      if (!board) return void pendingRef.current.push(m)
      if (m.type === 'draw') board.enqueue(m as unknown as Op)
      else if (m.type === 'board_snapshot') board.snapshot(m as unknown as Parameters<BoardController['snapshot']>[0])
    }
    const offDraw = client.on('draw', (m) => handle(m))
    const offSnap = client.on('board_snapshot', (m) => handle(m))
    return () => {
      offDraw()
      offSnap()
    }
  }, [client])

  useEffect(() => {
    if (!editor) return
    const board = new BoardController(editor, (m) => client.send(m), (has) => onContentRef.current?.(has))
    boardRef.current = board
    const pending = pendingRef.current.splice(0)
    // Only the latest snapshot matters; replay draws that came after it.
    let start = 0
    pending.forEach((m, i) => m.type === 'board_snapshot' && (start = i))
    for (const m of pending.slice(start)) {
      if (m.type === 'draw') board.enqueue(m as unknown as Op)
      else if (m.type === 'board_snapshot') board.snapshot(m as unknown as Parameters<BoardController['snapshot']>[0])
    }
    ;(window as unknown as { twinBoard?: BoardController }).twinBoard = board
    return () => {
      board.dispose()
      if (boardRef.current === board) boardRef.current = null
    }
  }, [editor, client])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
      <div style={{ position: 'absolute', inset: 0 }}>
        <Tldraw shapeUtils={richShapeUtils} onMount={(ed) => setEditor(ed)} />
      </div>
    </div>
  )
}
