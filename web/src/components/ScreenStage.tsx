// Screen share stage (RC-18): shows my live capture or the latest frame someone else sent,
// with Twin's `annotate` items drawn on top in 0..1 frame coordinates.
import { useEffect, useRef, useState } from 'react'
import { useVideoRef } from '../lib/media'

export interface RemoteFrame {
  user: string
  jpeg: string
  w: number
  h: number
}

export interface AnnotateItem {
  kind: 'box' | 'arrow' | 'text' | 'highlight' | 'circle'
  x: number
  y: number
  w?: number
  h?: number
  x2?: number
  y2?: number
  text?: string
}

export interface Annotation {
  id: string
  items: AnnotateItem[]
  until: number | null
}

/** Fit a w x h frame inside the container, keeping aspect ratio, so overlay % coords line up. */
function useFitBox(aspect: number) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [box, setBox] = useState({ w: 0, h: 0 })
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      const W = el.clientWidth
      const H = el.clientHeight
      if (!W || !H || !aspect) return
      const w = Math.min(W, H * aspect)
      setBox({ w, h: w / aspect })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [aspect])
  return { ref, box }
}

function Overlay({ annotations }: { annotations: Annotation[] }) {
  return (
    <svg className="annot" viewBox="0 0 1000 1000" preserveAspectRatio="none">
      <defs>
        <marker id="annot-head" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M0 0L10 5L0 10z" fill="#ff9f0a" />
        </marker>
      </defs>
      {annotations.flatMap((a) =>
        a.items.map((it, i) => {
          const k = `${a.id}_${i}`
          const X = it.x * 1000
          const Y = it.y * 1000
          const W = (it.w ?? 0) * 1000
          const H = (it.h ?? 0) * 1000
          const label = it.text ? (
            <text key={`${k}_t`} x={X} y={Math.max(24, Y - 10)} className="annot-text">
              {it.text}
            </text>
          ) : null
          switch (it.kind) {
            case 'box':
              return [<rect key={k} x={X} y={Y} width={W} height={H} rx={6} className="annot-box" />, label]
            case 'highlight':
              return [<rect key={k} x={X} y={Y} width={W} height={H} className="annot-hl" />, label]
            case 'circle':
              return [<ellipse key={k} cx={X + W / 2} cy={Y + H / 2} rx={Math.max(W / 2, 12)} ry={Math.max(H / 2, 12)} className="annot-box" />, label]
            case 'arrow':
              return [
                <line key={k} x1={X} y1={Y} x2={(it.x2 ?? it.x) * 1000} y2={(it.y2 ?? it.y) * 1000} className="annot-arrow" markerEnd="url(#annot-head)" />,
                label,
              ]
            case 'text':
              return [
                <text key={k} x={X} y={Y} className="annot-text">
                  {it.text}
                </text>,
              ]
            default:
              return []
          }
        }),
      )}
    </svg>
  )
}

export default function ScreenStage({
  myStream,
  remote,
  annotations,
}: {
  myStream: MediaStream | null
  remote: RemoteFrame | null
  annotations: Annotation[]
}) {
  const videoRef = useVideoRef(myStream)
  const [vidAspect, setVidAspect] = useState(16 / 10)
  const aspect = myStream ? vidAspect : remote ? remote.w / Math.max(1, remote.h) : 16 / 10
  const { ref, box } = useFitBox(aspect)

  return (
    <div className="screen-stage" ref={ref}>
      <div className="screen-fit" style={{ width: box.w, height: box.h }}>
        {myStream ? (
          <video
            ref={videoRef}
            autoPlay
            playsInline
            muted
            onLoadedMetadata={(e) => {
              const v = e.currentTarget
              if (v.videoWidth && v.videoHeight) setVidAspect(v.videoWidth / v.videoHeight)
            }}
            onResize={(e) => {
              const v = e.currentTarget
              if (v.videoWidth && v.videoHeight) setVidAspect(v.videoWidth / v.videoHeight)
            }}
          />
        ) : remote ? (
          <img src={`data:image/jpeg;base64,${remote.jpeg}`} alt={`${remote.user}'s screen`} />
        ) : null}
        <Overlay annotations={annotations} />
      </div>
      {!myStream && !remote && <div className="board-placeholder">No one is sharing</div>}
    </div>
  )
}

/** Capture ~1 fps JPEG frames (max 1280 wide, quality 0.6) from a display stream. */
export function startFrameSender(stream: MediaStream, send: (f: { jpeg: string; w: number; h: number }) => void) {
  const video = document.createElement('video')
  video.muted = true
  video.playsInline = true
  video.srcObject = stream
  void video.play().catch(() => undefined)
  const canvas = document.createElement('canvas')
  const ctx = canvas.getContext('2d')
  let lastSize = 0
  const id = window.setInterval(() => {
    if (!ctx || !video.videoWidth) return
    const scale = Math.min(1, 1280 / video.videoWidth)
    canvas.width = Math.round(video.videoWidth * scale)
    canvas.height = Math.round(video.videoHeight * scale)
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height)
    let q = 0.6
    let url = canvas.toDataURL('image/jpeg', q)
    // Keep frames under ~200 KB of base64.
    while (url.length > 270_000 && q > 0.25) {
      q -= 0.15
      url = canvas.toDataURL('image/jpeg', q)
    }
    lastSize = url.length
    send({ jpeg: url.slice(url.indexOf(',') + 1), w: canvas.width, h: canvas.height })
  }, 1000)
  return () => {
    window.clearInterval(id)
    video.srcObject = null
    void lastSize
  }
}
