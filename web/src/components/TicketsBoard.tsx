// Tickets board (dashboard): every ticket Twin created in this workspace, as it lives in Jira, plus its Ticket Agent.
// Tickets are pushed to Jira automatically when a meeting ends; the agent then works each one and reports back here.
import { useCallback, useEffect, useState } from 'react'
import type { WorkspaceSession } from '../lib/workspace'
import { serverHttpBase } from '../lib/ws'

interface AgentRun {
  status?: 'queued' | 'running' | 'done' | 'needs_review' | 'no_change' | 'failed'
  step?: string
  summary?: string
  plan?: string[]
  tests?: string[]
  notes?: string
  files?: { path: string; action: string }[]
  checks?: { path: string; ok: boolean; detail: string }[]
  has_patch?: boolean
  pr_url?: string | null
  pr_error?: string | null
  error?: string | null
  started_at?: number
  finished_at?: number
}

export interface BoardTicket {
  id: string
  title: string
  owner?: string | null
  status?: string
  jira_key?: string
  links?: Record<string, string | null>
  jira?: { assignee?: string | null; reporter?: string | null; status?: string | null; url?: string | null }
  agent?: AgentRun
  room?: string
  ended_at?: string
  live?: boolean
}

const STEP_LABEL: Record<string, string> = {
  queued: 'Queued',
  reading_jira: 'Reading the Jira ticket',
  cloning: 'Cloning the repo',
  reading_diagrams: 'Reading diagrams',
  writing_code: 'Writing the change',
  checking: 'Checking the change',
  delivering: 'Posting to Jira',
}
const RESULT_LABEL: Record<string, string> = {
  done: 'Change ready',
  needs_review: 'Needs review',
  no_change: 'No change made',
  failed: 'Failed',
}

