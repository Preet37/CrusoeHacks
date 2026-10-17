import { useEffect, useState } from 'react'
import type { SideTab } from './SidePanel'
import {
  BoardIcon, CamIcon, CamOffIcon, CaptionsIcon, HandIcon, LeaveIcon, MicIcon, MicOffIcon,
  PeopleIcon, SparkIcon, SpeakerIcon, SpeakerOffIcon, TalkIcon,
} from './Icons'

export interface ControlBarProps {
  room: string
  micOn: boolean
  camOn: boolean
  captionsOn: boolean
  boardOn: boolean
  boardHasContent: boolean
  handUp: boolean
  pttActive: boolean
  twinMuted: boolean
  speechSupported: boolean
  side: SideTab | null
  onMic(): void
  onCam(): void
  onCaptions(): void
  onBoard(): void
  onHand(): void
  onPtt(active: boolean): void
  onTwinMute(): void
  onSide(tab: SideTab): void
  onLeave(): void
}

function Clock() {
  const [t, setT] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setT(new Date()), 10_000)
    return () => window.clearInterval(id)
  }, [])
  return <span className="clock">{t.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</span>
}

export default function ControlBar(p: ControlBarProps) {
  return (
    <div className="controls">
      <div className="left">
        <Clock />
        <span>·</span>
        <span>{p.room}</span>
      </div>
      <div className="mid glass">
        <button
          className={`pill icon ${p.micOn ? '' : 'off'}`}
          title={!p.speechSupported ? 'Speech recognition needs Chrome' : p.micOn ? 'Mute (Twin stops hearing you)' : 'Unmute'}
          onClick={p.onMic}
        >
          {p.micOn ? <MicIcon /> : <MicOffIcon />}
        </button>
        <button className={`pill icon ${p.camOn ? '' : 'off'}`} title={p.camOn ? 'Turn off camera' : 'Turn on camera'} onClick={p.onCam}>
          {p.camOn ? <CamIcon /> : <CamOffIcon />}
        </button>
        <button
          className={`pill icon ptt ${p.pttActive ? 'on' : ''}`}
          title="Hold to talk to Twin (or hold Space)"
          onPointerDown={(e) => {
            e.currentTarget.setPointerCapture(e.pointerId)
            p.onPtt(true)
          }}
          onPointerUp={() => p.onPtt(false)}
          onPointerCancel={() => p.onPtt(false)}
        >
          <TalkIcon />
        </button>
        <span className="sep" />
        <button
          className={`pill icon ${p.boardOn ? 'on' : ''}`}
          title={p.boardOn ? 'Hide whiteboard' : p.boardHasContent ? 'Show whiteboard' : 'Show whiteboard (empty)'}
          onClick={p.onBoard}
        >
          <BoardIcon />
        </button>
        <button className={`pill icon ${p.captionsOn ? 'on' : ''}`} title="Captions" onClick={p.onCaptions}>
          <CaptionsIcon />
        </button>
        <button className={`pill icon ${p.handUp ? 'on' : ''}`} title="Raise hand" onClick={p.onHand}>
          <HandIcon />
        </button>
        <button className={`pill icon ${p.twinMuted ? 'off' : ''}`} title={p.twinMuted ? 'Unmute Twin' : 'Mute Twin'} onClick={p.onTwinMute}>
          {p.twinMuted ? <SpeakerOffIcon /> : <SpeakerIcon />}
        </button>
        {/* Narrow windows hide the right-hand group, so keep one panel toggle here. */}
        <button
          className={`pill icon narrow-only ${p.side ? 'on' : ''}`}
          title="People, Twin activity, tasks, connections"
          onClick={() => p.onSide(p.side ?? 'activity')}
        >
          <PeopleIcon />
        </button>
        <span className="sep" />
        <button className="pill danger" title="Leave call" onClick={p.onLeave} style={{ width: 56, height: 44 }}>
          <LeaveIcon />
        </button>
      </div>
      <div className="right">
        <button className={`pill icon ${p.side === 'people' ? 'on' : ''}`} title="People" onClick={() => p.onSide('people')}>
          <PeopleIcon />
        </button>
        <button className={`pill icon ${p.side === 'activity' ? 'on' : ''}`} title="Twin activity" onClick={() => p.onSide('activity')}>
          <SparkIcon />
        </button>
      </div>
    </div>
  )
}
