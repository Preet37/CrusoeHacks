import { test } from '@playwright/test'

// Browser-side startup timings against the live server (:8000). Prints JSON; run: npx playwright test tests/timing.spec.ts
test('startup timings', async ({ page }) => {
  const runs: Record<string, number>[] = []
  for (let i = 0; i < 5; i++) {
    const t0 = Date.now()
    await page.goto(`/room/timing-${Date.now()}`)
    await page.getByRole('button', { name: 'Join now' }).waitFor()
    const joinScreen = Date.now() - t0
    await page.getByPlaceholder('Your name').fill('Timer')
    const t1 = Date.now()
    await page.getByRole('button', { name: 'Join now' }).click()
    await page.locator('.dot.open').waitFor({ timeout: 15_000 })
    const connected = Date.now() - t1
    await page.locator('.tile.twin .tw-pill').waitFor()
    await page.locator('.twin-orb canvas').waitFor({ timeout: 15_000 })
    const orb = Date.now() - t1
    runs.push({ page_load_to_join_screen_ms: joinScreen, join_to_connected_ms: connected, join_to_twin_orb_ms: orb })
    await page.getByTitle('Leave call').click()
  }
  const keys = Object.keys(runs[0])
  const med = (k: string) => runs.map((r) => r[k]).sort((a, b) => a - b)[Math.floor(runs.length / 2)]
  console.log('TIMING', JSON.stringify({ n: runs.length, median: Object.fromEntries(keys.map((k) => [k, med(k)])), runs }))
})
