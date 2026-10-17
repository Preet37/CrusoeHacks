// Pure helpers shared by the SVG renderers (no tldraw / DOM imports so they can be unit-tested in node).
export const FONT = `-apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, 'Helvetica Neue', Arial, sans-serif`
export const MONO = `'SF Mono', SFMono-Regular, Menlo, Monaco, Consolas, monospace`
export const PALETTE = ['#007AFF', '#FF9500', '#34C759', '#AF52DE', '#FF2D55', '#5AC8FA', '#FFCC00', '#8E8E93']

export function esc(s: unknown): string {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;')
    // strip XML-illegal control chars
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, '')
}

export function svgDoc(w: number, h: number, body: string, style = ''): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><style>text{font-family:${FONT}}${style}</style>${body}</svg>`
}

/** Approximate rendered text width for the system font. */
export function textWidth(s: string, size: number, mono = false): number {
  return String(s).length * size * (mono ? 0.61 : 0.56)
}

export function truncate(s: string, maxW: number, size: number, mono = false): string {
  s = String(s ?? '')
  if (textWidth(s, size, mono) <= maxW) return s
  const n = Math.max(1, Math.floor(maxW / (size * (mono ? 0.61 : 0.56))) - 1)
  return s.slice(0, n) + '…'
}

export function fmtNum(v: number): string {
  if (!isFinite(v)) return ''
  const a = Math.abs(v)
  if (a >= 1e9) return +(v / 1e9).toFixed(1) + 'B'
  if (a >= 1e6) return +(v / 1e6).toFixed(1) + 'M'
  if (a >= 1e4) return +(v / 1e3).toFixed(1) + 'k'
  return String(+v.toFixed(2))
}

export interface SvgOut { svg: string; w: number; h: number }
