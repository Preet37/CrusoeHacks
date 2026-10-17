import { BaseBoxShapeUtil, HTMLContainer, Rectangle2d, T, type Editor, type RecordProps, type TLShape, type TLShapeId, createShapeId, useIsEditing } from 'tldraw'

declare module '@tldraw/tlschema' {
  interface TLGlobalShapePropsMap {
    'twin-mock': { w: number; h: number; html: string; title: string }
  }
}

export type TwinMockShape = TLShape<'twin-mock'>

const MAX_HTML = 4096
const TITLE_H = 28

export function sanitizeMockHtml(html: unknown): string {
  let s = String(html ?? '')
  s = s.replace(/<script\b[\s\S]*?<\/script\s*>/gi, '').replace(/<script\b[^>]*>/gi, '')
  s = s.replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '')
  if (s.length > MAX_HTML) s = s.slice(0, MAX_HTML)
  return s
}

function MockBody({ shape }: { shape: TwinMockShape }) {
  const editing = useIsEditing(shape.id)
  const { w, h, html, title } = shape.props
  return (
    <HTMLContainer
      style={{
        width: w,
        height: h,
        borderRadius: 12,
        overflow: 'hidden',
        background: '#FFFFFF',
        border: '1px solid rgba(0,0,0,0.12)',
        boxShadow: '0 8px 24px rgba(0,0,0,0.12)',
        display: 'flex',
        flexDirection: 'column',
        pointerEvents: editing ? 'all' : 'none',
        fontFamily: `-apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif`,
      }}
    >
      <div
        style={{
          height: TITLE_H,
          flex: `0 0 ${TITLE_H}px`,
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '0 10px',
          background: '#F2F2F7',
          borderBottom: '1px solid rgba(0,0,0,0.08)',
          fontSize: 12,
          fontWeight: 600,
          color: '#3A3A3C',
        }}
      >
        <span style={{ width: 9, height: 9, borderRadius: 9, background: '#FF5F57' }} />
        <span style={{ width: 9, height: 9, borderRadius: 9, background: '#FEBC2E' }} />
        <span style={{ width: 9, height: 9, borderRadius: 9, background: '#28C840' }} />
        <span style={{ marginLeft: 6, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{title}</span>
      </div>
      <iframe
        sandbox=""
        srcDoc={sanitizeMockHtml(html)}
        title={title || 'mock'}
        style={{ border: 0, width: '100%', flex: 1, background: '#fff', pointerEvents: editing ? 'auto' : 'none' }}
      />
    </HTMLContainer>
  )
}

export class TwinMockShapeUtil extends BaseBoxShapeUtil<TwinMockShape> {
  static override type = 'twin-mock' as const
  static override props: RecordProps<TwinMockShape> = {
    w: T.number,
    h: T.number,
    html: T.string,
    title: T.string,
  }

  getDefaultProps(): TwinMockShape['props'] {
    return { w: 300, h: 400, html: '', title: '' }
  }

  override canEdit() {
    return true
  }

  override getGeometry(shape: TwinMockShape) {
    return new Rectangle2d({ width: shape.props.w, height: shape.props.h, isFilled: true })
  }

  component(shape: TwinMockShape) {
    return <MockBody shape={shape} />
  }

  getIndicatorPath(shape: TwinMockShape) {
    const p = new Path2D()
    p.roundRect(0, 0, shape.props.w, shape.props.h, 12)
    return p
  }
}

export function createMock(editor: Editor, op: Record<string, unknown>, box: { x: number; y: number; w: number; h: number }, id?: TLShapeId): TLShapeId {
  const sid = id ?? createShapeId()
  editor.createShape<TwinMockShape>({
    id: sid,
    type: 'twin-mock',
    x: box.x,
    y: box.y,
    opacity: 1,
    props: { w: Math.max(80, box.w), h: Math.max(60, box.h), html: sanitizeMockHtml(op.html), title: String(op.title ?? '') },
  })
  return sid
}

export function updateMock(editor: Editor, shapeId: TLShapeId, op: Record<string, unknown>) {
  const props: Partial<TwinMockShape['props']> = {}
  if (op.html !== undefined) props.html = sanitizeMockHtml(op.html)
  if (op.title !== undefined) props.title = String(op.title)
  editor.updateShape<TwinMockShape>({ id: shapeId, type: 'twin-mock', props })
}
