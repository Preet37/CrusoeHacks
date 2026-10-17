// Dev harness: the whiteboard alone, fed by the mock server. Open /board-dev.html
import { StrictMode, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import Whiteboard from '../components/Whiteboard'
import { createClient } from '../lib/ws'

function BoardDev() {
  const client = useMemo(() => createClient({ room: 'demo', user: 'dev', mock: true }), [])
  const [has, setHas] = useState(false)
  return (
    <div style={{ position: 'fixed', inset: 0 }}>
      <Whiteboard client={client} onHasContent={setHas} />
      <div style={{ position: 'absolute', top: 8, right: 8, zIndex: 1000, font: '12px system-ui', background: '#fff', padding: 4 }}>
        has content: {String(has)}
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BoardDev />
  </StrictMode>,
)
