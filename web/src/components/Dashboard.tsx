// /dashboard (B5): the company home outside a call. Fewer things, more space:
// Upcoming meetings, past meetings (summary, decisions, to-dos, tickets), the remembered repo, Twin's learning.
// Server data: GET /workspace, /meetings, /tickets (same invite auth as /ws), GET /calendar.
import { useEffect, useState } from 'react'
import { meetingHistory, scopedRoom, signOut, slug, type WorkspaceSession } from '../lib/workspace'
import { createClient, serverHttpBase, type TwinClient } from '../lib/ws'
import { Scoreboard } from './optional'
import { TwinAvatar } from './ParticipantTile'

interface CalMeeting {
  title: string
  scheduled: string
  duration_min?: number
  predictions?: string[]
}
interface Ticket {
  id?: string
  title: string
  agent_prompt?: string
  owner?: string | null
  status?: string
  links?: Record<string, string | null>
}
interface Meeting {
  id: string
  room: string
  title?: string
  summary?: string
  decisions?: string[]
  todos?: (string | { text?: string; title?: string; owner?: string })[]
  tickets?: Ticket[]
  ended_at?: number | string
}
interface WorkspaceInfo {
  workspace: string
  repo_url?: string | null
  integrations?: Record<string, unknown>
}

const AUTO_KEY = 'twin.autojoin'
function loadAuto(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(AUTO_KEY) || '{}')
  } catch {
    return {}
  }
}
function todayAt(hhmm: string) {
  const [h, m] = hhmm.split(':').map(Number)
  const d = new Date()
  d.setHours(h || 0, m || 0, 0, 0)
  return d.getTime()
}
const fmt = (t: number) => new Date(t).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
const toMs = (v: number | string | undefined) => (typeof v === 'number' ? (v < 1e12 ? v * 1000 : v) : v ? Date.parse(v) : 0)
const todoText = (t: string | { text?: string; title?: string; owner?: string }) =>
  typeof t === 'string' ? t : `${t.text ?? t.title ?? ''}${t.owner ? ` (${t.owner})` : ''}`

