import { esc, MONO, svgDoc, type SvgOut } from './svgUtil'

const KW: Record<string, string[]> = {
  python: 'def class return if elif else for while in not and or is import from as with try except finally raise pass break continue lambda yield None True False async await global nonlocal assert del self'.split(' '),
  js: 'const let var function return if else for while do in of new class extends super this import from export default async await try catch finally throw typeof instanceof null undefined true false switch case break continue yield delete void'.split(' '),
}
KW.ts = [...KW.js, ...'interface type enum implements private public protected readonly as keyof declare namespace abstract string number boolean any unknown never'.split(' ')]
const ALIAS: Record<string, string> = { javascript: 'js', jsx: 'js', typescript: 'ts', tsx: 'ts', py: 'python' }

const C = { text: '#E5E5EA', kw: '#FF7AB2', str: '#FC6A5D', com: '#6C7986', num: '#D9C97C', fn: '#67B7A4', ln: '#5A5A60' }

function highlight(line: string, lang: string): string {
  const kws = KW[lang]
  if (!kws) return `<tspan fill="${C.text}">${esc(line)}</tspan>`
  const comment = lang === 'python' ? '#[^\\n]*' : '\\/\\/[^\\n]*'
  const re = new RegExp(`(${comment})|("(?:[^"\\\\]|\\\\.)*"?|'(?:[^'\\\\]|\\\\.)*'?|\`(?:[^\`\\\\]|\\\\.)*\`?)|\\b(\\d+(?:\\.\\d+)?)\\b|\\b([A-Za-z_$][\\w$]*)\\b(?=\\s*\\()|\\b([A-Za-z_$][\\w$]*)\\b`, 'g')
  let out = '', last = 0
  let m: RegExpExecArray | null
  const plain = (s: string) => (s ? `<tspan fill="${C.text}">${esc(s)}</tspan>` : '')
  while ((m = re.exec(line))) {
    out += plain(line.slice(last, m.index))
    const [tok, com, str, numb, fn, word] = m
    let color = C.text
    if (com) color = C.com
    else if (str) color = C.str
    else if (numb) color = C.num
    else if (fn) color = kws.includes(fn) ? C.kw : C.fn
    else if (word && kws.includes(word)) color = C.kw
    out += `<tspan fill="${color}">${esc(tok)}</tspan>`
    last = m.index + tok.length
    if (tok.length === 0) re.lastIndex++
  }
  return out + plain(line.slice(last))
}

export function renderCodeSvg(op: Record<string, unknown>, W: number, H: number): SvgOut {
  W = Math.max(160, Math.round(W)); H = Math.max(80, Math.round(H))
  const code = String(op.code ?? '').replace(/\t/g, '    ').replace(/\r/g, '')
  const langRaw = String(op.language ?? '').toLowerCase()
  const lang = ALIAS[langRaw] ?? langRaw
  const FS = 12.5, LH = 18, HEAD = 30, PADX = 14
  const lines = code.split('\n')
  const maxLines = Math.max(1, Math.floor((H - HEAD - 12) / LH))
  const shown = lines.slice(0, maxLines)
  const clipped = lines.length > maxLines
  const gutter = String(shown.length).length * 7.6 + 14
  const parts: string[] = []
  parts.push(`<defs><clipPath id="cc"><rect x="${PADX}" y="${HEAD}" width="${W - PADX * 2}" height="${H - HEAD}"/></clipPath></defs>`)
  parts.push(`<rect width="${W}" height="${H}" rx="14" fill="#1E1F24"/><rect x="0.5" y="0.5" width="${W - 1}" height="${H - 1}" rx="14" fill="none" stroke="#FFFFFF" stroke-opacity="0.08"/>`)
  ;['#FF5F57', '#FEBC2E', '#28C840'].forEach((c, i) => parts.push(`<circle cx="${16 + i * 16}" cy="15" r="5" fill="${c}"/>`))
  if (langRaw) parts.push(`<text x="${W - 14}" y="19" font-size="11" font-weight="500" fill="#8E8E93" text-anchor="end">${esc(langRaw)}</text>`)
  parts.push(`<g clip-path="url(#cc)">`)
  shown.forEach((ln, i) => {
    const y = HEAD + 6 + LH * (i + 1) - 5
    parts.push(`<text class="m" x="${PADX + gutter - 10}" y="${y}" font-size="${FS - 1}" fill="${C.ln}" text-anchor="end">${i + 1}</text>`)
    const isLast = clipped && i === shown.length - 1
    parts.push(`<text class="m" x="${PADX + gutter}" y="${y}" font-size="${FS}" xml:space="preserve">${isLast ? `<tspan fill="${C.com}">…</tspan>` : highlight(ln, lang)}</text>`)
  })
  parts.push(`</g>`)
  return { svg: svgDoc(W, H, parts.join(''), `.m{font-family:${MONO};white-space:pre}`), w: W, h: H }
}
