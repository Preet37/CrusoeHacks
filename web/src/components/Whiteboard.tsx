// Live whiteboard: tldraw editor that applies server draw ops on a 0-1000 grid.
// Handles boxes, arrows, rename, remove, highlight, clear, frames, focus and board_snapshot.
// Any other op goes to renderRichOp() from components/board/index.ts (PREET-CLAUDE, rich shapes).
import { useEffect, useRef, useState } from 'react'
import {
  Tldraw,
  createShapeId,
  toRichText,
  type Editor,
  type TLShapeId,
  type TLShapePartial,
  type TLParentId,
} from 'tldraw'
import 'tldraw/tldraw.css'
import type { Box, BoxKind, Edge } from '../lib/protocol'
import type { AnyClientMessage, AnyServerMessage, TwinClient } from '../lib/ws'

/** Page units per grid unit. Boxes (160 x 70 grid) become 192 x 84 px. */
const S = 1.2
const BOX_W = 160
const BOX_H = 70
/** Topic frames are laid out left to right, this far apart (page units). */
const FRAME_GAP = 240
const APPEAR_MS = 220

type Op = Record<string, unknown> & { op: string }

export interface RichCtx {
  gridToPage(x: number, y: number): { x: number; y: number }
  sizeToPage(w: number, h: number): { w: number; h: number }
  shapeIdFor(id: string): TLShapeId
  register(id: string, shapeId: TLShapeId): void
  /** tldraw parent for a topic frame id (or the active frame when omitted). */
  parentFor(frameId?: string): TLParentId
  frameShapeId(frameId: string): TLShapeId | undefined
  send(msg: AnyClientMessage): void
}

type RichRenderer = (editor: Editor, op: Op, ctx: RichCtx) => boolean | Promise<boolean>
const richModules = import.meta.glob('./board/index.ts')
let richRenderer: RichRenderer | null | undefined
async function loadRich(): Promise<RichRenderer | null> {
  if (richRenderer !== undefined) return richRenderer
  const loader = richModules['./board/index.ts']
  richRenderer = null
  if (loader) {
    try {
      const mod = (await loader()) as { renderRichOp?: RichRenderer }
      richRenderer = mod.renderRichOp ?? null
    } catch (e) {
      console.warn('[whiteboard] board/index.ts failed to load', e)
    }
  }
  return richRenderer
}

const KIND_STYLE: Record<BoxKind, { geo: string; color: string; fill: string; dash: string }> = {
  service: { geo: 'rectangle', color: 'blue', fill: 'semi', dash: 'solid' },
  datastore: { geo: 'ellipse', color: 'green', fill: 'semi', dash: 'solid' },
  client: { geo: 'oval', color: 'violet', fill: 'semi', dash: 'solid' },
  external: { geo: 'cloud', color: 'grey', fill: 'none', dash: 'dashed' },
  note: { geo: 'rectangle', color: 'yellow', fill: 'solid', dash: 'draw' },
}
const HIGHLIGHT_COLOR = 'orange'

interface BoxRec {
  shapeId: TLShapeId
  kind: BoxKind
  frame?: string
}
interface EdgeRec {
  shapeId: TLShapeId
  from: string
  to: string
}
interface FrameRec {
  shapeId: TLShapeId
  index: number
}

/** All board bookkeeping for one editor. Plain class so ops apply synchronously and in order. */
class BoardController {
  boxes = new Map<string, BoxRec>()
  edges = new Map<string, EdgeRec>()
  frames = new Map<string, FrameRec>()
  /** Any other shape registered by rich renderers: protocol id -> shape id. */
  others = new Map<string, TLShapeId>()
  activeFrame: string | undefined
  private fitTimer: number | undefined
  private focusTarget: TLShapeId | undefined
  private queue: Promise<void> = Promise.resolve()
  private editor: Editor
  private send: (m: AnyClientMessage) => void
  private onContent: (has: boolean) => void

  constructor(editor: Editor, send: (m: AnyClientMessage) => void, onContent: (has: boolean) => void) {
    this.editor = editor
    this.send = send
    this.onContent = onContent
  }

  // ------------------------------------------------------------- geometry

  parentFor(frame?: string): TLParentId {
    const f = this.frames.get(frame ?? this.activeFrame ?? '')
    return f ? f.shapeId : this.editor.getCurrentPageId()
  }

  /** Grid -> local coords of the shape's parent (frames use local coords). */
  private local(x: number, y: number) {
    return { x: clamp(x) * S, y: clamp(y) * S }
  }

  private resolveFrame(op: Op): string | undefined {
    const f = typeof op.frame === 'string' ? op.frame : undefined
    if (f && this.frames.has(f)) return f
    return this.activeFrame
  }