async function getJson<T>(path: string, s: WorkspaceSession): Promise<T | null> {
  try {
    const q = new URLSearchParams({ workspace: s.workspace, code: s.code })
    const r = await fetch(`${serverHttpBase()}${path}?${q}`)
    if (!r.ok) return null
    return (await r.json()) as T
  } catch {
    return null
  }
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
  const [calendar, setCalendar] = useState<CalMeeting[]>([])
  const [meetings, setMeetings] = useState<Meeting[] | null>(null)
  const [info, setInfo] = useState<WorkspaceInfo | null>(null)
  const [auto, setAuto] = useState(loadAuto)
  const [lobby, setLobby] = useState<TwinClient | null>(null)
  const [repo, setRepo] = useState('')
  const [repoStatus, setRepoStatus] = useState<string | null>(null)
  const [newRoom, setNewRoom] = useState('')

  const refresh = () => {
    void getJson<{ meetings: CalMeeting[] }>('/calendar', session).then((d) => setCalendar(d?.meetings ?? []))
    void getJson<WorkspaceInfo>('/workspace', session).then((d) => d && setInfo(d))
    void Promise.all([
      getJson<{ meetings: Meeting[] }>('/meetings', session),
      getJson<{ tickets: (Ticket & { meeting_id?: string })[] }>('/tickets', session),
    ]).then(([m, t]) => {
      if (!m) return setMeetings(null)
      const loose = t?.tickets ?? []
      setMeetings(
        (m.meetings ?? []).map((x) => ({
          ...x,
          tickets: x.tickets?.length ? x.tickets : loose.filter((k) => k.meeting_id === x.id),
        })),
      )
    })
  }

  useEffect(() => {
    refresh()
    const id = window.setInterval(refresh, 15_000)
    return () => window.clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session])

  // Lobby connection: Twin's scores + repo connect (the server remembers the repo per workspace).
  useEffect(() => {
    const c = createClient({ room: scopedRoom(session, 'lobby'), user: session.name, workspace: session.workspace, code: session.code })
    setLobby(c)
    const offE = c.on('error', (m) => /repo/i.test(m.message) && setRepoStatus(m.message))
    const offT = c.on('transcript', (m) => {
      if (m.speaker === 'Twin' && /repo|files|indexed|code map/i.test(m.text)) {
        setRepoStatus(m.text)
        refresh()
      }
    })
    return () => {
      offE()
      offT()
      c.close()
      setLobby(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session])

  // Auto-join at start time.
  useEffect(() => {
    const id = window.setInterval(() => {
      const now = Date.now()
      for (const m of calendar) {
        const at = todayAt(m.scheduled)
        if (auto[m.title] && now >= at && now - at < 5 * 60_000) return onOpenRoom(slug(m.title), m.title)
      }
    }, 5000)
    return () => window.clearInterval(id)
  }, [calendar, auto, onOpenRoom])

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
  const upcoming = calendar.slice().sort((a, b) => todayAt(a.scheduled) - todayAt(b.scheduled))
  const local = meetingHistory()

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
        <input
          className="field"
          style={{ height: 36, width: 180 }}
          placeholder="Room name"
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
      </header>

      <main className="dash-cols">
        <div className="dash-col">
          <section className="dash-card glass">
            <h2>Today</h2>
            {upcoming.length === 0 && <div className="empty">No meetings scheduled.</div>}
            {upcoming.map((m) => {
              const at = todayAt(m.scheduled)
              const live = now >= at && now - at < (m.duration_min ?? 30) * 60_000
              return (
                <div key={m.title} className="dash-row">
                  <div className="dash-time">{fmt(at)}</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="dash-title">
                      {m.title} {live && <span className="tag-live">LIVE</span>}
                    </div>
                    {m.predictions?.[0] && <div className="hint ellipsis">Twin expects: {m.predictions[0]}</div>}
                  </div>
                  <label className="switch" title="Twin joins automatically at start time">
                    <input type="checkbox" checked={!!auto[m.title]} onChange={() => toggleAuto(m.title)} />
                    <span />
                    Auto
                  </label>
                  <button className="pill small on" onClick={() => onOpenRoom(slug(m.title), m.title)}>
                    Join
                  </button>
                </div>
              )
            })}
          </section>

          <section className="dash-card glass">
            <h2>Past meetings</h2>
            {meetings === null && local.length === 0 && <div className="empty">Summaries, decisions and tickets appear here after a meeting ends.</div>}
            {meetings?.length === 0 && <div className="empty">No meetings yet. Summaries, decisions and tickets appear here after a meeting ends.</div>}
            {(meetings ?? [])
              .slice()
              .sort((a, b) => toMs(b.ended_at) - toMs(a.ended_at))
              .map((m) => (
                <details key={m.id} className="meet" open={m === meetings?.[meetings.length - 1]}>
                  <summary>
                    <span className="dash-title">{m.room.replace(`${session.workspace}-`, '').replace(/-/g, ' ')}</span>
                    {m.title && <span className="hint">· {m.title}</span>}
                    <span className="hint">{m.ended_at ? fmt(toMs(m.ended_at)) : ''}</span>
                    {!!m.tickets?.length && <span className="tag">{m.tickets.length} tickets</span>}
                  </summary>
                  {m.summary && <p className="meet-sum">{m.summary}</p>}
                  {!!m.decisions?.length && (
                    <>
                      <h3>Decisions</h3>
                      <ul>{m.decisions.map((d, i) => <li key={i}>{d}</li>)}</ul>
                    </>
                  )}
                  {!!m.todos?.length && (
                    <>
                      <h3>To-dos</h3>
                      <ul>{m.todos.map((t, i) => <li key={i}>{todoText(t)}</li>)}</ul>
                    </>
                  )}
                  {!!m.tickets?.length && (
                    <>
                      <h3>Tickets</h3>
                      {m.tickets.map((t, i) => {
                        const links = Object.entries(t.links ?? {}).filter(([, v]) => !!v) as [string, string][]
                        return (
                          <div key={t.id ?? i} className="ticket">
                            <span className="tag">{t.id ?? 'T'}</span>
                            <span style={{ flex: 1 }}>{t.title}</span>
                            {t.owner && <span className="hint">{t.owner}</span>}
                            {t.status && <span className="tag">{t.status}</span>}
                            {t.agent_prompt && (
                              <button
                                className="pill small"
                                title="Copy a ready-to-run prompt for a coding agent"
                                onClick={() => void navigator.clipboard?.writeText(t.agent_prompt ?? '')}
                              >
                                Copy agent prompt
                              </button>
                            )}
                            {links.map(([k, v]) => (
                              <a key={k} href={v} target="_blank" rel="noreferrer" className="pill small">
                                {k}
                              </a>
                            ))}
                          </div>
                        )
                      })}
                    </>
                  )}
                </details>
              ))}
            {meetings === null &&
              local.map((h) => (
                <div key={`${h.room}_${h.at}`} className="dash-row">
                  <div className="dash-time">{fmt(h.at)}</div>
                  <div style={{ flex: 1 }} className="dash-title">{h.title}</div>
                  <button className="pill small" onClick={() => onOpenRoom(h.room)}>Rejoin</button>
                </div>
              ))}
          </section>
        </div>

        <div className="dash-col">
          <section className="dash-card glass">
            <h2>Codebase</h2>
            {info?.repo_url ? (
              <div className="repo">
                <span className="logo">GH</span>
                <a href={info.repo_url} target="_blank" rel="noreferrer">{info.repo_url.replace('https://github.com/', '').replace(/\.git$/, '')}</a>
                <span className="tag live">REMEMBERED</span>
              </div>
            ) : (
              <p className="hint">Connect your repo once. Twin loads it whenever anyone in {session.workspace} joins a meeting.</p>
            )}
            <div className="row" style={{ display: 'flex', gap: 6, marginTop: 8 }}>
              <input className="field" style={{ flex: 1, height: 32, fontSize: 12 }} placeholder="https://github.com/owner/repo" value={repo} onChange={(e) => setRepo(e.target.value)} />
              <button
                className="pill small on"
                disabled={!validRepo || !lobby}
                onClick={() => {
                  lobby?.send({ type: 'connect_repo', url: repo.trim().replace(/\/$/, '') })
                  setRepoStatus('Sent to Twin, indexing…')
                }}
              >
                {info?.repo_url ? 'Replace' : 'Connect'}
              </button>
            </div>
            {repoStatus && <div className="hint" style={{ marginTop: 6 }}>{repoStatus}</div>}
          </section>

          <section className="dash-card glass">
            <h2>Twin's learning</h2>
            {lobby ? <Scoreboard client={lobby} /> : <div className="empty">Connecting…</div>}
          </section>
        </div>
      </main>
    </div>
  )
}
