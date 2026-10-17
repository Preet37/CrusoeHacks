import { expect, test } from '@playwright/test'
import { writeFile } from 'node:fs/promises'

test('deployed repo diagram speaks a grounded contribution', async ({ page }, info) => {
  test.skip(process.env.TWIN_LIVE_E2E !== '1', 'Opt-in real repo/model/TTS calls')
  test.setTimeout(240_000)
  process.env.PW_TEST_SCREENSHOT_NO_FONTS_READY = '1'
  const base = process.env.TWIN_LIVE_URL || 'https://crusoehacks.vercel.app'
  const workspace = `speechqa-${Date.now()}`
  const received: any[] = [], sent: any[] = [], sockets: string[] = [], errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  await page.addInitScript(() => {
    const w = window as any
    w.__playback = []
    const byteSizes = new WeakMap<AudioBuffer, number>()
    const decode = BaseAudioContext.prototype.decodeAudioData
    BaseAudioContext.prototype.decodeAudioData = function (data, success, failure) {
      const bytes = data.byteLength
      const remember = (buffer: AudioBuffer) => { byteSizes.set(buffer, bytes); success?.(buffer) }
      const result = decode.call(this, data, remember, failure)
      result.then(buffer => byteSizes.set(buffer, bytes), () => {})
      return result
    }
    const original = AudioBufferSourceNode.prototype.start
    AudioBufferSourceNode.prototype.start = function (...args: Parameters<typeof original>) {
      w.__playback.push({ at: Date.now(), scheduledAt: Date.now() + Math.max(0, (args[0] || 0) - this.context.currentTime) * 1000, bytes: this.buffer ? byteSizes.get(this.buffer) : 0, duration: this.buffer?.duration, state: this.context.state })
      return original.apply(this, args)
    }
    const speak = speechSynthesis.speak.bind(speechSynthesis)
    speechSynthesis.speak = utterance => {
      utterance.addEventListener('start', () => w.__playback.push({ at: Date.now(), text: utterance.text, fallback: true }))
      speak(utterance)
    }
  })
  page.on('websocket', socket => {
    sockets.push(socket.url())
    socket.on('framereceived', frame => { try { received.push({ at: Date.now(), ...JSON.parse(String(frame.payload)) }) } catch {} })
    socket.on('framesent', frame => { try { sent.push(JSON.parse(String(frame.payload))) } catch {} })
  })
  let start = 0
  try {
    await page.goto(base)
    await page.getByLabel('Workspace', { exact: true }).fill(workspace)
    await page.getByLabel('Invite code', { exact: true }).fill(`qa-${Date.now()}`)
    await page.getByLabel('Your name', { exact: true }).fill('Speech QA')
    await page.getByRole('button', { name: 'Continue', exact: true }).click()
    await expect(page.locator('.dash-ws')).toBeVisible()
    const codebase = page.locator('.dash-card', { has: page.getByRole('heading', { name: 'Codebase', exact: true }) })
    await codebase.getByPlaceholder('https://github.com/owner/repo').fill('https://github.com/Preet37/LoominPhysicsSandbox')
    await codebase.getByRole('button', { name: 'Connect', exact: true }).click()
    await expect.poll(() => received.some(m => m.type === 'agent_event' && m.agent === 'Repository index' && m.state === 'done'), { timeout: 90_000 }).toBeTruthy()
    await page.goto(`${base}/room/speech-verification`)
    await page.getByRole('button', { name: 'Join now', exact: true }).click()
    await expect(page.locator('.tile.twin')).toBeVisible()
    await page.getByTitle('Mute (Twin stops hearing you)', { exact: true }).click()
    await page.getByTitle('Paste a spec: Twin draws a deep diagram').click()
    await page.locator('.spec textarea').fill('Sketch the architecture and workflow of the connected repository. Use only real indexed modules and import dependencies; show owners where known. Based on the repository sources, explain what would change if CAD geometry validation were bypassed, identify a concrete risk, and ask one useful design question. Do not substitute a generic example.')
    start = Date.now()
    await page.getByRole('button', { name: 'Draw it (⌘↵)' }).click()
    await expect.poll(() => sent.some(m => m.type === 'user_text' && m.source === 'typed'), { timeout: 10_000 }).toBeTruthy()
    await expect.poll(() => received.filter(m => m.at >= start && m.op === 'add_box').length, { timeout: 90_000 }).toBeGreaterThanOrEqual(3)
    await expect(page.locator('.tl-shape[data-shape-type="geo"], [data-testid="native-box"]').first()).toBeVisible({ timeout: 45_000 })
    await expect.poll(() => received.some(m => m.at >= start && m.type === 'contribution' && m.sources?.length), { timeout: 60_000 }).toBeTruthy()
    const contribution = received.find(m => m.at >= start && m.type === 'contribution' && m.sources?.length)
    expect(contribution.current).not.toMatch(/no verified repo|auth\/login\.py/i)
    expect(contribution.question).toMatch(/\?$/)
    expect(contribution.impact.length + contribution.risks.length).toBeGreaterThan(0)
    // Prove the substantive current state AND question reach actual playback,
    // whether Magpie WAV or the supported browser voice fallback is used.
    for (const phrase of [contribution.current.slice(0, 35), contribution.question.slice(0, 35)]) {
      await expect.poll(async () => {
        const chunks = received.filter(m => m.at >= contribution.at && m.type === 'audio' && m.text?.includes(phrase))
        const bytes = chunks.filter(m => m.data).map(m => Buffer.from(m.data, 'base64').length)
        return page.evaluate(({ phrase, bytes }) => (window as any).__playback.some((p: any) =>
          (p.fallback && p.text.includes(phrase)) ||
          (p.state === 'running' && bytes.includes(p.bytes) && Date.now() >= p.scheduledAt)
        ), { phrase, bytes })
      }, { timeout: 90_000 }).toBeTruthy()
    }
    await expect.poll(() => page.locator('[data-testid="native-box"], .tl-shape[data-shape-type="geo"]').count()).toBeGreaterThanOrEqual(20)
    expect(sockets.every(url => !/localhost|127\.0\.0\.1/.test(url))).toBeTruthy()
    await expect(page.locator('.tl-shape[data-shape-type="geo"], [data-testid="native-box"]').first()).toBeVisible()
    console.log('LIVE_SPEECH', JSON.stringify({ elapsed_ms: Date.now() - start, contribution, audio_chunks: received.filter(m => m.at >= start && m.type === 'audio').length, sockets: sockets.map(u => u.split('?')[0]) }))
  } finally {
    const playback = await page.evaluate(() => (window as any).__playback || []).catch(() => [])
    const board = await page.evaluate(() => {
      const board = (window as any).twinBoard
      return board ? { boxes: board.boxes.size, frames: board.frames.size, camera: board.editor.getCamera(),
        pageBounds: board.editor.getCurrentPageBounds(), focusTarget: board.focusTarget,
        renderedBoxes: document.querySelectorAll('.tl-shape[data-shape-type="geo"], [data-testid="native-box"]').length } : { renderer: 'native',
        renderedBoxes: document.querySelectorAll('[data-testid="native-box"]').length,
        renderedEdges: document.querySelectorAll('[data-testid="native-edge"]').length }
    }).catch(() => null)
    const safeEvents = received.filter(m => m.at >= start && ['draw', 'agent_event', 'contribution', 'audio', 'stop_audio', 'status'].includes(m.type)).map(({ data, ...m }) => ({ ...m, ...(data ? { audio_base64_bytes: data.length } : {}) }))
    await writeFile(info.outputPath('speech-events.json'), JSON.stringify({ base, workspace, start, errors, board, sockets: sockets.map(u => u.split('?')[0]), playback, events: safeEvents }, null, 2))
    const wav = received.find(m => m.at >= start && m.type === 'audio' && m.data && m.say_id !== 's_1')?.data
    if (wav) await writeFile(info.outputPath('speech.wav'), Buffer.from(wav, 'base64'))
    await page.screenshot({ path: info.outputPath('live-speech.png'), fullPage: true, timeout: 10_000 }).catch(() => {})
  }
})
