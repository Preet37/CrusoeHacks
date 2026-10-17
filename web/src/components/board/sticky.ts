import { type Editor, type TLDefaultColorStyle, type TLNoteShape, type TLShapeId, createShapeId, toRichText } from 'tldraw'

const COLORS: Record<string, TLDefaultColorStyle> = {
  yellow: 'yellow',
  blue: 'light-blue',
  green: 'light-green',
  pink: 'light-red',
  orange: 'orange',
  red: 'light-red',
  violet: 'light-violet',
  purple: 'light-violet',
}

export function stickyColor(c: unknown): TLDefaultColorStyle {
  return COLORS[String(c ?? 'yellow').toLowerCase()] ?? 'yellow'
}

export function createSticky(editor: Editor, op: Record<string, unknown>, pos: { x: number; y: number }, id?: TLShapeId): TLShapeId {
  const sid = id ?? createShapeId()
  editor.createShape<TLNoteShape>({
    id: sid,
    type: 'note',
    x: pos.x,
    y: pos.y,
    opacity: 1,
    props: { richText: toRichText(String(op.text ?? '')), color: stickyColor(op.color), size: 'm', font: 'sans' },
  })
  return sid
}

export function updateSticky(editor: Editor, shapeId: TLShapeId, op: Record<string, unknown>) {
  const props: Partial<TLNoteShape['props']> = {}
  if (op.text !== undefined) props.richText = toRichText(String(op.text))
  if (op.color !== undefined) props.color = stickyColor(op.color)
  editor.updateShape<TLNoteShape>({ id: shapeId, type: 'note', props })
}
