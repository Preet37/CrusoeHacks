// Adapted from LoominPhysicsSandbox (Preet37, pre-existing project) lib/partRig.js
/**
 * Part rigging for library GLBs, in the browser.
 *
 *  0. namedRig — fast path: GLB nodes named blade/rotor/propeller/wheel/fan/hub are wrapped
 *     in a pivot at their own centre and spun directly (no model call).
 *  1. captureViews — render the mesh orthographically front + top to small JPEGs.
 *  2. POST /cad/part-rig — a vision model boxes the moving parts in each view.
 *  3. partBoxes — each 2D box is unprojected and the views combined into one 3D box per part.
 *  4. applyRig — mesh pieces inside each spin/tilt box are cut out into their own pivot group.
 *  5. driveRig — every frame: spin parts turn at a constant speed, tilt parts sway gently.
 */
import * as THREE from 'three'

const SIZE = 448
const AXES = ['x', 'y', 'z'] as const
type Axis = (typeof AXES)[number]
type View = 'front' | 'top'
export type PartKind = 'spin' | 'tilt' | 'static'
export interface VisionPart { name: string; kind: PartKind; axis?: Axis | null; boxes: Partial<Record<View, [number, number, number, number]>> }
export interface BoxedPart extends VisionPart { box: THREE.Box3 }
export interface RigPart { name: string; kind: PartKind; pivot: THREE.Object3D | null; axis: THREE.Vector3; angle: number; box: THREE.Box3 }

export const MOVING_NAME = /blade|rotor|propeller|prop|wheel|fan|hub/i
/** Blades ~1 rev / 2 s. */
export const SPIN_RAD_PER_S = Math.PI

function viewCameras(bounds: THREE.Box3) {
  const c = bounds.getCenter(new THREE.Vector3())
  const s = bounds.getSize(new THREE.Vector3())
  const r = Math.max(s.x, s.y, s.z) * 0.6
  const make = (pos: THREE.Vector3, up: THREE.Vector3) => {
    const cam = new THREE.OrthographicCamera(-r, r, r, -r, 0.01, r * 20)
    cam.position.copy(c).add(pos.multiplyScalar(r * 5))
    cam.up.copy(up)
    cam.lookAt(c)
    cam.updateMatrixWorld()
    cam.updateProjectionMatrix()
    return cam
  }
  return {
    front: make(new THREE.Vector3(0, 0, 1), new THREE.Vector3(0, 1, 0)),
    top: make(new THREE.Vector3(0, 1, 0), new THREE.Vector3(0, 0, -1)),
  } as Record<View, THREE.OrthographicCamera>
}

/** A copy of the model in the root's own space (so boxes come back in that space). */
function detachedClone(model: THREE.Object3D) {
  const clone = model.clone(true)
  clone.position.set(0, 0, 0)
  clone.rotation.set(0, 0, 0)
  clone.scale.set(1, 1, 1)
  clone.updateMatrixWorld(true)
  return clone
}

export function captureViews(gl: THREE.WebGLRenderer, model: THREE.Object3D) {
  const clone = detachedClone(model)
  const bounds = new THREE.Box3().setFromObject(clone)
  const scene = new THREE.Scene()
  scene.background = new THREE.Color('#f2f3f5')
  scene.add(new THREE.AmbientLight(0xffffff, 1.4))
  const sun = new THREE.DirectionalLight(0xffffff, 1.6)
  sun.position.set(3, 5, 4)
  scene.add(sun, clone)
  const cams = viewCameras(bounds)
  const target = new THREE.WebGLRenderTarget(SIZE, SIZE, { colorSpace: THREE.SRGBColorSpace })
  const pixels = new Uint8Array(SIZE * SIZE * 4)
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = SIZE
  const ctx = canvas.getContext('2d')!
  const images: Partial<Record<View, string>> = {}
  const prevTarget = gl.getRenderTarget()
  for (const [name, cam] of Object.entries(cams) as [View, THREE.OrthographicCamera][]) {
    gl.setRenderTarget(target)
    gl.render(scene, cam)
    gl.readRenderTargetPixels(target, 0, 0, SIZE, SIZE, pixels)
    const img = ctx.createImageData(SIZE, SIZE) // WebGL rows are bottom-up
    for (let y = 0; y < SIZE; y++) img.data.set(pixels.subarray((SIZE - 1 - y) * SIZE * 4, (SIZE - y) * SIZE * 4), y * SIZE * 4)
    ctx.putImageData(img, 0, 0)
    images[name] = canvas.toDataURL('image/jpeg', 0.8)
  }
  gl.setRenderTarget(prevTarget)
  target.dispose()
  scene.remove(clone)
  return { images: images as Record<View, string>, bounds }
}

