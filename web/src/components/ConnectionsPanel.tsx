// Connections tab (ARCH 1E.6). GitHub is real (sends connect_repo); everything else is clearly marked demo.
import { useState } from 'react'
import type { TwinClient } from '../lib/ws'

const DEMO_CALENDAR = [
  { time: '9:30 AM', title: 'Daily standup' },
  { time: '1:30 PM', title: 'Auth redesign review' },
  { time: '4:00 PM', title: 'Billing sync' },
]

function Card({
  name, color, letter, live, children,
}: { name: string; color: string; letter: string; live?: boolean; children: React.ReactNode }) {
  return (
    <div className="conn-card">
      <div className="head">
        <span className="logo" style={{ background: color }}>{letter}</span>
        {name}
        <span className={`tag ${live ? 'live' : ''}`}>{live ? 'LIVE' : 'DEMO DATA'}</span>
      </div>
      {children}
    </div>
  )
}

export default function ConnectionsPanel({ client }: { client: TwinClient | null }) {
  const [repo, setRepo] = useState('')
  const [sent, setSent] = useState<string | null>(null)
  const [joined, setJoined] = useState<string | null>(null)

  const validRepo = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/?$/.test(repo.trim())

  return (
    <div>
      <Card name="GitHub" color="#24292f" letter="GH" live>
        <p>Paste a public repo. Twin builds a code map so it can answer "what does this change touch?"</p>
        <div className="row">
          <input
            className="field"
            placeholder="https://github.com/owner/repo"
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
          />
          <button
            className="pill small on"
            disabled={!validRepo || !client}
            onClick={() => {
              const url = repo.trim().replace(/\/$/, '')
              client?.send({ type: 'connect_repo', url })
              setSent(url)
            }}
          >
            Connect
          </button>
        </div>
        {sent && <div className="hint">Sent to Twin: {sent}</div>}
      </Card>

      <Card name="Google Calendar" color="#1a73e8" letter="31">
        <p>Twin sees your meetings and joins them on its own. These events are sample data.</p>
        {DEMO_CALENDAR.map((m) => (
          <div key={m.title} className="cal-item">
            <span className="t">{m.time}</span>
            <span style={{ flex: 1 }}>{m.title}</span>
            <button
              className="pill small"
              disabled={!client}
              onClick={() => {
                client?.send({ type: 'calendar_join', meeting: m.title, scheduled: m.time })
                setJoined(m.title)
              }}
            >
              Simulate
            </button>
          </div>
        ))}
        {joined && <div className="hint">Simulated start of "{joined}". Twin joins and posts its brief.</div>}
      </Card>

      <Card name="Slack" color="#611f69" letter="S">
        <p>Coming soon: Twin posts meeting decisions and tasks to a channel.</p>
      </Card>
      <Card name="Jira" color="#0052cc" letter="J">
        <p>Coming soon: tasks from decisions become tickets with owners.</p>
      </Card>
      <Card name="Zoom / Google Meet" color="#2d8cff" letter="Z">
        <p>Coming soon: Twin joins external calls through a meeting bot, the same way note-takers do.</p>
      </Card>
    </div>
  )
}
