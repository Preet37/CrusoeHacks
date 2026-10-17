import { b64Vecs, type Editor, type TLDefaultColorStyle, type TLDrawShape, type TLShapeId, createShapeId } from 'tldraw'

const COLORS: Record<string, TLDefaultColorStyle> = {
  black: 'black', red: 'red', blue: 'blue', green: 'green', orange: 'orange', violet: 'violet', purple: 'violet', grey: 'grey', gray: 'grey', yellow: 'yellow',
}

type ToPage = (x: number, y: number) => { x: number; y: number }

/** Creates one draw shape containing all strokes as separate 'free' segments. Returns its id. */
export function createSketch(editor: Editor, op: Record<string, unknown>, toPage: ToPage, id?: TLShapeId): TLShapeId {
  const strokesRaw = Array.isArray(op.strokes) ? op.strokes : []
  const strokes = strokesRaw
    .map(s => (Array.isArray(s) ? s : []).filter((p): p is [number, number] => Array.isArray(p) && p.length >= 2 && isFinite(+p[0]) && isFinite(+p[1])))
    .filter(s => s.length > 0)
    .map(s => s.map(([x, y]) => toPage(+x, +y)))
  if (!strokes.length) throw new Error('sketch needs at least one stroke of [x, y] points')
  let minX = Infinity, minY = Infinity
  for (const s of strokes) for (const p of s) { minX = Math.min(minX, p.x); minY = Math.min(minY, p.y) }
  const segments: TLDrawShape['props']['segments'] = strokes.map(s => {
    const pts = (s.length === 1 ? [s[0], { x: s[0].x + 0.5, y: s[0].y + 0.5 }] : s).map(p => ({ x: p.x - minX, y: p.y - minY, z: 0.5 }))
    return { type: 'free' as const, path: b64Vecs.encodePoints(pts) }
  })
  const sid = id ?? createShapeId()
  editor.createShape<TLDrawShape>({
    id: sid,
    type: 'draw',
    x: minX,
    y: minY,
    opacity: 1,
    props: {
      segments,
      color: COLORS[String(op.color ?? 'black').toLowerCase()] ?? 'black',
      size: 'm',
      dash: 'draw',
      fill: 'none',
      isComplete: true,
      isClosed: false,
      isPen: false,
    },
  })
  return sid
}
