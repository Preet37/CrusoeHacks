// RX-5: live agent tree built from agent_event messages. Nests by parent_id; proves recursion depth.
import { useEffect, useRef, useState } from 'react'
import type { TwinClient } from '../lib/ws'
import './panels.css'

type NodeState = 'started' | 'done' | 'failed' | 'cancelled'

interface AgentNode {
  id: string
  parent: string | null
  agent: string
  model: string
  state: NodeState
  detail: string
  seq: number
  startAt: number // local receipt time (ms)
  startTs: number | null // server ts of started event
  endAt: number | null // local time of finish
  elapsed: number | null // ms, when finished
}

const MAX_ROOTS = 12
const COLLAPSE_AFTER_MS = 6000
const STATES: ReadonlySet<string> = new Set(['started', 'done', 'failed', 'cancelled'])

const str = (v: unknown): string => (typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '')
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

export function modelBadge(model: string): string {
  const m = model.toLowerCase()
  if (!m) return ''
  if (m.includes('nano')) return 'Nano'
  if (m.includes('super')) return 'Super'
  if (m.includes('ultra')) return 'Ultra'
  if (m.includes('lightning')) return 'Lightning'
  if (m.includes('whisper') || m.includes('asr') || m.includes('parakeet')) return 'ASR'
  if (m.includes('tts') || m.includes('magpie')) return 'TTS'
  const last = model.split(/[/:]/).pop() ?? model
  return last.length > 10 ? last.slice(0, 10) : last
}

