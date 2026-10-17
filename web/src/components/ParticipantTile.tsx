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

const STATE_LABEL: Record<StatusState, string> = {
  listening: 'Listening',
  thinking: 'Thinking',
  speaking: 'Speaking',
  hand_raised: 'Hand raised',
}

export function TwinTile({ status, muted }: { status: StatusState; muted: boolean }) {
  return (
    <div className={`tile twin ${status} ${status === 'speaking' ? 'speaking' : ''}`}>
      <TwinAvatar />
      <div className="state glass">
        {status === 'thinking' ? (
          <span className="typing"><i /><i /><i /></span>
        ) : status === 'hand_raised' ? (
          <span className="hand" role="img" aria-label="hand raised">✋</span>
        ) : null}
        {muted ? 'Muted' : STATE_LABEL[status]}
      </div>
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