/** 2D boxes (0..1 fractions) per view -> one 3D box per part, in the model root's space. */
export function partBoxes(parts: VisionPart[], bounds: THREE.Box3): BoxedPart[] {
  const cams = viewCameras(bounds)
  return parts.map(part => {
    const ranges: Record<Axis, [number, number][]> = { x: [], y: [], z: [] }
    for (const [view, box] of Object.entries(part.boxes || {}) as [View, [number, number, number, number]][]) {
      const cam = cams[view]
      if (!cam || !box) continue
      const [u0, v0, u1, v1] = box
      const a = new THREE.Vector3(u0 * 2 - 1, 1 - v0 * 2, 0).unproject(cam)
      const b = new THREE.Vector3(u1 * 2 - 1, 1 - v1 * 2, 0).unproject(cam)
      const along = cam.getWorldDirection(new THREE.Vector3()) // carries no information in this view
      for (const axis of AXES) {
        if (Math.abs(along[axis]) > 0.5) continue
        ranges[axis].push([Math.min(a[axis], b[axis]), Math.max(a[axis], b[axis])])
      }
    }
    const min = new THREE.Vector3(), max = new THREE.Vector3()
    for (const axis of AXES) {
      const rs = ranges[axis]
      if (!rs.length) { min[axis] = bounds.min[axis]; max[axis] = bounds.max[axis]; continue }
      min[axis] = rs.reduce((s, r) => s + r[0], 0) / rs.length
      max[axis] = rs.reduce((s, r) => s + r[1], 0) / rs.length
    }
    return { ...part, box: new THREE.Box3(min, max) }
  })
}

/** Copy the listed triangles of a non-indexed geometry, positions moved into root space. */
function takeTriangles(geom: THREE.BufferGeometry, tris: number[], toRoot: THREE.Matrix4) {
  const out = new THREE.BufferGeometry()
  const normalMatrix = new THREE.Matrix3().getNormalMatrix(toRoot)
  const v = new THREE.Vector3()
  for (const [name, attr] of Object.entries(geom.attributes) as [string, THREE.BufferAttribute][]) {
    const size = attr.itemSize
    const arr = new Float32Array(tris.length * 3 * size)
    let w = 0
    for (const t of tris) {
      for (let k = t * 3; k < t * 3 + 3; k++) {
        if (name === 'position') {
          v.fromBufferAttribute(attr, k).applyMatrix4(toRoot); arr[w++] = v.x; arr[w++] = v.y; arr[w++] = v.z
        } else if (name === 'normal') {
          v.fromBufferAttribute(attr, k).applyMatrix3(normalMatrix).normalize(); arr[w++] = v.x; arr[w++] = v.y; arr[w++] = v.z
        } else {
          for (let c = 0; c < size; c++) arr[w++] = attr.getComponent(k, c)
        }
      }
    }
    out.setAttribute(name, new THREE.BufferAttribute(arr, size, attr.normalized))
  }
  return out
}

interface Piece { tris: number[]; box: THREE.Box3; center: THREE.Vector3; size: THREE.Vector3 }

