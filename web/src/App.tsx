import { useCallback, useEffect, useState } from 'react'
import Dashboard from './components/Dashboard'
import JoinScreen, { type JoinChoice } from './components/JoinScreen'
import LoginPage from './components/LoginPage'
import MeetingRoom from './components/MeetingRoom'
import { unlockAudio } from './lib/player'
import { loadSession, recordMeeting, scopedRoom, type WorkspaceSession } from './lib/workspace'
import { createClient, type TwinClient } from './lib/ws'
import './styles/theme.css'

type Route = { page: 'dashboard' } | { page: 'room'; room: string }

/** "/" and "/dashboard" = login -> dashboard (home). Only /room/<id> (or a legacy ?room=) opens a call. */
function parseRoute(): Route {
  const m = location.pathname.match(/^\/room\/([^/]+)/)
  if (m) return { page: 'room', room: decodeURIComponent(m[1]) }
  const legacy = new URLSearchParams(location.search).get('room')
  if (legacy) return { page: 'room', room: legacy }
  return { page: 'dashboard' }
}

/** Keep ?mock / ?server across navigation; drop one-shot params. */
function keepParams(extra?: Record<string, string>) {
  const q = new URLSearchParams(location.search)
  for (const k of ['brief', 'workspace', 'code']) q.delete(k)
  for (const [k, v] of Object.entries(extra ?? {})) q.set(k, v)
  const s = q.toString()
  return s ? `?${s}` : ''
}

export default function App() {
  const [route, setRoute] = useState<Route>(parseRoute)
  const [session, setSession] = useState<WorkspaceSession | null>(loadSession)
  const [joined, setJoined] = useState<JoinChoice | null>(null)
  const [client, setClient] = useState<TwinClient | null>(null)
  const [joinError, setJoinError] = useState<string | null>(null)

  useEffect(() => {
    const onPop = () => {
      setRoute(parseRoute())
      setJoined(null)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  useEffect(() => {
    if (route.page === 'room' && !location.pathname.startsWith('/room/')) {
      const q = new URLSearchParams(location.search)
      q.delete('room')
      const qs = q.toString()
      history.replaceState(null, '', `/room/${encodeURIComponent(route.room)}${qs ? `?${qs}` : ''}`)
    }
  }, [route])

  const openRoom = useCallback((room: string, briefFor?: string) => {
    history.pushState(null, '', `/room/${encodeURIComponent(room)}${keepParams(briefFor ? { brief: briefFor } : undefined)}`)
    setRoute({ page: 'room', room })
    setJoined(null)
  }, [])

  const goDashboard = useCallback(() => {
    history.pushState(null, '', `/dashboard${keepParams()}`)
    setRoute({ page: 'dashboard' })
    setJoined(null)
  }, [])

  useEffect(() => {
    if (!joined) return
    if (route.page === 'room' && joined.room !== route.room)
      history.replaceState(null, '', `/room/${encodeURIComponent(joined.room)}${location.search}`)
    const ws = joined.session
    const c = createClient({ room: scopedRoom(ws, joined.room), user: joined.name, workspace: ws?.workspace, code: ws?.code })
    setClient(c)
    recordMeeting({ room: joined.room, title: joined.room.replace(/-/g, ' '), at: Date.now() })
    // Wrong workspace or invite code: back to the join screen with the server's message.
    const offErr = c.on('error', (m) => {
      if (/workspace|invite|code/i.test(m.message)) {
        setJoinError(m.message)
        setJoined(null)
        // A wrong code shouldn't stay remembered: back to the login page.
        if (session) setSession(null)
      }
    })
    // Joining from the calendar: Twin joins this meeting and posts its brief.
    const brief = new URLSearchParams(location.search).get('brief')
    const offState = brief
      ? c.onState((st) => {
          if (st === 'open') {
            c.send({ type: 'calendar_join', meeting: brief })
            offState?.()
          }
        })
      : null
    if (brief) history.replaceState(null, '', `${location.pathname}${keepParams()}`)
    return () => {
      offErr()
      offState?.()
      c.close()
      setClient(null)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [joined, route])

  if (route.page === 'dashboard') {
    if (!session)
      return (
        <LoginPage
          error={joinError}
          onSignedIn={(s) => {
            setSession(s)
            setJoinError(null)
          }}
        />
      )
    return <Dashboard session={session} onOpenRoom={openRoom} onSignOut={() => setSession(null)} />
  }

  if (!joined || !client) {
    return (
      <JoinScreen
        key={route.room}
        room={route.room}
        error={joinError}
        onJoin={(choice) => {
          void unlockAudio() // AudioContext needs a user gesture; the Join click is one.
          setJoinError(null)
          if (choice.session) setSession(choice.session)
          setJoined(choice)
        }}
      />
    )
  }

  return (
    <MeetingRoom
      client={client}
      user={joined.name}
      initialCam={joined.cam}
      initialMic={joined.mic}
      onLeave={() => (session ? goDashboard() : setJoined(null))}
      session={joined.session}
    />
  )
}