  private shapeOf(id: string): TLShapeId | undefined {
    return this.boxes.get(id)?.shapeId ?? this.edges.get(id)?.shapeId ?? this.frames.get(id)?.shapeId ?? this.others.get(id)
  }

  // ------------------------------------------------------------- public api

  /** Apply ops strictly in arrival order (rich renderers may be async). */
  apply(op: Op) {
    this.queue = this.queue.then(() => this.applyNow(op)).catch((e) => console.warn('[whiteboard] op failed', op, e))
  }

  snapshot(boxes: Box[], edges: Edge[]) {
    this.queue = this.queue.then(() => {
      this.clearAll()
      for (const b of boxes) this.addBox({ op: 'add_box', ...b } as Op, false)
      for (const e of edges) this.connect({ op: 'connect', ...e } as Op)
      this.scheduleFit(true)
    })
  }

  private async applyNow(op: Op) {
    const ed = this.editor
    switch (op.op) {
      case 'add_box':
        this.addBox(op, true)
        break
      case 'connect':
        this.connect(op)
        break
      case 'rename':
        this.rename(String(op.id), String(op.label ?? ''))
        break
      case 'remove':
        this.remove(String(op.id))
        break
      case 'highlight':
        this.highlight((op.ids as string[]) ?? [], op.on !== false)
        break
      case 'clear':
        this.clearAll()
        break
      case 'frame':
        this.frame(op)
        break
      case 'focus':
        this.focus(String(op.id))
        break
      default: {
        const rich = await loadRich()
        let handled = false
        if (rich) {
          try {
            handled = await rich(ed, op, this.ctx())
          } catch (e) {
            this.send({ type: 'draw_error', id: op.id, op: op.op, error: String(e).slice(0, 300) })
            handled = true
          }
        }
        if (!handled) console.info('[whiteboard] unhandled op (rich shapes not loaded yet):', op.op)
      }
    }
    this.scheduleFit()
    this.onContent(this.hasContent())
  }

  ctx(): RichCtx {
    return {
      gridToPage: (x, y) => this.local(x, y),
      sizeToPage: (w, h) => ({ w: Math.max(1, w) * S, h: Math.max(1, h) * S }),
      shapeIdFor: (id) => this.shapeOf(id) ?? createShapeId(`rich_${id}`),
      register: (id, shapeId) => this.others.set(id, shapeId),
      parentFor: (f) => this.parentFor(f),
      frameShapeId: (f) => this.frames.get(f)?.shapeId,
      send: this.send,
    }
  }

  hasContent() {
    return this.editor.getCurrentPageShapeIds().size > 0
  }

  // ------------------------------------------------------------- ops

  private addBox(op: Op, animate: boolean) {
    const ed = this.editor
    const id = String(op.id)
    const kind = (KIND_STYLE[op.kind as BoxKind] ? op.kind : 'service') as BoxKind
    const label = String(op.label ?? id)
    const existing = this.boxes.get(id)
    const frame = this.resolveFrame(op)
    const parentId = this.parentFor(frame)
    const style = KIND_STYLE[kind]
    let { x, y } = this.local(Number(op.x ?? 500), Number(op.y ?? 500))
    if (!existing) ({ x, y } = this.freeSpot(parentId, x, y))

    const props = {
      geo: style.geo,
      color: style.color,
      fill: style.fill,
      dash: style.dash,
      w: BOX_W * S,
      h: BOX_H * S,
      size: 's',
      font: 'sans',
      richText: toRichText(label),
    }
    if (existing) {
      ed.updateShape({ id: existing.shapeId, type: 'geo', x, y, props } as TLShapePartial)
      this.boxes.set(id, { ...existing, kind, frame })
      return
    }
    const shapeId = createShapeId(`box_${id}_${Date.now().toString(36)}`)
    ed.createShape({ id: shapeId, type: 'geo', parentId, x, y, opacity: animate ? 0 : 1, props } as TLShapePartial)
    this.boxes.set(id, { shapeId, kind, frame })
    if (animate) fadeIn(ed, shapeId, 'geo')
  }

