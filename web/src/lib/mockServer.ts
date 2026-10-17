// Fake Twin server for UI work without a backend. Enabled with ?mock=1 (see ws.ts).
// Plays a scripted meeting: participants, captions, a 5-box architecture drawn op by op,
// a rename, a highlight, agent events, a topic frame, a task, a brief, and every v2 rich op.
// Also answers any final user_text that mentions "Twin" with a short spoken reply.
import type { ServerMessage } from './protocol'
import type { AnyClientMessage } from './ws'

type Emit = (m: ServerMessage) => void
type Loose = Record<string, unknown> & { type: string }

let evSeq = 0
let saySeq = 0

export function startMockServer(
  emit: Emit,
  onClientMessage?: (handler: (m: AnyClientMessage) => void) => void,
): () => void {
  const timers: number[] = []
  const send = (m: Loose) => emit(m as unknown as ServerMessage)
  const at = (ms: number, fn: () => void) => timers.push(window.setTimeout(fn, ms))
  const now = () => Date.now()

  const people = [
    { name: 'Preet', kind: 'human', speaking: false },
    { name: 'Parsh', kind: 'human', speaking: false },
    { name: 'Twin', kind: 'ai', speaking: false },
  ]
  const setSpeaking = (name: string | null) =>
    send({ type: 'participants', people: people.map((p) => ({ ...p, speaking: p.name === name })) })

  const say = (text: string, delay = 0) => {
    const sayId = `s_mock_${++saySeq}`
    at(delay, () => send({ type: 'status', state: 'thinking' }))
    at(delay + 600, () => {
      send({ type: 'status', state: 'speaking' })
      setSpeaking('Twin')
      send({ type: 'transcript', speaker: 'Twin', text, ts: now() })
      send({ type: 'audio', say_id: sayId, seq: 0, format: 'wav', text, data: null, last: true })
    })
    at(delay + 600 + 300 * text.split(' ').length, () => {
      send({ type: 'status', state: 'listening' })
      setSpeaking(null)
    })
  }

  const human = (speaker: string, text: string, delay: number) => {
    at(delay, () => setSpeaking(speaker))
    at(delay + 900, () => {
      send({ type: 'transcript', speaker, text, ts: now() })
      setSpeaking(null)
    })
  }

  const agentChain = (delay: number, detail: string) => {
    const l = `ev_m${++evSeq}`
    const c = `ev_m${++evSeq}`
    const d = `ev_m${++evSeq}`
    const ev = (event_id: string, parent_id: string | undefined, agent: string, model: string, state: string, det: string) =>
      send({ type: 'agent_event', event_id, parent_id, agent, model, state, detail: det, ts: now() })
    at(delay, () => ev(l, undefined, 'Listener', 'Nemotron-3-Nano', 'started', 'read 1 line'))
    at(delay + 400, () => ev(l, undefined, 'Listener', 'Nemotron-3-Nano', 'done', '2 system facts'))
    at(delay + 450, () => ev(c, l, 'Conductor', 'Nemotron-3-Super', 'started', 'route 2 moment(s)'))
    at(delay + 500, () => ev(d, c, 'Diagrammer', 'Nemotron-3-Super', 'started', detail))
    at(delay + 2400, () => ev(d, c, 'Diagrammer', 'Nemotron-3-Super', 'done', detail))
    at(delay + 2450, () => ev(c, l, 'Conductor', 'Nemotron-3-Super', 'done', 'routed 2 system fact(s)'))
  }

  const draws = (start: number, ops: Record<string, unknown>[], gap = 350) =>
    ops.forEach((op, i) => at(start + i * gap, () => send({ type: 'draw', ...op })))

  // ------------------------------------------------------------- the script
  send({ type: 'status', state: 'listening' })
  send({ type: 'board_snapshot', boxes: [], edges: [] })
  setSpeaking(null)
  at(300, () =>
    send({
      type: 'brief',
      title: 'Daily standup',
      predictions: ['Auth migration status', 'Redis rollout risk', 'Billing webhook flakiness'],
      ts: now(),
    }),
  )

  human('Preet', 'So the web app talks to an auth service, and auth uses Postgres.', 800)
  agentChain(1900, 'draw web, auth, postgres')
  at(2000, () => send({ type: 'topic', topic_id: 't_arch', title: 'Login architecture', active: true, changed: true }))
  draws(2100, [
    { op: 'frame', id: 't_arch', title: 'Login architecture', x: 0, y: 0, w: 1000, h: 1000 },
    { op: 'add_box', id: 'web', label: 'Web app', x: 80, y: 300, kind: 'client' },
    { op: 'add_box', id: 'auth', label: 'Auth service', x: 420, y: 300, kind: 'service' },
    { op: 'add_box', id: 'db', label: 'Postgres', x: 420, y: 650, kind: 'datastore' },
    { op: 'connect', id: 'e_web_auth', from_id: 'web', to_id: 'auth', label: 'login' },
    { op: 'connect', id: 'e_auth_db', from_id: 'auth', to_id: 'db', label: 'users' },
  ])
  say('I sketched web, auth and Postgres. Does that match what you meant?', 4400)

  human('Parsh', 'Auth also uses Redis for sessions, and billing calls Stripe.', 8500)
  agentChain(9600, 'add redis, billing, stripe')
  draws(9800, [
    { op: 'add_box', id: 'redis', label: 'Redis', x: 760, y: 650, kind: 'datastore' },
    { op: 'connect', id: 'e_auth_redis', from_id: 'auth', to_id: 'redis', label: 'sessions' },
    { op: 'add_box', id: 'billing', label: 'Billing', x: 760, y: 300, kind: 'service' },
    { op: 'add_box', id: 'stripe', label: 'Stripe', x: 760, y: 60, kind: 'external' },
    { op: 'connect', id: 'e_billing_stripe', from_id: 'billing', to_id: 'stripe', label: 'charges' },
  ])
  at(12000, () => send({ type: 'draw', op: 'rename', id: 'auth', label: 'Auth (sessions)' }))
  say('I added Redis next to auth and hooked billing up to Stripe.', 12300)

  human('Preet', 'Twin, what does changing login touch?', 16500)
  at(17600, () => send({ type: 'draw', op: 'highlight', ids: ['auth', 'redis', 'e_auth_redis'], on: true }))
  say('Login touches auth and its Redis sessions. I highlighted both.', 17600)
  at(22000, () => send({ type: 'draw', op: 'highlight', ids: ['auth', 'redis', 'e_auth_redis'], on: false }))

  human('Parsh', "Let's move sessions to Redis by Friday. Priya owns it.", 22500)
  at(23600, () =>
    send({ type: 'task', id: 't_1', title: 'Move session storage to Redis', owner: 'Priya', source: 'standup @ mock' }),
  )

  // Topic switch + every v2 rich op (rendered by PREET-CLAUDE's board/ renderers when they land).
  human('Preet', 'Different topic: what does the login sequence look like now?', 25000)
  at(26000, () => send({ type: 'topic', topic_id: 't_flow', title: 'Login flow', active: true, changed: true }))
  draws(26200, [
    { op: 'frame', id: 't_flow', title: 'Login flow', x: 0, y: 0, w: 1000, h: 1000 },
    { op: 'add_text', id: 'x1', text: 'Login flow today', x: 60, y: 40, size: 'l', frame: 't_flow' },
    {
      op: 'add_mermaid',
      id: 'm1',
      code: 'sequenceDiagram\n  Browser->>Auth: POST /login\n  Auth->>Redis: store session\n  Auth-->>Browser: cookie',
      x: 60, y: 140, w: 520, h: 360, frame: 't_flow',
    },
    { op: 'add_sticky', id: 's1', text: 'Risk: session migration', color: 'yellow', x: 700, y: 120, frame: 't_flow' },
    {
      op: 'add_chart', id: 'c1', chart: 'bar', title: 'p95 latency (ms)', labels: ['v1', 'v2'],
      series: [{ name: 'p95', values: [420, 180] }], x: 620, y: 520, w: 340, h: 240, frame: 't_flow',
    },
    {
      op: 'add_table', id: 'tb1', columns: ['Option', 'Cost', 'Risk'],
      rows: [['JWT', 'low', 'token theft'], ['Sessions', 'med', 'Redis outage']], x: 60, y: 560, frame: 't_flow',
    },
    { op: 'add_code', id: 'k1', language: 'python', code: 'def login(user, pw):\n    ...', x: 60, y: 800, w: 400, h: 160, frame: 't_flow' },
    {
      op: 'add_mock', id: 'u1', title: 'New login screen',
      html: "<div style='font-family:system-ui;padding:16px'><h3>Sign in</h3><input placeholder='Email'/><button>Continue</button></div>",
      x: 480, y: 780, w: 260, h: 200, frame: 't_flow',
    },
    { op: 'sketch', id: 'sk1', strokes: [[[700, 100], [960, 100], [960, 220], [700, 220], [700, 100]]], color: 'red', frame: 't_flow' },
  ], 450)
  say('I drew the login sequence and a couple of notes on a new page.', 30500)
  at(35000, () => send({ type: 'draw', op: 'focus', id: 't_arch' }))

  // ------------------------------------------------------------- live replies
  onClientMessage?.((m) => {
    const msg = m as Loose
    if (msg.type === 'user_text') {
      const speaker = String(msg.speaker ?? 'You')
      if (!msg.final) return
      send({ type: 'transcript', speaker, text: String(msg.text), ts: now() })
      if (/\btwin\b/i.test(String(msg.text))) say("I'm the mock server, but I heard you loud and clear.", 200)
    } else if (msg.type === 'push_to_talk' && msg.active) {
      say('Push to talk works. Mock reply.', 400)
    } else if (msg.type === 'barge_in') {
      send({ type: 'stop_audio' })
      send({ type: 'status', state: 'listening' })
    } else if (msg.type === 'control') {
      if (msg.action === 'clear_board' || msg.action === 'reset_session') {
        send({ type: 'draw', op: 'clear' })
        send({ type: 'board_snapshot', boxes: [], edges: [] })
      }
    } else if (msg.type === 'calendar_join') {
      send({ type: 'brief', title: String(msg.meeting ?? 'Meeting'), predictions: ['Mock prediction A', 'Mock prediction B'], ts: now() })
      say(`Hi all, Twin here for ${String(msg.meeting ?? 'the meeting')}.`, 300)
    } else if (msg.type === 'connect_repo') {
      say('Got the repo link. Mock mode, so I will pretend I read it.', 300)
    }
  })

  return () => timers.forEach((t) => window.clearTimeout(t))
}
