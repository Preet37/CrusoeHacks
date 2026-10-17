/** Three.js renderer for 'twin-3d' (lazy chunk). Simple lights only: no network-fetched HDRs or fonts. */
import { Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { ContactShadows, OrbitControls, RoundedBox, useGLTF } from '@react-three/drei'
import * as THREE from 'three'
import type { Obj3D, Scene3D as SceneData } from './scene3d'

function Geometry({ shape }: { shape: Obj3D['shape'] }) {
  switch (shape) {
    case 'box': return <boxGeometry args={[1, 1, 1]} />
    case 'cylinder': return <cylinderGeometry args={[0.5, 0.5, 1, 64]} />
    case 'sphere': return <sphereGeometry args={[0.5, 64, 40]} />
    case 'cone': return <coneGeometry args={[0.5, 1, 64]} />
    case 'torus': return <torusGeometry args={[0.4, 0.12, 32, 96]} />
    case 'plane': return <planeGeometry args={[1, 1]} />
  }
}

type Finish = { metalness: number; roughness: number; clearcoat?: number }
/** PBR finish: honour an optional `material` hint, else infer from colour (low saturation -> brushed metal). */
function finishFor(o: Obj3D): Finish {
  const hint = String((o as Obj3D & { material?: unknown }).material ?? '').toLowerCase()
  if (/metal|steel|alu|iron|chrome|brass|copper/.test(hint)) return { metalness: 0.85, roughness: 0.3 }
  if (/rubber|tire|tyre|foam/.test(hint)) return { metalness: 0, roughness: 0.9 }
  if (/glass|acrylic/.test(hint)) return { metalness: 0, roughness: 0.08, clearcoat: 1 }
  if (/wood/.test(hint)) return { metalness: 0, roughness: 0.75 }
  if (/plastic|abs|pla|nylon/.test(hint)) return { metalness: 0.05, roughness: 0.45, clearcoat: 0.3 }
  const hsl = { h: 0, s: 0, l: 0 }
  try { new THREE.Color(o.color).getHSL(hsl) } catch { /* unknown name: treat as plastic */ hsl.s = 1 }
  return hsl.s < 0.12 && hsl.l > 0.12 && hsl.l < 0.9 ? { metalness: 0.6, roughness: 0.35 } : { metalness: 0.05, roughness: 0.45, clearcoat: 0.3 }
}


type Parts = Map<string, THREE.Object3D>
type LabelNode = { el: HTMLDivElement | null; line: SVGLineElement | null; dot: SVGCircleElement | null }
type Fit = { center: THREE.Vector3; radius: number; floor: number; footprint: number; height: number }

function Obj({ o, parts, onHover }: { o: Obj3D; parts: Parts; onHover: (id: string | null) => void }) {
  const f = useMemo(() => finishFor(o), [o])
  const side = o.shape === 'plane' ? THREE.DoubleSide : THREE.FrontSide
  const mat = <meshPhysicalMaterial color={o.color} metalness={f.metalness} roughness={f.roughness} clearcoat={f.clearcoat ?? 0} clearcoatRoughness={0.35} envMapIntensity={0} side={side} />
  const hover = {
    onPointerOver: (e: { stopPropagation(): void }) => { e.stopPropagation(); onHover(o.id) },
    onPointerOut: () => onHover(null),
  }
  const dims = o.scale.map(v => Math.max(Math.abs(v), 1e-3)) as [number, number, number]
  return (
    <group position={o.pos} rotation={o.rot} userData={{ ground: o.shape === 'plane' }} ref={g => { if (g) parts.set(o.id, g); else parts.delete(o.id) }}>
      {o.shape === 'box'
        ? <RoundedBox args={dims} radius={Math.min(...dims) * 0.08} smoothness={4} castShadow receiveShadow {...hover}>{mat}</RoundedBox>
        : <mesh scale={o.scale} castShadow receiveShadow {...hover}><Geometry shape={o.shape} />{mat}</mesh>}
    </group>
  )
}

/** glTF normalised to ~2 units and sitting on y=0; calls onReady once placed so the camera can refit. */
function Model({ url, onReady }: { url: string; onReady: () => void }) {
  const { scene } = useGLTF(url)
  const obj = useMemo(() => scene.clone(true), [scene])
  const ref = useRef<THREE.Group>(null)
  useLayoutEffect(() => {
    const g = ref.current
    if (!g) return
    g.scale.setScalar(1); g.position.set(0, 0, 0)
    const b = new THREE.Box3().setFromObject(obj, true)
    const size = b.getSize(new THREE.Vector3())
    const s = 2 / Math.max(size.x, size.y, size.z, 1e-6)
    const c = b.getCenter(new THREE.Vector3())
    g.scale.setScalar(s)
    g.position.set(-c.x * s, -b.min.y * s, -c.z * s)
    onReady()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [obj])
  return <group ref={ref}><primitive object={obj} /></group>
}

/** Cheap pre-render estimate (used until the real geometry has been measured). */
function estimate(scene: SceneData): Fit {
  if (!scene.objects.length) return { center: new THREE.Vector3(0, 1, 0), radius: 1.6, floor: 0, footprint: 2, height: 2 }
  const b = new THREE.Box3()
  for (const o of scene.objects) {
    const half = Math.max(...o.scale.map(Math.abs)) * 0.5
    b.expandByPoint(new THREE.Vector3(o.pos[0] - half, o.pos[1] - half, o.pos[2] - half))
    b.expandByPoint(new THREE.Vector3(o.pos[0] + half, o.pos[1] + half, o.pos[2] + half))
  }
  if (scene.gltf) { b.expandByPoint(new THREE.Vector3(-1, 0, -1)); b.expandByPoint(new THREE.Vector3(1, 2, 1)) }
  return fitFromBox(b)
}

function fitFromBox(b: THREE.Box3): Fit {
  const sphere = b.getBoundingSphere(new THREE.Sphere())
  const size = b.getSize(new THREE.Vector3())
  return { center: sphere.center, radius: Math.max(0.05, sphere.radius), floor: b.min.y, footprint: Math.max(size.x, size.z, 0.1), height: Math.max(size.y, 0.05) }
}

const VIEW_DIR = new THREE.Vector3(0.75, 0.5, 0.75).normalize()

/** Measures the real model geometry (objects + glTF, not grid/shadows) and fits camera + orbit target to it. */
function Fitter({ root, fitKey, version, onFit, autoRotate }: { autoRotate: boolean; root: React.RefObject<THREE.Group | null>; fitKey: string; version: number; onFit: (f: Fit) => void }) {
  const camera = useThree(s => s.camera) as THREE.PerspectiveCamera
  const controls = useThree(s => s.controls) as unknown as { target: THREE.Vector3; minDistance: number; maxDistance: number; update(): void } | null
  const size = useThree(s => s.size)
  const invalidate = useThree(s => s.invalidate)
  const autoRotateRef = useRef(autoRotate)
  autoRotateRef.current = autoRotate
  useEffect(() => {
    const g = root.current
    if (!g) return
    g.updateWorldMatrix(true, true)
    // Fit to real geometry; flat 'plane' parts are treated as ground (unless that's all there is).
    const all = new THREE.Box3().setFromObject(g, true)
    if (all.isEmpty()) return
    const solid = g.children.filter(ch => !ch.userData.ground)
    const box = new THREE.Box3()
    for (const ch of solid) box.union(new THREE.Box3().setFromObject(ch, true))
    if (box.isEmpty()) box.copy(all)
    const fit = { ...fitFromBox(box), floor: Math.min(box.min.y, all.min.y) }
    const aspect = Math.max(size.width, 1) / Math.max(size.height, 1)
    const tv = Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2), th = tv * aspect
    // Distance so every box corner lands inside the frustum with ~15% margin (camera-space test).
    const fwd = VIEW_DIR.clone().negate()
    const right = new THREE.Vector3().crossVectors(fwd, new THREE.Vector3(0, 1, 0)).normalize()
    const up = new THREE.Vector3().crossVectors(right, fwd)
    const hr = Math.max(Math.hypot(box.max.x - fit.center.x, box.max.z - fit.center.z), Math.hypot(box.min.x - fit.center.x, box.min.z - fit.center.z))
    let dist = 0
    for (let i = 0; i < 8; i++) {
      const p = new THREE.Vector3(i & 1 ? box.max.x : box.min.x, i & 2 ? box.max.y : box.min.y, i & 4 ? box.max.z : box.min.z).sub(fit.center)
      const x = autoRotateRef.current ? hr : Math.abs(p.dot(right)), y = Math.abs(p.dot(up)), z = -p.dot(fwd) // z>0: toward camera
      dist = Math.max(dist, (x * 1.15) / th + z, (y * 1.15) / tv + z)
    }
    dist = Math.max(dist, fit.radius * 0.5, 0.1)
    camera.position.copy(fit.center).addScaledVector(VIEW_DIR, dist)
    camera.near = Math.max(dist / 200, 0.001); camera.far = dist * 30
    camera.updateProjectionMatrix()
    camera.lookAt(fit.center)
    if (controls) {
      controls.target.copy(fit.center)
      controls.minDistance = dist * 0.3; controls.maxDistance = dist * 4
      controls.update()
    }
    onFit(fit)
    invalidate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey, version, size.width, size.height, camera, controls])
  return null
}

const MAX_LABELS = 8
const _v = new THREE.Vector3(), _c = new THREE.Vector3(), _box = new THREE.Box3(), _ray = new THREE.Raycaster()

/** Projects label anchors to screen each frame, de-overlaps them (stacking upward) and draws leader lines. */
function LabelProjector({ objs, parts, nodes, hovered, fit }: { objs: Obj3D[]; parts: Parts; nodes: Map<string, LabelNode>; hovered: string | null; fit: Fit }) {
  const invalidate = useThree(s => s.invalidate)
  useEffect(() => { invalidate() }, [hovered, objs, invalidate])
  useFrame(({ camera, size }) => {
    const W = size.width, H = size.height
    _c.copy(fit.center).project(camera)
    const cx = (_c.x + 1) / 2 * W, cy = (1 - _c.y) / 2 * H
    type C = { id: string; ax: number; ay: number; r: number; dist: number; anchor: THREE.Vector3; obj: THREE.Object3D }
    let cands: C[] = []
    for (const o of objs) {
      const obj = parts.get(o.id)
      if (!obj) continue
      _box.setFromObject(obj)
      if (_box.isEmpty()) continue
      const anchor = _box.getCenter(new THREE.Vector3()); anchor.y = _box.max.y
      const r = _box.getSize(_v).length() / 2
      _v.copy(anchor).project(camera)
      if (_v.z > 1 || _v.z < -1) continue
      cands.push({ id: o.id, ax: (_v.x + 1) / 2 * W, ay: (1 - _v.y) / 2 * H, r, dist: camera.position.distanceTo(anchor), anchor, obj })
    }
    if (cands.length > MAX_LABELS) {
      const meshes = [...parts.values()]
      cands = cands.filter(c => {
        if (c.id === hovered) return true
        if (c.r < fit.radius * 0.08) return false
        _ray.set(camera.position, _v.copy(c.anchor).sub(camera.position).normalize())
        const hit = _ray.intersectObjects(meshes, true)[0]
        if (!hit || hit.distance >= c.dist - c.r * 0.1) return true
        let p: THREE.Object3D | null = hit.object
        while (p && p !== c.obj) p = p.parent
        return p === c.obj
      })
      cands.sort((a, b) => (b.id === hovered ? 1 : 0) - (a.id === hovered ? 1 : 0) || b.r - a.r)
      cands = cands.slice(0, MAX_LABELS)
    }
    const shown = new Set(cands.map(c => c.id))
    // Desired spot: pushed outward from the model's screen centre, a little above the anchor.
    const placed: { x: number; y: number; w: number; h: number; c: C }[] = []
    const want = cands.map(c => {
      const n = nodes.get(c.id)
      const el = n?.el
      if (el) el.style.display = 'block'
      const w = el?.offsetWidth ?? 60, h = el?.offsetHeight ?? 20
      let dx = c.ax - cx, dy = c.ay - cy
      const len = Math.hypot(dx, dy)
      if (len < 4) { dx = 0; dy = -1 } else { dx /= len; dy /= len }
      const off = 34
      const lx = c.ax + dx * off, ly = c.ay + Math.min(dy, 0) * off * 0.6 - 22
      return { c, w, h, x: lx - w / 2 + dx * (w / 2 - 4), y: ly - h / 2 }
    })
    want.sort((a, b) => b.y - a.y) // bottom first, later ones stack upward
    for (const r of want) {
      r.x = Math.min(Math.max(r.x, 2), Math.max(2, W - r.w - 2))
      for (let it = 0; it < placed.length + 1; it++) {
        const hit = placed.find(p => r.x < p.x + p.w + 4 && r.x + r.w + 4 > p.x && r.y < p.y + p.h + 3 && r.y + r.h + 3 > p.y)
        if (!hit) break
        r.y = hit.y - r.h - 3
      }
      r.y = Math.min(Math.max(r.y, 2), Math.max(2, H - r.h - 2))
      placed.push(r)
    }
    for (const r of placed) {
      const n = nodes.get(r.c.id)
      if (!n) continue
      if (n.el) n.el.style.transform = `translate(${r.x.toFixed(1)}px, ${r.y.toFixed(1)}px)`
      const ex = Math.min(Math.max(r.c.ax, r.x + 3), r.x + r.w - 3)
      const ey = Math.min(Math.max(r.c.ay, r.y), r.y + r.h)
      if (n.line) {
        n.line.setAttribute('x1', r.c.ax.toFixed(1)); n.line.setAttribute('y1', r.c.ay.toFixed(1))
        n.line.setAttribute('x2', ex.toFixed(1)); n.line.setAttribute('y2', ey.toFixed(1))
        n.line.style.display = ''
      }
      if (n.dot) { n.dot.setAttribute('cx', r.c.ax.toFixed(1)); n.dot.setAttribute('cy', r.c.ay.toFixed(1)); n.dot.style.display = '' }
    }
    for (const [id, n] of nodes) {
      if (shown.has(id)) continue
      if (n.el) n.el.style.display = 'none'
      if (n.line) n.line.style.display = 'none'
      if (n.dot) n.dot.style.display = 'none'
    }
  })
  return null
}

/** Plain-DOM overlay over the canvas (positioned imperatively by LabelProjector; no drei <Html>). */
function LabelLayer({ objs, nodes }: { objs: Obj3D[]; nodes: Map<string, LabelNode> }) {
  const node = (id: string) => { let n = nodes.get(id); if (!n) { n = { el: null, line: null, dot: null }; nodes.set(id, n) } return n }
  useEffect(() => {
    const ids = new Set(objs.map(o => o.id))
    for (const id of [...nodes.keys()]) if (!ids.has(id)) nodes.delete(id)
  }, [objs, nodes])
  return (
    <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none', overflow: 'hidden' }}>
      <svg style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', overflow: 'visible' }}>
        {objs.map(o => (
          <g key={o.id}>
            <line ref={l => { node(o.id).line = l }} stroke="rgba(28,28,30,0.55)" strokeWidth={1} style={{ display: 'none' }} />
            <circle ref={c => { node(o.id).dot = c }} r={2.5} fill="#1C1C1E" stroke="#fff" strokeWidth={1} style={{ display: 'none' }} />
          </g>
        ))}
      </svg>
      {objs.map(o => (
        <div
          key={o.id}
          ref={el => { node(o.id).el = el }}
          style={{
            position: 'absolute', left: 0, top: 0, display: 'none', whiteSpace: 'nowrap',
            font: `600 11px -apple-system, system-ui, sans-serif`, color: '#1C1C1E',
            background: 'rgba(255,255,255,0.94)', border: '1px solid rgba(0,0,0,0.14)', borderRadius: 6,
            padding: '2px 6px', boxShadow: '0 1px 3px rgba(0,0,0,0.12)', willChange: 'transform',
          }}
        >{o.label}</div>
      ))}
    </div>
  )
}

export default function Scene3D({ scene, interactive }: { scene: SceneData; interactive: boolean }) {
  const fitKey = useMemo(() => JSON.stringify(scene), [scene])
  const initial = useMemo(() => estimate(scene), [scene])
  const [measured, setMeasured] = useState<{ key: string; fit: Fit } | null>(null)
  const fit = measured && measured.key === fitKey ? measured.fit : initial
  const [glbVersion, setGlbVersion] = useState(0)
  const [hovered, setHovered] = useState<string | null>(null)
  const root = useRef<THREE.Group>(null)
  const parts = useMemo<Parts>(() => new Map(), [])
  const nodes = useMemo(() => new Map<string, LabelNode>(), [])
  const labeled = useMemo(() => scene.objects.filter(o => o.label), [scene])
  const { center, radius, floor } = fit
  const d = radius * 2.6
  const camPos: [number, number, number] = [center.x + d * 0.65, center.y + d * 0.45, center.z + d * 0.65]
  const ground = Math.max(fit.footprint * 2.5, fit.height)
  const sh = radius * 2
  return (
    <>
    <Canvas
      dpr={[1, 2]}
      shadows
      frameloop={scene.autoRotate || interactive ? 'always' : 'demand'}
      resize={{ offsetSize: true /* tldraw scales us with a CSS transform */ }}
      camera={{ position: camPos, fov: 40, near: 0.05, far: 500 }}
      gl={{ antialias: true, alpha: true, preserveDrawingBuffer: true }}
      style={{ position: 'absolute', inset: 0, pointerEvents: interactive ? 'auto' : 'none' }}
    >
      {/* Soft studio rig: sky/ground fill, warm key with soft shadows, cool rim from behind. */}
      <hemisphereLight args={['#f4f7ff', '#b9b2a8', 0.65]} />
      <ambientLight intensity={0.15} />
      <directionalLight
        position={[center.x + radius * 3, center.y + radius * 5, center.z + radius * 2]}
        color="#fff4e6" intensity={1.6} castShadow
        shadow-mapSize={[2048, 2048]} shadow-bias={-0.0004} shadow-normalBias={0.02} shadow-radius={6}
        shadow-camera-left={-sh} shadow-camera-right={sh} shadow-camera-top={sh} shadow-camera-bottom={-sh}
        shadow-camera-near={0.01} shadow-camera-far={radius * 20}
      />
      <directionalLight position={[center.x - radius * 4, center.y + radius * 1.5, center.z - radius * 3]} color="#9ec5ff" intensity={0.9} />
      <directionalLight position={[center.x - radius * 3, center.y + radius * 2, center.z + radius * 3]} intensity={0.25} />
      <directionalLight position={[center.x, center.y + radius * 4, center.z]} intensity={0.2} />
      <group ref={root}>
        {scene.objects.map(o => <Obj key={o.id} o={o} parts={parts} onHover={setHovered} />)}
        {scene.gltf && (
          <Suspense fallback={null}>
            <Model url={scene.gltf} onReady={() => setGlbVersion(v => v + 1)} />
          </Suspense>
        )}
      </group>
      <gridHelper key={`g${ground.toFixed(3)}`} args={[ground, 20, '#c3c9d4', '#dde1e8']} position={[center.x, floor - 0.001, center.z]} />
      <ContactShadows key={`${fitKey}|${ground.toFixed(3)}` /* frames=1: re-bake on change */} position={[center.x, floor, center.z]} scale={ground} blur={2.4} opacity={0.42} far={Math.max(fit.height, radius)} frames={1} />
      <Fitter autoRotate={scene.autoRotate} root={root} fitKey={fitKey} version={glbVersion} onFit={f => setMeasured({ key: fitKey, fit: f })} />
      <LabelProjector objs={labeled} parts={parts} nodes={nodes} hovered={hovered} fit={fit} />
      <OrbitControls
        makeDefault
        autoRotate={scene.autoRotate}
        autoRotateSpeed={0.8}
        enableDamping
        enabled={interactive}
        enablePan={false}
      />
    </Canvas>
    <LabelLayer objs={labeled} nodes={nodes} />
    </>
  )
}
