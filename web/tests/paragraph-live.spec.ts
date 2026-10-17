import { writeFile } from 'node:fs/promises'
import { expect, test } from '@playwright/test'

test('live pasted spec renders its complete layered graph', async ({ page }, info) => {
  test.skip(process.env.TWIN_LIVE_E2E !== '1', 'Opt-in: calls live Nemotron')
  test.setTimeout(120_000)
  process.env.PW_TEST_SCREENSHOT_NO_FONTS_READY = '1'
  const received: any[] = []
  page.on('websocket', socket => socket.on('framereceived', event => {
    try { received.push(JSON.parse(String(event.payload))) } catch { /* non-JSON frame */ }
  }))
  await page.goto(`/room/paragraph-live-${Date.now()}`)
  await page.getByPlaceholder('Your name').fill('Paragraph QA')
  await page.getByRole('button', { name: 'Join now' }).click()
  await expect(page.locator('.tile.twin')).toBeVisible()
  await page.getByTitle('Mute (Twin stops hearing you)', { exact: true }).click()
  await page.getByTitle('Paste a spec: Twin draws a deep diagram').click()
  await page.locator('.spec textarea').fill('Use exactly these eleven components: Web App, API Gateway, Auth, Checkout, User DB, Inventory, Payments, Catalog DB, Fraud Service, Event Bus, and Notifications. Web App calls API Gateway. The gateway sends identity requests to Auth and orders to Checkout. Auth reads the User DB. Checkout calls Inventory and Payments. Inventory reads the Catalog DB. Payments calls the Fraud service. Checkout publishes order events to the Event Bus. Notifications consumes events from the Event Bus. Use five layers: client, gateway, services, data, and messaging. Order events are messages on an edge, not a component.')
  const start = Date.now()
  await page.getByRole('button', { name: 'Draw it (⌘↵)' }).click()
  try {
    while (Date.now() - start < 90_000 && received.filter(m => m.op === 'add_box').length < 11) {
      const failure = received.find(m => m.type === 'agent_event' && m.agent === 'Diagrammer' && m.state === 'failed')
      if (failure) throw new Error(`Live Diagrammer failed: ${failure.detail || 'provider timeout'}`)
      await page.waitForTimeout(200)
    }
    expect(received.filter(m => m.op === 'add_box').length).toBeGreaterThanOrEqual(11)
    await expect.poll(() => received.filter(m => m.type === 'draw' && m.op === 'connect').length, { timeout: 15_000 }).toBeGreaterThanOrEqual(10)
    await expect(page.locator('.stage-wrap:not(.hidden)')).toBeVisible()
    await expect.poll(() => page.locator('.tl-shape[data-shape-type="geo"]').count()).toBeGreaterThanOrEqual(11)
    await expect.poll(() => page.locator('.tl-shape[data-shape-type="arrow"]').count()).toBeGreaterThanOrEqual(10)
    const boxes = received.filter(m => m.op === 'add_box')
    const edges = received.filter(m => m.op === 'connect')
    const names = [/web.*app/i, /gateway/i, /auth|identity/i, /checkout/i, /user.*(?:db|database)/i, /inventory/i, /payment/i, /catalog/i, /fraud/i, /event.*bus/i, /notif/i]
    const ids = names.map(name => {
      const box = boxes.find(b => name.test(b.label))
      expect(box, `missing component ${name}`).toBeTruthy()
      return box.id
    })
    for (const [from, to] of [[0,1],[1,2],[1,3],[2,4],[3,5],[3,6],[5,7],[6,8],[3,9],[9,10]]) {
      expect(edges.some(e => e.from_id === ids[from] && e.to_id === ids[to] && e.label?.trim()), `missing labeled edge ${names[from]} -> ${names[to]}`).toBeTruthy()
    }
    await page.waitForTimeout(1500)
    const viewport = await page.locator('.tl-container').boundingBox()
    expect(viewport).toBeTruthy()
    for (const shape of await page.locator('.tl-shape[data-shape-type="geo"]').all()) {
      const bounds = await shape.boundingBox()
      expect(bounds).toBeTruthy()
      expect(bounds!.y).toBeGreaterThanOrEqual(viewport!.y)
      expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport!.y + viewport!.height)
    }
    console.log('PARAGRAPH_LIVE' , JSON.stringify({ elapsed_ms: Date.now() - start, boxes: received.filter(m => m.op === 'add_box').length, edges: received.filter(m => m.op === 'connect').length }))
  } finally {
    await info.attach('live-events', { body: JSON.stringify(received, null, 2), contentType: 'application/json' })
    await writeFile(info.outputPath('live-events.json'), JSON.stringify(received, null, 2))
    await page.waitForTimeout(1200)
    await page.screenshot({ path: info.outputPath('paragraph-board.png'), fullPage: true, timeout: 10_000 })
  }
})
