import { defineConfig } from '@playwright/test'

// Uses the installed Google Chrome (no browser download). Starts Vite if it isn't running.
export default defineConfig({
  testDir: './tests',
  timeout: 90_000,
  use: {
    baseURL: 'http://localhost:5173',
    channel: 'chrome',
    headless: true,
    permissions: ['camera', 'microphone'],
    launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--use-gl=angle'] },
    viewport: { width: 1440, height: 900 },
  },
  webServer: { command: 'npm run dev', url: 'http://localhost:5173', reuseExistingServer: true, timeout: 60_000 },
})
