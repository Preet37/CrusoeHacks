import { expect, test } from '@playwright/test'
import { readFile } from 'node:fs/promises'

function reconstructedDraws(): Array<Record<string, unknown>> {
  const prefix = 'repo_replay'
  const draws: Array<Record<string, unknown>> = [
    { type: 'draw', op: 'add_sticky', id: 'ghost_1', text: 'Twin is sketching…', color: 'blue', x: 820, y: 80 },
    { type: 'draw', op: 'frame', id: 't_arch', title: 'Arch architecture', x: 0, y: 0, w: 1000, h: 1000 },
    { type: 'draw', op: 'focus', id: 't_arch' },
  ]
  const names = [
    ['app/editor/PhysicsScene.jsx', 'app/editor/components/GenericVisual.jsx', 'app/dashboard/page.tsx', 'app/page.tsx', 'lib/specSheet.ts'],
    ['app/components/FeatureCard.tsx', 'render-worker/binPaths.js', 'Backend/lib/physics-config.ts', 'app/api/agent-pipeline/route.ts', 'app/api/analyze_document/route.ts'],
    ['app/api/analyze_video/route.ts', 'app/api/ask/route.ts', 'app/api/auth/[auth0]/route.ts', 'app/api/compile-wiki/route.ts', 'app/api/explain-failure/route.ts'],
    ['app/api/extract/route.ts', 'app/api/flashcards/route.ts', 'app/api/generate-3d-model/route.ts', 'app/api/generate-3d/route.ts', 'app/api/generate-artifacts/route.ts'],
  ]
  names.forEach((row, r) => {
    const frame = `${prefix}_r${r}`
    draws.push({ type: 'draw', op: 'frame', id: frame, title: row.join(' / '), x: 20, y: 40 + r * 220, w: 960, h: 210, frame: 't_arch' })
    row.forEach((name, c) => draws.push({
      type: 'draw', op: 'add_box', id: `${prefix}_n${r * 5 + c}`,
      label: `${name}\nOwner: Preet37`, kind: 'service',
      x: 50 + c * 195, y: 380, frame,
    }))
    if (r === 0) draws.push({ type: 'draw', op: 'remove', id: 'ghost_1' })
  })
  draws.push({ type: 'draw', op: 'connect', id: `${prefix}_e0`, from_id: `${prefix}_n3`, to_id: `${prefix}_n5`, label: 'imports', frame: 't_arch' })
  draws.push({ type: 'draw', op: 'add_text', id: `${prefix}_legend`, text: 'Repository map', x: 30, y: 950, frame: 't_arch' })
  draws.push({ type: 'draw', op: 'focus', id: 't_arch' })
  return draws
}

// Local diagnostic: replay one recorded room's draw frames without calling a model.
test('recorded speech-room draws render on the whiteboard', async ({ page }, info) => {
  test.setTimeout(45_000)
  process.env.PW_TEST_SCREENSHOT_NO_FONTS_READY = '1'
  const path = process.env.TWIN_REPLAY_EVENTS
  const draws = path
    ? (JSON.parse(await readFile(path, 'utf8')) as { events: Array<Record<string, unknown>> })
      .events.filter((event) => event.type === 'draw')
    : reconstructedDraws()
  const early = process.env.TWIN_REPLAY_EARLY !== '0'
  const warnings: string[] = []
  const errors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'warning' || message.type() === 'error') warnings.push(message.text())
  })
  page.on('pageerror', (error) => errors.push(error.message))
  let socket: { send(data: string): void } | undefined
  await page.routeWebSocket(/\/ws\?/, (ws) => {
    socket = ws
    ws.send(JSON.stringify({ type: 'status', state: 'listening' }))
    ws.send(JSON.stringify({ type: 'board_snapshot', boxes: [], edges: [], shapes: [] }))
    if (early) for (const draw of draws) ws.send(JSON.stringify(draw))
  })
  const base = process.env.TWIN_REPLAY_URL || 'http://localhost:5173'
  await page.goto(`${base}/room/recorded-replay`)
  await page.getByPlaceholder('Your name').fill('Replay QA')
  await page.getByRole('button', { name: 'Join now' }).click()
  await expect(page.locator('.tl-container')).toHaveCount(1)
  expect(socket).toBeTruthy()
  if (!early) for (const draw of draws) socket!.send(JSON.stringify(draw))
  await expect.poll(() => page.locator('.tl-shape[data-shape-type="geo"]').count(), { timeout: 30_000 }).toBeGreaterThanOrEqual(20)
  const state = await page.evaluate(() => {
    const board = (window as unknown as { twinBoard?: Record<string, unknown> }).twinBoard
    return {
      boxes: (board?.boxes as Map<string, unknown> | undefined)?.size,
      frames: (board?.frames as Map<string, unknown> | undefined)?.size,
      queue: (board?.queue as unknown[] | undefined)?.length,
      shapes: document.querySelectorAll('.tl-shape').length,
      geo: document.querySelectorAll('.tl-shape[data-shape-type="geo"]').length,
      frame: document.querySelectorAll('.tl-shape[data-shape-type="frame"]').length,
      stageVisible: !!document.querySelector('.stage-wrap:not(.hidden)'),
    }
  })
  await page.screenshot({ path: info.outputPath('replayed-speech-board.png'), fullPage: true })
  console.log('REPLAY_BOARD', JSON.stringify({ draws: draws.length, state, warnings, errors }))
  expect(errors).toEqual([])
  expect(state.boxes).toBeGreaterThanOrEqual(20)
  expect(state.geo).toBeGreaterThanOrEqual(20)
})

test('late board subscriber replays only the newest snapshot and subsequent draws', async ({ page }) => {
  await page.routeWebSocket(/\/ws\?/, (ws) => {
    const send = (message: Record<string, unknown>) => ws.send(JSON.stringify(message))
    send({ type: 'status', state: 'listening' })
    send({ type: 'draw', op: 'add_box', id: 'before_clear', label: 'Old', kind: 'service', x: 20, y: 20 })
    send({ type: 'draw', op: 'clear' })
    send({ type: 'draw', op: 'add_box', id: 'before_snapshot', label: 'Stale', kind: 'service', x: 40, y: 40 })
    send({ type: 'board_snapshot', boxes: [
      { id: 'from_snapshot', label: 'Kept', kind: 'service', x: 100, y: 100 },
    ], edges: [], shapes: [] })
    send({ type: 'draw', op: 'add_box', id: 'after_snapshot', label: 'New', kind: 'service', x: 300, y: 100 })
  })
  const base = process.env.TWIN_REPLAY_URL || 'http://localhost:5173'
  await page.goto(`${base}/room/replay-reset`)
  await page.getByPlaceholder('Your name').fill('Replay QA')
  await page.getByRole('button', { name: 'Join now' }).click()
  await expect.poll(() => page.locator('.tl-shape[data-shape-type="geo"]').count(), { timeout: 20_000 }).toBe(2)
  const ids = await page.evaluate(() => [
    ...((window as unknown as { twinBoard: { boxes: Map<string, unknown> } }).twinBoard.boxes.keys()),
  ])
  expect(ids.sort()).toEqual(['after_snapshot', 'from_snapshot'])
})
