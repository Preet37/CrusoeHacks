import { useEffect, useState } from 'react'
import type { Line } from '../lib/meetingState'

const SHOW_MS = 6000

/** Live captions: last couple of committed lines plus anyone's in-progress speech. */
export default function Captions({ lines, interim }: { lines: Line[]; interim: Record<string, string> }) {
  const [, tick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => tick((n) => n + 1), 1000)
    return () => window.clearInterval(id)
  }, [])

  const now = Date.now()
  // Lines are stamped with the local receive time (meetingState), so this is clock-skew safe.
  const live = Object.entries(interim).filter(([, t]) => t.trim()).slice(-3)
  const fresh = lines.slice(-3).filter((l) => now - l.ts < SHOW_MS).slice(-(3 - live.length) || 0)
  if (live.length >= 3) fresh.length = 0
  if (!fresh.length && !live.length) return null

  return (
    <div className="captions" aria-live="polite">
      {fresh.map((l) => (
        <div key={l.id} className={l.speaker === 'Twin' ? 'twin' : ''}>
          <b>{l.speaker}</b>
          {l.text}
        </div>
      ))}
      {live.map(([speaker, text]) => (
        <div key={`i_${speaker}`} className="interim">
          <b>{speaker}</b>
          {text}
        </div>
      ))}
    </div>
  )
}
