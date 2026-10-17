// Integrations: connect this workspace's own GitHub / Jira / Slack (stored server-side).
// OAuth when the server has an app configured (GET /oauth/<p>/start), else a small token form
// (POST /integrations/<p>/connect). The server never returns secrets; env stays the fallback.
import { Fragment, useEffect, useState } from 'react'
import type { WorkspaceSession } from '../lib/workspace'
import { serverHttpBase } from '../lib/ws'

type Provider = 'github' | 'jira' | 'slack'
type Status = Record<string, unknown>

const NAMES: Record<Provider, string> = { github: 'GitHub', jira: 'Jira', slack: 'Slack' }

const FIELDS: Record<Provider, { key: string; label: string; secret?: boolean }[]> = {
  github: [{ key: 'token', label: 'Personal access token', secret: true }],
  jira: [
    { key: 'site_url', label: 'your-team.atlassian.net' },
    { key: 'email', label: 'Atlassian email' },
    { key: 'api_token', label: 'API token', secret: true },
    { key: 'project_key', label: 'Project key (e.g. SCRUM)' },
  ],
  slack: [
    { key: 'bot_token', label: 'Bot token (xoxb-…)', secret: true },
    { key: 'channel', label: 'Channel (e.g. standup)' },
  ],
}

const HINTS: Record<Provider, string> = {
  github: 'github.com/settings/tokens: fine-grained token with Contents read (write for PRs).',
  jira: 'id.atlassian.com/manage-profile/security/api-tokens: create an API token.',
  slack: 'api.slack.com/apps: your app > OAuth & Permissions > Bot User OAuth Token; invite the bot to the channel.',
}

export default function IntegrationsCard({
  session,
  integ,
  repoUrl,
  onChange,
}: {
  session: WorkspaceSession
  integ: Record<string, Status> | null
  repoUrl?: string | null
  onChange: () => void
}) {
  const [open, setOpen] = useState<Provider | null>(null)
  const [form, setForm] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const q = new URLSearchParams({ workspace: session.workspace, code: session.code }).toString()

  // Back from an OAuth redirect: ?connected=<p> or ?connect_error=<p>&reason=...
  useEffect(() => {
    const p = new URLSearchParams(window.location.search)
    const ok = p.get('connected')
    const bad = p.get('connect_error')
    if (!ok && !bad) return
    setMsg(ok ? `${NAMES[ok as Provider] ?? ok} connected.` : `${NAMES[bad as Provider] ?? bad}: ${p.get('reason') ?? 'connect failed'}`)
    for (const k of ['connected', 'connect_error', 'reason']) p.delete(k)
    const rest = p.toString()
    window.history.replaceState(null, '', `${window.location.pathname}${rest ? `?${rest}` : ''}`)
    onChange()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const connect = (k: Provider, v: Status | undefined) => {
    setMsg(null)
    if (v?.oauth_available) {
      window.location.href = `${serverHttpBase()}/oauth/${k}/start?${q}`
      return
    }
    setForm(k === 'slack' ? { channel: 'standup' } : {})
    setOpen(open === k ? null : k)
  }

  const submit = async (k: Provider) => {
    setBusy(true)
    setMsg(null)
    try {
      const r = await fetch(`${serverHttpBase()}/integrations/${k}/connect?${q}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(form),
      })
      const d = await r.json().catch(() => ({}))
      if (!r.ok || !d.ok) throw new Error(d.error || d.detail || `HTTP ${r.status}`)
      setOpen(null)
      setForm({})
      setMsg(`${NAMES[k]} connected.`)
      onChange()
    } catch (e) {
      setMsg(`${NAMES[k]}: ${e instanceof Error ? e.message : 'connect failed'}`)
    } finally {
      setBusy(false)
    }
  }

  const disconnect = async (k: Provider) => {
    setMsg(null)
    try {
      await fetch(`${serverHttpBase()}/integrations/${k}/disconnect?${q}`, { method: 'POST' })
    } catch {
      /* ignore */
    }
    onChange()
  }

  return (
    <section className="dash-card glass">
      <h2>Integrations</h2>
      {(['github', 'jira', 'slack'] as const).map((k) => {
        const v = integ?.[k]
        const connected = k === 'github' ? !!(v?.connected ?? repoUrl) : !!v?.connected
        const stored = !!v?.stored
        const parts = [v?.account, k === 'jira' ? v?.project : null, k === 'slack' && v?.channel ? `#${v.channel}` : null]
          .filter(Boolean)
          .map(String)
        if (!parts.length && k === 'github') parts.push(String(v?.repo_url ?? repoUrl ?? '').replace('https://github.com/', '').replace(/\.git$/, ''))
        if (v?.via === 'env' && !stored) parts.push('server default')
        const detail = parts.filter(Boolean).join(' · ')
        return (
          <Fragment key={k}>
            <div className="perm-row">
              <span className="dash-title" style={{ width: 70 }}>{NAMES[k]}</span>
              <span className="hint ellipsis" style={{ flex: 1 }}>{detail}</span>
              <span className={`tag ${connected ? 'live' : ''}`}>{connected ? 'CONNECTED' : integ ? 'NOT CONNECTED' : '—'}</span>
              {integ &&
                (stored ? (
                  <button className="pill small" onClick={() => void disconnect(k)}>Disconnect</button>
                ) : (
                  <button className="pill small on" onClick={() => connect(k, v)}>Connect</button>
                ))}
            </div>
            {open === k && (
              <form
                style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '0 0 10px' }}
                onSubmit={(e) => {
                  e.preventDefault()
                  void submit(k)
                }}
              >
                {FIELDS[k].map((f) => (
                  <input
                    key={f.key}
                    className="field"
                    style={{ height: 32, fontSize: 12 }}
                    type={f.secret ? 'password' : 'text'}
                    autoComplete="off"
                    placeholder={f.label}
                    value={form[f.key] ?? ''}
                    onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
                  />
                ))}
                <div className="hint">{HINTS[k]}</div>
                <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                  <button type="button" className="pill small" onClick={() => setOpen(null)}>Cancel</button>
                  <button type="submit" className="pill small on" disabled={busy}>{busy ? 'Checking…' : 'Save'}</button>
                </div>
              </form>
            )}
          </Fragment>
        )
      })}
      {msg && <div className="hint" style={{ marginTop: 6 }}>{msg}</div>}
      {!integ && <div className="hint">Integration status appears when the server exposes it.</div>}
    </section>
  )
}
