import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// After a redeploy, lazy chunks from the old build are gone. Vite fires vite:preloadError when one fails to load:
// reload once to pick up the new build (guarded so a real outage can't cause a reload loop).
window.addEventListener('vite:preloadError', (event) => {
  const KEY = 'twin-chunk-reload' // same key as optional.tsx: never two reloads
  let last = 0
  try {
    last = Number(sessionStorage.getItem(KEY) || 0)
  } catch {
    /* ignore */
  }
  if (Date.now() - last < 30_000) return
  try {
    sessionStorage.setItem(KEY, String(Date.now()))
  } catch {
    /* ignore */
  }
  event.preventDefault()
  window.location.reload()
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