const fmtMs = (ms: number) => (ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`)

export default function AgentTree({ client }: { client: TwinClient }) {
  const nodes = useRef(new Map<string, AgentNode>())
  const seq = useRef(0)
  const maxDepth = useRef(0)
  const [, setVersion] = useState(0)
  const [now, setNow] = useState(() => Date.now())
  // per-root override: true = expanded by user, false = collapsed by user
  const [override, setOverride] = useState<Record<string, boolean>>({})

  useEffect(() => {
    const depthOf = (id: string): number => {
      let d = 1
      let cur = nodes.current.get(id)
      const seen = new Set<string>()
      while (cur && cur.parent && nodes.current.has(cur.parent) && !seen.has(cur.parent)) {
        seen.add(cur.parent)
        d++
        cur = nodes.current.get(cur.parent)
      }
      return d
    }
    const rootOf = (id: string): string => {
      let cur = nodes.current.get(id)
      const seen = new Set<string>()
      while (cur && cur.parent && nodes.current.has(cur.parent) && !seen.has(cur.parent)) {
        seen.add(cur.parent)
        cur = nodes.current.get(cur.parent)
      }
      return cur?.id ?? id
    }
    const prune = () => {
      const map = nodes.current
      const roots = [...map.values()].filter((n) => !n.parent || !map.has(n.parent)).sort((a, b) => a.seq - b.seq)
      if (roots.length <= MAX_ROOTS) return
      const drop = new Set(roots.slice(0, roots.length - MAX_ROOTS).map((r) => r.id))
      for (const n of [...map.values()]) if (drop.has(rootOf(n.id))) map.delete(n.id)
    }

    const off = client.on('agent_event', (raw) => {
      const m = raw as unknown as Record<string, unknown>
      const id = str(m.event_id) || str(m.id)
      if (!id) return
      const state = (STATES.has(str(m.state)) ? str(m.state) : 'started') as NodeState
      const parent = str(m.parent_id) || null
      const ts = num(m.ts)
      const t = Date.now()
      const map = nodes.current
      let n = map.get(id)
      if (!n) {
        n = {
          id,
          parent: parent && parent !== id ? parent : null,
          agent: str(m.agent) || 'agent',
          model: str(m.model),
          state: 'started',
          detail: str(m.detail),
          seq: seq.current++,
          startAt: t,
          startTs: state === 'started' ? ts : null,
          endAt: null,
          elapsed: null,
        }
        map.set(id, n)
      } else {
        if (parent && parent !== id && !n.parent) n.parent = parent
        if (str(m.agent)) n.agent = str(m.agent)
        if (str(m.model)) n.model = str(m.model)
        if (str(m.detail)) n.detail = str(m.detail)
      }
      if (state !== 'started' && n.state === 'started') {
        n.endAt = t
        const explicit = num(m.elapsed_ms) ?? num(m.duration_ms) ?? num(m.ms)
        n.elapsed = explicit ?? (ts != null && n.startTs != null ? Math.max(0, ts - n.startTs) : t - n.startAt)
      }
      n.state = state
      // depth for this node and any orphans that just got re-parented under it
      for (const k of map.keys()) maxDepth.current = Math.max(maxDepth.current, depthOf(k))
      prune()
      setVersion((v) => v + 1)
    })
    const timer = window.setInterval(() => setNow(Date.now()), 500)
    return () => {
      off()
      window.clearInterval(timer)
    }
  }, [client])

  // ---- derive tree for render
  const map = nodes.current
  const children = new Map<string, AgentNode[]>()
  const roots: AgentNode[] = []
  for (const n of map.values()) {
    if (n.parent && map.has(n.parent)) {
      const arr = children.get(n.parent) ?? []
      arr.push(n)
      children.set(n.parent, arr)
    } else roots.push(n)
  }
  for (const arr of children.values()) arr.sort((a, b) => a.seq - b.seq)
  roots.sort((a, b) => b.seq - a.seq) // newest first

  const running = [...map.values()].filter((n) => n.state === 'started').length

  const subtree = (n: AgentNode): AgentNode[] => {
    const out: AgentNode[] = []
    const stack = [...(children.get(n.id) ?? [])]
    while (stack.length) {
      const c = stack.pop()!
      out.push(c)
      stack.push(...(children.get(c.id) ?? []))
    }
    return out
  }

  const renderNode = (n: AgentNode) => {
    const kids = children.get(n.id) ?? []
    const badge = modelBadge(n.model)
    const ms = n.elapsed != null ? n.elapsed : now - n.startAt
    return (
      <div className="tw-node tw-spring" key={n.id}>
        <div className="tw-node-row" title={[n.agent, n.model, n.detail].filter(Boolean).join(' - ')}>
          <span className={`tw-dot tw-dot-${n.state}`} />
          <span className="tw-agent">{n.agent}</span>
          {badge && <span className={`tw-badge tw-badge-${badge.toLowerCase()}`}>{badge}</span>}
          <span className="tw-detail">{n.detail}</span>
          <span className={`tw-ms${n.state === 'started' ? ' tw-ms-live' : ''}`}>
            {n.state === 'started' ? fmtMs(ms) : n.elapsed != null ? fmtMs(n.elapsed) : ''}
          </span>
        </div>
        {kids.length > 0 && <div className="tw-node-children">{kids.map(renderNode)}</div>}
      </div>
    )
  }

  const renderRoot = (r: AgentNode) => {
    const all = [r, ...subtree(r)]
    const finished = all.every((n) => n.state !== 'started')
    const lastEnd = Math.max(...all.map((n) => n.endAt ?? 0))
    const autoCollapsed = finished && now - lastEnd > COLLAPSE_AFTER_MS
    const ov = override[r.id]
    const collapsed = ov === undefined ? autoCollapsed : !ov
    const jobs = all.length - 1
    const failed = all.some((n) => n.state === 'failed')
    const toggle = () => setOverride((o) => ({ ...o, [r.id]: collapsed }))
    if (collapsed) {
      return (
        <div className="tw-card tw-tree-root tw-spring" key={r.id}>
          <button className="tw-summary" onClick={toggle} aria-expanded={false}>
            <span className="tw-caret">&#9654;</span>
            <span className={`tw-dot tw-dot-${failed ? 'failed' : r.state}`} />
            <span>
              <b>{r.agent}</b> - {jobs} {jobs === 1 ? 'job' : 'jobs'}
              {r.elapsed != null ? `, ${fmtMs(r.elapsed)}` : ''}
            </span>
          </button>
        </div>
      )
    }
    return (
      <div className="tw-card tw-tree-root tw-spring" key={r.id}>
        {finished && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: -4 }}>
            <button className="tw-collapse-btn" onClick={toggle} aria-expanded={true} title="Collapse">
              collapse
            </button>
          </div>
        )}
        {renderNode(r)}
      </div>
    )
  }

  return (
    <div className="tw-panel">
      <div className="tw-head">
        <span className="tw-head-title">Agents</span>
        <span className={`tw-chip${running ? ' tw-chip-hot' : ''}`}>{running} running</span>
        <span className={`tw-chip${maxDepth.current >= 3 ? ' tw-chip-depth' : ''}`} title="Max recursion depth reached">
          depth {maxDepth.current}
        </span>
      </div>
      {roots.length === 0 ? (
        <div className="tw-empty">Agent jobs appear here as Twin thinks</div>
      ) : (
        roots.map(renderRoot)
      )}
    </div>
  )
}
