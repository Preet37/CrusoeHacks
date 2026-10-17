/** add_3d scene model + validation (no Three.js import: keeps three out of the main chunk). */
export type Vec3 = [number, number, number]
export const SHAPES_3D = ['box', 'cylinder', 'sphere', 'cone', 'torus', 'plane'] as const
export type Shape3D = (typeof SHAPES_3D)[number]
export interface Obj3D { id: string; shape: Shape3D; pos: Vec3; rot: Vec3; scale: Vec3; color: string; label?: string; material?: string }
export interface Scene3D { autoRotate: boolean; objects: Obj3D[]; gltf?: string }

export const MAX_OBJECTS_3D = 60
const COLOR_RE = /^#[0-9a-f]{3,8}$|^[a-z]{3,20}$/i

function vec(v: unknown, d: Vec3, where: string): Vec3 {
  if (v === undefined || v === null) return d
  if (typeof v === 'number' && isFinite(v)) return [v, v, v] // uniform scale shorthand
  if (!Array.isArray(v) || v.length !== 3) throw new Error(`${where}: expected [x,y,z]`)
  return v.map((n, i) => {
    const x = typeof n === 'number' ? n : Number(n)
    if (!isFinite(x)) throw new Error(`${where}[${i}] is not a finite number`)
    return Math.max(-1e4, Math.min(1e4, x))
  }) as Vec3
}

/** Throws a short human-readable message (sent back as draw_error) on bad input. */
export function validateScene(raw: unknown): Scene3D {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('scene must be an object')
  const s = raw as Record<string, unknown>
  let gltf: string | undefined
  if (s.gltf !== undefined && s.gltf !== null && s.gltf !== '') {
    if (typeof s.gltf !== 'string' || !/^(https?:|data:|\/)/.test(s.gltf)) throw new Error('scene.gltf must be an http(s) URL')
    gltf = s.gltf
  }
  if (s.objects !== undefined && !Array.isArray(s.objects)) throw new Error('scene.objects must be an array')
  const list = (s.objects as unknown[] | undefined) ?? []
  if (!list.length && !gltf) throw new Error('scene needs objects or gltf')
  const objects: Obj3D[] = list.slice(0, MAX_OBJECTS_3D).map((o, i) => {
    if (!o || typeof o !== 'object') throw new Error(`objects[${i}] must be an object`)
    const r = o as Record<string, unknown>
    const shape = String(r.shape ?? '').toLowerCase()
    if (!(SHAPES_3D as readonly string[]).includes(shape)) throw new Error(`objects[${i}].shape "${String(r.shape)}" unknown (use ${SHAPES_3D.join('|')})`)
    const color = typeof r.color === 'string' && COLOR_RE.test(r.color) ? r.color : '#8a8f98'
    return {
      id: String(r.id ?? `o${i}`),
      shape: shape as Shape3D,
      pos: vec(r.pos, [0, 0, 0], `objects[${i}].pos`),
      rot: vec(r.rot, [0, 0, 0], `objects[${i}].rot`),
      scale: vec(r.scale, [1, 1, 1], `objects[${i}].scale`),
      color,
      ...(typeof r.label === 'string' && r.label ? { label: r.label.slice(0, 40) } : {}),
      ...(typeof r.material === 'string' && r.material ? { material: r.material.slice(0, 20) } : {}),
    }
  })
  return { autoRotate: s.autoRotate === true, objects, ...(gltf ? { gltf } : {}) }
}
