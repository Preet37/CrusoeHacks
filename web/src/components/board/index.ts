/**
 * Rich whiteboard shapes for Twin (PROTOCOL.md "v2 additions").
 *
 * INTEGRATION (Whiteboard.tsx):
 *   1. Pass the custom shape utils:   <Tldraw shapeUtils={richShapeUtils} ... />
 *      (keep the array reference stable: it is a module-level constant).
 *   2. For every `draw` message, first try:  if (renderRichOp(editor, op, ctx)) return
 *      before handling its own ops (add_box/connect/frame/focus/remove/...).
 *      `update` is handled here only when the id belongs to a rich shape; otherwise false is returned.
 *
 * renderRichOp is synchronous; Mermaid rendering finishes asynchronously and reports failures
 * via ctx.send({type:'draw_error', id, op, error}).
 */
import { createShapeId, type Editor, type TLShapeId } from 'tldraw'
import type { RichCtx } from './types'
import { createSticky, updateSticky } from './sticky'
import { createText, updateText } from './text'
import { renderMermaidSvg } from './mermaid'
import { renderChartSvg } from './chart'
import { renderTableSvg } from './table'
import { renderCodeSvg } from './code'
import { createMock, TwinMockShapeUtil, updateMock } from './mock'
import { createSketch } from './sketch'
import { placeSvgImage, type Box } from './svgImage'

export type { RichCtx } from './types'
export { TwinMockShapeUtil } from './mock'

export const richShapeUtils = [TwinMockShapeUtil]

type Op = { op: string; id?: string; [k: string]: unknown }

const RICH_OPS = new Set(['add_sticky', 'add_text', 'add_mermaid', 'add_chart', 'add_table', 'add_code', 'add_mock', 'sketch'])
const DEFAULT_SIZE: Record<string, [number, number]> = {
  add_mermaid: [600, 400],
  add_chart: [400, 260],
  add_table: [500, 300],
  add_code: [420, 240],
  add_mock: [300, 400],
}

/** Original (merged) op dicts by protocol id, so `update` can merge and re-render. */
const richOps = new Map<string, Op>()
/** Generation counter per id so stale async (mermaid) results are dropped. */
const gen = new Map<string, number>()

function num(v: unknown, d: number): number {
  const n = typeof v === 'number' ? v : Number(v)
  return isFinite(n) ? n : d
}

