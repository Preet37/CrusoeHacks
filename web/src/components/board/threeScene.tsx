/** Three.js renderer for 'twin-3d' (lazy chunk). Simple lights only: no network-fetched HDRs or fonts. */
import { Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { ContactShadows, OrbitControls, RoundedBox, useGLTF } from '@react-three/drei'
import * as THREE from 'three'
import type { Obj3D, Scene3D as SceneData } from './scene3d'
import { applyRig, captureViews, driveRig, namedRig, partBoxes, topicFromUrl, type RigPart, type VisionPart } from './partRig'
import { serverHttpBase } from '../../lib/ws'

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

/** One vision request per GLB per page load (the server also caches per GLB). */
const rigRequests = new Map<string, Promise<VisionPart[]>>()
function requestRig(url: string, views: Record<string, string>): Promise<VisionPart[]> {
  let p = rigRequests.get(url)
  if (!p) {
    p = fetch(`${serverHttpBase()}/cad/part-rig`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: url.split('?')[0], topic: topicFromUrl(url), views }),
      signal: AbortSignal.timeout(12_000),
    }).then(r => (r.ok ? r.json() : { parts: [] })).then(j => (Array.isArray(j?.parts) ? j.parts : [])).catch(() => [])
    rigRequests.set(url, p)
  }
  return p
}

/** glTF normalised to ~2 units and sitting on y=0; calls onReady once placed so the camera can refit.
 *  Moving parts (named nodes, else vision-located) are cut into pivots and spun each frame. */
