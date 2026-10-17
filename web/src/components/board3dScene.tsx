/** Three.js scene for the 3D architecture view (lazy chunk, loaded by Board3D). */
import { useEffect, useMemo, useRef } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { OrbitControls, RoundedBox } from '@react-three/drei'
import * as THREE from 'three'
import type { BoxKind } from '../lib/protocol'

export interface Node3D {
  id: string
  label: string
  kind: BoxKind
  /** Grid x (0-1000) used for left-right ordering inside a layer. */
  x: number
  /** Frame index, so topics in separate frames sit side by side. */
  order: number
  highlighted: boolean
}
export interface Edge3D {
  id: string
  from: string
  to: string
  highlighted: boolean
}

const KIND_COLOR: Record<BoxKind, string> = {
  client: '#a78bfa',
  service: '#0a84ff',
  datastore: '#30d158',
  external: '#8e8e93',
  note: '#ffd60a',
}
const LAYER_Y: Record<BoxKind, number> = { client: 2.4, service: 0, datastore: -2.4, external: 0, note: 2.4 }
const SPACING = 2.6
const SLAB: [number, number, number] = [1.9, 0.42, 1.25]
const RED = new THREE.Color('#ff3b30')

type Placed = Node3D & { pos: THREE.Vector3 }

function layout(nodes: Node3D[]): Map<string, Placed> {
  const out = new Map<string, Placed>()
  const rows: Record<'client' | 'service' | 'datastore', Node3D[]> = { client: [], service: [], datastore: [] }
  const side: Node3D[] = []
  for (const n of nodes) {
    if (n.kind === 'external' || n.kind === 'note') side.push(n)
    else rows[n.kind].push(n)
  }
  const sortFn = (a: Node3D, b: Node3D) => a.order - b.order || a.x - b.x
  let maxHalf = 0
  for (const k of ['client', 'service', 'datastore'] as const) {
    const r = rows[k].sort(sortFn)
    const half = ((r.length - 1) * SPACING) / 2
    maxHalf = Math.max(maxHalf, half)
    r.forEach((n, i) => out.set(n.id, { ...n, pos: new THREE.Vector3(i * SPACING - half, LAYER_Y[k], 0) }))
  }
  // Externals and notes: a side column to the right, stacked front-to-back.
  const ext = side.filter((n) => n.kind === 'external').sort(sortFn)
  const notes = side.filter((n) => n.kind === 'note').sort(sortFn)
  const sx = maxHalf + SPACING * 1.25
  ext.forEach((n, i) => out.set(n.id, { ...n, pos: new THREE.Vector3(sx, 0, (i - (ext.length - 1) / 2) * 1.8) }))
  notes.forEach((n, i) => out.set(n.id, { ...n, pos: new THREE.Vector3(-sx, 2.4 - i * 1.3, 0) }))
  return out
}

function useLabelTexture(text: string) {
  const res = useMemo(() => {
    const c = document.createElement('canvas')
    const fs = 46, pad = 22
    const g = c.getContext('2d')!
    g.font = `600 ${fs}px -apple-system, system-ui, sans-serif`
    const w = Math.ceil(g.measureText(text).width) + pad * 2, h = fs + pad
    c.width = w; c.height = h
    const g2 = c.getContext('2d')!
    g2.fillStyle = 'rgba(20,21,24,0.82)'
    g2.strokeStyle = 'rgba(255,255,255,0.22)'; g2.lineWidth = 3
    g2.beginPath(); g2.roundRect(1.5, 1.5, w - 3, h - 3, h / 2 - 2); g2.fill(); g2.stroke()
    g2.font = `600 ${fs}px -apple-system, system-ui, sans-serif`
    g2.fillStyle = '#f5f5f7'; g2.textBaseline = 'middle'; g2.textAlign = 'center'
    g2.fillText(text, w / 2, h / 2 + 2)
    const t = new THREE.CanvasTexture(c)
    t.colorSpace = THREE.SRGBColorSpace
    t.anisotropy = 4
    return { tex: t, aspect: w / h }
  }, [text])
  useEffect(() => () => res.tex.dispose(), [res])
  return res
}