function boxFor(op: Op, ctx: RichCtx): Box {
  const frame = typeof op.frame === 'string' ? op.frame : undefined
  const [dw, dh] = DEFAULT_SIZE[op.op] ?? [300, 200]
  const p = ctx.gridToPage(num(op.x, 100), num(op.y, 100), frame)
  const s = ctx.sizeToPage(num(op.w, dw), num(op.h, dh), frame)
  return { x: p.x, y: p.y, w: Math.max(40, s.w), h: Math.max(30, s.h) }
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function reportError(editor: Editor, op: Op, ctx: RichCtx, e: unknown, box: Box | null, existing?: TLShapeId) {
  const error = errMsg(e).slice(0, 300)
  console.warn('[board] render failed', op.op, op.id, error)
  try { ctx.send({ type: 'draw_error', id: op.id ?? '', op: op.op, error }) } catch { /* ignore */ }
  // On a failed update keep the previous good render; on a fresh add drop a small red note.
  if (existing && editor.getShape(existing)) return
  if (!box || !op.id) return
  try {
    const sid = createText(editor, { text: `⚠ ${op.op.replace('add_', '')} failed: ${error.slice(0, 80)}`, size: 's' }, box, undefined, 'red')
    ctx.register(op.id, sid)
  } catch { /* ignore */ }
}

function kindOf(op: Op): string {
  return op.op
}

/** Render one rich op (fresh add or merged update). `existing` = shape to replace in place. */
function render(editor: Editor, op: Op, ctx: RichCtx, existing?: TLShapeId) {
  const id = op.id
  const box = boxFor(op, ctx)
  // For in-place replacement keep the current position.
  if (existing) {
    const s = editor.getShape(existing)
    if (s) { box.x = s.x; box.y = s.y }
  }
  const reg = (sid: TLShapeId) => { if (id) ctx.register(id, sid) }
  const isImageKind = ['add_mermaid', 'add_chart', 'add_table', 'add_code'].includes(op.op)
  // A non-image shape cannot be swapped into an image in place: delete first.
  if (existing && !isImageKind) {
    if (editor.getShape(existing)) editor.deleteShape(existing)
  }
  if (existing && isImageKind) {
    const s = editor.getShape(existing)
    if (s && s.type !== 'image') { editor.deleteShape(existing); existing = undefined }
  }
  try {
    switch (op.op) {
      case 'add_sticky':
        reg(createSticky(editor, op, box, existing))
        return
      case 'add_text':
        reg(createText(editor, op, box, existing))
        return
      case 'add_mock':
        reg(createMock(editor, op, box, existing))
        return
      case 'sketch': {
        const frame = typeof op.frame === 'string' ? op.frame : undefined
        reg(createSketch(editor, op, (x, y) => ctx.gridToPage(x, y, frame), existing))
        return
      }
      case 'add_chart': {
        const out = renderChartSvg(op, box.w, box.h)
        reg(placeSvgImage(editor, out.svg, out.w, out.h, box, { name: `chart ${id ?? ''}`, existing }))
        return
      }
      case 'add_code': {
        const out = renderCodeSvg(op, box.w, box.h)
        reg(placeSvgImage(editor, out.svg, out.w, out.h, box, { name: `code ${id ?? ''}`, existing }))
        return
      }
      case 'add_table': {
        const hasW = op.w !== undefined
        const out = renderTableSvg(op, hasW ? box.w : undefined)
        const fit = hasW || op.h !== undefined
        reg(placeSvgImage(editor, out.svg, out.w, out.h, box, { name: `table ${id ?? ''}`, existing, fit: fit && out.h > box.h ? true : false }))
        return
      }
      case 'add_mermaid': {
        const myGen = (gen.get(id ?? '') ?? 0) + 1
        if (id) gen.set(id, myGen)
        const code = String(op.code ?? '')
        renderMermaidSvg(code)
          .then(out => {
            if (id && gen.get(id) !== myGen) return // superseded
            const cur = id ? ctx.shapeIdFor(id) : undefined
            const target = existing ?? (cur && editor.getShape(cur) ? cur : undefined)
            reg(placeSvgImage(editor, out.svg, out.w, out.h, box, { name: `mermaid ${id ?? ''}`, existing: target }))
          })
          .catch(e => {
            if (id && gen.get(id) !== myGen) return
            reportError(editor, op, ctx, e, box, existing)
          })
        return
      }
    }
  } catch (e) {
    reportError(editor, op, ctx, e, box, existing)
  }
}

export function renderRichOp(editor: Editor, op: { op: string; id?: string; [k: string]: unknown }, ctx: RichCtx): boolean {
  if (!editor || !op || typeof op.op !== 'string') return false
  try {
    if (RICH_OPS.has(op.op)) {
      const id = op.id ?? `rich_${Math.random().toString(36).slice(2, 9)}`
      const full: Op = { ...op, id }
      richOps.set(id, full)
      const prev = ctx.shapeIdFor(id)
      const existing = prev && editor.getShape(prev) ? prev : undefined
      render(editor, full, ctx, existing)
      return true
    }
    if (op.op === 'update' && op.id) {
      const base = richOps.get(op.id)
      if (!base) return false
      const shapeId = ctx.shapeIdFor(op.id)
      const { op: _ignored, ...fields } = op
      void _ignored
      const merged: Op = { ...base, ...fields, op: base.op, id: op.id }
      richOps.set(op.id, merged)
      const existing = shapeId && editor.getShape(shapeId) ? shapeId : undefined
      const kind = kindOf(base)
      if (existing && kind === 'add_sticky') updateSticky(editor, existing, op)
      else if (existing && kind === 'add_text' && editor.getShape(existing)?.type === 'text') updateText(editor, existing, op)
      else if (existing && kind === 'add_mock') updateMock(editor, existing, op)
      else render(editor, merged, ctx, existing ?? createShapeId())
      return true
    }
    if (op.op === 'remove' && op.id) {
      // Whiteboard removes the shape; we just forget our record. Not "handled".
      richOps.delete(op.id); gen.delete(op.id)
      return false
    }
    if (op.op === 'clear') {
      richOps.clear(); gen.clear()
      return false
    }
  } catch (e) {
    console.warn('[board] renderRichOp error', e)
    try { ctx.send({ type: 'draw_error', id: op.id ?? '', op: op.op, error: errMsg(e) }) } catch { /* ignore */ }
    return true
  }
  return false
}
