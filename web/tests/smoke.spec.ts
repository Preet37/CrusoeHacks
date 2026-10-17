import { expect, test } from '@playwright/test'

// T-2: mock-mode smoke test. join -> room -> board receives shapes -> side panel opens -> no console errors.
test('mock meeting smoke', async ({ page }, info) => {
  const errors: string[] = []
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
  page.on('pageerror', (e) => errors.push(String(e)))

  await page.goto('/room/smoke?mock=1')
  await expect(page.getByRole('heading', { name: 'Ready to join?' })).toBeVisible()
  await page.getByPlaceholder('Your name').fill('Parsh')
  await page.getByRole('button', { name: 'Join now' }).click()

  // Call view with Twin's tile.
  await expect(page.locator('.tile.twin')).toBeVisible()
  await expect(page.locator('.tile.twin .badge', { hasText: 'AI' })).toBeVisible()
  await page.screenshot({ path: info.outputPath('1-call.png') })

  // The mock meeting draws: stage takes over and tldraw gets shapes.
  await expect(page.locator('.stage-wrap:not(.hidden)')).toBeVisible({ timeout: 45_000 })
  await expect.poll(async () => page.locator('.tl-shape').count(), { timeout: 20_000 }).toBeGreaterThan(3)
  await page.waitForTimeout(2500)
  await page.screenshot({ path: info.outputPath('2-board.png') })

  // Captions appear.
  await expect(page.locator('.captions')).toBeVisible({ timeout: 15_000 })

  // Side panel opens (Twin activity) and shows the agent tree.
  await page.getByTitle('Twin activity', { exact: true }).click()
  await expect(page.locator('aside.side:not([hidden])')).toBeVisible()
  await page.waitForTimeout(1500)
  await page.screenshot({ path: info.outputPath('3-panel.png') })

  // Narrow window keeps a way into the panel.
  await page.setViewportSize({ width: 800, height: 800 })
  await expect(page.locator('.narrow-only')).toBeVisible()
  await page.screenshot({ path: info.outputPath('4-narrow.png') })

  const real = errors.filter((e) => !/favicon|DevTools|WebGL|GPU stall|speech/i.test(e))
  expect(real, real.join('\n')).toEqual([])
})
