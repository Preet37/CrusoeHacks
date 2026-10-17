import { expect, test, type Page } from '@playwright/test'

type Message = Record<string, unknown>

function graph(count: number, prefix = 'repo') {
  const frames: Message[] = [{ op: 'frame', id: `${prefix}_topic`, title: 'Repository architecture', x: 0, y: 0, w: 1000, h: 1000 }]
  const boxes: Message[] = []
  const edges: Message[] = []
  const rows = Math.ceil(count / 5)
  for (let row = 0; row < rows; row++) {
    const frame = `${prefix}_row${row}`
    frames.push({ op: 'frame', id: frame, title: `Indexed modules ${row + 1}`, x: 20, y: 40 + row * 880 / rows,
      w: 960, h: 880 / rows - 12, frame: `${prefix}_topic` })
    const columns = Math.min(5, count - row * 5)
    for (let column = 0; column < columns; column++) {
      const index = row * 5 + column
      boxes.push({ id: `${prefix}_node${index}`, label: `app/modules/component${index}.tsx\nOwner: Repo author`,
        kind: 'service', x: Math.round(50 + column * 780 / Math.max(1, columns - 1)), y: 380, frame })
      if (index > 0) edges.push({ id: `${prefix}_edge${index}`, from_id: `${prefix}_node${index - 1}`,
        to_id: `${prefix}_node${index}`, label: 'imports', frame: `${prefix}_topic` })
    }
  }
  return { boxes, edges, shapes: frames, active_frame: `${prefix}_topic` }
}

function drawsFor(count: number): Message[] {
  const snapshot = graph(count)
  return [
    ...snapshot.shapes.map(shape => ({ type: 'draw', ...shape })),
    ...snapshot.boxes.map(box => ({ type: 'draw', op: 'add_box', ...box })),
    ...snapshot.edges.map(edge => ({ type: 'draw', op: 'connect', ...edge })),
    { type: 'draw', op: 'focus', id: snapshot.active_frame },
  ]
}

async function join(page: Page, room: string) {
  await page.addInitScript(() => localStorage.setItem('twin.name', 'Native board QA'))
  await page.goto(`/room/${room}?nativeBoard=1`)
  await page.getByPlaceholder('Your name').fill('Native board QA')
  await page.getByRole('button', { name: 'Join now' }).click({ timeout: 15_000 })
  await expect(page.getByTestId('native-board')).toBeVisible()
}

async function expectAllBoxesInsideBoard(page: Page, count: number) {
  const board = page.getByTestId('native-board')
  const boxes = page.getByTestId('native-box')
  await expect(boxes).toHaveCount(count)
  const viewport = await board.boundingBox()
  expect(viewport).toBeTruthy()
  expect(viewport!.width).toBeGreaterThan(200)
  expect(viewport!.height).toBeGreaterThan(200)
  for (const box of await boxes.all()) {
    await expect(box).toBeVisible()
    const bounds = await box.boundingBox()
    expect(bounds).toBeTruthy()
    expect(bounds!.width).toBeGreaterThan(20)
    expect(bounds!.height).toBeGreaterThan(10)
    expect(bounds!.x).toBeGreaterThanOrEqual(viewport!.x - 1)
    expect(bounds!.y).toBeGreaterThanOrEqual(viewport!.y - 1)
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport!.x + viewport!.width + 1)
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport!.y + viewport!.height + 1)
  }
}

test('native repo graph survives seven seconds and fits every early streamed node', async ({ page }, info) => {
  process.env.PW_TEST_SCREENSHOT_NO_FONTS_READY = '1'
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  // Send before the lazy board subscribes, as production does during a fast diagram.
  await page.routeWebSocket(/\/ws\?/, socket => {
    socket.send(JSON.stringify({ type: 'status', state: 'listening' }))
    socket.send(JSON.stringify({ type: 'board_snapshot', boxes: [], edges: [], shapes: [] }))
    for (const draw of drawsFor(20)) socket.send(JSON.stringify(draw))
  })
  await join(page, 'native-stream-regression')
  await expect(page.getByTestId('native-edge')).toHaveCount(19)
  await expectAllBoxesInsideBoard(page, 20)
  await page.waitForTimeout(7100)
  await expect(page.getByTestId('native-board')).toBeVisible()
  await expectAllBoxesInsideBoard(page, 20)
  await expect(page.getByTestId('native-edge')).toHaveCount(19)
  await page.screenshot({ path: info.outputPath('native-repository-after-seven-seconds.png'), fullPage: true, timeout: 10_000 })
  expect(errors).toEqual([])
})

test('native board applies snapshot replacement, rename, clear, and new drawings', async ({ page }) => {
  let send: ((message: Message) => void) | undefined
  await page.routeWebSocket(/\/ws\?/, socket => {
    send = message => socket.send(JSON.stringify(message))
    send({ type: 'status', state: 'listening' })
    send({ type: 'board_snapshot', ...graph(6, 'initial') })
  })
  await join(page, 'native-state-regression')
  await expect(page.getByTestId('native-box')).toHaveCount(6)
  await expect(page.getByTestId('native-edge')).toHaveCount(5)
  send!({ type: 'draw', op: 'rename', id: 'initial_node0', label: 'Renamed indexed entrypoint' })
  const renamed = page.getByTestId('native-box').filter({ hasText: /Renamed indexed\s*entrypoint/ })
  await expect(renamed).toBeVisible()

  send!({ type: 'board_snapshot', ...graph(2, 'replacement') })
  await expectAllBoxesInsideBoard(page, 2)
  await expect(page.getByTestId('native-edge')).toHaveCount(1)
  await expect(renamed).toHaveCount(0)

  send!({ type: 'draw', op: 'clear' })
  await expect(page.getByTestId('native-box')).toHaveCount(0)
  await expect(page.getByTestId('native-edge')).toHaveCount(0)
  send!({ type: 'draw', op: 'add_box', id: 'after_clear', label: 'Fresh after clear', kind: 'service', x: 300, y: 300 })
  await expect(page.getByTestId('native-box')).toHaveCount(1)
  await expect(page.getByTestId('native-box').filter({ hasText: 'Fresh after clear' })).toBeVisible()
})
