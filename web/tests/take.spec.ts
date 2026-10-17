import { expect, test } from '@playwright/test'

// "Twin's take" card renders a server `contribution` event (server faked with routeWebSocket).
test("twin's take card", async ({ page }, info) => {
  await page.routeWebSocket(/\/ws/, (ws) => {
    ws.send(JSON.stringify({ type: 'status', state: 'listening' }))
    ws.send(JSON.stringify({ type: 'board_snapshot', boxes: [], edges: [] }))
    setTimeout(() => {
      ws.send(JSON.stringify({ type: 'transcript', speaker: 'Preet', text: "Let's move sessions to Redis." }))
      ws.send(JSON.stringify({
        type: 'contribution', id: 'contrib_1', topic_id: 't_auth',
        show: 'Login flow with sessions in Redis',
        current: 'Login lives in auth/login.py and reads users from Postgres via models/user.py.',
        impact: ['The token check in auth/login.py:18-24 changes', 'Logout must delete the Redis session key'],
        risks: ['Users logged in during the cutover get kicked out', 'Redis outage blocks every login'],
        question: 'What TTL do we want for sessions?',
        sources: ['auth/login.py:18-24', 'models/user.py'],
      }))
    }, 800)
  })
  await page.goto('/room/take')
  await page.getByPlaceholder('Your name').fill('Parsh')
  await page.getByRole('button', { name: 'Join now' }).click()
  const card = page.locator('.take-float .take')
  await expect(card).toBeVisible({ timeout: 10_000 })
  await expect(card.getByText('If we change it')).toBeVisible()
  await expect(card.getByText('What TTL do we want for sessions?')).toBeVisible()
  await expect(card.locator('.chip-src')).toHaveCount(2)
  await page.screenshot({ path: info.outputPath('take.png') })
  await page.getByTitle('Twin activity', { exact: true }).click()
  await expect(page.locator('aside .take.compact')).toBeVisible()
  await card.getByRole('button', { name: 'Close' }).click()
  await expect(page.locator('.take-float')).toHaveCount(0)
})
