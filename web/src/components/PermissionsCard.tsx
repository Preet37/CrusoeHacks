// Per-workspace tool permissions (judge feedback: sandboxing). Saved server-side via
// GET/POST /permissions?workspace=W&code=C; the server refuses tools that are off.
import { useEffect, useState } from 'react'
import type { WorkspaceSession } from '../lib/workspace'
import { serverHttpBase } from '../lib/ws'

export type PermValue = 'on' | 'ask' | 'off'
export interface Permissions {
  internet: PermValue
  read_repo: PermValue
  create_tickets: PermValue
  read_screen: PermValue
  speak_unprompted: PermValue
}

export const DEFAULT_PERMISSIONS: Permissions = {
  internet: 'off',
  read_repo: 'on',
  create_tickets: 'ask',
  read_screen: 'ask',
  speak_unprompted: 'off',
}

const ROWS: { key: keyof Permissions; label: string; hint: string }[] = [
  { key: 'internet', label: 'Internet access', hint: 'Search or fetch web pages' },
  { key: 'read_repo', label: 'Read connected repo', hint: 'Code map for impact answers' },
  { key: 'create_tickets', label: 'Create tickets', hint: 'Turn decisions into tickets' },
  { key: 'read_screen', label: 'Read shared screen', hint: 'Look at screen-share frames' },
  { key: 'speak_unprompted', label: 'Speak unprompted', hint: 'Interject without being named' },
]

const localKey = (ws: string) => `twin.permissions.${ws}`

export default function PermissionsCard({ session }: { session: WorkspaceSession }) {
  const [perms, setPerms] = useState<Permissions>(DEFAULT_PERMISSIONS)
  const [saved, setSaved] = useState<'server' | 'browser' | null>(null)
  const q = new URLSearchParams({ workspace: session.workspace, code: session.code }).toString()

  useEffect(() => {
    let alive = true
    fetch(`${serverHttpBase()}/permissions?${q}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => alive && setPerms({ ...DEFAULT_PERMISSIONS, ...(d.permissions ?? d) }))
      .catch(() => {
        try {
          const local = JSON.parse(localStorage.getItem(localKey(session.workspace)) || 'null')
          if (alive && local) setPerms({ ...DEFAULT_PERMISSIONS, ...local })
        } catch {
          /* ignore */
        }
      })
    return () => {
      alive = false
    }
  }, [q, session.workspace])

  const update = async (key: keyof Permissions, value: PermValue) => {
    const next = { ...perms, [key]: value }
    setPerms(next)
    try {
      localStorage.setItem(localKey(session.workspace), JSON.stringify(next))
    } catch {
      /* ignore */
    }
    try {
      const r = await fetch(`${serverHttpBase()}/permissions?${q}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ permissions: next }),
      })
      setSaved(r.ok ? 'server' : 'browser')
    } catch {
      setSaved('browser')
    }
  }

  return (
    <section className="dash-card glass">
      <h2>Permissions</h2>
      <p className="hint" style={{ marginTop: -4 }}>What Twin may do in {session.workspace}. Tools that are off are refused by the server.</p>
      {ROWS.map((r) => (
        <div key={r.key} className="perm-row">
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="dash-title">{r.label}</div>
            <div className="hint">{r.hint}</div>
          </div>
          <span className="seg">
            {(['off', 'ask', 'on'] as PermValue[]).map((v) => (
              <button key={v} className={perms[r.key] === v ? 'on' : ''} onClick={() => void update(r.key, v)}>
                {v === 'ask' ? 'Ask first' : v === 'on' ? 'On' : 'Off'}
              </button>
            ))}
          </span>
        </div>
      ))}
      {saved && (
        <div className="hint" style={{ marginTop: 6 }}>
          {saved === 'server' ? 'Saved for the workspace.' : 'Saved in this browser (server permissions endpoint not available yet).'}
        </div>
      )}
    </section>
  )
}
