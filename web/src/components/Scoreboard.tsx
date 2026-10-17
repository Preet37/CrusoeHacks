// RX-7: scoreboard from metric messages. v1 vs latest on locked_test, rejected changes, other splits.
import { useEffect, useState } from 'react'
import type { TwinClient } from '../lib/ws'
import './panels.css'

interface MetricRow {
  version: string
  split: string
  name: string
  value: number | null
  extra: Record<string, string>
  seq: number
}

const LOCKED = 'locked_test'
const str = (v: unknown): string => (typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '')
const KNOWN = new Set(['type', 'version', 'split', 'name', 'value'])

const natCmp = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })
const lowerIsBetter = (name: string) => /latency|cost|false_alarm/i.test(name)
const isRejected = (r: MetricRow) =>
  /^rejected/i.test(r.name) || /reject/i.test(r.split) || r.name === 'coach_change' || /reject/i.test(r.extra.status ?? '')

function fmt(v: number | null): string {
  if (v == null) return '-'
  if (Number.isInteger(v)) return String(v)
  const a = Math.abs(v)
  if (a < 10) return v.toFixed(3).replace(/\.?0+$/, '') || '0'
  return v.toFixed(1)
}

export default function Scoreboard({ client }: { client: TwinClient }) {
  const [rows, setRows] = useState<Map<string, MetricRow>>(() => new Map())

  useEffect(() => {
    let seq = 0
    return client.on('metric', (raw) => {
      const m = raw as unknown as Record<string, unknown>
      const extra: Record<string, string> = {}
      for (const [k, v] of Object.entries(m)) {
        if (KNOWN.has(k) || v == null) continue
        extra[k] = typeof v === 'object' ? JSON.stringify(v) : String(v)
      }
      const value = typeof m.value === 'number' && Number.isFinite(m.value) ? m.value : Number.isFinite(Number(m.value)) && m.value !== '' && m.value != null ? Number(m.value) : null
      const row: MetricRow = {
        version: str(m.version) || '?',
        split: str(m.split) || 'unknown',
        name: str(m.name) || 'metric',
        value,
        extra,
        seq: seq++,
      }
      const key = `${isRejected(row) ? "rej|" : ""}${row.split}|${row.name}|${row.version}`
      setRows((prev) => {
        const next = new Map(prev)
        const old = next.get(key)
        next.set(key, old ? { ...row, seq: old.seq } : row)
        return next
      })
    })
  }, [client])

  const all = [...rows.values()]
  const rejected = all.filter(isRejected).sort((a, b) => b.seq - a.seq)
  const locked = all.filter((r) => !isRejected(r) && r.split === LOCKED)
  const other = all.filter((r) => !isRejected(r) && r.split !== LOCKED).sort((a, b) => natCmp(a.split, b.split) || natCmp(a.name, b.name) || natCmp(a.version, b.version))

  const versions = [...new Set(locked.map((r) => r.version))].sort(natCmp)
  const latest = versions.filter((v) => v !== 'v1').pop() ?? null
  const names = [...new Set(locked.map((r) => r.name))].sort(natCmp)
  const get = (name: string, ver: string | null) =>
    ver == null ? null : (locked.find((r) => r.name === name && r.version === ver)?.value ?? null)

  return (
    <div className="tw-panel">
      <div className="tw-head">
        <span className="tw-head-title">Scoreboard</span>
        <span className="tw-chip">locked test</span>
      </div>

      {all.length === 0 && <div className="tw-empty">No scores yet. Run the eval to compare v1 against the coached Twin.</div>}

      {names.length > 0 && (
        <>
          <div className="tw-vs">
            <span>v1</span>
            <span>vs</span>
            <span style={{ color: 'var(--tw-accent)', fontWeight: 600 }}>{latest ?? 'no newer version yet'}</span>
          </div>
          {names.map((name) => {
            const a = get(name, 'v1')
            const b = get(name, latest)
            const lower = lowerIsBetter(name)
            const delta = a != null && b != null ? b - a : null
            const better = delta == null || delta === 0 ? null : lower ? delta < 0 : delta > 0
            const cls = better == null ? 'tw-flat' : better ? 'tw-good' : 'tw-bad'
            const arrow = delta == null || delta === 0 ? '' : delta > 0 ? '▲ ' : '▼ '
            const max = Math.max(Math.abs(a ?? 0), Math.abs(b ?? 0)) || 1
            const pct = (v: number | null) => `${Math.max(2, (Math.abs(v ?? 0) / max) * 100)}%`
            const rel = delta != null && a ? ` (${delta > 0 ? '+' : ''}${((delta / Math.abs(a)) * 100).toFixed(0)}%)` : ''
            return (
              <div className="tw-card tw-metric tw-spring" key={name}>
                <div className="tw-metric-head">
                  <span className="tw-metric-name">{name.replace(/_/g, ' ')}</span>
                  <span className={`tw-delta ${cls}`}>
                    {delta == null ? '-' : `${arrow}${delta > 0 ? '+' : ''}${fmt(delta)}${rel}`}
                  </span>
                </div>
                <div className="tw-bar-row">
                  <span className="tw-bar-label">v1</span>
                  <div className="tw-bar-track">
                    <div className="tw-bar" style={{ width: a == null ? 0 : pct(a) }} />
                  </div>
                  <span className="tw-bar-val">{fmt(a)}</span>
                </div>
                {latest && (
                  <div className="tw-bar-row">
                    <span className="tw-bar-label">{latest}</span>
                    <div className="tw-bar-track">
                      <div
                        className={`tw-bar tw-bar-latest${better == null ? '' : better ? ' tw-bar-good' : ' tw-bar-bad'}`}
                        style={{ width: b == null ? 0 : pct(b) }}
                      />
                    </div>
                    <span className="tw-bar-val">{fmt(b)}</span>
                  </div>
                )}
              </div>
            )
          })}
        </>
      )}

      {rejected.length > 0 && (
        <>
          <div className="tw-section-label">Rejected changes</div>
          {rejected.map((r) => {
            const extras = Object.entries(r.extra)
            return (
              <div className="tw-card tw-rejected tw-slide" key={`rej-${r.seq}`}>
                <div className="tw-rejected-head">
                  <span className="tw-badge">{r.version}</span>
                  <b>{r.name.replace(/_/g, ' ')}</b>
                  {r.value != null && <span className="tw-ms">{fmt(r.value)}</span>}
                </div>
                {extras.length > 0 && (
                  <div className="tw-rejected-extra">{extras.map(([k, v]) => `${k}: ${v}`).join(' · ')}</div>
                )}
                {r.split && r.split !== 'rejected' && r.split !== 'unknown' && (
                  <div className="tw-rejected-extra">split: {r.split}</div>
                )}
              </div>
            )
          })}
        </>
      )}

      {other.length > 0 && (
        <>
          <div className="tw-section-label">Other splits</div>
          <div className="tw-card">
            <table className="tw-table">
              <thead>
                <tr>
                  <th>split</th>
                  <th>metric</th>
                  <th>ver</th>
                  <th style={{ textAlign: 'right' }}>value</th>
                </tr>
              </thead>
              <tbody>
                {other.map((r) => (
                  <tr key={`${r.split}|${r.name}|${r.version}`}>
                    <td>{r.split}</td>
                    <td>{r.name}</td>
                    <td>{r.version}</td>
                    <td className="tw-num">{fmt(r.value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
