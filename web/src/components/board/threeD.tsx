/**
 * 'twin-3d' tldraw shape (PROTOCOL v2.1 add_3d). The Three.js renderer lives in ./threeScene.tsx and is
 * lazy-loaded, so three/@react-three/* stay in their own chunk until a 3D shape is on screen.
 *
 * Interaction choice: canEdit = true. Normally the shape behaves like any tldraw shape (drag/resize,
 * the scene only auto-rotates). Double-click to enter editing: then pointer events go to the canvas
 * (orbit by drag, wheel zoom) and are stopped from reaching tldraw. Esc / click outside exits.
 */
import { Component, Suspense, lazy, type ReactNode } from 'react'
import { BaseBoxShapeUtil, HTMLContainer, Rectangle2d, T, createShapeId, useIsEditing, type Editor, type RecordProps, type TLShape, type TLShapeId } from 'tldraw'
import type { Scene3D } from './scene3d'

declare module '@tldraw/tlschema' {
  interface TLGlobalShapePropsMap {
    'twin-3d': { w: number; h: number; title: string; pid: string; scene: Scene3D }
  }
}

export type Twin3DShape = TLShape<'twin-3d'>

const TITLE_H = 28
const LazyScene = lazy(() => import('./threeScene'))

/** Set by renderRichOp so render failures inside the shape can be reported (draw_error, once per id). */
let errorSink: ((pid: string, error: string) => void) | null = null
const reported = new Set<string>()
export function set3dErrorSink(fn: (pid: string, error: string) => void) { errorSink = fn }
export function reset3dError(pid: string) { reported.delete(pid) }

class Boundary extends Component<{ pid: string; resetKey: string; children: ReactNode }, { err: string | null; key: string }> {
  state = { err: null as string | null, key: this.props.resetKey }
  static getDerivedStateFromError(e: unknown) {
    return { err: e instanceof Error ? e.message : String(e) }
  }
  static getDerivedStateFromProps(p: { resetKey: string }, s: { err: string | null; key: string }) {
    return p.resetKey !== s.key ? { err: null, key: p.resetKey } : null
  }
  componentDidCatch(e: unknown) {
    const pid = this.props.pid
    if (reported.has(pid)) return
    reported.add(pid)
    const msg = (e instanceof Error ? e.message : String(e)).slice(0, 300)
    console.warn('[board] 3d failed', pid, msg)
    try { errorSink?.(pid, msg) } catch { /* ignore */ }
  }
  render() {
    if (this.state.err) {
      return (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#C7372F', fontSize: 13, padding: 12, textAlign: 'center' }}>
          3D failed: {this.state.err.slice(0, 90)}
        </div>
      )
    }
    return this.props.children
  }
}

function Body({ shape }: { shape: Twin3DShape }) {
  const editing = useIsEditing(shape.id)
  const { w, h, title, scene, pid } = shape.props
  const stop = (e: { stopPropagation(): void }) => { if (editing) e.stopPropagation() }
  return (
    <HTMLContainer
      style={{
        width: w, height: h, borderRadius: 12, overflow: 'hidden',
        background: 'linear-gradient(180deg, #FBFCFE 0%, #EEF1F6 100%)',
        border: editing ? '1px solid #4F8CFF' : '1px solid rgba(0,0,0,0.12)',
        boxShadow: '0 8px 24px rgba(0,0,0,0.12)',
        display: 'flex', flexDirection: 'column',
        pointerEvents: editing ? 'all' : 'none',
        fontFamily: `-apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif`,
      }}
    >
      <div style={{ height: TITLE_H, flex: `0 0 ${TITLE_H}px`, display: 'flex', alignItems: 'center', gap: 6, padding: '0 10px', background: '#F2F2F7', borderBottom: '1px solid rgba(0,0,0,0.08)', fontSize: 12, fontWeight: 600, color: '#3A3A3C' }}>
        <span style={{ fontSize: 10, fontWeight: 700, color: '#fff', background: '#4F8CFF', borderRadius: 4, padding: '1px 5px' }}>3D</span>
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{title}</span>
        <span style={{ fontWeight: 500, color: '#8E8E93', fontSize: 11 }}>{editing ? 'drag to orbit' : 'double-click to orbit'}</span>
      </div>
      <div
        style={{ flex: 1, position: 'relative', minHeight: 0 }}
        onPointerDown={stop} onPointerMove={stop} onPointerUp={stop} onWheel={stop} onDoubleClick={stop}
      >
        <Boundary pid={pid} resetKey={JSON.stringify(scene)}>
          <Suspense fallback={<div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#8E8E93', fontSize: 12 }}>Loading 3D…</div>}>
            <LazyScene scene={scene} interactive={editing} />
          </Suspense>
        </Boundary>
      </div>
    </HTMLContainer>
  )
}

export class Twin3DShapeUtil extends BaseBoxShapeUtil<Twin3DShape> {
  static override type = 'twin-3d' as const
  static override props: RecordProps<Twin3DShape> = {
    w: T.number,
    h: T.number,
    title: T.string,
    pid: T.string,
    scene: T.any,
  }
  getDefaultProps(): Twin3DShape['props'] {
    return { w: 400, h: 350, title: '', pid: '', scene: { autoRotate: true, objects: [] } }
  }
  override canEdit() { return true }
  override getGeometry(shape: Twin3DShape) {
    return new Rectangle2d({ width: shape.props.w, height: shape.props.h, isFilled: true })
  }
  component(shape: Twin3DShape) { return <Body shape={shape} /> }
  getIndicatorPath(shape: Twin3DShape) {
    const p = new Path2D()
    p.roundRect(0, 0, shape.props.w, shape.props.h, 12)
    return p
  }
}

export function create3d(editor: Editor, pid: string, title: string, scene: Scene3D, box: { x: number; y: number; w: number; h: number }, id?: TLShapeId): TLShapeId {
  const sid = id ?? createShapeId()
  reset3dError(pid)
  editor.createShape<Twin3DShape>({
    id: sid, type: 'twin-3d', x: box.x, y: box.y, opacity: 1,
    props: { w: Math.max(160, box.w), h: Math.max(140, box.h), title, pid, scene },
  })
  return sid
}

export function update3d(editor: Editor, shapeId: TLShapeId, pid: string, patch: { title?: string; scene?: Scene3D }) {
  const props: Partial<Twin3DShape['props']> = {}
  if (patch.title !== undefined) props.title = patch.title
  if (patch.scene !== undefined) { props.scene = patch.scene; reset3dError(pid) }
  editor.updateShape<Twin3DShape>({ id: shapeId, type: 'twin-3d', props })
}