function Label({ text, y, h = 0.3, dim = false }: { text: string; y: number; h?: number; dim?: boolean }) {
  const { tex, aspect } = useLabelTexture(text)
  return (
    <sprite position={[0, y, 0]} scale={[h * aspect, h, 1]} renderOrder={10}>
      <spriteMaterial map={tex} depthTest={false} transparent opacity={dim ? 0.55 : 1} />
    </sprite>
  )
}

function NodeSlab({ n }: { n: Placed }) {
  const group = useRef<THREE.Group>(null)
  const mat = useRef<THREE.MeshPhysicalMaterial>(null)
  const ageRef = useRef(0)
  const target = useRef(n.pos.clone())
  target.current.copy(n.pos)
  const base = useMemo(() => new THREE.Color(KIND_COLOR[n.kind]), [n.kind])
  useFrame((_, dt) => {
    const g = group.current
    if (!g) return
    const age = (ageRef.current += Math.min(dt, 0.05))
    const k = Math.min(1, age / 0.45)
    const s = 1 - Math.pow(1 - k, 3) * Math.cos(k * 4) // springy ease-out
    g.scale.setScalar(Math.max(0.001, s))
    g.position.lerp(target.current, Math.min(1, dt * 6))
    g.position.y += Math.sin(age * 1.3 + n.pos.x) * 0.0015
    const m = mat.current
    if (m) {
      m.opacity = 0.82 * k
      if (n.highlighted) {
        const p = 0.5 + 0.5 * Math.sin(age * 6)
        m.color.copy(base).lerp(RED, 0.85)
        m.emissive.copy(RED)
        m.emissiveIntensity = 0.4 + p * 1.1
      } else {
        m.color.copy(base)
        m.emissive.copy(base)
        m.emissiveIntensity = 0.22
      }
    }
  })
  return (
    <group ref={group} position={n.pos.toArray()} scale={0.001}>
      <RoundedBox args={SLAB} radius={0.16} smoothness={4}>
        <meshPhysicalMaterial
          ref={mat}
          color={KIND_COLOR[n.kind]}
          transparent
          opacity={0}
          roughness={0.18}
          metalness={0.05}
          clearcoat={1}
          clearcoatRoughness={0.15}
          emissive={KIND_COLOR[n.kind]}
          emissiveIntensity={0.22}
        />
      </RoundedBox>
      <Label text={n.label} y={SLAB[1] / 2 + 0.32} />
    </group>
  )
}

const PARTICLES = 4

function EdgeFlow({ a, b, highlighted }: { a: THREE.Vector3; b: THREE.Vector3; highlighted: boolean }) {
  const curve = useMemo(() => {
    const mid = a.clone().add(b).multiplyScalar(0.5)
    const sameLayer = Math.abs(a.y - b.y) < 0.1
    mid.z += sameLayer ? 1.1 : 0.7
    if (sameLayer) mid.y += 0.6
    return new THREE.QuadraticBezierCurve3(a.clone(), mid, b.clone())
  }, [a.x, a.y, a.z, b.x, b.y, b.z]) // eslint-disable-line react-hooks/exhaustive-deps
  const tube = useMemo(() => new THREE.TubeGeometry(curve, 48, 0.025, 8, false), [curve])
  useEffect(() => () => tube.dispose(), [tube])
  const dots = useRef<(THREE.Mesh | null)[]>([])
  const color = highlighted ? '#ff453a' : '#64d2ff'
  useFrame(({ clock }) => {
    const t0 = clock.getElapsedTime() * (highlighted ? 0.55 : 0.32)
    dots.current.forEach((m, i) => {
      if (!m) return
      const t = (t0 + i / PARTICLES) % 1
      m.position.copy(curve.getPoint(t))
      const s = 0.6 + Math.sin(t * Math.PI) * 0.7
      m.scale.setScalar(s)
    })
  })
  return (
    <group>
      <mesh geometry={tube}>
        <meshBasicMaterial color={color} transparent opacity={highlighted ? 0.65 : 0.28} />
      </mesh>
      {Array.from({ length: PARTICLES }, (_, i) => (
        <mesh key={i} ref={(m) => { dots.current[i] = m }}>
          <sphereGeometry args={[0.075, 14, 10]} />
          <meshBasicMaterial color={color} transparent opacity={0.95} blending={THREE.AdditiveBlending} depthWrite={false} toneMapped={false} />
        </mesh>
      ))}
    </group>
  )
}