/** Split a mesh's triangles into connected pieces (shared edges of exactly two triangles). */
export function pieces(geom: THREE.BufferGeometry, toRoot: THREE.Matrix4): Piece[] {
  const pos = geom.attributes.position
  const index = geom.index ? geom.index.array : null
  const triCount = index ? index.length / 3 : pos.count / 3
  const vert = (t: number, k: number) => (index ? index[t * 3 + k] : t * 3 + k)
  const canon = new Int32Array(pos.count)
  if (index) {
    for (let i = 0; i < pos.count; i++) canon[i] = i
  } else {
    const weld = new Map<string, number>()
    for (let i = 0; i < pos.count; i++) {
      const key = `${pos.getX(i)},${pos.getY(i)},${pos.getZ(i)}`
      let id = weld.get(key)
      if (id === undefined) { id = weld.size; weld.set(key, id) }
      canon[i] = id
    }
  }
  const parent = new Int32Array(triCount).map((_, i) => i)
  const find = (x: number) => { while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x] } return x }
  const edges = new Map<number, number[]>()
  for (let t = 0; t < triCount; t++) {
    for (let k = 0; k < 3; k++) {
      const a = canon[vert(t, k)], b = canon[vert(t, (k + 1) % 3)]
      const key = a < b ? a * 4294967296 + b : b * 4294967296 + a
      const list = edges.get(key)
      if (list) list.push(t); else edges.set(key, [t])
    }
  }
  for (const list of edges.values()) {
    if (list.length !== 2) continue
    const ra = find(list[0]), rb = find(list[1])
    if (ra !== rb) parent[rb] = ra
  }
  const groups = new Map<number, { tris: number[]; box: THREE.Box3 }>()
  const v = new THREE.Vector3()
  for (let t = 0; t < triCount; t++) {
    const root = find(t)
    let g = groups.get(root)
    if (!g) { g = { tris: [], box: new THREE.Box3() }; groups.set(root, g) }
    g.tris.push(t)
    for (let k = 0; k < 3; k++) g.box.expandByPoint(v.fromBufferAttribute(pos, vert(t, k)).applyMatrix4(toRoot))
  }
  return [...groups.values()].map(g => ({ ...g, center: g.box.getCenter(new THREE.Vector3()), size: g.box.getSize(new THREE.Vector3()) }))
}

const axisVec = (a: Axis) => new THREE.Vector3(a === 'x' ? 1 : 0, a === 'y' ? 1 : 0, a === 'z' ? 1 : 0)

/**
 * Cut each spin/tilt part out of the model into a pivot group under the model root.
 * Static parts are returned for labelling only. Whole mesh pieces, never sliced.
 */
