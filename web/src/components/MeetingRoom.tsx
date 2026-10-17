// The call: tile grid (or whiteboard stage + tile strip), captions, control bar, side panel.
import { useEffect, useMemo, useRef, useState } from 'react'
import { useCallAudio } from '../lib/callAudio'
import { useLocalMedia } from '../lib/media'
import { useMeeting, type Person } from '../lib/meetingState'
import type { TwinClient } from '../lib/ws'
import Captions from './Captions'
import ControlBar from './ControlBar'
import { CloseIcon } from './Icons'
import { Whiteboard } from './optional'
import { HumanTile, TwinTile } from './ParticipantTile'
import SidePanel, { type SideTab } from './SidePanel'

const SPEAKING_MS = 1500

export default function MeetingRoom({
  client,
  user,
  initialCam,
  initialMic,
  onLeave,
}: {
  client: TwinClient
  user: string
  initialCam: boolean
  initialMic: boolean
  onLeave(): void
}) {
  const m = useMeeting(client)
  const media = useLocalMedia({ cam: initialCam, mic: initialMic })
  const [captionsOn, setCaptionsOn] = useState(true)
  const [boardHasContent, setBoardHasContent] = useState(false)
  /** null = follow the board automatically (present when it has content). */
  const [boardPinned, setBoardPinned] = useState<boolean | null>(null)
  const [handUp, setHandUp] = useState(false)
  const [ptt, setPtt] = useState(false)
  const [side, setSide] = useState<SideTab | null>(null)
  const [briefOpen, setBriefOpen] = useState(true)
  const [, tick] = useState(0)

  const audio = useCallAudio({
    client,
    user,
    micOn: media.micOn,
    twinMuted: m.twinMuted,
    onInterim: (t) => m.addLocalInterim(user, t),
    onFinal: () => m.addLocalInterim(user, ''),
  })

  // Re-render periodically so "speaking" rings fade out.
  useEffect(() => {
    const id = window.setInterval(() => tick((n) => n + 1), 500)
    return () => window.clearInterval(id)
  }, [])

  // Re-open the brief whenever a new one arrives.
  useEffect(() => {
    if (m.brief) setBriefOpen(true)
  }, [m.brief])

  // Hold Space = push-to-talk (ignored while typing in a field).
  const pttRef = useRef(false)
  const setPushToTalk = (active: boolean) => {
    if (pttRef.current === active) return
    pttRef.current = active
    setPtt(active)
    client.send({ type: 'push_to_talk', active, ts: Date.now() })
  }
  useEffect(() => {
    const typing = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      return !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable || !!t.closest('.tl-container'))
    }
    const down = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || e.repeat || typing(e)) return
      e.preventDefault()
      setPushToTalk(true)
    }
    const up = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || typing(e)) return
      setPushToTalk(false)
    }
    const blur = () => setPushToTalk(false)
    window.addEventListener('keydown', down)
    window.addEventListener('keyup', up)
    window.addEventListener('blur', blur)
    return () => {
      window.removeEventListener('keydown', down)
      window.removeEventListener('keyup', up)
      window.removeEventListener('blur', blur)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client])

  // People: server `participants` if we have it, else me + Twin + anyone heard in the transcript.
  const now = Date.now()
  const people: Person[] = useMemo(() => {
    const byName = new Map<string, Person>()
    for (const p of m.people) byName.set(p.name, p)
    if (!byName.has(user)) byName.set(user, { name: user, kind: 'human', speaking: false })
    if (!byName.has('Twin')) byName.set('Twin', { name: 'Twin', kind: 'ai', speaking: false })
    // Transcript speakers are only a fallback until the server sends an authoritative participant list.
    if (!m.people.length)
      for (const l of m.lines) if (!byName.has(l.speaker)) byName.set(l.speaker, { name: l.speaker, kind: 'human', speaking: false })
    return [...byName.values()]
  }, [m.people, m.lines, user])
  const isSpeaking = (p: Person) =>
    p.speaking || (m.lastSpoke[p.name] !== undefined && now - m.lastSpoke[p.name] < SPEAKING_MS) || !!m.interim[p.name]

  const presenting = boardPinned ?? boardHasContent
  const humans = people.filter((p) => p.kind === 'human')

  const tiles = (
    <>
      {humans.map((p) =>
        p.name === user ? (
          <HumanTile key={p.name} name={p.name} me speaking={isSpeaking(p)} stream={media.stream} camOn={media.camOn} micOn={media.micOn} />
        ) : (
          <HumanTile key={p.name} name={p.name} speaking={isSpeaking(p)} />
        ),
      )}
      <TwinTile
        key="Twin"
        status={m.twinMuted ? 'listening' : m.twinStatus}
        muted={m.twinMuted}
        sketching={m.sketching.length > 0}
        handRaisedAt={m.handRaisedAt}
        onLetSpeak={() =>
          // Turn-taking answers when addressed by name, which also ends a "not now" hold.
          client.send({ type: 'user_text', speaker: user, text: 'Twin, go ahead.', final: true, ts: Date.now() })
        }
      />
    </>
  )

  return (
    <div className="call">
      <div className="topbar">
        <span className="room">{client.room}</span>
        <span className="chip">
          <span className={`dot ${m.conn}`} />
          {client.mock ? 'Mock server' : m.conn === 'open' ? 'Connected' : m.conn === 'connecting' ? 'Connecting…' : 'Reconnecting…'}
        </span>
        {m.topic && <span className="chip">Topic: {m.topic.title}</span>}
        {presenting && boardHasContent && <span className="chip presenting">Twin is presenting</span>}
        <span className="spacer" />
        {audio.speechError && <span className="chip" style={{ color: 'var(--danger)' }}>{audio.speechError}</span>}
        <button
          className="chip"
          style={{ cursor: 'pointer' }}
          title="Speakers mode ignores Twin's own voice coming back through your mic. Headphones mode interrupts Twin faster."
          onClick={() => audio.setMode(audio.mode === 'speakers' ? 'headphones' : 'speakers')}
        >
          {audio.mode === 'speakers' ? '🔈 Speakers (echo guard on)' : '🎧 Headphones'}
        </button>
      </div>

      <div className="main">
        <div className="center">
          {/* The whiteboard stays mounted so it never misses draw ops; hidden when not presenting. */}
          <div className={`stage-wrap ${presenting ? '' : 'hidden'}`}>
            <div className="stage">
              <div className="stage-label">
                <button className="pill small" onClick={() => client.send({ type: 'control', action: 'clear_board' })}>
                  Clear board
                </button>
                <button className="pill small" title="Back to tiles" onClick={() => setBoardPinned(false)}>
                  <CloseIcon />
                </button>
              </div>
              <Whiteboard client={client} onHasContent={(b: boolean) => setBoardHasContent(b)} />
            </div>
            <div className="strip">{presenting && tiles}</div>
          </div>
          {!presenting && <div className="grid">{tiles}</div>}
          {captionsOn && <Captions lines={m.lines} interim={m.interim} />}
          {m.brief && briefOpen && (
            <div className="brief glass">
              <button className="pill icon small x" style={{ width: 24, height: 24 }} onClick={() => setBriefOpen(false)}>
                <CloseIcon />
              </button>
              <h4>Twin's brief: {m.brief.title}</h4>
              <div className="sub">What Twin expects to come up</div>
              <ul>
                {m.brief.predictions.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            </div>
          )}
          {m.lastError && (
            <div className="toast glass" onClick={() => undefined}>
              {m.lastError}
            </div>
          )}
        </div>
        {/* Always mounted so AgentTree/TaskPanel/Scoreboard collect events even while closed. */}
        <SidePanel
            open={side !== null}
            tab={side ?? 'people'}
            onTab={setSide}
            onClose={() => setSide(null)}
            people={people}
            twinStatus={m.twinStatus}
            client={client}
          />
      </div>

      <ControlBar
        room={client.room}
        micOn={media.micOn}
        camOn={media.camOn}
        captionsOn={captionsOn}
        boardOn={presenting}
        boardHasContent={boardHasContent}
        handUp={handUp}
        pttActive={ptt}
        twinMuted={m.twinMuted}
        speechSupported={audio.speechSupported}
        side={side}
        onMic={() => media.setMicOn(!media.micOn)}
        onCam={() => media.setCamOn(!media.camOn)}
        onCaptions={() => setCaptionsOn((c) => !c)}
        onBoard={() => setBoardPinned(!presenting)}
        onHand={() => setHandUp((h) => !h)}
        onPtt={setPushToTalk}
        onTwinMute={() => {
          const next = !m.twinMuted
          m.setTwinMuted(next)
          client.send({ type: 'control', action: next ? 'mute_twin' : 'unmute_twin' })
        }}
        onSide={(t) => setSide((s) => (s === t ? null : t))}
        onLeave={onLeave}
      />
    </div>
  )
}
