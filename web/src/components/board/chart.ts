import { esc, fmtNum, PALETTE, svgDoc, truncate, type SvgOut } from './svgUtil'

interface Series { name?: string; values: unknown[] }

function niceTicks(min: number, max: number, count = 5): number[] {
  if (min === max) { max = min + 1; if (min > 0) min = 0 }
  const span = max - min
  const raw = span / count
  const mag = Math.pow(10, Math.floor(Math.log10(raw)))
  const norm = raw / mag
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag
  const lo = Math.floor(min / step) * step
  const hi = Math.ceil(max / step) * step
  const out: number[] = []
  for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10))
  return out
}

function num(v: unknown): number {
  const n = typeof v === 'number' ? v : Number(v)
  if (!isFinite(n)) throw new Error(`non-numeric value: ${JSON.stringify(v)}`)
  return n
}

/** Hand-rendered chart SVG. Throws on bad data (caller turns that into draw_error). */
export function renderChartSvg(op: Record<string, unknown>, W: number, H: number): SvgOut {
  W = Math.max(160, Math.round(W)); H = Math.max(120, Math.round(H))
  const kind = String(op.chart ?? 'bar')
  if (!['bar', 'line', 'pie', 'scatter'].includes(kind)) throw new Error(`unknown chart type: ${kind}`)
  const title = op.title ? String(op.title) : ''
  const labels = Array.isArray(op.labels) ? op.labels.map(String) : []
  let seriesRaw = op.series as unknown
  if (!Array.isArray(seriesRaw) && Array.isArray(op.values)) seriesRaw = [{ name: title, values: op.values }]
  if (!Array.isArray(seriesRaw) || seriesRaw.length === 0) throw new Error('series must be a non-empty list')
  const series: Series[] = seriesRaw.map((s, i) => {
    if (Array.isArray(s)) return { name: `Series ${i + 1}`, values: s }
    if (!s || typeof s !== 'object' || !Array.isArray((s as Series).values)) throw new Error(`series[${i}].values must be a list`)
    return s as Series
  })

  const parts: string[] = []
  parts.push(`<rect x="0.5" y="0.5" width="${W - 1}" height="${H - 1}" rx="14" fill="#FFFFFF" stroke="#E5E5EA"/>`)
  let top = 14
  if (title) {
    parts.push(`<text x="16" y="${top + 14}" font-size="15" font-weight="600" fill="#1C1C1E">${esc(truncate(title, W - 32, 15))}</text>`)
    top += 26
  }
  const multi = series.length > 1 || kind === 'pie'
  // legend
  let legendH = 0
  const legendItems: { name: string; color: string }[] =
    kind === 'pie'
      ? (series[0].values.map((_, i) => ({ name: labels[i] ?? `#${i + 1}`, color: PALETTE[i % PALETTE.length] })))
      : series.map((s, i) => ({ name: String(s.name ?? `Series ${i + 1}`), color: PALETTE[i % PALETTE.length] }))
  if (multi) {
    let lx = 16, ly = top + 6
    for (const it of legendItems) {
      const nm = truncate(it.name, 120, 11)
      const iw = 14 + nm.length * 6.2 + 14
      if (lx + iw > W - 12) { lx = 16; ly += 16 }
      parts.push(`<rect x="${lx}" y="${ly - 8}" width="9" height="9" rx="2.5" fill="${it.color}"/><text x="${lx + 13}" y="${ly}" font-size="11" fill="#3A3A3C">${esc(nm)}</text>`)
      lx += iw
    }
    legendH = ly - top + 10
    top += legendH
  }

  if (kind === 'pie') {
    const vals = series[0].values.map(num)
    if (vals.some(v => v < 0)) throw new Error('pie values must be >= 0')
    const total = vals.reduce((a, b) => a + b, 0)
    if (total <= 0) throw new Error('pie values sum to 0')
    const cx = W / 2, cy = top + (H - top) / 2
    const r = Math.max(10, Math.min(W - 40, H - top - 20) / 2)
    let a0 = -Math.PI / 2
    vals.forEach((v, i) => {
      const frac = v / total
      const a1 = a0 + frac * Math.PI * 2
      const color = PALETTE[i % PALETTE.length]
      if (frac >= 0.9999) {
        parts.push(`<circle cx="${cx}" cy="${cy}" r="${r}" fill="${color}" stroke="#fff" stroke-width="2"/>`)
      } else if (frac > 0) {
        const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0)
        const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1)
        const large = a1 - a0 > Math.PI ? 1 : 0
        parts.push(`<path d="M${cx.toFixed(2)},${cy.toFixed(2)} L${x0.toFixed(2)},${y0.toFixed(2)} A${r.toFixed(2)},${r.toFixed(2)} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)} Z" fill="${color}" stroke="#fff" stroke-width="2"/>`)
      }
      if (frac > 0.06) {
        const am = (a0 + a1) / 2
        parts.push(`<text x="${(cx + r * 0.62 * Math.cos(am)).toFixed(1)}" y="${(cy + r * 0.62 * Math.sin(am) + 4).toFixed(1)}" font-size="11" font-weight="600" fill="#fff" text-anchor="middle">${Math.round(frac * 100)}%</text>`)
      }
      a0 = a1
    })
    return { svg: svgDoc(W, H, parts.join('')), w: W, h: H }
  }

  // cartesian
  const isScatter = kind === 'scatter'
  let xs: number[] = [], ys: number[] = []
  const pts: [number, number][][] = []
  if (isScatter) {
    for (const s of series) {
      const sp = s.values.map((p, j) => {
        if (!Array.isArray(p) || p.length < 2) throw new Error(`scatter point ${j} must be [x, y]`)
        return [num(p[0]), num(p[1])] as [number, number]
      })
      pts.push(sp); xs.push(...sp.map(p => p[0])); ys.push(...sp.map(p => p[1]))
    }
    if (!xs.length) throw new Error('no points')
  } else {
    for (const s of series) ys.push(...s.values.map(num))
    if (!ys.length) throw new Error('no values')
  }
  let yMin = Math.min(...ys), yMax = Math.max(...ys)
  if (!isScatter || yMin > 0) yMin = Math.min(0, yMin)
  const yTicks = niceTicks(yMin, yMax)
  const y0v = yTicks[0], y1v = yTicks[yTicks.length - 1]
  const yLabelW = Math.max(...yTicks.map(t => fmtNum(t).length)) * 6.5 + 10
  const L = 12 + yLabelW, R = W - 16, T = top + 8, B = H - 28
  if (B - T < 20) throw new Error('chart box too small')
  const sy = (v: number) => B - ((v - y0v) / (y1v - y0v || 1)) * (B - T)
  for (const t of yTicks) {
    const y = sy(t).toFixed(1)
    parts.push(`<line x1="${L}" x2="${R}" y1="${y}" y2="${y}" stroke="${t === 0 ? '#C7C7CC' : '#F2F2F7'}" stroke-width="1"/>`)
    parts.push(`<text x="${L - 6}" y="${(+y + 4).toFixed(1)}" font-size="10.5" fill="#8E8E93" text-anchor="end">${esc(fmtNum(t))}</text>`)
  }
  parts.push(`<line x1="${L}" x2="${L}" y1="${T}" y2="${B}" stroke="#C7C7CC"/>`)

  if (isScatter) {
    const xTicks = niceTicks(Math.min(...xs), Math.max(...xs))
    const x0v = xTicks[0], x1v = xTicks[xTicks.length - 1]
    const sx = (v: number) => L + ((v - x0v) / (x1v - x0v || 1)) * (R - L)
    for (const t of xTicks) parts.push(`<text x="${sx(t).toFixed(1)}" y="${B + 16}" font-size="10.5" fill="#8E8E93" text-anchor="middle">${esc(fmtNum(t))}</text>`)
    parts.push(`<line x1="${L}" x2="${R}" y1="${B}" y2="${B}" stroke="#C7C7CC"/>`)
    pts.forEach((sp, i) => {
      const c = PALETTE[i % PALETTE.length]
      for (const [x, y] of sp) parts.push(`<circle cx="${sx(x).toFixed(1)}" cy="${sy(y).toFixed(1)}" r="4" fill="${c}" fill-opacity="0.85" stroke="#fff" stroke-width="1"/>`)
    })
    return { svg: svgDoc(W, H, parts.join('')), w: W, h: H }
  }

  const n = Math.max(labels.length, ...series.map(s => s.values.length))
  const band = (R - L) / Math.max(1, n)
  for (let i = 0; i < n; i++) {
    const lab = labels[i] ?? String(i + 1)
    parts.push(`<text x="${(L + band * (i + 0.5)).toFixed(1)}" y="${B + 16}" font-size="10.5" fill="#636366" text-anchor="middle">${esc(truncate(lab, band - 4, 10.5))}</text>`)
  }
  if (kind === 'bar') {
    const groupW = band * 0.72
    const bw = groupW / series.length
    series.forEach((s, si) => {
      const c = PALETTE[si % PALETTE.length]
      s.values.forEach((v, i) => {
        const val = num(v)
        const x = L + band * i + (band - groupW) / 2 + bw * si
        const ya = sy(Math.max(0, val)), yb = sy(Math.min(0, val))
        const hh = Math.max(1, yb - ya)
        parts.push(`<rect x="${(x + 1).toFixed(1)}" y="${ya.toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${hh.toFixed(1)}" rx="${Math.min(4, bw / 4).toFixed(1)}" fill="${c}"/>`)
        if (bw >= 22) parts.push(`<text x="${(x + bw / 2).toFixed(1)}" y="${(ya - 4).toFixed(1)}" font-size="10" fill="#3A3A3C" text-anchor="middle">${esc(fmtNum(val))}</text>`)
      })
    })
  } else {
    series.forEach((s, si) => {
      const c = PALETTE[si % PALETTE.length]
      const p = s.values.map((v, i) => `${(L + band * (i + 0.5)).toFixed(1)},${sy(num(v)).toFixed(1)}`)
      parts.push(`<polyline points="${p.join(' ')}" fill="none" stroke="${c}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>`)
      for (const q of p) { const [x, y] = q.split(','); parts.push(`<circle cx="${x}" cy="${y}" r="3.5" fill="#fff" stroke="${c}" stroke-width="2"/>`) }
    })
  }
  return { svg: svgDoc(W, H, parts.join('')), w: W, h: H }
}
