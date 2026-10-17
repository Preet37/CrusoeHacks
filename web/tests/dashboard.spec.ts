import { expect, test } from '@playwright/test'

// Workspace sign-in -> dashboard (live server on :8000) -> join a calendar meeting -> leave back to dashboard.
test('workspace dashboard', async ({ page }, info) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(String(e)))
  await page.goto('/dashboard')
  await page.getByLabel('Workspace').fill('CrusoeHacks')
  await page.getByLabel('Invite code').fill('demo-code')
  await page.getByPlaceholder('Your name').fill('Parsh')
  await page.getByRole('button', { name: 'Join now' }).click()

  await expect(page.locator('.dash-ws')).toHaveText('crusoehacks')
  await expect(page.locator('.dash-row').first()).toBeVisible({ timeout: 10_000 })
  await expect(page.getByText('Daily standup')).toBeVisible()
  // Twin's scores arrive at join; the lazy Scoreboard must still show them (ws.ts replay).
  await expect(page.locator('.dash-card', { hasText: "Twin's learning" }).getByText('No scores yet')).toHaveCount(0, { timeout: 10_000 })
  await page.waitForTimeout(1500)
  await page.screenshot({ path: info.outputPath('dashboard.png'), fullPage: true })

  // Join from the calendar -> join screen for that room (workspace remembered) -> call.
  await page.getByRole('button', { name: 'Join', exact: true }).first().click()
  await expect(page).toHaveURL(/\/room\//)
  await page.getByRole('button', { name: 'Join now' }).click()
  await expect(page.locator('.tile.twin')).toBeVisible()
  await expect(page.locator('.brief')).toBeVisible({ timeout: 15_000 })
  await page.screenshot({ path: info.outputPath('call-from-calendar.png') })

  await page.getByTitle('Leave call').click()
  await expect(page.locator('.dash-ws')).toBeVisible()
  expect(errors).toEqual([])
})
