import { expect, test } from '@playwright/test'

// Dashboard Tickets board: Jira key/link, assigned to, reporter, status, agent progress + Run agent (server API faked).
test('tickets board', async ({ page }, info) => {
  await page.routeWebSocket(/\/ws/, (ws) => ws.send(JSON.stringify({ type: 'status', state: 'listening' })))
  const json = (body: unknown) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  const runs: string[] = []
  const tickets = [
    { id: 'T-1', title: 'Add retry with backoff to the Jira push', jira_key: 'SCRUM-11', status: 'sent',
      links: { jira: 'https://crusoehacks.atlassian.net/browse/SCRUM-11' },
      jira: { assignee: 'Parsh', reporter: 'Preet', status: 'In Review', url: 'https://crusoehacks.atlassian.net/browse/SCRUM-11' },
      agent: { status: 'done', step: 'done', summary: 'push() now retries up to three times on 429/5xx.',
        plan: ['Wrap send() in a retry loop', 'Add exponential backoff'], files: [{ path: 'server/integrations/jira.py', action: 'modify' }],
        checks: [{ path: 'server/integrations/jira.py', ok: true, detail: 'Python syntax OK' }],
        tests: ['Mock a 429 then 201 and assert one issue is created'], has_patch: true, finished_at: Date.now() - 60000 },
      ended_at: '2026-10-17T23:00:00' },
    { id: 'T-2', title: 'Show Jira status on the dashboard', jira_key: 'SCRUM-12', status: 'sent',
      jira: { assignee: 'Preet', reporter: 'Parsh', status: 'To Do', url: 'https://crusoehacks.atlassian.net/browse/SCRUM-12' },
      agent: { status: 'running', step: 'writing_code' }, ended_at: '2026-10-17T23:00:00' },
    { id: 'T-3', title: 'Decide session TTL', status: 'draft', owner: 'Parsh', ended_at: '2026-10-17T22:00:00' },
  ]
  await page.route(/:8000\/tickets\?/, (r) => r.fulfill(json({ tickets })))
  await page.route(/:8000\/tickets\/T-1\/agent\?/, (r) => { runs.push('T-1'); return r.fulfill(json({ ok: true, started: true })) })
  await page.route(/:8000\/(calendar|workspace|meetings|integrations|permissions)/, (r) => r.fulfill({ status: 404, body: '{}' }))

  await page.goto('/')
  await page.getByLabel('Workspace').fill('crusoehacks')
  await page.getByLabel('Invite code').fill('x')
  await page.getByLabel('Your name').fill('Parsh')
  await page.getByRole('button', { name: 'Continue' }).click()

  const board = page.locator('.tickets-board')
  const row1 = board.locator('.tb-row', { hasText: 'SCRUM-11' })
  await expect(row1.getByRole('link', { name: 'SCRUM-11' })).toHaveAttribute('href', /browse\/SCRUM-11/)
  await expect(row1).toContainText('Parsh')
  await expect(row1).toContainText('Preet')
  await expect(row1).toContainText('In Review')
  await expect(row1).toContainText('Change ready')
  await expect(board.locator('.tb-row', { hasText: 'SCRUM-12' })).toContainText('Writing the change')
  await expect(board.locator('.tb-row', { hasText: 'Decide session TTL' }).getByRole('button', { name: 'Create in Jira' })).toBeVisible()
  await row1.locator('.tb-main').click()
  await expect(row1.getByRole('link', { name: 'Download patch' })).toHaveAttribute('href', /T-1\/agent\.patch/)
  await expect(row1).toContainText('server/integrations/jira.py')
  await page.screenshot({ path: info.outputPath('tickets.png'), fullPage: true })
  await row1.getByRole('button', { name: 'Re-run agent' }).click()
  await expect.poll(() => runs.length).toBe(1)
})
