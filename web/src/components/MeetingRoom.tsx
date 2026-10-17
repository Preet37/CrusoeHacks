// The call: tile grid (or whiteboard stage + tile strip), captions, control bar, side panel.
import { useEffect, useMemo, useRef, useState } from 'react'
import { useCallAudio } from '../lib/callAudio'
import { useLocalMedia } from '../lib/media'
import { useMeeting, type Person } from '../lib/meetingState'
import type { TwinClient } from '../lib/ws'
import Captions from './Captions'
import ControlBar, { type TwinMode } from './ControlBar'
import { CloseIcon } from './Icons'
import { Whiteboard } from './optional'
import { HumanTile, TwinTile } from './ParticipantTile'
import ScreenStage, { startFrameSender } from './ScreenStage'
import SidePanel, { type SideTab } from './SidePanel'
import TwinTake from './TwinTake'
import type { WorkspaceSession } from '../lib/workspace'
import { serverHttpBase } from '../lib/ws'

const SPEAKING_MS = 1500

export default function MeetingRoom({
  client,
  user,
  initialCam,
  initialMic,
  onLeave,
  session,
}: {
  session?: WorkspaceSession | null
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
  // Twin mode (judge feedback: users control when Twin speaks/draws). Default: only when asked.
  const [twinMode, setTwinModeState] = useState<TwinMode>(() => {
    try {
      return (localStorage.getItem('twin.mode') as TwinMode) || 'on_request'
    } catch {
      return 'on_request'
    }
  })
  const setTwinMode = (mode: TwinMode) => {
    setTwinModeState(mode)
    try {
      localStorage.setItem('twin.mode', mode)
    } catch {
      /* ignore */
    }
    client.send({ type: 'control', action: 'set_mode', mode })
  }
  // Tell the server on join (and after reconnects).
  useEffect(() => {
    const send = () => client.send({ type: 'control', action: 'set_mode', mode: twinMode })
    if (client.state === 'open') send()
    return client.onState((st) => st === 'open' && send())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client])
  // "Paste a spec": a paragraph -> Twin draws a deep, layered diagram in one go.
  const [specOpen, setSpecOpen] = useState(false)
  const [spec, setSpec] = useState('')
  const sendSpec = () => {
    const text = spec.trim().replace(/\s+/g, ' ')
    if (!text) return
    client.send({
      type: 'user_text',
      speaker: user,
      text: `Twin, draw this spec as a detailed layered architecture diagram: ${text}`,
      final: true,
      ts: Date.now(),
    })
    setSpec('')
    setSpecOpen(false)
    setBoardPinned(true)
  }
  // Workspace repo (for source links in Twin's take).
  const [repoUrl, setRepoUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!session) return
    const q = new URLSearchParams({ workspace: session.workspace, code: session.code })
    fetch(`${serverHttpBase()}/workspace?${q}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d?.repo_url && setRepoUrl(d.repo_url))
      .catch(() => undefined)
  }, [session])
  const [takeHidden, setTakeHidden] = useState<string | null>(null)
  const [myScreen, setMyScreen] = useState<MediaStream | null>(null)
  const [stageTab, setStageTab] = useState<'board' | 'screen'>('board')
  const [screenErr, setScreenErr] = useState<string | null>(null)
  /** Server rejected screen_frame (older server): keep sharing locally, stop sending frames. */
  const framesUnsupported = useRef(false)
  const [, tick] = useState(0)

  const audio = useCallAudio({
    client,
    user,
    micOn: media.micOn,
    twinMuted: m.twinMuted || twinMode === 'off',
    onInterim: (t) => m.addLocalInterim(user, t),
    onFinal: () => m.addLocalInterim(user, ''),
  })

  // Screen share (RC-18): local capture + ~1 fps JPEG frames to the room.
  const stopShare = () => {
    myScreen?.getTracks().forEach((t) => t.stop())
    setMyScreen(null)
    client.send({ type: 'screen_stop', user })
  }
  const startShare = async () => {
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 5 }, audio: false })
      setMyScreen(stream)
      setStageTab('screen')
      setScreenErr(null)
    } catch (e) {
      if ((e as Error).name !== 'NotAllowedError') setScreenErr((e as Error).message)
    }
  }
  useEffect(() => {
    if (!myScreen) return
    const track = myScreen.getVideoTracks()[0]
    const onEnded = () => {
      setMyScreen(null)
      client.send({ type: 'screen_stop', user })
    }
    track?.addEventListener('ended', onEnded)
    const stop = startFrameSender(myScreen, (f) => {
      if (!framesUnsupported.current) client.send({ type: 'screen_frame', user, ...f, ts: Date.now() })
    })
    const offErr = client.on('error', (msg) => {
      if (/screen_frame/.test(String((msg as { message?: string }).message))) framesUnsupported.current = true
    })
    return () => {
      stop()
      offErr()
      track?.removeEventListener('ended', onEnded)
    }
  }, [myScreen, client, user])
  // Someone else started sharing: show their screen.
  const remoteSharing = !!m.remoteScreen
  useEffect(() => {
    if (remoteSharing) setStageTab('screen')
  }, [remoteSharing])

  // Server errors show as a toast for 5 s.
  const [toast, setToast] = useState<string | null>(null)
  useEffect(() => {
    if (!m.lastError) return
    // Older servers don't know set_mode yet; that's not worth a toast.
    if (/set_mode/.test(m.lastError)) return
    setToast(m.lastError)
    const id = window.setTimeout(() => setToast(null), 5000)
    return () => window.clearTimeout(id)
  }, [m.lastError, m.lines.length])

  // Re-render periodically so "speaking" rings fade out.
  useEffect(() => {
    const id = window.setInterval(() => tick((n) => n + 1), 500)
    return () => window.clearInterval(id)
  }, [])

  // Re-open the brief whenever a new one arrives.
  useEffect(() => {
    if (!m.brief) return
    setBriefOpen(true)
    // The brief is a glance, not a panel: it tucks itself away after 15 s.
    const id = window.setTimeout(() => setBriefOpen(false), 15_000)
    return () => window.clearTimeout(id)
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

  const screenActive = !!myScreen || remoteSharing
  const boardPresenting = boardPinned ?? boardHasContent
  const presenting = boardPresenting || screenActive
  // Sharing never opens the board; the board never hides an active share.
  const tab: 'board' | 'screen' = !screenActive ? 'board' : !boardPresenting ? 'screen' : stageTab
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
        {screenActive && (
          <span className="chip presenting">{myScreen ? 'You are presenting' : `${m.remoteScreen?.user} is presenting`}</span>
        )}
        {!screenActive && presenting && boardHasContent && <span className="chip presenting">Twin is presenting</span>}
        {screenErr && <span className="chip" style={{ color: 'var(--danger)' }}>{screenErr}</span>}
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
                {screenActive && boardPresenting && (
                  <span className="seg">
                    <button className={tab === 'screen' ? 'on' : ''} onClick={() => setStageTab('screen')}>Screen</button>
                    <button className={tab === 'board' ? 'on' : ''} onClick={() => setStageTab('board')}>Whiteboard</button>
                  </span>
                )}
                {tab === 'board' && (
                  <button className="pill small" onClick={() => client.send({ type: 'control', action: 'clear_board' })}>
                    Clear board
                  </button>
                )}
                {tab === 'screen' && myScreen && (
                  <button className="pill small danger" onClick={stopShare}>Stop sharing</button>
                )}
                {tab === 'board' && (
                  <button className="pill small" title="Back to tiles" onClick={() => setBoardPinned(false)}>
                    <CloseIcon />
                  </button>
                )}
              </div>
              <div className={`stage-layer ${tab === 'board' ? '' : 'off'}`}>
                <Whiteboard client={client} onHasContent={(b: boolean) => setBoardHasContent(b)} />
              </div>
              {screenActive && (
                <div className={`stage-layer ${tab === 'screen' ? '' : 'off'}`}>
                  <ScreenStage myStream={myScreen} remote={myScreen ? null : m.remoteScreen} annotations={m.annotations} />
                </div>
              )}
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
          {toast && <div className="toast glass">{toast}</div>}
          {m.contribution && takeHidden !== m.contribution.id && (
            <div className={`take-float ${presenting ? 'on-stage' : ''}`}>
              <TwinTake
                key={m.contribution.id}
                c={m.contribution}
                repoUrl={repoUrl}
                startCollapsed={presenting}
                onClose={() => setTakeHidden(m.contribution!.id)}
              />
            </div>
          )}
          {specOpen && (
            <div className="spec glass">
              <div className="dash-title">Paste a spec</div>
              <div className="hint">One paragraph describing a system. Twin draws it as a layered diagram.</div>
              <textarea
                className="field"
                autoFocus
                rows={6}
                placeholder="e.g. A mobile app calls an API gateway, which routes to auth, orders and payments services..."
                value={spec}
                onChange={(e) => setSpec(e.target.value)}
                onKeyDown={(e) => (e.metaKey || e.ctrlKey) && e.key === 'Enter' && sendSpec()}
              />
              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                <button className="pill small" onClick={() => setSpecOpen(false)}>Cancel</button>
                <button className="pill small on" disabled={!spec.trim()} onClick={sendSpec}>Draw it (⌘↵)</button>
              </div>
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
            contribution={m.contribution}
            repoUrl={repoUrl}
          />
      </div>

      <ControlBar
        room={client.room}
        micOn={media.micOn}
        camOn={media.camOn}
        captionsOn={captionsOn}
        boardOn={boardPresenting && (!screenActive || tab === 'board')}
        sharing={!!myScreen}
        twinMode={twinMode}
        specOpen={specOpen}
        onSpec={() => setSpecOpen((o) => !o)}
        onTwinMode={setTwinMode}
        onShare={() => (myScreen ? stopShare() : void startShare())}
        boardHasContent={boardHasContent}
        handUp={handUp}
        pttActive={ptt}
        twinMuted={m.twinMuted}
        speechSupported={audio.speechSupported}
        side={side}
        onMic={() => media.setMicOn(!media.micOn)}
        onCam={() => media.setCamOn(!media.camOn)}
        onCaptions={() => setCaptionsOn((c) => !c)}
        onBoard={() => {
          if (screenActive && boardPresenting && tab !== 'board') return setStageTab('board')
          setBoardPinned(!boardPresenting)
          if (!boardPresenting) setStageTab('board')
        }}
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
