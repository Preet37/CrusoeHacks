// Mermaid -> sanitized standalone SVG string (browser only).
let initialized = false
let counter = 0

export async function renderMermaidSvg(code: string): Promise<{ svg: string; w: number; h: number }> {
  const { default: mermaid } = await import('mermaid')
  if (!initialized) {
    mermaid.initialize({
      startOnLoad: false,
      theme: 'neutral',
      securityLevel: 'strict',
      htmlLabels: false,
      flowchart: { htmlLabels: false },
      fontFamily: `-apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif`,
    })
    initialized = true
  }
  const renderId = `twin-mmd-${Date.now().toString(36)}-${counter++}`
  let raw: string
  try {
    await mermaid.parse(code)
    raw = (await mermaid.render(renderId, code)).svg
  } finally {
    // mermaid can leave temp nodes behind on failure
    document.getElementById(renderId)?.remove()
    document.getElementById('d' + renderId)?.remove()
  }
  // Re-serialize through the DOM so the result is well-formed XML usable inside <img>.
  const doc = new DOMParser().parseFromString(raw, 'text/html')
  const svgEl = doc.querySelector('svg')
  if (!svgEl) throw new Error('mermaid produced no SVG')
  let w = 0, h = 0
  const vb = svgEl.getAttribute('viewBox')?.split(/[\s,]+/).map(Number)
  if (vb && vb.length === 4 && vb.every(isFinite)) { w = vb[2]; h = vb[3] }
  if (!w || !h) { w = parseFloat(svgEl.getAttribute('width') || '') || 400; h = parseFloat(svgEl.getAttribute('height') || '') || 300 }
  svgEl.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  svgEl.setAttribute('width', String(w))
  svgEl.setAttribute('height', String(h))
  svgEl.removeAttribute('style')
  svgEl.querySelectorAll('script').forEach(s => s.remove())
  // white rounded background so it reads on any canvas theme
  const bg = doc.createElementNS('http://www.w3.org/2000/svg', 'rect')
  const [vx, vy] = vb && vb.length === 4 ? vb : [0, 0]
  bg.setAttribute('x', String(vx)); bg.setAttribute('y', String(vy))
  bg.setAttribute('width', String(w)); bg.setAttribute('height', String(h))
  bg.setAttribute('fill', '#ffffff')
  svgEl.insertBefore(bg, svgEl.firstChild)
  const svg = new XMLSerializer().serializeToString(svgEl)
  return { svg, w, h }
}
