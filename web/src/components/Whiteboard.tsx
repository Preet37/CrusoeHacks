// Live whiteboard: tldraw editor that applies server draw ops on a 0-1000 grid.
// Handles boxes, arrows, rename, remove, highlight, clear, frames, focus and board_snapshot here.
// Every other op goes to renderRichOp() from ./board/index.ts (optional module, rich shapes).
import { useEffect, useRef, useState } from 'react'
import {
  Tldraw,
  createShapeId,
  toRichText,
  type Editor,
  type TLRecord,
  type TLShape,
  type TLAnyShapeUtilConstructor,
  type TLShapeId,
  type TLShapePartial,
} from 'tldraw'
import 'tldraw/tldraw.css'
import type { BoxKind } from '../lib/protocol'
import type { AnyServerMessage, TwinClient } from '../lib/ws'
import Board3D from './Board3D'

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
const BOX_W = 240
const BOX_H = 110
const FRAME_GAP = 200
const APPEAR_MS = 260
const OP_GAP_MS = 120
const FIT_DEBOUNCE_MS = 300
const USER_IDLE_MS = 5000
/** v2.2: debounce for human edits (create settles, label typing) before sending ops. */
const HUMAN_DEBOUNCE_MS = 500
const HUMAN_TYPES = new Set(['geo', 'note', 'text', 'arrow'])
const STICKY_COLORS = new Set(['yellow', 'blue', 'green', 'pink', 'orange'])

/** Plain text of a tldraw richText doc. */
function plainText(n: unknown): string {
  if (!n || typeof n !== 'object') return ''
  const o = n as { text?: string; content?: unknown[] }
  return (o.text ?? '') + (o.content ?? []).map(plainText).join(' ')
}

