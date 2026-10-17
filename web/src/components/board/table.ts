import { esc, svgDoc, textWidth, truncate, type SvgOut } from './svgUtil'

/** Table as SVG. Natural size from content; if maxW given, columns are scaled down to fit. */
export function renderTableSvg(op: Record<string, unknown>, maxW?: number): SvgOut {
  const columns = Array.isArray(op.columns) ? op.columns.map(c => String(c ?? '')) : []
  const rowsRaw = Array.isArray(op.rows) ? op.rows : []
  if (!columns.length && !rowsRaw.length) throw new Error('table needs columns or rows')
  const rows = rowsRaw.map((r, i) => {
    if (!Array.isArray(r)) throw new Error(`rows[${i}] must be a list`)
    return r.map(c => String(c ?? ''))
  })
  const nCols = Math.max(columns.length, ...rows.map(r => r.length))
  const FS = 13, PAD = 12, ROW_H = 30, HEAD_H = 34
  let widths = Array.from({ length: nCols }, (_, j) => {
    const cells = [columns[j] ?? '', ...rows.map(r => r[j] ?? '')]
    return Math.min(260, Math.max(48, ...cells.map((c, k) => textWidth(c, FS) * (k === 0 ? 1.08 : 1))) + PAD * 2)
  })
  let W = widths.reduce((a, b) => a + b, 0)
  if (maxW && W > maxW) { const k = maxW / W; widths = widths.map(w => w * k); W = maxW }
  W = Math.ceil(W)
  const hasHead = columns.length > 0
  const H = (hasHead ? HEAD_H : 0) + rows.length * ROW_H + 1
  const parts: string[] = []
  parts.push(`<defs><clipPath id="c"><rect x="0" y="0" width="${W}" height="${H}" rx="12"/></clipPath></defs><g clip-path="url(#c)">`)
  parts.push(`<rect width="${W}" height="${H}" fill="#FFFFFF"/>`)
  let y = 0
  if (hasHead) {
    parts.push(`<rect width="${W}" height="${HEAD_H}" fill="#F2F2F7"/>`)
    let x = 0
    for (let j = 0; j < nCols; j++) {
      parts.push(`<text x="${(x + PAD).toFixed(1)}" y="${HEAD_H / 2 + 4.5}" font-size="${FS}" font-weight="600" fill="#1C1C1E">${esc(truncate(columns[j] ?? '', widths[j] - PAD * 2, FS))}</text>`)
      x += widths[j]
    }
    y = HEAD_H
    parts.push(`<line x1="0" x2="${W}" y1="${y}" y2="${y}" stroke="#D1D1D6"/>`)
  }
  rows.forEach((r, i) => {
    if (i % 2 === 1) parts.push(`<rect x="0" y="${y}" width="${W}" height="${ROW_H}" fill="#FAFAFC"/>`)
    let x = 0
    for (let j = 0; j < nCols; j++) {
      parts.push(`<text x="${(x + PAD).toFixed(1)}" y="${y + ROW_H / 2 + 4.5}" font-size="${FS}" fill="#3A3A3C">${esc(truncate(r[j] ?? '', widths[j] - PAD * 2, FS))}</text>`)
      x += widths[j]
    }
    y += ROW_H
    if (i < rows.length - 1) parts.push(`<line x1="0" x2="${W}" y1="${y}" y2="${y}" stroke="#EFEFF4"/>`)
  })
  let x = 0
  for (let j = 0; j < nCols - 1; j++) { x += widths[j]; parts.push(`<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="0" y2="${H}" stroke="#EFEFF4"/>`) }
  parts.push(`</g><rect x="0.5" y="0.5" width="${W - 1}" height="${H - 1}" rx="12" fill="none" stroke="#D1D1D6"/>`)
  return { svg: svgDoc(W, H, parts.join('')), w: W, h: H }
}
