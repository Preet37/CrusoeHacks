import { expect, test } from '@playwright/test'

// Dashboard shows a finished meeting from GET /meetings (needs a meeting saved for workspace "parshqa" on :8000).
test('past meetings render', async ({ page }, info) => {
  test.skip(!process.env.TWIN_QA_WORKSPACE, 'set TWIN_QA_WORKSPACE=parshqa:<code> after running a meeting')
  const [ws, code] = process.env.TWIN_QA_WORKSPACE!.split(':')
  await page.goto('/dashboard')
  await page.getByLabel('Workspace').fill(ws)
  await page.getByLabel('Invite code').fill(code)
  await page.getByLabel('Your name').fill('Parsh')
  await page.getByRole('button', { name: 'Continue' }).click()
  await expect(page.locator('details.meet').first()).toBeVisible({ timeout: 10_000 })
  await page.locator('details.meet summary').first().click()
  await page.locator('details.meet summary').first().click()
  await expect(page.getByText('Decisions').first()).toBeVisible()
  await page.screenshot({ path: info.outputPath('meetings.png'), fullPage: true })
})
