import { expect, test } from '@playwright/test'

// Dashboard integrations + Push to Jira, with the server's HTTP API faked.
test('integrations and push to jira', async ({ page }, info) => {
  await page.routeWebSocket(/\/ws/, (ws) => ws.send(JSON.stringify({ type: 'status', state: 'listening' })))
  const json = (body: unknown) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  await page.route(/:8000\/calendar/, (r) => r.fulfill(json({ meetings: [] })))
  await page.route(/:8000\/workspace\?/, (r) => r.fulfill(json({ workspace: 'acme', repo_url: 'https://github.com/Preet37/CrusoeHacks.git', integrations: {} })))
  await page.route(/:8000\/integrations/, (r) =>
    r.fulfill(json({ integrations: { github: { connected: true, repo_url: 'https://github.com/Preet37/CrusoeHacks' }, jira: { connected: true, project: 'SCRUM' }, slack: { connected: false } } })),
  )
  await page.route(/:8000\/meetings/, (r) =>
    r.fulfill(json({ meetings: [{ id: 'm1', room: 'acme-standup', title: 'Auth', ended_at: Date.now(), summary: 'Sessions move to Redis.', decisions: ['Move sessions to Redis'], todos: [], tickets: [{ id: 'T-1', title: 'Migrate session storage to Redis', owner: 'Priya', status: 'draft', links: { jira: null } }] }] })),
  )
  await page.route(/:8000\/tickets\/T-1\/push_jira/, (r) => r.fulfill(json({ ok: true, links: { jira: 'https://example.atlassian.net/browse/SCRUM-7' } })))
  await page.route(/:8000\/tickets\?/, (r) => r.fulfill(json({ tickets: [] })))

  await page.goto('/dashboard')
  await page.getByLabel('Workspace').fill('acme')
  await page.getByLabel('Invite code').fill('x')
  await page.getByLabel('Your name').fill('Parsh')
  await page.getByRole('button', { name: 'Continue' }).click()
  const integ = page.locator('.dash-card', { hasText: 'Integrations' })
  await expect(integ.getByText('CONNECTED', { exact: true })).toHaveCount(2)
  await expect(integ.getByText('SCRUM')).toBeVisible()
  await page.getByRole('button', { name: 'Push to Jira' }).click()
  await expect(page.getByRole('link', { name: 'Open in Jira' })).toHaveAttribute('href', /SCRUM-7/)
  await expect(page.getByRole('link', { name: 'Brief .md' })).toHaveAttribute('href', /\/tickets\/T-1\/brief\.md\?workspace=acme&code=x/)
  await page.screenshot({ path: info.outputPath('jira.png'), fullPage: true })
})
