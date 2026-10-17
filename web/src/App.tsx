import { useEffect, useState } from 'react'
import JoinScreen, { type JoinChoice } from './components/JoinScreen'
import MeetingRoom from './components/MeetingRoom'
import { unlockAudio } from './lib/player'
import { createClient, type TwinClient } from './lib/ws'
import './styles/theme.css'

/** Room from /room/<name>, else ?room=, else "demo". */
function roomFromUrl() {
  const m = location.pathname.match(/^\/room\/([^/]+)/)
  if (m) return decodeURIComponent(m[1])
  return new URLSearchParams(location.search).get('room') || 'demo'
}

export default function App() {
  const [room] = useState(roomFromUrl)
  const [joined, setJoined] = useState<JoinChoice | null>(null)
  const [client, setClient] = useState<TwinClient | null>(null)

  useEffect(() => {
    if (!location.pathname.startsWith('/room/')) {
      history.replaceState(null, '', `/room/${encodeURIComponent(room)}${location.search}`)
    }
  }, [room])

  useEffect(() => {
    if (!joined) return
    if (joined.room !== room) history.replaceState(null, '', `/room/${encodeURIComponent(joined.room)}${location.search}`)
    const c = createClient({ room: joined.room, user: joined.name })
    setClient(c)
    return () => {
      c.close()
      setClient(null)
    }
  }, [joined, room])

  if (!joined || !client) {
    return (
      <JoinScreen
        room={room}
        onJoin={(choice) => {
          void unlockAudio() // AudioContext needs a user gesture; the Join click is one.
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
      onLeave={() => setJoined(null)}
    />
  )
}
