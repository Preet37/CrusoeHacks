// /dashboard: the workspace home outside a call. Upcoming meetings (join / auto-join), connections,
// recent meetings and Twin's learning. Not the product's main feature: the call is.
import { useEffect, useMemo, useState } from 'react'
import type { Participant } from '../lib/protocol'
import { avatarColor, initials } from '../lib/media'
import { meetingHistory, scopedRoom, signOut, slug, type WorkspaceSession } from '../lib/workspace'
import { createClient, serverHttpBase, type TwinClient } from '../lib/ws'
import { Scoreboard } from './optional'
import { TwinAvatar } from './ParticipantTile'

interface CalMeeting {
  title: string
  scheduled: string
  duration_min?: number
  attendees?: string[]
  predictions?: string[]
}

const AUTO_KEY = 'twin.autojoin'
function loadAuto(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(AUTO_KEY) || '{}')
  } catch {
    return {}
  }
}

/** "13:30" today -> ms. */
function todayAt(hhmm: string) {
  const [h, m] = hhmm.split(':').map(Number)
  const d = new Date()
  d.setHours(h || 0, m || 0, 0, 0)
  return d.getTime()
}

function fmtTime(hhmm: string) {
  return new Date(todayAt(hhmm)).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

export default function Dashboard({
  session,
  onOpenRoom,
  onSignOut,
}: {
  session: WorkspaceSession
  onOpenRoom: (room: string, briefFor?: string) => void
  onSignOut: () => void
}) {
  const [meetings, setMeetings] = useState<CalMeeting[] | null>(null)
  const [calErr, setCalErr] = useState<string | null>(null)
  const [auto, setAuto] = useState(loadAuto)
  const [lobby, setLobby] = useState<TwinClient | null>(null)
  const [online, setOnline] = useState<Participant[]>([])
  const [repo, setRepo] = useState('')
  const [repoStatus, setRepoStatus] = useState<string | null>(null)
  const [newRoom, setNewRoom] = useState('')
  const [, tick] = useState(0)
  const history = useMemo(() => meetingHistory(), [])

  // Calendar from the server.
  useEffect(() => {
    fetch(`${serverHttpBase()}/calendar`)
      .then((r) => r.json())
      .then((d) => setMeetings(d.meetings ?? []))
      .catch(() => {
        setMeetings([])
        setCalErr("Couldn't reach the Twin server")
      })
  }, [])

  // A lobby connection: who's online in the workspace, Twin's scores, and repo connect.
  useEffect(() => {
    const c = createClient({ room: scopedRoom(session, 'lobby'), user: session.name, workspace: session.workspace, code: session.code })
    setLobby(c)
    const offP = c.on('participants', (m) => setOnline(m.people.filter((p) => p.kind === 'human')))
    const offE = c.on('error', (m) => {
      if (/repo/i.test(m.message)) setRepoStatus(m.message)
    })
    const offT = c.on('transcript', (m) => {
      if (m.speaker === 'Twin' && /repo|files|indexed|code map/i.test(m.text)) setRepoStatus(m.text)
    })
    return () => {
      offP()
      offE()
      offT()
      c.close()
      setLobby(null)
    }
  }, [session])

  // Auto-join: when a toggled meeting's start time arrives (within its first 5 minutes), jump in.
  useEffect(() => {
    const id = window.setInterval(() => {
      tick((n) => n + 1)
      const now = Date.now()
      for (const m of meetings ?? []) {
        const at = todayAt(m.scheduled)
        if (auto[m.title] && now >= at && now - at < 5 * 60_000) {
          onOpenRoom(slug(m.title), m.title)
          return
        }
      }
    }, 5000)
    return () => window.clearInterval(id)
  }, [meetings, auto, onOpenRoom])

  const toggleAuto = (title: string) => {
    const next = { ...auto, [title]: !auto[title] }
    setAuto(next)
    try {
      localStorage.setItem(AUTO_KEY, JSON.stringify(next))
    } catch {
      /* ignore */
    }
  }

  const validRepo = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/?$/.test(repo.trim())
  const now = Date.now()
  const upcoming = (meetings ?? []).slice().sort((a, b) => todayAt(a.scheduled) - todayAt(b.scheduled))

  return (
    <div className="dash">
      <header className="dash-head">
        <div className="dash-brand">
          <TwinAvatar />
          <div>
            <div className="dash-ws">{session.workspace}</div>
            <div className="hint">Signed in as {session.name}</div>
          </div>
        </div>
        <div className="dash-online">
          {online.slice(0, 6).map((p) => (
            <span key={p.name} className="mini" title={p.name} style={{ background: avatarColor(p.name) }}>
              {initials(p.name)}
            </span>
          ))}
          <span className="hint">{online.length} online</span>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <input
            className="field"
            style={{ height: 36, width: 160 }}
            placeholder="New room name"
            value={newRoom}
            onChange={(e) => setNewRoom(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && onOpenRoom(slug(newRoom, 'standup'))}
          />
          <button className="pill primary" style={{ height: 36 }} onClick={() => onOpenRoom(slug(newRoom, 'standup'))}>
            Start meeting
          </button>
          <button
            className="pill"
            onClick={() => {
              signOut()
              onSignOut()
            }}
          >
            Sign out
          </button>
        </div>
      </header>

      <main className="dash-grid">
        <section className="dash-card glass">
          <h2>Upcoming</h2>
          {meetings === null && <div className="empty">Loading calendar…</div>}
          {calErr && <div className="empty">{calErr}</div>}
          {upcoming.map((m) => {
            const at = todayAt(m.scheduled)
            const live = now >= at && now - at < (m.duration_min ?? 30) * 60_000
            return (
              <div key={m.title} className="dash-row">
                <div className="dash-time">{fmtTime(m.scheduled)}</div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="dash-title">
                    {m.title} {live && <span className="tag-live">LIVE</span>}
                  </div>
                  {m.predictions?.[0] && <div className="hint ellipsis">Twin expects: {m.predictions[0]}</div>}
                </div>
                <label className="switch" title="Twin joins automatically at start time">
                  <input type="checkbox" checked={!!auto[m.title]} onChange={() => toggleAuto(m.title)} />
                  <span />
                  Auto-join
                </label>
                <button className="pill small on" onClick={() => onOpenRoom(slug(m.title), m.title)}>
                  Join
                </button>
              </div>
            )
          })}
          <div className="hint" style={{ marginTop: 8 }}>Calendar is demo data from the Twin server.</div>
        </section>

        <section className="dash-card glass">
          <h2>Connections</h2>
          <div className="conn-card">
            <div className="head">
              <span className="logo" style={{ background: '#24292f' }}>GH</span>GitHub
              <span className="tag live">LIVE</span>
            </div>
            <p>Connect a public repo so Twin can answer "what does this change touch?"</p>
            <div className="row">
              <input className="field" placeholder="https://github.com/owner/repo" value={repo} onChange={(e) => setRepo(e.target.value)} />
              <button
                className="pill small on"
                disabled={!validRepo || !lobby}
                onClick={() => {
                  lobby?.send({ type: 'connect_repo', url: repo.trim().replace(/\/$/, '') })
                  setRepoStatus('Sent to Twin, indexing…')
                }}
              >
                Connect
              </button>
            </div>
            {repoStatus && <div className="hint">{repoStatus}</div>}
          </div>
          {[
            ['Jira', '#0052cc', 'J'],
            ['Slack', '#611f69', 'S'],
            ['Google Calendar', '#1a73e8', '31'],
          ].map(([n, c, l]) => (
            <div key={n} className="conn-card">
              <div className="head">
                <span className="logo" style={{ background: c }}>{l}</span>
                {n}
                <span className="tag">COMING SOON</span>
              </div>
              <button className="pill small" disabled>
                Connect
              </button>
            </div>
          ))}
        </section>

        <section className="dash-card glass">
          <h2>Recent meetings</h2>
          {history.length === 0 && <div className="empty">Meetings you join show up here.</div>}
          {history.map((h) => (
            <div key={`${h.room}_${h.at}`} className="dash-row">
              <div className="dash-time">{new Date(h.at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</div>
              <div style={{ flex: 1 }} className="dash-title">
                {h.title}
              </div>
              <button className="pill small" onClick={() => onOpenRoom(h.room)}>
                Rejoin
              </button>
            </div>
          ))}
          <div className="hint" style={{ marginTop: 8 }}>From this browser.</div>
        </section>

        <section className="dash-card glass">
          <h2>Twin's learning</h2>
          {lobby ? <Scoreboard client={lobby} /> : <div className="empty">Connecting…</div>}
        </section>
      </main>
    </div>
  )
}
