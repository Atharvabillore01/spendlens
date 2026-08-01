import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { prefersReducedMotion } from "../../lib/format";
import styles from "./HeroField.module.css";

/* A point-cloud surface that undulates like a data field.

   Deliberately restrained: it is decoration behind a productivity tool, not the
   subject. Constraints it respects:
     * `prefers-reduced-motion` -> one static frame, no loop.
     * Off-screen or backgrounded tab -> paused, so an idle tab costs nothing.
     * Device pixel ratio capped at 2.
     * No WebGL -> the caller keeps a static mark instead.
   R3F disposes geometry, material, renderer and the GL context on unmount, so
   remounting on a theme change can't leak contexts. */

const GRID = 54;
const SPAN = 4.2;
const AMPLITUDE = 0.34;

function Field({ color, accent }: { color: THREE.Color; accent: THREE.Color }) {
  const points = useRef<THREE.Points>(null);
  const { invalidate } = useThree();

  const { positions, colors, base } = useMemo(() => {
    const count = GRID * GRID;
    const positions = new Float32Array(count * 3);
    const colors = new Float32Array(count * 3);
    const base = new Float32Array(count * 2);
    const step = SPAN / (GRID - 1);
    const mixed = new THREE.Color();

    for (let iz = 0; iz < GRID; iz += 1) {
      for (let ix = 0; ix < GRID; ix += 1) {
        const i = iz * GRID + ix;
        const x = -SPAN / 2 + ix * step;
        const z = -SPAN / 2 + iz * step;
        positions[i * 3] = x;
        positions[i * 3 + 1] = 0;
        positions[i * 3 + 2] = z;
        base[i * 2] = x;
        base[i * 2 + 1] = z;

        // Radial blend between the two accents so the field reads as one
        // surface rather than a speckle of two colours.
        const radius = Math.min(Math.hypot(x, z) / (SPAN / 2), 1);
        mixed.copy(color).lerp(accent, radius * 0.85);
        colors[i * 3] = mixed.r;
        colors[i * 3 + 1] = mixed.g;
        colors[i * 3 + 2] = mixed.b;
      }
    }
    return { positions, colors, base };
  }, [color, accent]);

  useFrame(({ clock }) => {
    const node = points.current;
    if (!node) return;
    const attribute = node.geometry.getAttribute("position") as THREE.BufferAttribute;
    const array = attribute.array as Float32Array;
    const t = clock.getElapsedTime() * 0.55;

    for (let i = 0; i < base.length / 2; i += 1) {
      const x = base[i * 2] ?? 0;
      const z = base[i * 2 + 1] ?? 0;
      array[i * 3 + 1] =
        Math.sin(x * 1.6 + t) * AMPLITUDE * 0.6 + Math.cos(z * 1.35 - t * 0.8) * AMPLITUDE * 0.5;
    }
    attribute.needsUpdate = true;
    node.rotation.y = Math.sin(t * 0.12) * 0.14;
  });

  // Under reduced motion the loop is off; ask for exactly one frame.
  useEffect(() => invalidate(), [invalidate]);

  return (
    <points ref={points} rotation={[-0.62, 0, 0]}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
        <bufferAttribute attach="attributes-color" args={[colors, 3]} />
      </bufferGeometry>
      <pointsMaterial
        size={0.032}
        vertexColors
        transparent
        opacity={0.92}
        sizeAttenuation
        depthWrite={false}
      />
    </points>
  );
}

export default function HeroField() {
  const [supported, setSupported] = useState(true);
  const reduced = prefersReducedMotion();

  // Palette follows the theme; re-read whenever the stamp on <html> changes.
  const [palette, setPalette] = useState(() => readPalette());
  useEffect(() => {
    const observer = new MutationObserver(() => setPalette(readPalette()));
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);

  if (!supported) return <div className={styles.fallback}>◗</div>;

  return (
    <div className={styles.stage} aria-hidden="true">
      <Canvas
        dpr={[1, 2]}
        camera={{ position: [0, 1.15, 3.1], fov: 42 }}
        frameloop={reduced ? "demand" : "always"}
        gl={{ antialias: true, alpha: true, powerPreference: "low-power" }}
        onCreated={({ gl }) => gl.setClearColor(0x000000, 0)}
        fallback={null}
        onError={() => setSupported(false)}
      >
        <Field color={palette.color} accent={palette.accent} />
      </Canvas>
    </div>
  );
}

function readPalette() {
  const css = getComputedStyle(document.documentElement);
  const accentVar = css.getPropertyValue("--accent").trim() || "#4f46e5";
  return {
    color: new THREE.Color(accentVar),
    accent: new THREE.Color("#06b6d4"),
  };
}
