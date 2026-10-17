// Loads PREET-CLAUDE's board components if their files exist, so a missing file never breaks the build.
import { Component, Suspense, lazy, type ComponentType, type ReactNode } from 'react'
import type { TwinClient } from '../lib/ws'

const modules = import.meta.glob(['./Whiteboard.tsx', './AgentTree.tsx', './TaskPanel.tsx', './Scoreboard.tsx'])

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyProps = Record<string, any>

function optional(path: string): ComponentType<AnyProps> | null {
  const loader = modules[path]
  if (!loader) return null
  return lazy(async () => {
    try {
      const mod = (await loader()) as { default?: ComponentType<AnyProps> }
      return { default: mod.default ?? (() => null) }
    } catch (e) {
      // A newer deploy replaced this chunk (old file names are gone). Reload once to pick up the new build.
      const key = 'twin-chunk-reload'
      let last = 0
      try { last = Number(sessionStorage.getItem(key) || 0) } catch { /* storage blocked */ }
      if (Date.now() - last > 30_000) {
        try { sessionStorage.setItem(key, String(Date.now())) } catch { /* storage blocked */ }
        window.location.reload()
        return new Promise<never>(() => {}) // keep Suspense showing "Loading…" until the reload happens
      }
      throw e
    }
  })
}

class Boundary extends Component<{ fallback: ReactNode; children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() {
    return { failed: true }
  }
  componentDidCatch(e: unknown) {
    console.error('[optional component]', e)
  }
  render() {
    return this.state.failed ? this.props.fallback : this.props.children
  }
}

function wrap(path: string, label: string) {
  const C = optional(path)
  return function Optional(props: AnyProps & { client: TwinClient | null }) {
    const fallback = <div className="empty">{label}</div>
    if (!C) return fallback
    return (
      <Boundary
        fallback={
          <div className="empty">
            Couldn't load this part of the app.{' '}
            <button className="pill small" onClick={() => window.location.reload()}>Reload</button>
          </div>
        }
      >
        <Suspense fallback={<div className="empty">Loading…</div>}>
          <C {...props} />
        </Suspense>
      </Boundary>
    )
  }
}

export const Whiteboard = wrap('./Whiteboard.tsx', 'Whiteboard is coming soon')
export const AgentTree = wrap('./AgentTree.tsx', 'Twin activity appears here as agents work.')
export const TaskPanel = wrap('./TaskPanel.tsx', 'Tasks from decisions show up here.')
export const Scoreboard = wrap('./Scoreboard.tsx', 'Scoreboard appears after Twin learns.')