function Model({ url, onReady, onRig }: { url: string; onReady: () => void; onRig: (obj: THREE.Object3D, rig: RigPart[]) => void }) {
  const { scene } = useGLTF(url)
  const obj = useMemo(() => scene.clone(true), [scene])
  const ref = useRef<THREE.Group>(null)
  const rig = useRef<RigPart[]>([])
  const gl = useThree(s => s.gl)
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
  useEffect(() => {
    let live = true
    rig.current = []
    const named = namedRig(obj)
    if (named.length) { rig.current = named; onRig(obj, named); return }
    let views: ReturnType<typeof captureViews>
    try { views = captureViews(gl, obj) } catch { return }
    requestRig(url, views.images).then(parts => {
      if (!live || !parts.length) return // no rig (e.g. vision failed): leave the model as-is
      try {
        rig.current = applyRig(obj, partBoxes(parts, views.bounds))
        onRig(obj, rig.current)
      } catch { /* malformed geometry: keep the model static */ }
    })
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [obj, gl, url])
  useFrame(({ clock }, dt) => { if (rig.current.length) driveRig(rig.current, Math.min(dt, 0.1), clock.elapsedTime) })
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

const MAX_LABELS = 6
/** Parts whose on-screen size is below this fraction of the view get no label (unless hovered). */
const MIN_LABEL_FRAC = 0.04
const _v = new THREE.Vector3(), _c = new THREE.Vector3(), _box = new THREE.Box3(), _ray = new THREE.Raycaster()

type LabelItem = { id: string; label: string }
/** Rig-part label: a box in `obj`'s local space (the GLB root). */
type RigAnchor = { obj: THREE.Object3D; box: THREE.Box3 }

/**
 * Projects label anchors to screen each frame and lays labels out without overlap:
 * each label goes to the side of the model its anchor is on, the labels on a side are
 * swept top-to-bottom and pushed apart, and a leader line joins label and anchor.
 */
function LabelProjector({ items, parts, rigAnchors, nodes, hovered, fit }: { items: LabelItem[]; parts: Parts; rigAnchors: Map<string, RigAnchor>; nodes: Map<string, LabelNode>; hovered: string | null; fit: Fit }) {
  const invalidate = useThree(s => s.invalidate)
  useEffect(() => { invalidate() }, [hovered, items, invalidate])
  useFrame(({ camera, size }) => {
    const W = size.width, H = size.height
    const view = Math.min(W, H)
    _c.copy(fit.center).project(camera)
    const cx = (_c.x + 1) / 2 * W
    type C = { id: string; ax: number; ay: number; r: number; px: number; dist: number; anchor: THREE.Vector3; obj: THREE.Object3D | null }
    let cands: C[] = []
    for (const it of items) {
      const rig = rigAnchors.get(it.id)
      const obj = rig ? null : parts.get(it.id) ?? null
      if (rig) { rig.obj.updateWorldMatrix(true, false); _box.copy(rig.box).applyMatrix4(rig.obj.matrixWorld) }
      else if (obj) _box.setFromObject(obj)
      else continue
      if (_box.isEmpty()) continue
      const anchor = _box.getCenter(new THREE.Vector3())
      const r = _box.getSize(_v).length() / 2
      const dist = Math.max(camera.position.distanceTo(anchor), 1e-6)
      const fov = (camera as THREE.PerspectiveCamera).fov ?? 40
      const px = (r / (dist * Math.tan(THREE.MathUtils.degToRad(fov) / 2))) * (H / 2) // projected radius
      _v.copy(anchor).project(camera)
      if (_v.z > 1 || _v.z < -1) continue
      if (it.id !== hovered && px * 2 < view * MIN_LABEL_FRAC) continue
      cands.push({ id: it.id, ax: (_v.x + 1) / 2 * W, ay: (1 - _v.y) / 2 * H, r, px, dist, anchor, obj })
    }
    if (cands.length > MAX_LABELS) {
      const meshes = [...parts.values()]
      cands = cands.filter(c => {
        if (c.id === hovered || !c.obj) return true
        _ray.set(camera.position, _v.copy(c.anchor).sub(camera.position).normalize())
        const hit = _ray.intersectObjects(meshes, true)[0]
        if (!hit || hit.distance >= c.dist - c.r * 0.1) return true
        let p: THREE.Object3D | null = hit.object
        while (p && p !== c.obj) p = p.parent
        return p === c.obj
      })
      cands.sort((a, b) => (b.id === hovered ? 1 : 0) - (a.id === hovered ? 1 : 0) || b.px - a.px)
      cands = cands.slice(0, MAX_LABELS)
    }
    const shown = new Set(cands.map(c => c.id))
    // Screen extent of the labelled parts: labels sit just outside it, left or right.
    let minX = Infinity, maxX = -Infinity
    for (const c of cands) { minX = Math.min(minX, c.ax - c.px * 0.7); maxX = Math.max(maxX, c.ax + c.px * 0.7) }
    type R = { x: number; y: number; w: number; h: number; c: C; right: boolean }
    const rows: R[] = cands.map(c => {
      const el = nodes.get(c.id)?.el
      if (el) el.style.display = 'block'
      const w = el?.offsetWidth ?? 60, h = el?.offsetHeight ?? 20
      const right = cands.length === 1 ? c.ax >= cx - 1 : c.ax >= cx
      const gap = 18
      let x = right ? Math.max(c.ax + 24, maxX + gap) : Math.min(c.ax - 24 - w, minX - gap - w)
      x = Math.min(Math.max(x, 2), Math.max(2, W - w - 2))
      return { c, w, h, right, x, y: c.ay - h / 2 }
    })
    const GAP = 4
    for (const side of [false, true]) {
      const col = rows.filter(r => r.right === side).sort((a, b) => a.c.ay - b.c.ay)
      for (let i = 0; i < col.length; i++) { // sweep down, pushing each below the previous
        col[i].y = Math.max(col[i].y, 2)
        if (i) col[i].y = Math.max(col[i].y, col[i - 1].y + col[i - 1].h + GAP)
      }
      for (let i = col.length - 1; i >= 0; i--) { // then up, if the column ran off the bottom
        const limit = i === col.length - 1 ? H - col[i].h - 2 : col[i + 1].y - col[i].h - GAP
        col[i].y = Math.min(col[i].y, limit)
      }
    }
    // Any remaining overlap across columns (narrow views): nudge sideways.
    for (let i = 0; i < rows.length; i++) for (let j = 0; j < i; j++) {
      const a = rows[i], b = rows[j]
      if (a.x < b.x + b.w + GAP && a.x + a.w + GAP > b.x && a.y < b.y + b.h + GAP && a.y + a.h + GAP > b.y) {
        a.x = Math.min(Math.max(a.right ? b.x + b.w + GAP : b.x - a.w - GAP, 2), Math.max(2, W - a.w - 2))
      }
    }
    for (const r of rows) {
      const n = nodes.get(r.c.id)
      if (!n) continue
      if (n.el) n.el.style.transform = `translate(${r.x.toFixed(1)}px, ${r.y.toFixed(1)}px)`
      const ex = r.right ? r.x : r.x + r.w
      const ey = r.y + r.h / 2
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
function LabelLayer({ items, nodes }: { items: LabelItem[]; nodes: Map<string, LabelNode> }) {
  const node = (id: string) => { let n = nodes.get(id); if (!n) { n = { el: null, line: null, dot: null }; nodes.set(id, n) } return n }
  useEffect(() => {
    const ids = new Set(items.map(o => o.id))
    for (const id of [...nodes.keys()]) if (!ids.has(id)) nodes.delete(id)
  }, [items, nodes])
  return (
    <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none', overflow: 'hidden' }}>
      <svg style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', overflow: 'visible' }}>
        {items.map(o => (
          <g key={o.id}>
            <line ref={l => { node(o.id).line = l }} stroke="rgba(28,28,30,0.55)" strokeWidth={1} style={{ display: 'none' }} />
            <circle ref={c => { node(o.id).dot = c }} r={2.5} fill="#1C1C1E" stroke="#fff" strokeWidth={1} style={{ display: 'none' }} />
          </g>
        ))}
      </svg>
      {items.map(o => (
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
  const [rigState, setRigState] = useState<{ url: string; obj: THREE.Object3D; rig: RigPart[] } | null>(null)
  const rig = rigState && rigState.url === scene.gltf ? rigState : null
  const spinning = !!rig?.rig.some(p => p.pivot)
  const rigAnchors = useMemo(() => {
    const m = new Map<string, RigAnchor>()
    rig?.rig.forEach((p, i) => m.set(`rig:${i}`, { obj: rig.obj, box: p.box }))
    return m
  }, [rig])
  const items = useMemo<LabelItem[]>(() => [
    ...scene.objects.filter(o => o.label).map(o => ({ id: o.id, label: o.label! })),
    ...(rig?.rig.map((p, i) => ({ id: `rig:${i}`, label: p.name })) ?? []),
  ], [scene, rig])
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
      frameloop={scene.autoRotate || interactive || spinning ? 'always' : 'demand'}
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
            <Model url={scene.gltf} onReady={() => setGlbVersion(v => v + 1)} onRig={(obj, r) => setRigState({ url: scene.gltf!, obj, rig: r })} />
          </Suspense>
        )}
      </group>
      <gridHelper key={`g${ground.toFixed(3)}`} args={[ground, 20, '#c3c9d4', '#dde1e8']} position={[center.x, floor - 0.001, center.z]} />
      <ContactShadows key={`${fitKey}|${ground.toFixed(3)}` /* frames=1: re-bake on change */} position={[center.x, floor, center.z]} scale={ground} blur={2.4} opacity={0.42} far={Math.max(fit.height, radius)} frames={1} />
      <Fitter autoRotate={scene.autoRotate} root={root} fitKey={fitKey} version={glbVersion} onFit={f => setMeasured({ key: fitKey, fit: f })} />
      <LabelProjector items={items} parts={parts} rigAnchors={rigAnchors} nodes={nodes} hovered={hovered} fit={fit} />
      <OrbitControls
        makeDefault
        autoRotate={scene.autoRotate}
        autoRotateSpeed={spinning ? 0.4 : 0.8 /* slow the turntable while parts spin */}
        enableDamping
        enabled={interactive}
        enablePan={false}
      />
    </Canvas>
    <LabelLayer items={items} nodes={nodes} />
    </>
  )
}