export function applyRig(model: THREE.Object3D, parts: BoxedPart[]): RigPart[] {
  model.updateMatrixWorld(true)
  const rootInv = new THREE.Matrix4().copy(model.matrixWorld).invert()
  const meshes: THREE.Mesh[] = []
  model.traverse(o => { if ((o as THREE.Mesh).isMesh && !o.userData.rigPart) meshes.push(o as THREE.Mesh) })
  const moving = parts.filter(p => p.kind !== 'static')
  const rig: RigPart[] = parts.filter(p => p.kind === 'static').map(p => ({ name: p.name, kind: p.kind, pivot: null, axis: new THREE.Vector3(0, 1, 0), angle: 0, box: p.box }))
  if (!moving.length) return rig
  const info = meshes.map(mesh => {
    const toRoot = new THREE.Matrix4().multiplyMatrices(rootInv, mesh.matrixWorld)
    return { mesh, toRoot, pieces: pieces(mesh.geometry, toRoot), taken: new Set<number>() }
  })
  for (const m of info) { // triangle order survives toNonIndexed (t -> vertices 3t..3t+2)
    if (m.mesh.geometry.index) { const flat = m.mesh.geometry.toNonIndexed(); m.mesh.geometry = flat }
  }
  type Pick = { part: BoxedPart; chosen: { m: (typeof info)[number]; pc: Piece }[]; center: THREE.Vector3; axisName: Axis }
  const picks: Pick[] = []
  const used = new Set<Piece>()
  for (const part of moving) {
    const size = part.box.getSize(new THREE.Vector3())
    const grown = part.box.clone().expandByVector(size.clone().multiplyScalar(0.12))
    const limit = size.clone().multiplyScalar(1.3).addScalar(1e-4)
    const chosen: Pick['chosen'] = []
    for (const m of info) for (const pc of m.pieces) {
      if (used.has(pc) || !grown.containsPoint(pc.center)) continue
      if (pc.size.x > limit.x || pc.size.y > limit.y || pc.size.z > limit.z) continue
      chosen.push({ m, pc })
    }
    if (!chosen.length) continue // the box landed on nothing real
    chosen.forEach(({ pc }) => used.add(pc))
    const core = chosen.filter(({ pc }) => part.box.containsPoint(pc.center))
    const extent = new THREE.Box3()
    for (const { pc } of core.length ? core : chosen) extent.union(pc.box)
    const ext = extent.getSize(new THREE.Vector3())
    const thinnest = [...AXES].sort((a, b) => ext[a] - ext[b])[0]
    const axisName: Axis = part.kind === 'spin' ? (part.axis && AXES.includes(part.axis) ? part.axis : thinnest) : (ext.x >= ext.z ? 'x' : 'z')
    picks.push({ part, chosen, center: extent.getCenter(new THREE.Vector3()), axisName })
  }
  // A spinning part is anchored on its largest round piece (rotor disc, tyre).
  for (const pk of picks) {
    if (pk.part.kind !== 'spin') continue
    const ax = pk.axisName
    const [p1, p2] = AXES.filter(a => a !== ax)
    const roundness = (pc: Piece) => Math.min(pc.size[p1], pc.size[p2]) / Math.max(pc.size[p1], pc.size[p2], 1e-9)
    const disc = (pc: Piece) => pc.size[ax] <= Math.max(pc.size[p1], pc.size[p2])
    const score = (pc: Piece) => pc.tris.length * (roundness(pc) >= 0.6 && disc(pc) ? 1 : 0.05)
    const anchor = pk.chosen.reduce((a, b) => (score(b.pc) > score(a.pc) ? b : a)).pc
    const half = Math.max(anchor.size[ax] * 0.6, anchor.size[p1] * 0.25, anchor.size[p2] * 0.25) + 1e-4
    pk.chosen = pk.chosen.filter(({ pc }) => Math.abs(pc.center[ax] - anchor.center[ax]) <= half)
    const hub = new THREE.Box3()
    for (const { pc } of pk.chosen) hub.union(pc.box)
    pk.center = hub.getCenter(new THREE.Vector3())
  }
  for (const { part, chosen, center, axisName } of picks) {
    const pivot = new THREE.Group()
    pivot.userData.rigPart = true
    pivot.position.copy(center)
    const byMesh = new Map<(typeof info)[number], number[]>()
    for (const { m, pc } of chosen) {
      if (!byMesh.has(m)) byMesh.set(m, [])
      byMesh.get(m)!.push(...pc.tris)
      pc.tris.forEach(t => m.taken.add(t))
    }
    for (const [m, tris] of byMesh) {
      const geom = takeTriangles(m.mesh.geometry, tris, m.toRoot)
      geom.translate(-center.x, -center.y, -center.z)
      const piece = new THREE.Mesh(geom, m.mesh.material)
      piece.castShadow = piece.receiveShadow = true
      piece.userData.rigPart = true
      pivot.add(piece)
    }
    model.add(pivot)
    rig.push({ name: part.name, kind: part.kind, pivot, axis: axisVec(axisName), angle: 0, box: part.box })
  }
  for (const m of info) { // remove claimed triangles from the body so nothing is drawn twice
    if (!m.taken.size) continue
    const geom = m.mesh.geometry
    const keep: number[] = []
    for (let t = 0; t < geom.attributes.position.count / 3; t++) if (!m.taken.has(t)) keep.push(t)
    // Identity: the body keeps its triangles in the mesh's own space.
    const rest = takeTriangles(geom, keep, new THREE.Matrix4())
    m.mesh.geometry = rest
  }
  return rig
}

const titleCase = (s: string) => s.replace(/\b\w/g, c => c.toUpperCase())

/**
 * Fast path: wrap every node whose name says it moves in a pivot at its own vertex centroid
 * (the hub, for a symmetric rotor) and spin it about its thinnest axis. Other named nodes become
 * static labels. Returns [] when no node name matches MOVING_NAME.
 */