const busy = (a?: AgentRun) => a?.status === 'queued' || a?.status === 'running'
const ago = (ms?: number) => {
  if (!ms) return ''
  const s = Math.max(1, Math.round((Date.now() - ms) / 1000))
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`
}

export default function TicketsBoard({ session }: { session: WorkspaceSession }) {
  const [tickets, setTickets] = useState<BoardTicket[] | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const q = new URLSearchParams({ workspace: session.workspace, code: session.code }).toString()
  const base = serverHttpBase()

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${base}/tickets?${q}`)
      if (!r.ok) return setTickets((t) => t ?? [])
      const d = (await r.json()) as { tickets?: BoardTicket[] }
      const list = (d.tickets ?? []).filter((t) => t.status !== 'dismissed')
      list.sort((a, b) => Number(!!b.live) - Number(!!a.live) || String(b.ended_at ?? '').localeCompare(String(a.ended_at ?? '')))
      setTickets(list)
    } catch {
      setTickets((t) => t ?? [])
    }
  }, [base, q])

  // Poll fast while any agent works, slowly otherwise.
  const anyBusy = (tickets ?? []).some((t) => busy(t.agent))
  useEffect(() => {
    void load()
    const id = window.setInterval(load, anyBusy ? 3000 : 15000)
    return () => window.clearInterval(id)
  }, [load, anyBusy])

  const post = async (t: BoardTicket, path: string) => {
    setErrors((e) => ({ ...e, [t.id]: '' }))
    try {
      const r = await fetch(`${base}/tickets/${encodeURIComponent(t.id)}/${path}?${q}`, { method: 'POST' })
      if (!r.ok) {
        const d = await r.json().catch(() => ({}))
        setErrors((e) => ({ ...e, [t.id]: d?.error || d?.detail || `Failed (${r.status})` }))
      }
    } catch {
      setErrors((e) => ({ ...e, [t.id]: 'Server unreachable' }))
    }
    void load()
  }

  return (
    <section className="dash-card glass tickets-board">
      <h2>Tickets</h2>
      <p className="hint" style={{ marginTop: -4 }}>
        Created in Jira automatically when a meeting ends. Twin's ticket agent then reads the ticket, its diagrams and the repo, and
        posts a change back.
      </p>
      {tickets === null && <div className="empty">Loading tickets…</div>}
      {tickets?.length === 0 && <div className="empty">No tickets yet. Decisions from your meetings show up here.</div>}
      {!!tickets?.length && (
        <div className="tb-head">
          <span>Ticket</span>
          <span>Assigned to</span>
          <span>Reporter</span>
          <span>Status</span>
          <span>Agent</span>
        </div>
      )}
      {tickets?.map((t) => {
        const a = t.agent
        const jiraUrl = t.jira?.url || t.links?.jira || null
        const expanded = open === t.id
        return (
          <div key={t.id} className={`tb-row ${expanded ? 'open' : ''}`}>
            <div className="tb-main" onClick={() => setOpen(expanded ? null : t.id)}>
              <span className="tb-title">
                {t.jira_key ? (
                  <a href={jiraUrl ?? '#'} target="_blank" rel="noreferrer" className="tb-key" onClick={(e) => e.stopPropagation()}>
                    {t.jira_key}
                  </a>
                ) : (
                  <span className="tb-key off">{t.live ? 'LIVE' : 'DRAFT'}</span>
                )}
                <span className="ellipsis">{t.title}</span>
              </span>
              <span className="tb-person">{t.jira?.assignee || t.owner || <i className="hint">Unassigned</i>}</span>
              <span className="tb-person">{t.jira?.reporter || <i className="hint">—</i>}</span>
              <span>
                <span className={`tb-status s-${String(t.jira?.status || t.status || '').toLowerCase().replace(/\s+/g, '-')}`}>
                  {t.jira?.status || (t.jira_key ? 'In Jira' : t.live ? 'In meeting' : 'Not in Jira')}
                </span>
              </span>
              <span className="tb-agent" onClick={(e) => e.stopPropagation()}>
                {busy(a) ? (
                  <span className="tb-busy">
                    <span className="tb-spin" />
                    {STEP_LABEL[a?.step ?? 'queued'] ?? 'Working'}…
                  </span>
                ) : !t.jira_key ? (
                  t.live ? (
                    <span className="hint">After the meeting</span>
                  ) : (
                    <button className="pill small on" onClick={() => void post(t, 'push_jira')}>
                      Create in Jira
                    </button>
                  )
                ) : (
                  <>
                    {a?.status && <span className={`tb-result r-${a.status}`}>{RESULT_LABEL[a.status] ?? a.status}</span>}
                    <button
                      className={`pill small ${a?.status ? '' : 'on'}`}
                      title="The agent reads the Jira ticket, its brief and diagrams and the repo, then posts a change back to Jira"
                      onClick={() => void post(t, 'agent')}
                    >
                      {a?.status ? 'Re-run agent' : 'Run agent'}
                    </button>
                  </>
                )}
              </span>
            </div>
            {errors[t.id] && <div className="join-error" style={{ marginTop: 6 }}>{errors[t.id]}</div>}
            {expanded && (
              <div className="tb-detail">
                <div className="tb-links">
                  {jiraUrl && (
                    <a className="pill small" href={jiraUrl} target="_blank" rel="noreferrer">
                      Open in Jira
                    </a>
                  )}
                  <a className="pill small" href={`${base}/tickets/${encodeURIComponent(t.id)}/brief.md?${q}`} target="_blank" rel="noreferrer">
                    Brief .md
                  </a>
                  {a?.has_patch && (
                    <a className="pill small" href={`${base}/tickets/${encodeURIComponent(t.id)}/agent.patch?${q}`} target="_blank" rel="noreferrer">
                      Download patch
                    </a>
                  )}
                  {a?.pr_url && (
                    <a className="pill small on" href={a.pr_url} target="_blank" rel="noreferrer">
                      Pull request
                    </a>
                  )}
                </div>
                {!a && <p className="hint">The agent hasn't run on this ticket yet.</p>}
                {a?.error && <div className="join-error">{a.error}</div>}
                {a?.summary && (
                  <>
                    <h3>What the agent did</h3>
                    <p>{a.summary}</p>
                  </>
                )}
                {!!a?.plan?.length && (
                  <>
                    <h3>Plan</h3>
                    <ol>{a.plan.map((p, i) => <li key={i}>{p}</li>)}</ol>
                  </>
                )}
                {!!a?.files?.length && (
                  <>
                    <h3>Files changed</h3>
                    <ul className="tb-files">
                      {a.files.map((f) => {
                        const c = a.checks?.find((x) => x.path === f.path)
                        return (
                          <li key={f.path}>
                            <code>{f.path}</code> <span className="hint">{f.action}</span>
                            {c && <span className={`tb-check ${c.ok ? 'ok' : 'bad'}`}>{c.ok ? '✓' : '✗'} {c.detail}</span>}
                          </li>
                        )
                      })}
                    </ul>
                  </>
                )}
                {!!a?.tests?.length && (
                  <>
                    <h3>How to verify</h3>
                    <ul>{a.tests.map((x, i) => <li key={i}>{x}</li>)}</ul>
                  </>
                )}
                {a?.notes && (
                  <>
                    <h3>Notes</h3>
                    <p>{a.notes}</p>
                  </>
                )}
                {a?.pr_error && <p className="hint">Pull request not opened: {a.pr_error}</p>}
                {a?.finished_at && <p className="hint">Finished {ago(a.finished_at)}</p>}
              </div>
            )}
          </div>
        )
      })}
    </section>
  )
}
