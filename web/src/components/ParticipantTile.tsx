import { Component, Suspense, lazy, type ReactNode } from 'react'
import type { StatusState } from '../lib/protocol'
import { avatarColor, initials, useVideoRef } from '../lib/media'
import { MicOffIcon } from './Icons'

export function TwinAvatar({ className = '' }: { className?: string }) {
  return (
    <div className={`twin-avatar ${className}`}>
      <span className="ring" />
      <span className="ring r2" />
      <span className="ring r3" />
    </div>
  )
}

const QUIET_S = 30

// 3D orb is lazy-loaded (three.js is big); the CSS avatar shows while it loads or if WebGL fails.
const TwinOrb = lazy(() => import('./TwinOrb'))
class OrbBoundary extends Component<{ fallback: ReactNode; children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() {
    return { failed: true }
  }
  render() {
    return this.state.failed ? this.props.fallback : this.props.children
  }
}
function hasWebGL() {
  try {
    const c = document.createElement('canvas')
    return !!(c.getContext('webgl2') || c.getContext('webgl'))
  } catch {
    return false
  }
}
const WEBGL = typeof document !== 'undefined' && hasWebGL()

export function TwinTile({
  status,
  muted,
  sketching,
  handRaisedAt,
  onLetSpeak,
}: {
  status: StatusState
  muted: boolean
  sketching?: boolean
  handRaisedAt?: number | null
  onLetSpeak?: () => void
}) {
  const quietLeft = handRaisedAt ? Math.max(0, QUIET_S - Math.floor((Date.now() - handRaisedAt) / 1000)) : 0
  let pill: React.ReactNode
  if (muted) pill = <span className="tw-pill muted">Muted · won't speak</span>
  else if (status === 'speaking') pill = <span className="tw-pill speaking"><span className="eq"><i /><i /><i /></span>Speaking</span>
  else if (status === 'hand_raised')
    pill = (
      <button className="tw-pill add" onClick={onLetSpeak} title="Twin is holding back. Click to let it speak now.">
        ✋ Has something to add{quietLeft > 0 ? ` · quiet ${quietLeft}s` : ''} <b>Let it speak</b>
      </button>
    )
  else if (sketching) pill = <span className="tw-pill sketch"><span className="pen">✎</span>Sketching on the board…</span>
  else if (status === 'thinking') pill = <span className="tw-pill thinking"><span className="typing"><i /><i /><i /></span>Thinking</span>
  else pill = <span className="tw-pill">Listening</span>

  return (
    <div className={`tile twin ${status} ${status === 'speaking' ? 'speaking' : ''} ${sketching ? 'sketching' : ''}`}>
      {WEBGL ? (
        <div className="twin-orb">
          <OrbBoundary fallback={<TwinAvatar />}>
            <Suspense fallback={<TwinAvatar />}>
              <TwinOrb mode={muted ? 'listening' : sketching && status !== 'speaking' ? 'sketching' : status} />
            </Suspense>
          </OrbBoundary>
        </div>
      ) : (
        <TwinAvatar />
      )}
      <div className="tw-pills">{pill}</div>
      <div className="name glass">
        Twin <span className="badge">AI</span>
      </div>
    </div>
  )
}

export function HumanTile({
  name,
  speaking,
  stream,
  camOn,
  micOn,
  me,
}: {
  name: string
  speaking: boolean
  stream?: MediaStream | null
  camOn?: boolean
  micOn?: boolean
  me?: boolean
}) {
  const videoRef = useVideoRef(stream ?? null)
  const showVideo = !!stream && camOn && stream.getVideoTracks().length > 0
  return (
    <div className={`tile ${speaking ? 'speaking' : ''}`}>
      {showVideo ? (
        <video ref={videoRef} autoPlay playsInline muted />
      ) : (
        <div className="avatar" style={{ background: avatarColor(name) }}>
          {initials(name)}
        </div>
      )}
      <div className="name glass">
        {me && micOn === false && (
          <span className="muted-ic"><MicOffIcon /></span>
        )}
        {name}
        {me ? ' (you)' : ''}
      </div>
    </div>
  )
}
