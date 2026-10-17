/** Three.js renderer for 'twin-3d' (lazy chunk). Simple lights only: no network-fetched HDRs or fonts. */
import { Suspense, useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { Canvas, useThree } from '@react-three/fiber'
import { ContactShadows, OrbitControls, useGLTF } from '@react-three/drei'
import * as THREE from 'three'
import type { Obj3D, Scene3D as SceneData } from './scene3d'

function Geometry({ shape }: { shape: Obj3D['shape'] }) {
  switch (shape) {
    case 'box': return <boxGeometry args={[1, 1, 1]} />
    case 'cylinder': return <cylinderGeometry args={[0.5, 0.5, 1, 40]} />
    case 'sphere': return <sphereGeometry args={[0.5, 40, 24]} />
    case 'cone': return <coneGeometry args={[0.5, 1, 40]} />
    case 'torus': return <torusGeometry args={[0.4, 0.12, 20, 60]} />
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

function Obj({ o }: { o: Obj3D }) {
  return (
    <>
    {o.label && <group position={o.pos}><Label text={o.label} y={Math.abs(o.scale[1]) / 2 + 0.2} /></group>}
    <group position={o.pos} rotation={o.rot}>
      <mesh scale={o.scale} castShadow receiveShadow>
        <Geometry shape={o.shape} />
        <meshStandardMaterial color={o.color} roughness={0.45} metalness={0.15} side={o.shape === 'plane' ? THREE.DoubleSide : THREE.FrontSide} />
      </mesh>
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
      <hemisphereLight args={['#ffffff', '#c9d2e0', 0.7]} />
      <ambientLight intensity={0.35} />
      <directionalLight position={[center.x + radius * 3, center.y + radius * 5, center.z + radius * 2]} intensity={1.4} castShadow shadow-mapSize={[1024, 1024]} />
      <directionalLight position={[center.x - radius * 3, center.y + radius * 2, center.z - radius * 3]} intensity={0.35} />
      {scene.objects.map(o => <Obj key={o.id} o={o} />)}
      {scene.gltf && (
        <Suspense fallback={null}>
          <Model url={scene.gltf} />
        </Suspense>
      )}
      <gridHelper args={[radius * 6, 24, '#c3c9d4', '#dde1e8']} position={[center.x, floor - 0.001, center.z]} />
      <ContactShadows key={shadowKey /* frames=1: re-bake on scene replace */} position={[center.x, floor, center.z]} scale={radius * 5} blur={2.4} opacity={0.35} far={radius * 3} frames={1} />
      <Rig pos={camPos} target={[center.x, center.y, center.z]} />
      <OrbitControls
        makeDefault
        autoRotate={scene.autoRotate}
        autoRotateSpeed={1.2}
        enableDamping
        enabled={interactive}
        enablePan={false}
        minDistance={radius * 0.8}
        maxDistance={radius * 8}
      />
    </Canvas>
  )
}
