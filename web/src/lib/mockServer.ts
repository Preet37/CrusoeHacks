// Fake Twin server for UI work without a backend. Enabled with ?mock=1 (see ws.ts).
// A ~40 s scripted meeting that loops: status, captions, participants, agent tree, a topic frame
// with a 5-box architecture drawn op by op, rename, highlight, every v2 rich op, a second topic,
// a task and metrics. Answers any final user_text mentioning "twin" with a canned reply.
import type { ServerMessage } from './protocol'

type Emit = (m: ServerMessage) => void
type Loose = Record<string, unknown> & { type: string }
type ClientMsg = { type: string; [k: string]: unknown }

const LOOP_MS = 42000

export function startMockServer(
  emit: Emit,
  onClientMessage?: (handler: (m: ClientMsg) => void) => void,
): () => void {
  const timers = new Set<number>()
  let stopped = false
  let evSeq = 0
  let saySeq = 0
  const send = (m: Loose) => {
    if (!stopped) emit(m as unknown as ServerMessage)
  }
  const at = (ms: number, fn: () => void) => {
    const t = window.setTimeout(() => {
      timers.delete(t)
      if (!stopped) fn()
    }, ms)
    timers.add(t)
  }
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
    const sentences = text.match(/[^.!?]+[.!?]*/g)?.map((s) => s.trim()).filter(Boolean) ?? [text]
    at(delay, () => send({ type: 'status', state: 'thinking' }))
    at(delay + 500, () => {
      send({ type: 'status', state: 'speaking' })
      setSpeaking('Twin')
      send({ type: 'transcript', speaker: 'Twin', text, ts: now() })
      sentences.forEach((s, i) =>
        send({ type: 'audio', say_id: sayId, seq: i, format: 'wav', sample_rate: 44100, text: s, data: null, last: i === sentences.length - 1 }),
      )
    })
    at(delay + 500 + 320 * text.split(' ').length, () => {
      send({ type: 'status', state: 'listening' })
      setSpeaking(null)
    })
  }

  const human = (speaker: string, text: string, delay: number) => {
    at(delay, () => {
      setSpeaking(speaker)
      send({ type: 'status', state: 'listening' })
    })
    at(delay + 900, () => {
      send({ type: 'transcript', speaker, text, ts: now() })
      setSpeaking(null)
    })
  }

  /** Listener -> Conductor -> worker (Diagrammer or Code Analyst), started then done. */
  const agentChain = (delay: number, worker: string, detail: string, dur = 2200) => {
    const l = `ev_m${++evSeq}`
    const c = `ev_m${++evSeq}`
    const d = `ev_m${++evSeq}`
    const ev = (event_id: string, parent_id: string | null, agent: string, model: string, state: string, det: string) =>
      send({ type: 'agent_event', event_id, parent_id, agent, model, state, detail: det, ts: now() })
    at(delay, () => ev(l, null, 'Listener', 'Nemotron-3-Nano', 'started', 'read 1 line'))
    at(delay + 350, () => ev(l, null, 'Listener', 'Nemotron-3-Nano', 'done', 'topic + 2 facts'))
    at(delay + 400, () => ev(c, l, 'Conductor', 'Nemotron-3-Super', 'started', 'route moments'))
    at(delay + 450, () => ev(d, c, worker, 'Nemotron-3-Super', 'started', detail))
    at(delay + dur, () => ev(d, c, worker, 'Nemotron-3-Super', 'done', detail))
    at(delay + dur + 50, () => ev(c, l, 'Conductor', 'Nemotron-3-Super', 'done', `routed to ${worker}`))
  }

  const draws = (start: number, ops: Record<string, unknown>[], gap = 300) =>
    ops.forEach((op, i) => at(start + i * gap, () => send({ type: 'draw', ...op })))

  const F1 = 't_auth'
  const F2 = 't_billing'

  const script = () => {
    send({ type: 'status', state: 'listening' })
    send({ type: 'board_snapshot', boxes: [], edges: [], shapes: [] })
    setSpeaking(null)
    at(200, () =>
      send({ type: 'brief', title: 'Daily standup', predictions: ['Auth migration status', 'Redis rollout risk'], ts: now() }),
    )

    // ---- topic 1: Auth redesign
    human('Preet', "Let's talk about the auth redesign. The web app calls an auth service backed by Postgres.", 600)
    agentChain(1600, 'Diagrammer', 'draw web, auth, postgres')
    at(1700, () => send({ type: 'topic', topic_id: F1, title: 'Auth redesign', active: true, changed: true }))
    draws(1800, [
      { op: 'frame', id: F1, title: 'Auth redesign', x: 0, y: 0, w: 1000, h: 1000 },
      { op: 'focus', id: F1 },
      { op: 'add_box', id: 'web', label: 'Web app', x: 60, y: 120, kind: 'client', frame: F1 },
      { op: 'add_box', id: 'auth', label: 'Auth service', x: 380, y: 120, kind: 'service', frame: F1 },
      { op: 'add_box', id: 'db', label: 'Postgres', x: 380, y: 420, kind: 'datastore', frame: F1 },
      { op: 'connect', id: 'e_web_auth', from_id: 'web', to_id: 'auth', label: 'login', frame: F1 },
      { op: 'connect', id: 'e_auth_db', from_id: 'auth', to_id: 'db', label: 'users', frame: F1 },
    ])
    say('I sketched the web app, auth and Postgres.', 4200)

    human('Parsh', 'Auth also keeps sessions in Redis, and billing checks auth.', 7000)
    agentChain(8000, 'Diagrammer', 'add redis, billing')
    draws(8200, [
      { op: 'add_box', id: 'redis', label: 'Redis', x: 700, y: 420, kind: 'datastore', frame: F1 },
      { op: 'connect', id: 'e_auth_redis', from_id: 'auth', to_id: 'redis', label: 'sessions', frame: F1 },
      { op: 'add_box', id: 'billing', label: 'Billing', x: 700, y: 120, kind: 'service', frame: F1 },
      { op: 'connect', id: 'e_billing_auth', from_id: 'billing', to_id: 'auth', label: 'verify', frame: F1 },
      { op: 'rename', id: 'auth', label: 'Auth (sessions)' },
    ])
    say('Added Redis for sessions and billing calling auth.', 10000)

    human('Preet', 'Twin, what does changing login touch?', 13000)
    agentChain(14000, 'Code Analyst', 'impact of changing auth/login.py', 1800)
    at(15200, () => send({ type: 'draw', op: 'highlight', ids: ['auth', 'redis', 'e_auth_redis'], on: true }))
    say('Login touches auth and its Redis sessions. I highlighted both.', 15200)

    // ---- rich ops in topic 1
    draws(17500, [
      { op: 'add_sticky', id: 's1', text: 'Risk: session migration', color: 'yellow', x: 60, y: 420, frame: F1 },
      {
        op: 'add_mermaid',
        id: 'm1',
        code: 'sequenceDiagram\n  Browser->>Auth: POST /login\n  Auth->>Redis: store session\n  Auth-->>Browser: cookie',
        x: 40, y: 600, w: 400, h: 360, frame: F1,
      },
      {
        op: 'add_chart', id: 'c1', chart: 'bar', title: 'p95 latency (ms)', labels: ['v1', 'v2'],
        series: [{ name: 'p95', values: [420, 180] }], x: 470, y: 620, w: 240, h: 200, frame: F1,
      },
      {
        op: 'add_table', id: 'tb1', columns: ['Option', 'Cost', 'Risk'],
        rows: [['JWT', 'low', 'token theft'], ['Sessions', 'med', 'Redis outage']], x: 740, y: 620, w: 240, h: 160, frame: F1,
      },
      { op: 'add_code', id: 'k1', language: 'python', code: 'def login(user, pw):\n    sid = redis.set_session(user)\n    return cookie(sid)', x: 470, y: 840, w: 260, h: 140, frame: F1 },
      {
        op: 'add_mock', id: 'u1', title: 'New login screen',
        html: "<div style='font-family:system-ui;padding:16px'><h3>Sign in</h3><input placeholder='Email'/><button>Continue</button></div>",
        x: 760, y: 800, w: 220, h: 190, frame: F1,
      },
      {
        op: 'sketch', id: 'sk1', color: 'red', frame: F1,
        strokes: [circle(450, 170, 140, 90)],
      },
    ], 450)
    at(21500, () => send({ type: 'draw', op: 'highlight', ids: ['auth', 'redis', 'e_auth_redis'], on: false }))

    human('Parsh', "Let's move sessions to Redis by Friday. Priya owns it.", 22000)
    at(23000, () => send({ type: 'task', id: 't_1', title: 'Move session storage to Redis', owner: 'Priya', source: 'standup @ mock' }))

    // ---- topic 2: Billing
    human('Preet', 'Switching to billing: how is the new scoring model doing?', 25000)
    agentChain(26000, 'Diagrammer', 'billing frame')
    at(26100, () => send({ type: 'topic', topic_id: F2, title: 'Billing', active: true, changed: true }))
    draws(26200, [
      { op: 'frame', id: F2, title: 'Billing', x: 0, y: 0, w: 1000, h: 1000 },
      { op: 'focus', id: F2 },
      { op: 'add_text', id: 'x2', text: 'Billing scoring model', x: 60, y: 40, size: 'l', frame: F2 },
      { op: 'add_box', id: 'b_api', label: 'Billing API', x: 100, y: 300, kind: 'service', frame: F2 },
      { op: 'add_box', id: 'stripe', label: 'Stripe', x: 500, y: 300, kind: 'external', frame: F2 },
      { op: 'connect', id: 'e_bapi_stripe', from_id: 'b_api', to_id: 'stripe', label: 'charges', frame: F2 },
      {
        op: 'add_chart', id: 'c2', chart: 'line', title: 'prediction_score', labels: ['v1', 'v2', 'v3'],
        series: [{ name: 'locked_test', values: [0.42, 0.51, 0.58] }], x: 100, y: 550, w: 450, h: 350, frame: F2,
      },
    ])
    at(28000, () => send({ type: 'metric', version: 'v1', split: 'locked_test', name: 'prediction_score', value: 0.42 }))
    at(29000, () => send({ type: 'metric', version: 'v3', split: 'locked_test', name: 'prediction_score', value: 0.58 }))
    at(30000, () => send({ type: 'metric', version: 'v4 (rejected)', split: 'locked_test', name: 'prediction_score', value: 0.39 }))
    say('Prediction score went from 0.42 to 0.58. I rejected one change that scored worse.', 30500)

    // ---- topic 3: Robotics (v2.1 add_3d), then an update that swings link1
    const F3 = 't_robot'
    const arm = (link1Rot: number, jointX: number) => ({
      autoRotate: true,
      objects: [
        { id: 'base', shape: 'cylinder', pos: [0, 0.15, 0], rot: [0, 0, 0], scale: [1.2, 0.3, 1.2], color: '#8a8f98', label: 'Base' },
        { id: 'link1', shape: 'box', pos: [Math.sin(-link1Rot) * 0.8, 0.3 + Math.cos(link1Rot) * 0.8, 0], rot: [0, 0, link1Rot], scale: [0.3, 1.6, 0.3], color: '#4f8cff', label: 'Link 1' },
        { id: 'joint', shape: 'sphere', pos: [jointX, 0.3 + Math.cos(link1Rot) * 1.6, 0], rot: [0, 0, 0], scale: [0.45, 0.45, 0.45], color: '#ffb020', label: 'Joint' },
        { id: 'grip', shape: 'cone', pos: [jointX + 0.35, 0.3 + Math.cos(link1Rot) * 1.6, 0], rot: [0, 0, -Math.PI / 2], scale: [0.25, 0.5, 0.25], color: '#ff5f57' },
      ],
    })
    human('Parsh', 'The robot arm team wants to see the 3-joint layout.', 31000)
    agentChain(31600, 'Diagrammer', '3d robot arm', 1200)
    at(31700, () => send({ type: 'topic', topic_id: F3, title: 'Robotics', active: true, changed: true }))
    draws(31800, [
      { op: 'frame', id: F3, title: 'Robotics', x: 0, y: 0, w: 1000, h: 1000 },
      { op: 'focus', id: F3 },
      { op: 'add_3d', id: 'r1', title: 'Robot arm, 3 joints', x: 150, y: 100, w: 700, h: 600, frame: F3, scene: arm(0, 0) },
    ])
    at(34200, () => send({ type: 'draw', op: 'update', id: 'r1', title: 'Robot arm, link1 at 0.4 rad', scene: arm(0.4, -0.62) }))

    at(36000, () => send({ type: 'draw', op: 'focus', id: F1 }))
    say('Back to auth: the session migration is the main risk.', 36500)
  }

  const loop = () => {
    script()
    at(LOOP_MS, loop)
  }
  loop()

  // ------------------------------------------------------------- live replies
  onClientMessage?.((msg) => {
    if (stopped) return
    if (msg.type === 'user_text') {
      if (!msg.final) return
      const speaker = String(msg.speaker ?? 'You')
      send({ type: 'transcript', speaker, text: String(msg.text ?? ''), ts: now() })
      if (/twin/i.test(String(msg.text ?? ''))) say("I'm the mock Twin, and I heard you loud and clear.", 200)
    } else if (msg.type === 'push_to_talk' && msg.active) {
      say('Push to talk works. Mock reply.', 300)
    } else if (msg.type === 'barge_in') {
      send({ type: 'stop_audio' })
      send({ type: 'status', state: 'listening' })
    } else if (msg.type === 'control' && (msg.action === 'clear_board' || msg.action === 'reset_session')) {
      send({ type: 'draw', op: 'clear' })
      send({ type: 'board_snapshot', boxes: [], edges: [], shapes: [] })
    } else if (msg.type === 'calendar_join') {
      send({ type: 'brief', title: String(msg.meeting ?? 'Meeting'), predictions: ['Auth migration status', 'Billing score'], ts: now() })
      say(`Hi all, Twin here for ${String(msg.meeting ?? 'the meeting')}.`, 300)
    } else if (msg.type === 'connect_repo') {
      say('Got the repo link. Mock mode, so I will pretend I read it.', 300)
    } else if (msg.type === 'draw_error') {
      console.info('[mock] draw_error', msg)
    }
  })

  return () => {
    stopped = true
    timers.forEach((t) => window.clearTimeout(t))
    timers.clear()
  }
}

function circle(cx: number, cy: number, rx: number, ry: number): [number, number][] {
  const pts: [number, number][] = []
  for (let i = 0; i <= 24; i++) {
    const a = (i / 24) * Math.PI * 2
    pts.push([Math.round(cx + rx * Math.cos(a)), Math.round(cy + ry * Math.sin(a))])
  }
  return pts
}
