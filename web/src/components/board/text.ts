import { type Editor, type TLDefaultSizeStyle, type TLShapeId, type TLTextShape, createShapeId, toRichText } from 'tldraw'

export function textSize(s: unknown): TLDefaultSizeStyle {
  const v = String(s ?? 'm').toLowerCase()
  return v === 's' || v === 'm' || v === 'l' || v === 'xl' ? v : 'm'
}

export function createText(editor: Editor, op: Record<string, unknown>, pos: { x: number; y: number }, id?: TLShapeId, color: TLTextShape['props']['color'] = 'black'): TLShapeId {
  const sid = id ?? createShapeId()
  editor.createShape<TLTextShape>({
    id: sid,
    type: 'text',
    x: pos.x,
    y: pos.y,
    opacity: 1,
    props: { richText: toRichText(String(op.text ?? '')), size: textSize(op.size), font: 'sans', color, autoSize: true },
  })
  return sid
}

export function updateText(editor: Editor, shapeId: TLShapeId, op: Record<string, unknown>) {
  const props: Partial<TLTextShape['props']> = {}
  if (op.text !== undefined) props.richText = toRichText(String(op.text))
  if (op.size !== undefined) props.size = textSize(op.size)
  editor.updateShape<TLTextShape>({ id: shapeId, type: 'text', props })
}