function LayerPlate({ y, w, label }: { y: number; w: number; label: string }) {
  return (
    <group position={[0, y - SLAB[1] / 2 - 0.06, 0]}>
      <mesh rotation={[-Math.PI / 2, 0, 0]}>
        <planeGeometry args={[w, 3.4]} />
        <meshBasicMaterial color="#5e5ce6" transparent opacity={0.07} side={THREE.DoubleSide} depthWrite={false} />
      </mesh>
      <lineSegments rotation={[-Math.PI / 2, 0, 0]}>
        <edgesGeometry args={[new THREE.PlaneGeometry(w, 3.4)]} />
        <lineBasicMaterial color="#8e8cff" transparent opacity={0.25} />
      </lineSegments>
      <group position={[-w / 2 + 0.9, 0.05, 1.7]}>
        <Label text={label} y={0} h={0.22} dim />
      </group>
    </group>
  )
}

function Rig({ radius }: { radius: number }) {
  const camera = useThree((s) => s.camera)
  const controls = useThree((s) => s.controls) as unknown as { target: THREE.Vector3; update(): void } | null
  const r = Math.round(radius * 2) / 2
  useEffect(() => {
    const d = Math.max(12, r * 2.6)
    camera.position.set(d * 0.5, d * 0.36, d * 0.8)
    camera.lookAt(0, 0, 0)
    if (controls) { controls.target.set(0, 0, 0); controls.update() }
  }, [r, camera, controls])
  return null
}

export default function Board3DScene({ nodes, edges, active }: { nodes: Node3D[]; edges: Edge3D[]; active: boolean }) {
  const placed = useMemo(() => layout(nodes), [nodes])
  const xs = [...placed.values()].map((p) => Math.abs(p.pos.x))
  const halfW = Math.max(2, ...xs) + 1.4
  const coreW = Math.max(3.4, ...[...placed.values()].filter((p) => p.kind !== 'external' && p.kind !== 'note').map((p) => Math.abs(p.pos.x) * 2 + 2.4))
  const layers = (['client', 'service', 'datastore'] as const).filter((k) => nodes.some((n) => n.kind === k))
  const LAYER_NAME = { client: 'CLIENTS', service: 'SERVICES', datastore: 'DATA' }
  return (
    <Canvas
      dpr={[1, 2]}
      frameloop={active ? 'always' : 'never'}
      camera={{ position: [6, 5, 9], fov: 42, near: 0.1, far: 200 }}
      gl={{ antialias: true, alpha: true, preserveDrawingBuffer: true }}
      style={{ position: 'absolute', inset: 0 }}
    >
      <hemisphereLight args={['#d8e4ff', '#1a1b22', 0.8]} />
      <ambientLight intensity={0.25} />
      <directionalLight position={[6, 10, 6]} intensity={1.3} />
      <pointLight position={[-6, 3, 4]} intensity={18} color="#5e5ce6" />
      <pointLight position={[6, -3, 4]} intensity={14} color="#64d2ff" />
      {layers.map((k) => <LayerPlate key={k} y={LAYER_Y[k]} w={coreW} label={LAYER_NAME[k]} />)}
      {[...placed.values()].map((n) => <NodeSlab key={n.id} n={n} />)}
      {edges.map((e) => {
        const a = placed.get(e.from), b = placed.get(e.to)
        if (!a || !b) return null
        return <EdgeFlow key={e.id} a={a.pos} b={b.pos} highlighted={e.highlighted || a.highlighted || b.highlighted} />
      })}
      <Rig radius={halfW} />
      <OrbitControls makeDefault autoRotate autoRotateSpeed={0.6} enableDamping enablePan={false} minDistance={3} maxDistance={40} />
    </Canvas>
  )
}