export function namedRig(model: THREE.Object3D): RigPart[] {
  model.updateMatrixWorld(true)
  const rootInv = new THREE.Matrix4().copy(model.matrixWorld).invert()
  const generic = /^(object|root|gltf|sketchfab|scene|node|mesh|output|tripo|group)/i
  const named: THREE.Object3D[] = []
  const hasMesh = (o: THREE.Object3D) => { let y = false; o.traverse(c => { if ((c as THREE.Mesh).isMesh) y = true }); return y }
  const walk = (o: THREE.Object3D) => {
    for (const c of o.children) {
      if (c.name && !generic.test(c.name) && hasMesh(c)) named.push(c) // topmost named node only
      else walk(c)
    }
  }
  walk(model)
  if (!named.some(n => MOVING_NAME.test(n.name))) return []
  const whole = new THREE.Box3().setFromObject(model, true).applyMatrix4(rootInv)
  const wholeSize = whole.getSize(new THREE.Vector3()).length()
  // Drop words every name shares ("Windturbine Support", "Windturbine Blades" -> "Support", "Blades").
  const words = named.map(n => n.name.replace(/[_.]\d+$/, '').replace(/[_-]+/g, ' ').trim().split(/\s+/))
  const shared = words[0].filter(w => named.length > 1 && words.every(ws => ws.includes(w)))
  const rig: RigPart[] = []
  named.forEach((node, i) => {
    const label = titleCase(words[i].filter(w => !shared.includes(w)).join(' ') || words[i].join(' '))
    const box = new THREE.Box3()
    const centroid = new THREE.Vector3()
    let n = 0
    const v = new THREE.Vector3()
    node.traverse(c => {
      const mesh = c as THREE.Mesh
      if (!mesh.isMesh) return
      const pos = mesh.geometry.attributes.position
      const toRoot = new THREE.Matrix4().multiplyMatrices(rootInv, mesh.matrixWorld)
      const step = Math.max(1, Math.floor(pos.count / 4000))
      for (let k = 0; k < pos.count; k += step) { v.fromBufferAttribute(pos, k).applyMatrix4(toRoot); box.expandByPoint(v); centroid.add(v); n++ }
    })
    if (!n) return
    centroid.divideScalar(n)
    const moving = MOVING_NAME.test(node.name) && box.getSize(new THREE.Vector3()).length() < wholeSize * 0.95
    if (!moving) { rig.push({ name: label, kind: 'static', pivot: null, axis: new THREE.Vector3(0, 1, 0), angle: 0, box }); return }
    const ext = box.getSize(new THREE.Vector3())
    const axisName = [...AXES].sort((a, b) => ext[a] - ext[b])[0]
    // Pivot under the model root at the centroid; the node keeps its world transform inside it.
    const pivot = new THREE.Group()
    pivot.userData.rigPart = true
    pivot.position.copy(centroid)
    model.add(pivot)
    pivot.updateMatrixWorld(true)
    pivot.attach(node)
    rig.push({ name: label, kind: 'spin', pivot, axis: axisVec(axisName), angle: 0, box })
  })
  return rig
}

const q = new THREE.Quaternion()

/** Per frame: spin parts turn at a constant speed; tilt parts sway +-12 degrees. */
export function driveRig(rig: RigPart[], dt: number, t: number) {
  for (const part of rig) {
    if (!part.pivot) continue
    if (part.kind === 'spin') part.angle += SPIN_RAD_PER_S * dt
    else if (part.kind === 'tilt') part.angle = Math.sin(t * 1.2) * THREE.MathUtils.degToRad(12)
    else continue
    q.setFromAxisAngle(part.axis, part.angle)
    part.pivot.quaternion.copy(q)
  }
}

/** Topic for the vision prompt from a library URL: /cad/photoreal-wind-turbine-eb6b1c70.glb -> "wind turbine". */
export function topicFromUrl(url: string) {
  const base = decodeURIComponent(url.split('?')[0].split('/').pop() || '').replace(/\.glb$|\.gltf$/i, '')
  return base.replace(/-[0-9a-f]{6,}$/i, '').replace(/^photoreal-/, '').replace(/-sketchfab$/, '').replace(/[-_]+/g, ' ').trim() || 'an object'
}
