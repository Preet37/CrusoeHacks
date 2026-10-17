// Twin's 3D orb (RC-16): breathes while listening, swirls while thinking/sketching,
// pulses with the real loudness of Twin's voice while speaking, glow ring when its hand is raised.
import { Canvas, useFrame } from '@react-three/fiber'
import { MeshDistortMaterial } from '@react-three/drei'
import { useRef, type ComponentRef } from 'react'
import * as THREE from 'three'
import { getOutputLevel } from '../lib/player'
import type { StatusState } from '../lib/protocol'

type Mode = StatusState | 'sketching'

const COLORS: Record<Mode, [string, string]> = {
  listening: ['#5e5ce6', '#0a84ff'],
  thinking: ['#7d5cf0', '#64d2ff'],
  sketching: ['#0a84ff', '#30d158'],
  speaking: ['#5e5ce6', '#64d2ff'],
  hand_raised: ['#ff9f0a', '#5e5ce6'],
}

function Blob({ mode }: { mode: Mode }) {
  const mesh = useRef<THREE.Mesh>(null)
  const mat = useRef<ComponentRef<typeof MeshDistortMaterial>>(null)
  const ring = useRef<THREE.Mesh>(null)
  const level = useRef(0)
  const color = useRef(new THREE.Color(COLORS.listening[0]))
  const emissive = useRef(new THREE.Color(COLORS.listening[1]))

  useFrame((state, dt) => {
    const t = state.clock.elapsedTime
    // Smooth the voice level so the pulse feels organic.
    const raw = mode === 'speaking' ? Math.max(getOutputLevel(), 0.18 + 0.12 * Math.sin(t * 9)) : 0
    level.current += (raw - level.current) * Math.min(1, dt * 14)

    const breathe = 1 + Math.sin(t * 1.6) * 0.03
    const scale = mode === 'speaking' ? 1 + level.current * 0.22 : breathe
    mesh.current?.scale.setScalar(scale)
    if (mesh.current) {
      const spin = mode === 'thinking' || mode === 'sketching' ? 1.4 : 0.25
      mesh.current.rotation.y += dt * spin
      mesh.current.rotation.x = Math.sin(t * 0.5) * 0.25
    }
    if (mat.current) {
      const targetDistort = mode === 'speaking' ? 0.3 + level.current * 0.45 : mode === 'thinking' || mode === 'sketching' ? 0.45 : 0.22
      mat.current.distort += (targetDistort - mat.current.distort) * Math.min(1, dt * 6)
      const [c, e] = COLORS[mode]
      color.current.lerp(new THREE.Color(c), Math.min(1, dt * 4))
      emissive.current.lerp(new THREE.Color(e), Math.min(1, dt * 4))
      mat.current.color.copy(color.current)
      mat.current.emissive.copy(emissive.current)
      mat.current.emissiveIntensity = 0.35 + level.current * 0.8
    }
    if (ring.current) {
      const on = mode === 'hand_raised'
      const m = ring.current.material as THREE.MeshBasicMaterial
      m.opacity += ((on ? 0.85 : 0) - m.opacity) * Math.min(1, dt * 6)
      ring.current.rotation.z += dt * 0.8
      ring.current.scale.setScalar(1 + Math.sin(t * 3) * 0.04)
    }
  })

  return (
    <>
      <mesh ref={mesh}>
        <icosahedronGeometry args={[1, 24]} />
        <MeshDistortMaterial ref={mat} roughness={0.25} metalness={0.15} distort={0.22} speed={mode === 'thinking' || mode === 'sketching' ? 4 : mode === 'speaking' ? 3 : 1.4} />
      </mesh>
      <mesh ref={ring} rotation={[Math.PI / 2.4, 0, 0]}>
        <torusGeometry args={[1.45, 0.035, 16, 96]} />
        <meshBasicMaterial color="#ff9f0a" transparent opacity={0} />
      </mesh>
    </>
  )
}

export default function TwinOrb({ mode }: { mode: Mode }) {
  return (
    <Canvas dpr={[1, 2]} camera={{ position: [0, 0, 4], fov: 40 }} gl={{ alpha: true, antialias: true }} style={{ background: 'transparent' }}>
      <ambientLight intensity={0.6} />
      <directionalLight position={[3, 3, 4]} intensity={1.4} />
      <pointLight position={[-3, -2, 2]} intensity={0.8} color="#64d2ff" />
      <Blob mode={mode} />
    </Canvas>
  )
}
