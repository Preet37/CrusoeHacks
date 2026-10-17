// RX-6: tasks created from meeting decisions (task messages), newest first, deduped by id.
import { useEffect, useState } from 'react'
import type { TwinClient } from '../lib/ws'
import './panels.css'

interface TaskItem {
  id: string
  title: string
  owner: string
  source: string
  seq: number
}

const str = (v: unknown): string => (typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '')

const initials = (name: string) =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]!.toUpperCase())
    .join('') || '?'

const AVATAR_COLORS = ['#0a84ff', '#7c5cff', '#ff9f0a', '#30b0c7', '#ff375f', '#34c759', '#af52de']
const colorFor = (name: string) => {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0
  return AVATAR_COLORS[Math.abs(h) % AVATAR_COLORS.length]
}

export default function TaskPanel({ client }: { client: TwinClient }) {
  const [tasks, setTasks] = useState<TaskItem[]>([])

  useEffect(() => {
    let seq = 0
    return client.on('task', (raw) => {
      const m = raw as unknown as Record<string, unknown>
      const id = str(m.id) || str(m.task_id) || `t_${Date.now()}_${seq}`
      const title = str(m.title) || str(m.text)
      if (!title) return
      setTasks((prev) => {
        const existing = prev.find((t) => t.id === id)
        const item: TaskItem = {
          id,
          title,
          owner: str(m.owner) || existing?.owner || '',
          source: str(m.source) || existing?.source || '',
          seq: existing ? existing.seq : seq++,
        }
        const rest = prev.filter((t) => t.id !== id)
        return [item, ...rest].sort((a, b) => b.seq - a.seq)
      })
    })
  }, [client])

  return (
    <div className="tw-panel">
      <div className="tw-head">
        <span className="tw-head-title">Tasks</span>
        <span className="tw-chip">{tasks.length}</span>
      </div>
      {tasks.length === 0 ? (
        <div className="tw-empty">Decisions become tasks here</div>
      ) : (
        tasks.map((t) => (
          <div className="tw-card tw-task tw-slide" key={t.id}>
            <div className="tw-task-title">{t.title}</div>
            <div className="tw-task-meta">
              {t.owner && (
                <span className="tw-owner">
                  <span className="tw-avatar" style={{ background: colorFor(t.owner) }}>
                    {initials(t.owner)}
                  </span>
                  {t.owner}
                </span>
              )}
              {t.source && <span className="tw-source">{t.source}</span>}
            </div>
          </div>
        ))
      )}
    </div>
  )
}
