// Company workspace sign-in (no open platform): workspace name + invite code + your name.
// Rooms are scoped to the workspace: server room id = `<workspace>-<room>`. The server checks the code.

export interface WorkspaceSession {
  workspace: string
  code: string
  name: string
}

const KEY = 'twin.workspace'
const HISTORY_KEY = 'twin.history'

export function slug(s: string, fallback = '') {
  return s.trim().toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '') || fallback
}

export function loadSession(): WorkspaceSession | null {
  // An invite link (?workspace=&code=) wins over what this browser remembers.
  const q = new URLSearchParams(location.search)
  const qw = q.get('workspace')
  let saved: WorkspaceSession | null = null
  try {
    saved = JSON.parse(localStorage.getItem(KEY) || 'null')
  } catch {
    saved = null
  }
  if (qw) return { workspace: slug(qw), code: q.get('code') ?? '', name: saved?.name ?? '' }
  return saved && saved.workspace ? saved : null
}

export function saveSession(s: WorkspaceSession) {
  try {
    localStorage.setItem(KEY, JSON.stringify(s))
    localStorage.setItem('twin.name', s.name)
  } catch {
    /* private mode */
  }
}

export function signOut() {
  try {
    localStorage.removeItem(KEY)
  } catch {
    /* ignore */
  }
}

export function scopedRoom(s: WorkspaceSession | null, room: string) {
  return s ? `${s.workspace}-${room}` : room
}

/** Link someone can open to join this room in this workspace (carries the code and any ?server). */
export function inviteLink(s: WorkspaceSession | null, room: string) {
  const q = new URLSearchParams()
  if (s) {
    q.set('workspace', s.workspace)
    if (s.code) q.set('code', s.code)
  }
  const server = new URLSearchParams(location.search).get('server')
  if (server) q.set('server', server)
  const qs = q.toString()
  return `${location.origin}/room/${encodeURIComponent(room)}${qs ? `?${qs}` : ''}`
}

export interface PastMeeting {
  room: string
  title: string
  at: number
}

/** Meetings joined from this browser (the server has no meeting history endpoint yet). */
export function meetingHistory(): PastMeeting[] {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]')
  } catch {
    return []
  }
}

export function recordMeeting(m: PastMeeting) {
  try {
    const list = meetingHistory().filter((x) => !(x.room === m.room && Math.abs(x.at - m.at) < 30 * 60_000))
    localStorage.setItem(HISTORY_KEY, JSON.stringify([m, ...list].slice(0, 12)))
  } catch {
    /* ignore */
  }
}