  /** Nudge a new box right/down until it doesn't overlap a sibling box (RX-4). */
  private freeSpot(parentId: TLParentId, x: number, y: number) {
    const w = BOX_W * S
    const h = BOX_H * S
    const pad = 16
    const siblings = [...this.boxes.values()]
      .map((b) => this.editor.getShape(b.shapeId))
      .filter((s): s is NonNullable<typeof s> => !!s && s.parentId === parentId)
    const hit = (cx: number, cy: number) =>
      siblings.some((s) => cx < s.x + w + pad && cx + w + pad > s.x && cy < s.y + h + pad && cy + h + pad > s.y)
    if (!hit(x, y)) return { x, y }
    for (let r = 1; r < 12; r++) {
      for (const [dx, dy] of [[1, 0], [0, 1], [1, 1], [-1, 0], [0, -1], [-1, 1], [1, -1]]) {
        const cx = Math.max(0, x + dx * r * (w * 0.6))
        const cy = Math.max(0, y + dy * r * (h * 0.9))
        if (!hit(cx, cy)) return { x: cx, y: cy }
      }
    }
    return { x, y }
  }

  private connect(op: Op) {
    const ed = this.editor
    const from = this.boxes.get(String(op.from_id))
    const to = this.boxes.get(String(op.to_id))
    if (!from || !to) {
      console.warn('[whiteboard] connect with unknown box', op)
      return
    }
    const id = String(op.id ?? `e_${op.from_id}_${op.to_id}`)
    const label = String(op.label ?? '')
    const existing = this.edges.get(id)
    if (existing) {
      ed.updateShape({ id: existing.shapeId, type: 'arrow', props: { richText: toRichText(label) } } as TLShapePartial)
      return
    }
    const a = ed.getShapePageBounds(from.shapeId)
    const b = ed.getShapePageBounds(to.shapeId)
    if (!a || !b) return
    const shapeId = createShapeId(`edge_${id}_${Date.now().toString(36)}`)
    // Arrows live on the page so they can cross frames; endpoints are bound to the boxes.
    ed.createShape({
      id: shapeId,
      type: 'arrow',
      x: a.center.x,
      y: a.center.y,
      props: {
        start: { x: 0, y: 0 },
        end: { x: b.center.x - a.center.x, y: b.center.y - a.center.y },
        richText: toRichText(label),
        color: 'grey',
        size: 's',
        font: 'sans',
      },
    } as TLShapePartial)
    ed.createBindings([
      bind(shapeId, from.shapeId, 'start'),
      bind(shapeId, to.shapeId, 'end'),
    ])
    this.edges.set(id, { shapeId, from: String(op.from_id), to: String(op.to_id) })
  }

  private rename(id: string, label: string) {
    const ed = this.editor
    const box = this.boxes.get(id)
    if (box) return void ed.updateShape({ id: box.shapeId, type: 'geo', props: { richText: toRichText(label) } } as TLShapePartial)
    const edge = this.edges.get(id)
    if (edge) return void ed.updateShape({ id: edge.shapeId, type: 'arrow', props: { richText: toRichText(label) } } as TLShapePartial)
    const frame = this.frames.get(id)
    if (frame) return void ed.updateShape({ id: frame.shapeId, type: 'frame', props: { name: label } } as TLShapePartial)
    const other = this.others.get(id)
    const shape = other && ed.getShape(other)
    if (shape && 'richText' in (shape.props as object))
      ed.updateShape({ id: shape.id, type: shape.type, props: { richText: toRichText(label) } } as TLShapePartial)
  }

