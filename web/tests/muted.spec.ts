import { expect, test } from '@playwright/test'

// Remote people muted on the server (participants[].muted) show a mic-off icon and never a speaking ring.
test('remote muted icon', async ({ page }) => {
  await page.routeWebSocket(/\/ws/, (ws) => {
    ws.send(JSON.stringify({ type: 'status', state: 'listening' }))
    setTimeout(() => ws.send(JSON.stringify({ type: 'participants', people: [
      { name: 'Parsh', kind: 'human', speaking: false },
      { name: 'Preet', kind: 'human', speaking: true, muted: true },
      { name: 'Twin', kind: 'ai', speaking: false },
    ] })), 500)
  })
  await page.goto('/room/mute')
  await page.getByPlaceholder('Your name').fill('Parsh')
  await page.getByRole('button', { name: 'Join now' }).click()
  const preet = page.locator('.tile', { hasText: 'Preet' })
  await expect(preet.locator('.muted-ic')).toBeVisible({ timeout: 10_000 })
  await expect(preet).not.toHaveClass(/speaking/)
  await page.getByTitle('People', { exact: true }).click()
  await expect(page.locator('.person', { hasText: 'Preet' }).getByText('muted')).toBeVisible()
})
