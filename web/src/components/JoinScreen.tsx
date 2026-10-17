import { useState } from 'react'
import { useLocalMedia, useVideoRef } from '../lib/media'
import { CamIcon, CamOffIcon, MicIcon, MicOffIcon } from './Icons'
import { TwinAvatar } from './ParticipantTile'

export interface JoinChoice {
  name: string
  room: string
  cam: boolean
  mic: boolean
}

const NAME_KEY = 'twin.name'
function savedName() {
  try {
    return localStorage.getItem(NAME_KEY) ?? ''
  } catch {
    return ''
  }
}

export default function JoinScreen({ room, onJoin }: { room: string; onJoin: (c: JoinChoice) => void }) {
  const [name, setName] = useState(savedName)
  const [roomName, setRoomName] = useState(room)
  const [copied, setCopied] = useState(false)
  const cleanRoom = roomName.trim().toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '') || 'demo'
  const inviteLink = `${location.origin}/room/${encodeURIComponent(cleanRoom)}${location.search}`
  const copyInvite = async () => {
    try {
      await navigator.clipboard.writeText(inviteLink)
    } catch {
      window.prompt('Copy this invite link', inviteLink)
    }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1800)
  }
  const media = useLocalMedia({ cam: true, mic: true })
  const videoRef = useVideoRef(media.stream)
  const hasVideo = media.camOn && !!media.stream?.getVideoTracks().length

  const join = () => {
    const n = name.trim()
    if (!n) return
    try {
      localStorage.setItem(NAME_KEY, n)
    } catch {
      /* private mode */
    }
    // Release devices; the meeting room acquires its own.
    media.stream?.getTracks().forEach((t) => t.stop())
    onJoin({ name: n, room: cleanRoom, cam: media.camOn, mic: media.micOn })
  }

  return (
    <div className="join">
      <div className="join-card glass">
        <div>
          <div className="join-preview">
            {hasVideo ? (
              <video ref={videoRef} autoPlay playsInline muted />
            ) : (
              <div className="preview-off">{media.error ?? 'Camera is off'}</div>
            )}
            <div className="preview-controls">
              <button
                className={`pill icon ${media.micOn ? '' : 'off'}`}
                title={media.micOn ? 'Turn off microphone' : 'Turn on microphone'}
                onClick={() => media.setMicOn(!media.micOn)}
              >
                {media.micOn ? <MicIcon /> : <MicOffIcon />}
              </button>
              <button
                className={`pill icon ${media.camOn ? '' : 'off'}`}
                title={media.camOn ? 'Turn off camera' : 'Turn on camera'}
                onClick={() => media.setCamOn(!media.camOn)}
              >
                {media.camOn ? <CamIcon /> : <CamOffIcon />}
              </button>
            </div>
          </div>
          <div className="meter" style={{ marginTop: 10 }} title="Mic level">
            <i style={{ width: `${Math.round(media.level * 100)}%` }} />
          </div>
        </div>
        <div className="join-side">
          <h1>Ready to join?</h1>
          <div className="row" style={{ display: 'flex', gap: 8 }}>
            <input
              className="field"
              style={{ flex: 1 }}
              aria-label="Room"
              value={roomName}
              onChange={(e) => setRoomName(e.target.value)}
            />
            <button className="pill" onClick={copyInvite} title={inviteLink}>
              {copied ? 'Copied ✓' : 'Copy invite link'}
            </button>
          </div>
          <div className="twin-chip">
            <TwinAvatar />
            <div>
              <div style={{ fontWeight: 600 }}>Twin is in this call</div>
              <div className="hint">AI teammate. Listens, talks, and draws on the whiteboard.</div>
            </div>
          </div>
          <input
            className="field"
            placeholder="Your name"
            value={name}
            autoFocus
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && join()}
          />
          <button className="pill primary" disabled={!name.trim()} onClick={join}>
            Join now
          </button>
          <div className="hint">Use Chrome for speech recognition. Wear headphones so Twin doesn't hear itself.</div>
        </div>
      </div>
    </div>
  )
}