interface KindStyle {
  geo: string
  color: string
  fill: string
  dash: string
}
const KIND_STYLE: Record<BoxKind, KindStyle> = {
  service: { geo: 'rectangle', color: 'blue', fill: 'solid', dash: 'solid' },
  datastore: { geo: 'ellipse', color: 'green', fill: 'solid', dash: 'solid' },
  client: { geo: 'oval', color: 'violet', fill: 'solid', dash: 'solid' },
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
  // v2.2 human drawing
  private user: string
  private humanSeq = 0
  /** protocol id -> last label we know the server has (so remote renames don't echo). */
  private labels = new Map<string, string>()
  /** User-made shapes not yet sent to the server. */
  private humanShapes = new Set<TLShapeId>()
  private createTimers = new Map<TLShapeId, number>()
  private renameTimers = new Map<string, number>()
  private richInFlight = 0
  private offStore: () => void

  constructor(
    editor: Editor,
    send: (m: { type: string; [k: string]: unknown }) => void,
    onContent: (has: boolean) => void,
    user = 'guest',
  ) {
    this.editor = editor
    this.send = send
    this.onContent = onContent
    this.user = user
    this.offStore = editor.store.listen((entry) => this.onUserChanges(entry.changes), { source: 'user', scope: 'document' })
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
    this.richInFlight++
    try {
      const handled = await renderRichOp(this.editor, op, this.ctx())
      if (!handled) console.info('[whiteboard] unhandled op', op.op)
    } catch (e) {
      this.send({ type: 'draw_error', id: String(op.id ?? ''), op: op.op, error: String(e).slice(0, 300) })
    } finally {
      // store listeners flush on the next frame; keep ignoring unknown creates until then
      window.setTimeout(() => this.richInFlight--, 150)
    }
  }

  // ------------------------------------------------------------- core ops

  private addBox(op: Op, animate: boolean) {
    const ed = this.editor
    const id = String(op.id)
    const kind = (typeof op.kind === "string" && op.kind in KIND_STYLE ? op.kind : "service") as BoxKind
    const byHuman = typeof op.by === 'string' && op.by !== 'Twin'
    const style = byHuman ? { ...KIND_STYLE[kind], color: 'orange' } : KIND_STYLE[kind]
    const label = String(op.label ?? id)
    this.labels.set(id, label)
    const frame = typeof op.frame === 'string' ? op.frame : undefined
    const p = this.gridToPage(num(op.x, 500), num(op.y, 500), frame)
    const props = {
      geo: style.geo,
      color: style.color,
      fill: style.fill,
      dash: style.dash,
      w: BOX_W,
      h: BOX_H,
      size: 'm',
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
        size: 'm',
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
    this.labels.set(id, label)
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
          props: on ? { color: 'red', dash: 'solid', size: 'xl' } : { color: base.color, dash: base.dash, size: 'm' },
        } as TLShapePartial)
        continue
      }
      const edge = this.edges.get(id)
      if (edge) {
        updates.push({ id: edge.shapeId, type: 'arrow', props: { color: on ? 'red' : 'grey', size: on ? 'xl' : 'm' } } as TLShapePartial)
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
    // Layer frames live within their topic; only independent topics avoid overlaps.
    const parent = typeof op.frame === 'string' ? this.frames.get(op.frame) : undefined
    // Grid coords for top-level frames map onto the global page area...
    let x = (clampGrid(num(op.x, 0)) / 1000) * PAGE_W
    let y = (clampGrid(num(op.y, 0)) / 1000) * PAGE_H
    let w = (Math.max(50, Math.min(1000, num(op.w, 1000))) / 1000) * PAGE_W
    let h = (Math.max(50, Math.min(1000, num(op.h, 1000))) / 1000) * PAGE_H
    // ...but topics are separate areas: shift right past any frame it would overlap.
    if (parent) {
      x = parent.x + x / PAGE_W * parent.w
      y = parent.y + y / PAGE_H * parent.h
      w = w / PAGE_W * parent.w
      h = h / PAGE_H * parent.h
    }
    for (let i = 0; !parent && i < 50; i++) {
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
    this.labels.clear()
    this.humanShapes.clear()
    this.activeFrame = undefined
    this.focusTarget = undefined
  }

  // ------------------------------------------------------------- v2.2: human edits -> draw ops

  private protoIdOf(shapeId: TLShapeId): string | undefined {
    for (const [id, sid] of this.ids) if (sid === shapeId) return id
    return undefined
  }

  private newHumanId() {
    const slug = this.user.toLowerCase().replace(/[^a-z0-9]+/g, '').slice(0, 16) || 'guest'
    return `h_${slug}_${Date.now().toString(36)}${++this.humanSeq}`
  }

  /** Inverse of gridToPage: page point -> 0..1000 grid, inside a frame when it lands in one. */
  private pageToGrid(px: number, py: number): { x: number; y: number; frame?: string } {
    const r = (v: number) => Math.round(clampGrid(v))
    const inside = (f: FrameRec) => px >= f.x && px <= f.x + f.w && py >= f.y && py <= f.y + f.h
    let frameId: string | undefined
    const active = this.activeFrame ? this.frames.get(this.activeFrame) : undefined
    if (active && inside(active)) frameId = this.activeFrame
    else for (const [fid, f] of this.frames) if (inside(f)) (frameId = fid)
    // gridToPage() without a frame uses the active frame, so stay in it (clamped) when outside every frame.
    frameId = frameId ?? (active ? this.activeFrame : undefined)
    const f = frameId ? this.frames.get(frameId) : undefined
    if (f && frameId) return { x: r(((px - f.x) / f.w) * 1000), y: r(((py - f.y) / f.h) * 1000), frame: frameId }
    return { x: r((px / PAGE_W) * 1000), y: r((py / PAGE_H) * 1000) }
  }

  private sendHuman(op: Op) {
    this.send({ type: 'draw', ...op, by: this.user })
  }

  private onUserChanges(changes: {
    added: Record<string, TLRecord>
    updated: Record<string, [TLRecord, TLRecord]>
    removed: Record<string, TLRecord>
  }) {
    if (this.disposed) return
    for (const rec of Object.values(changes.added)) {
      if (rec.typeName !== 'shape') continue
      const shape = rec as TLShape
      if (!HUMAN_TYPES.has(shape.type) || this.protoIdOf(shape.id) || this.richInFlight > 0) continue
      this.humanShapes.add(shape.id)
      this.scheduleCreate(shape.id)
    }
    for (const [from, to] of Object.values(changes.updated)) {
      if (to.typeName !== 'shape') continue
      const shape = to as TLShape
      if (this.humanShapes.has(shape.id)) {
        this.scheduleCreate(shape.id)
        continue
      }
      const before = (from as TLShape).props as { richText?: unknown }
      const after = shape.props as { richText?: unknown }
      if (shape.type === 'arrow' || !('richText' in after)) continue
      if (JSON.stringify(before.richText) === JSON.stringify(after.richText)) continue
      const id = this.protoIdOf(shape.id)
      if (id) this.scheduleRename(id, shape.id)
    }
    for (const rec of Object.values(changes.removed)) {
      if (rec.typeName !== 'shape') continue
      const sid = rec.id as TLShapeId
      if (this.humanShapes.delete(sid)) {
        window.clearTimeout(this.createTimers.get(sid))
        this.createTimers.delete(sid)
        continue
      }
      const id = this.protoIdOf(sid)
      if (!id) continue
      this.sendHuman({ op: 'remove', id })
      this.forget(id)
    }
  }

  /** Drop a protocol id (and its incident edges) from the maps without touching the canvas. */
  private forget(id: string) {
    if (this.boxes.has(id)) {
      for (const [eid, e] of this.edges) {
        if (e.from === id || e.to === id) {
          this.edges.delete(eid)
          this.ids.delete(eid)
        }
      }
    }
    this.boxes.delete(id)
    this.edges.delete(id)
    if (this.frames.delete(id) && this.activeFrame === id) this.activeFrame = undefined
    if (this.focusTarget === id) this.focusTarget = undefined
    this.ids.delete(id)
    this.labels.delete(id)
    window.clearTimeout(this.renameTimers.get(id))
    this.renameTimers.delete(id)
  }

  private scheduleCreate(sid: TLShapeId) {
    window.clearTimeout(this.createTimers.get(sid))
    this.createTimers.set(sid, window.setTimeout(() => this.fireCreate(sid), HUMAN_DEBOUNCE_MS))
  }

  private scheduleRename(id: string, sid: TLShapeId) {
    window.clearTimeout(this.renameTimers.get(id))
    this.renameTimers.set(
      id,
      window.setTimeout(() => {
        this.renameTimers.delete(id)
        const shape = this.editor.getShape(sid)
        if (!shape || this.ids.get(id) !== sid) return
        if (this.editor.getEditingShapeId() === sid) return this.scheduleRename(id, sid)
        const label = plainText((shape.props as { richText?: unknown }).richText).trim()
        if (!label || label === this.labels.get(id)) return
        this.labels.set(id, label)
        this.sendHuman({ op: 'rename', id, label })
      }, HUMAN_DEBOUNCE_MS),
    )
  }

  private fireCreate(sid: TLShapeId) {
    this.createTimers.delete(sid)
    const ed = this.editor
    if (this.disposed || !this.humanShapes.has(sid)) return
    const shape = ed.getShape(sid)
    if (!shape) return void this.humanShapes.delete(sid)
    if (ed.getEditingShapeId() === sid) return this.scheduleCreate(sid)
    const props = shape.props as { richText?: unknown; color?: string }
    const text = plainText(props.richText).trim()
    const b = ed.getShapePageBounds(sid)
    if (!b) return
    const pos = this.pageToGrid(b.minX, b.minY)
    const where = { x: pos.x, y: pos.y, ...(pos.frame ? { frame: pos.frame } : {}) }
    const id = this.newHumanId()
    if (shape.type === 'geo') {
      const label = text || 'Box'
      this.sendHuman({ op: 'add_box', id, label, kind: 'service', ...where })
      this.boxes.set(id, { shapeId: sid, kind: 'service' })
      this.labels.set(id, label)
    } else if (shape.type === 'note' || shape.type === 'text') {
      if (!text) return // wait until it has text (a later edit reschedules)
      if (shape.type === 'note') {
        const color = props.color && STICKY_COLORS.has(props.color) ? props.color : 'yellow'
        this.sendHuman({ op: 'add_sticky', id, text, color, ...where })
      } else this.sendHuman({ op: 'add_text', id, text, ...where })
      this.labels.set(id, text)
    } else if (shape.type === 'arrow') {
      const binds = ed.getBindingsFromShape(sid, 'arrow') as { toId: TLShapeId; props: { terminal: string } }[]
      const end = (t: string) => {
        const target = binds.find((x) => x.props.terminal === t)?.toId
        const pid = target ? this.protoIdOf(target) : undefined
        return pid && this.boxes.has(pid) ? pid : undefined
      }
      const from = end('start')
      const to = end('end')
      if (!from || !to || from === to) return // not between two known boxes: stays local
      this.sendHuman({ op: 'connect', id, from_id: from, to_id: to })
      this.edges.set(id, { shapeId: sid, from, to })
    } else return
    this.humanShapes.delete(sid)
    this.ids.set(id, sid)
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
    for (const t of this.createTimers.values()) window.clearTimeout(t)
    for (const t of this.renameTimers.values()) window.clearTimeout(t)
    this.offStore()
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

/** Current boxes/edges (for the 3D view to seed from). */
export function currentArchitecture() {
  const board = (window as unknown as { twinBoard?: BoardController }).twinBoard
  const boxes: { id: string; label: string; kind: BoxKind; x: number; highlighted: boolean }[] = []
  const edges: { id: string; from: string; to: string; highlighted: boolean }[] = []
  if (!board) return { boxes, edges }
  const ed = (board as unknown as { editor: Editor }).editor
  const text = (n: unknown): string => {
    if (!n || typeof n !== 'object') return ''
    const o = n as { text?: string; content?: unknown[] }
    return (o.text ?? '') + (o.content ?? []).map(text).join(' ')
  }
  for (const [id, b] of board.boxes) {
    const sh = ed.getShape(b.shapeId)
    const props = (sh?.props ?? {}) as { richText?: unknown; color?: string }
    boxes.push({ id, label: text(props.richText).trim() || id, kind: b.kind, x: sh ? sh.x / 2 : 500, highlighted: props.color === 'red' })
  }
  for (const [id, e] of board.edges) {
    const sh = ed.getShape(e.shapeId)
    edges.push({ id, from: e.from, to: e.to, highlighted: (sh?.props as { color?: string } | undefined)?.color === 'red' })
  }
  return { boxes, edges }
}

const ANNOTATE_TOOLS: { id: string; label: string; icon: string }[] = [
  { id: 'select', label: 'Select', icon: '\u2196' },
  { id: 'draw', label: 'Pen', icon: '\u270E' },
  { id: 'highlight', label: 'Highlighter', icon: '\u2591' },
  { id: 'arrow', label: 'Arrow', icon: '\u2197' },
  { id: 'note', label: 'Sticky note', icon: '\u25A4' },
  { id: 'text', label: 'Text', icon: 'T' },
  { id: 'eraser', label: 'Eraser', icon: '\u232B' },
]

function AnnotateToolbar({ editor }: { editor: Editor }) {
  const [active, setActive] = useState(() => editor.getCurrentToolId())
  useEffect(() => {
    // Tools like text/note return to select on their own; resync after each interaction.
    const sync = () => setActive(editor.getCurrentToolId())
    editor.on('event', sync)
    return () => void editor.off('event', sync)
  }, [editor])
  return (
    <div
      data-testid="board-annotate-toolbar"
      style={{
        position: 'absolute', left: 12, top: '50%', transform: 'translateY(-50%)', zIndex: 400,
        display: 'flex', flexDirection: 'column', gap: 2, padding: 4, borderRadius: 999,
        border: '1px solid rgba(0,0,0,0.1)', background: 'rgba(255,255,255,0.7)',
        backdropFilter: 'blur(20px) saturate(180%)', WebkitBackdropFilter: 'blur(20px) saturate(180%)',
        boxShadow: '0 4px 16px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.08)',
      }}
    >
      {ANNOTATE_TOOLS.map((t) => {
        const on = active === t.id
        return (
          <button
            key={t.id}
            title={t.label}
            aria-label={t.label}
            aria-pressed={on}
            onClick={() => {
              editor.setCurrentTool(t.id)
              setActive(editor.getCurrentToolId())
            }}
            style={{
              width: 28, height: 28, borderRadius: 999, border: 'none', padding: 0, cursor: 'pointer',
              background: on ? '#0a84ff' : 'transparent', color: on ? '#fff' : '#1d1d1f',
              font: '600 14px -apple-system, BlinkMacSystemFont, system-ui, sans-serif', lineHeight: '28px',
            }}
          >
            {t.icon}
          </button>
        )
      })}
    </div>
  )
}

export default function Whiteboard({ client, onHasContent }: { client: TwinClient; onHasContent?: (has: boolean) => void }) {
  const [editor, setEditor] = useState<Editor | null>(null)
  const [view3d, setView3d] = useState(() => new URLSearchParams(location.search).get('view') === '3d')
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
    const board = new BoardController(editor, (m) => client.send(m), (has) => onContentRef.current?.(has), client.user)
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
      <div style={{ position: 'absolute', inset: 0, visibility: view3d ? 'hidden' : 'visible' }}>
        <Tldraw hideUi shapeUtils={richShapeUtils} onMount={(ed) => setEditor(ed)} />
      </div>
      {!view3d && editor && <AnnotateToolbar editor={editor} />}
      {view3d && <Board3D client={client} onClose={() => setView3d(false)} />}
      <button
        data-testid="board-3d-toggle"
        onClick={() => setView3d((v) => !v)}
        title={view3d ? 'Back to 2D board' : '3D view of the architecture'}
        style={{
          position: 'absolute', top: 12, right: 12, zIndex: 400, height: 28, padding: '0 14px', borderRadius: 999,
          border: '1px solid ' + (view3d ? 'rgba(255,255,255,0.16)' : 'rgba(0,0,0,0.1)'),
          background: view3d ? 'rgba(40,41,48,0.6)' : 'rgba(255,255,255,0.7)',
          color: view3d ? '#f5f5f7' : '#1d1d1f',
          backdropFilter: 'blur(20px) saturate(180%)', WebkitBackdropFilter: 'blur(20px) saturate(180%)',
          boxShadow: '0 4px 16px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.08)',
          font: '600 12px -apple-system, BlinkMacSystemFont, system-ui, sans-serif', letterSpacing: 0.2, cursor: 'pointer',
        }}
      >
        {view3d ? '2D' : '3D'}
      </button>
    </div>
  )
}
