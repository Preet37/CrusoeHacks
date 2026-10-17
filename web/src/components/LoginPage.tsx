// B5: company sign-in. Workspace + invite code + name, entered once and remembered on this browser.
import { useState } from 'react'
import { loadSession, saveSession, slug, type WorkspaceSession } from '../lib/workspace'
import { TwinAvatar } from './ParticipantTile'

export default function LoginPage({ onSignedIn, error }: { onSignedIn: (s: WorkspaceSession) => void; error?: string | null }) {
  const prev = loadSession()
  const [workspace, setWorkspace] = useState(prev?.workspace ?? '')
  const [code, setCode] = useState(prev?.code ?? '')
  const [name, setName] = useState(prev?.name ?? '')
  const ok = slug(workspace) && name.trim()

  const submit = () => {
    if (!ok) return
    const s = { workspace: slug(workspace), code: code.trim(), name: name.trim() }
    saveSession(s)
    onSignedIn(s)
  }

  return (
    <div className="join">
      <form
        className="login-card glass"
        onSubmit={(e) => {
          e.preventDefault()
          submit()
        }}
      >
        <TwinAvatar />
        <h1>Sign in to your workspace</h1>
        <p className="hint">Twin joins your company's meetings, knows your code, and remembers what was decided.</p>
        <input className="field" placeholder="Workspace (e.g. crusoehacks)" aria-label="Workspace" value={workspace} onChange={(e) => setWorkspace(e.target.value)} autoFocus />
        <input className="field" placeholder="Invite code" aria-label="Invite code" type="password" value={code} onChange={(e) => setCode(e.target.value)} />
        <input className="field" placeholder="Your name" aria-label="Your name" value={name} onChange={(e) => setName(e.target.value)} />
        {error && <div className="join-error">{error}</div>}
        <button className="pill primary" type="submit" disabled={!ok}>
          Continue
        </button>
      </form>
    </div>
  )
}
