// "Twin's take" (15:17 feedback): Twin's contribution on the current topic, beside the drawing and in the side panel.
// Fed by the server's `contribution` event: {show, current, impact[], risks[], question, sources["path:lines"]}.
import { useState } from 'react'

export interface Contribution {
  id: string
  topic_id?: string | null
  show?: string
  current?: string
  impact?: string[]
  risks?: string[]
  question?: string
  sources?: string[]
}

/** "auth/login.py:18-24" -> GitHub blob link on the workspace repo (if one is connected). */
export function sourceLink(repoUrl: string | null | undefined, src: string): string | null {
  if (!repoUrl) return null
  const base = repoUrl.replace(/\.git$/, '').replace(/\/$/, '')
  const m = src.match(/^([^:]+?)(?::(\d+)(?:-(\d+))?)?$/)
  if (!m) return null
  const [, path, a, b] = m
  return `${base}/blob/HEAD/${path}${a ? `#L${a}${b ? `-L${b}` : ''}` : ''}`
}

export default function TwinTake({
  c,
  repoUrl,
  compact,
  onClose,
  startCollapsed,
}: {
  startCollapsed?: boolean
  c: Contribution
  repoUrl?: string | null
  compact?: boolean
  onClose?: () => void
}) {
  const [open, setOpen] = useState(!startCollapsed)
  const has = (xs?: string[]) => !!xs && xs.length > 0
  return (
    <div className={`take ${compact ? 'compact' : 'glass'}`} key={c.id}>
      <div className="take-head">
        <span className="take-dot" />
        <b>Twin's take</b>
        {c.show && <span className="hint ellipsis">{c.show}</span>}
        {!compact && (
          <span style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
            <button className="pill small" style={{ height: 22 }} onClick={() => setOpen((o) => !o)}>
              {open ? 'Less' : 'More'}
            </button>
            {onClose && (
              <button className="pill small" style={{ height: 22 }} onClick={onClose} aria-label="Close">
                ×
              </button>
            )}
          </span>
        )}
      </div>
      {(open || compact) && (
        <>
          {c.current && (
            <div className="take-sec">
              <h5>Current</h5>
              <p>{c.current}</p>
            </div>
          )}
          {has(c.impact) && (
            <div className="take-sec">
              <h5>If we change it</h5>
              <ul>{c.impact!.map((x, i) => <li key={i}>{x}</li>)}</ul>
            </div>
          )}
          {has(c.risks) && (
            <div className="take-sec">
              <h5>Risks</h5>
              <ul>{c.risks!.map((x, i) => <li key={i}>{x}</li>)}</ul>
            </div>
          )}
          {has(c.sources) && (
            <div className="take-chips">
              {c.sources!.map((s) => {
                const href = sourceLink(repoUrl, s)
                return href ? (
                  <a key={s} className="chip-src" href={href} target="_blank" rel="noreferrer">
                    {s}
                  </a>
                ) : (
                  <span key={s} className="chip-src">
                    {s}
                  </span>
                )
              })}
            </div>
          )}
        </>
      )}
      {c.question && <div className="take-q">{c.question}</div>}
    </div>
  )
}
