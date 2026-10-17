import { expect, test } from '@playwright/test'

// "/" is the home: login first, then the dashboard (remembered). Only /room/<id> opens a call.
test('home is login -> dashboard', async ({ page }) => {
  await page.routeWebSocket(/\/ws/, (ws) => ws.send(JSON.stringify({ type: 'status', state: 'listening' })))
  await page.route(/:8000\//, (r) => r.fulfill({ status: 404, body: '{}' }))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Sign in to your workspace' })).toBeVisible()
  await expect(page.locator('.tile')).toHaveCount(0)
  await page.getByLabel('Workspace').fill('acme')
  await page.getByLabel('Invite code').fill('x')
  await page.getByLabel('Your name').fill('Parsh')
  await page.getByRole('button', { name: 'Continue' }).click()
  await expect(page.locator('.dash-ws')).toHaveText('acme')
  // Remembered: reloading "/" goes straight to the dashboard.
  await page.reload()
  await expect(page.locator('.dash-ws')).toHaveText('acme')
  // Starting a meeting opens /room/<id>.
  await page.getByPlaceholder('Room name').fill('scrum')
  await page.getByRole('button', { name: 'Start meeting' }).click()
  await expect(page).toHaveURL(/\/room\/scrum/)
  await expect(page.getByRole('button', { name: 'Join now' })).toBeVisible()
})