  private remove(id: string) {
    const ed = this.editor
    const ids: TLShapeId[] = []
    const box = this.boxes.get(id)
    if (box) {
      ids.push(box.shapeId)
      this.boxes.delete(id)
      for (const [eid, e] of this.edges) {
        if (e.from === id || e.to === id) {
          ids.push(e.shapeId)
          this.edges.delete(eid)
        }
      }
    }
    const edge = this.edges.get(id)
    if (edge) {
      ids.push(edge.shapeId)
      this.edges.delete(id)
    }
    const frame = this.frames.get(id)
    if (frame) {
      ids.push(frame.shapeId)
      this.frames.delete(id)
      if (this.activeFrame === id) this.activeFrame = undefined
      // Children are deleted with the frame; drop their records.
      for (const [bid, b] of this.boxes) if (b.frame === id) this.boxes.delete(bid)
    }
    const other = this.others.get(id)
    if (other) {
      ids.push(other)
      this.others.delete(id)
    }
    if (ids.length) ed.deleteShapes(ids.filter((s) => ed.getShape(s)))
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
          props: on ? { color: HIGHLIGHT_COLOR, fill: 'solid', size: 'm' } : { color: base.color, fill: base.fill, size: 's' },
        } as TLShapePartial)
        continue
      }
      const edge = this.edges.get(id)
      if (edge) {
        updates.push({ id: edge.shapeId, type: 'arrow', props: { color: on ? HIGHLIGHT_COLOR : 'grey', size: on ? 'm' : 's' } } as TLShapePartial)
        continue
      }
      const other = this.others.get(id)
      const shape = other && ed.getShape(other)
      if (shape && 'color' in (shape.props as object))
        updates.push({ id: shape.id, type: shape.type, props: { color: on ? HIGHLIGHT_COLOR : 'black' } } as TLShapePartial)
    }
    if (updates.length) ed.updateShapes(updates)
  }

  private frame(op: Op) {
    const ed = this.editor
    const id = String(op.id)
    const title = String(op.title ?? op.label ?? id)
    const existing = this.frames.get(id)
    if (existing) {
      ed.updateShape({ id: existing.shapeId, type: 'frame', props: { name: title } } as TLShapePartial)
      this.activeFrame = id
      this.focus(id)
      return
    }
    // Each topic gets its own area, laid out left to right (server x/y are ignored for frames).
    const index = this.frames.size
    const fx = index * (1000 * S + FRAME_GAP)
    const shapeId = createShapeId(`frame_${id}_${Date.now().toString(36)}`)
    ed.createShape({
      id: shapeId,
      type: 'frame',
      x: fx,
      y: 0,
      props: { w: clamp(Number(op.w ?? 1000), 100) * S, h: clamp(Number(op.h ?? 1000), 100) * S, name: title },
    } as TLShapePartial)
    // Frames always sit behind content.
    ed.sendToBack([shapeId])
    this.frames.set(id, { shapeId, index })
    this.activeFrame = id
    this.focus(id)
  }

  private focus(id: string) {
    const target = this.shapeOf(id)
    if (!target) return
    const f = this.frames.get(id)
    if (f) this.activeFrame = id
    this.focusTarget = target
    this.scheduleFit(true)
  }

  clearAll() {
    const ed = this.editor
    const all = [...ed.getCurrentPageShapeIds()]
    if (all.length) ed.deleteShapes(all)
    this.boxes.clear()
    this.edges.clear()
    this.frames.clear()
    this.others.clear()
    this.activeFrame = undefined
    this.focusTarget = undefined
    this.onContent(false)
  }

  /** Zoom to the focused frame, or to everything, shortly after a burst of ops (RX-4). */
  scheduleFit(soon = false) {
    window.clearTimeout(this.fitTimer)
    this.fitTimer = window.setTimeout(() => {
      const ed = this.editor
      if (!this.hasContent()) return
      const animation = { duration: 450 }
      const target = this.focusTarget && ed.getShape(this.focusTarget) ? this.focusTarget : undefined
      if (target) {
        const bounds = ed.getShapePageBounds(target)
        if (bounds) return void ed.zoomToBounds(bounds, { animation, inset: 48, targetZoom: 1.2 })
      }
      ed.zoomToFit({ animation })
      if (ed.getZoomLevel() > 1.2) ed.setCamera({ ...ed.getCamera(), z: 1.2 }, { animation })
    }, soon ? 60 : 350)
  }

  dispose() {
    window.clearTimeout(this.fitTimer)
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

function fadeIn(ed: Editor, id: TLShapeId, type: string) {
  const start = performance.now()
  const step = (t: number) => {
    if (!ed.getShape(id)) return
    const k = Math.min(1, (t - start) / APPEAR_MS)
    ed.updateShape({ id, type, opacity: k } as TLShapePartial)
    if (k < 1) requestAnimationFrame(step)
  }
  requestAnimationFrame(step)
}

function clamp(v: number, min = 0) {
  return Math.max(min, Math.min(1000, Number.isFinite(v) ? v : 0))
}

export default function Whiteboard({
  client,
  onHasContent,
}: {
  client: TwinClient | null
  onHasContent?: (has: boolean) => void
}) {
  const [editor, setEditor] = useState<Editor | null>(null)
  const onContentRef = useRef(onHasContent)
  onContentRef.current = onHasContent

  useEffect(() => {
    if (!editor || !client) return
    const board = new BoardController(editor, (m) => client.send(m), (has) => onContentRef.current?.(has))
    const off = client.onAny((m: AnyServerMessage) => {
      if (m.type === 'draw') board.apply(m as unknown as Op)
      else if (m.type === 'board_snapshot') {
        const snap = m as unknown as { boxes?: Box[]; edges?: Edge[] }
        board.snapshot(snap.boxes ?? [], snap.edges ?? [])
      }
    })
    // Exposed for debugging in the console: window.twinBoard
    ;(window as unknown as { twinBoard?: BoardController }).twinBoard = board
    return () => {
      off()
      board.dispose()
    }
  }, [editor, client])

  return (
    <div className="whiteboard">
      <Tldraw
        hideUi
        onMount={(ed) => {
          ed.updateInstanceState({ isGridMode: false })
          setEditor(ed)
        }}
      />
    </div>
  )
}
