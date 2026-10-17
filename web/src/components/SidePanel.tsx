import type { StatusState } from '../lib/protocol'
import type { Person } from '../lib/meetingState'
import { avatarColor, initials } from '../lib/media'
import type { TwinClient } from '../lib/ws'
import ConnectionsPanel from './ConnectionsPanel'
import { AgentTree, Scoreboard, TaskPanel } from './optional'
import { CloseIcon } from './Icons'
import TwinTake, { type Contribution } from './TwinTake'

export type SideTab = 'people' | 'activity' | 'tasks' | 'connections'

const TABS: { id: SideTab; label: string }[] = [
  { id: 'people', label: 'People' },
  { id: 'activity', label: 'Twin' },
  { id: 'tasks', label: 'Tasks' },
  { id: 'connections', label: 'Connect' },
]

export default function SidePanel({
  open,
  tab,
  onTab,
  onClose,
  people,
  twinStatus,
  client,
  contribution,
  repoUrl,
}: {
  open: boolean
  tab: SideTab
  onTab(t: SideTab): void
  onClose(): void
  people: Person[]
  twinStatus: StatusState
  client: TwinClient | null
  contribution?: Contribution | null
  repoUrl?: string | null
}) {
  return (
    <aside className="side glass" hidden={!open}>
      <div className="tabs">
        {TABS.map((t) => (
          <button key={t.id} className={`tab ${tab === t.id ? 'active' : ''}`} onClick={() => onTab(t.id)}>
            {t.label}
          </button>
        ))}
        <button className="pill icon small" style={{ width: 28 }} title="Close" onClick={onClose}>
          <CloseIcon />
        </button>
      </div>
      <div className="body">
        {tab === 'people' && (
          <>
            <h3>In this call ({people.length})</h3>
            {people.map((p) => (
              <div key={p.name} className="person">
                {p.kind === 'ai' ? (
                  <div className="mini twin-avatar" />
                ) : (
                  <div className="mini" style={{ background: avatarColor(p.name) }}>
                    {initials(p.name)}
                  </div>
                )}
                <div className="who">
                  <div>
                    {p.name} {p.kind === 'ai' && <span className="tile"><span className="badge">AI</span></span>}
                  </div>
                  <div className="sub">
                    {p.kind === 'ai' ? twinStatus.replace('_', ' ') : p.muted ? 'muted' : p.speaking ? 'speaking' : 'in call'}
                  </div>
                </div>
              </div>
            ))}
          </>
        )}
        <div hidden={tab !== 'activity'}>
          {contribution && (
            <>
              <h3>Twin's take</h3>
              <TwinTake c={contribution} repoUrl={repoUrl} compact />
            </>
          )}
            <h3>Agent tree</h3>
            <AgentTree client={client} />
            <h3 style={{ marginTop: 18 }}>Learning</h3>
            <Scoreboard client={client} />
        </div>
        <div hidden={tab !== 'tasks'}>
          <TaskPanel client={client} />
        </div>
        {tab === 'connections' && <ConnectionsPanel client={client} />}
      </div>
    </aside>
  )
}
