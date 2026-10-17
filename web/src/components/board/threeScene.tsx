/** Three.js renderer for 'twin-3d' (lazy chunk). Simple lights only: no network-fetched HDRs or fonts. */
import { Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Canvas, useThree } from '@react-three/fiber'
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

/** Floating tag as a canvas-texture sprite (no font fetch, no extra React roots like drei <Html>). */
function Label({ text, y }: { text: string; y: number }) {
  const { tex, aspect } = useMemo(() => {
    const c = document.createElement('canvas')
    const fs = 44, pad = 18
    const g = c.getContext('2d')!
    g.font = `600 ${fs}px -apple-system, system-ui, sans-serif`
    const w = Math.ceil(g.measureText(text).width) + pad * 2, h = fs + pad
    c.width = w; c.height = h
    const g2 = c.getContext('2d')!
    g2.fillStyle = 'rgba(255,255,255,0.92)'
    g2.strokeStyle = 'rgba(0,0,0,0.15)'; g2.lineWidth = 3
    g2.beginPath(); g2.roundRect(1.5, 1.5, w - 3, h - 3, 14); g2.fill(); g2.stroke()
    g2.font = `600 ${fs}px -apple-system, system-ui, sans-serif`
    g2.fillStyle = '#1C1C1E'; g2.textBaseline = 'middle'; g2.textAlign = 'center'
    g2.fillText(text, w / 2, h / 2 + 2)
    const t = new THREE.CanvasTexture(c)
    t.colorSpace = THREE.SRGBColorSpace
    t.anisotropy = 4
    return { tex: t, aspect: w / h }
  }, [text])
  useEffect(() => () => tex.dispose(), [tex])
  const hgt = 0.17
  return (
    <sprite position={[0, y, 0]} scale={[hgt * aspect, hgt, 1]} renderOrder={10}>
      <spriteMaterial map={tex} depthTest={false} transparent />
    </sprite>
  )
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

function Obj({ o, showLabel, onHover }: { o: Obj3D; showLabel: boolean; onHover: (id: string | null) => void }) {
  const f = useMemo(() => finishFor(o), [o])
  const side = o.shape === 'plane' ? THREE.DoubleSide : THREE.FrontSide
  const mat = <meshPhysicalMaterial color={o.color} metalness={f.metalness} roughness={f.roughness} clearcoat={f.clearcoat ?? 0} clearcoatRoughness={0.35} envMapIntensity={0} side={side} />
  const hover = {
    onPointerOver: (e: { stopPropagation(): void }) => { e.stopPropagation(); onHover(o.id) },
    onPointerOut: () => onHover(null),
  }
  const dims = o.scale.map(v => Math.max(Math.abs(v), 1e-3)) as [number, number, number]
  return (
    <>
    {o.label && showLabel && <group position={o.pos}><Label text={o.label} y={Math.abs(o.scale[1]) / 2 + 0.2} /></group>}
    <group position={o.pos} rotation={o.rot}>
      {o.shape === 'box'
        ? <RoundedBox args={dims} radius={Math.min(...dims) * 0.08} smoothness={4} castShadow receiveShadow {...hover}>{mat}</RoundedBox>
        : <mesh scale={o.scale} castShadow receiveShadow {...hover}><Geometry shape={o.shape} />{mat}</mesh>}
    </group>
    </>
  )
}

/** Re-fit camera + orbit target when the scene's bounds change (no Canvas remount). */
function Rig({ pos, target }: { pos: [number, number, number]; target: [number, number, number] }) {
  const camera = useThree(s => s.camera)
  const controls = useThree(s => s.controls) as unknown as { target: THREE.Vector3; update(): void } | null
  const k = pos.join(',') + '|' + target.join(',')
  useEffect(() => {
    camera.position.set(...pos)
    camera.lookAt(...target)
    if (controls) { controls.target.set(...target); controls.update() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [k, camera, controls])
  return null
}

/** glTF normalised to ~2 units and sitting on y=0. */
function Model({ url }: { url: string }) {
  const { scene } = useGLTF(url)
  const obj = useMemo(() => scene.clone(true), [scene])
  const ref = useRef<THREE.Group>(null)
  useLayoutEffect(() => {
    const g = ref.current
    if (!g) return
    g.scale.setScalar(1); g.position.set(0, 0, 0)
    const b = new THREE.Box3().setFromObject(obj)
    const size = b.getSize(new THREE.Vector3())
    const s = 2 / Math.max(size.x, size.y, size.z, 1e-6)
    const c = b.getCenter(new THREE.Vector3())
    g.scale.setScalar(s)
    g.position.set(-c.x * s, -b.min.y * s, -c.z * s)
  }, [obj])
  return <group ref={ref}><primitive object={obj} /></group>
}

function bounds(scene: SceneData) {
  if (!scene.objects.length) return { center: new THREE.Vector3(0, 1, 0), radius: 1.6, floor: 0 }
  const b = new THREE.Box3()
  for (const o of scene.objects) {
    const half = Math.max(...o.scale.map(Math.abs)) * 0.6
    b.expandByPoint(new THREE.Vector3(o.pos[0] - half, o.pos[1] - half, o.pos[2] - half))
    b.expandByPoint(new THREE.Vector3(o.pos[0] + half, o.pos[1] + half, o.pos[2] + half))
  }
  if (scene.gltf) { b.expandByPoint(new THREE.Vector3(-1, 0, -1)); b.expandByPoint(new THREE.Vector3(1, 2, 1)) }
  const sphere = b.getBoundingSphere(new THREE.Sphere())
  const floor = Math.min(...scene.objects.map(o => o.pos[1] - Math.abs(o.scale[1]) / 2), scene.gltf ? 0 : Infinity)
  return { center: sphere.center, radius: Math.max(0.8, sphere.radius), floor }
}

export default function Scene3D({ scene, interactive }: { scene: SceneData; interactive: boolean }) {
  const { center, radius, floor } = useMemo(() => bounds(scene), [scene])
  const d = radius * 2.4
  const camPos: [number, number, number] = [center.x + d * 0.75, center.y + d * 0.5, center.z + d * 0.75]
  const shadowKey = useMemo(() => JSON.stringify(scene), [scene])
  const [hovered, setHovered] = useState<string | null>(null)
  const alwaysLabels = scene.objects.length <= 6
  const sh = radius * 3
  return (
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
        shadow-camera-near={0.1} shadow-camera-far={radius * 20}
      />
      <directionalLight position={[center.x - radius * 4, center.y + radius * 1.5, center.z - radius * 3]} color="#9ec5ff" intensity={0.9} />
      <directionalLight position={[center.x - radius * 3, center.y + radius * 2, center.z + radius * 3]} intensity={0.25} />
      <directionalLight position={[center.x, center.y + radius * 4, center.z]} intensity={0.2} />
      {scene.objects.map(o => <Obj key={o.id} o={o} showLabel={alwaysLabels || hovered === o.id} onHover={setHovered} />)}
      {scene.gltf && (
        <Suspense fallback={null}>
          <Model url={scene.gltf} />
        </Suspense>
      )}
      <gridHelper args={[radius * 6, 24, '#c3c9d4', '#dde1e8']} position={[center.x, floor - 0.001, center.z]} />
      <ContactShadows key={shadowKey /* frames=1: re-bake on scene replace */} position={[center.x, floor, center.z]} scale={radius * 5} blur={2.6} opacity={0.42} far={radius * 3} frames={1} />
      <Rig pos={camPos} target={[center.x, center.y, center.z]} />
      <OrbitControls
        makeDefault
        autoRotate={scene.autoRotate}
        autoRotateSpeed={0.8}
        enableDamping
        enabled={interactive}
        enablePan={false}
        minDistance={radius * 0.8}
        maxDistance={radius * 8}
      />
    </Canvas>
  )
}
