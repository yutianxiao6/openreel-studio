"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import * as THREE from "three"
import { resolveMediaUrl } from "@/lib/api"

export type PanoramaCaptureMode = "single" | "four" | "eight"

interface PanoramaViewerProps {
  src: string
  title?: string
  onClose: () => void
  onCapture: (dataUrl: string, mode: PanoramaCaptureMode) => Promise<void> | void
}

interface PanoramaRuntime {
  renderer: THREE.WebGLRenderer
  scene: THREE.Scene
  camera: THREE.PerspectiveCamera
  material?: THREE.MeshBasicMaterial
  texture?: THREE.Texture
  mesh?: THREE.Mesh
}

const CAPTURE_VIEWS: Record<Exclude<PanoramaCaptureMode, "single">, { yaw: number; pitch: number }[]> = {
  four: [
    { yaw: 0, pitch: 0 },
    { yaw: 90, pitch: 0 },
    { yaw: 180, pitch: 0 },
    { yaw: 270, pitch: 0 },
  ],
  eight: [
    { yaw: 0, pitch: 0 },
    { yaw: 45, pitch: 0 },
    { yaw: 90, pitch: 0 },
    { yaw: 135, pitch: 0 },
    { yaw: 180, pitch: 0 },
    { yaw: 225, pitch: 0 },
    { yaw: 270, pitch: 0 },
    { yaw: 315, pitch: 0 },
  ],
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value))
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = () => reject(new Error("截图图片加载失败"))
    image.src = src
  })
}

async function composeCaptureGrid(dataUrls: string[], cols: number, rows: number): Promise<string> {
  const images = await Promise.all(dataUrls.map(loadImage))
  const first = images[0]
  const naturalWidth = first?.naturalWidth || first?.width || 960
  const naturalHeight = first?.naturalHeight || first?.height || 540
  const cellWidth = Math.min(960, naturalWidth)
  const cellHeight = Math.max(1, Math.round((naturalHeight / naturalWidth) * cellWidth))
  const canvas = document.createElement("canvas")
  canvas.width = cellWidth * cols
  canvas.height = cellHeight * rows
  const ctx = canvas.getContext("2d")
  if (!ctx) throw new Error("无法创建截图画布")
  ctx.fillStyle = "#05070b"
  ctx.fillRect(0, 0, canvas.width, canvas.height)
  images.forEach((image, index) => {
    const x = (index % cols) * cellWidth
    const y = Math.floor(index / cols) * cellHeight
    ctx.drawImage(image, x, y, cellWidth, cellHeight)
  })
  return canvas.toDataURL("image/png")
}

export default function PanoramaViewer({ src, title, onClose, onCapture }: PanoramaViewerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const runtimeRef = useRef<PanoramaRuntime | null>(null)
  const yawRef = useRef(0)
  const pitchRef = useRef(0)
  const fovRef = useRef(72)
  const draggingRef = useRef<{ pointerId: number; x: number; y: number } | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [capturing, setCapturing] = useState<PanoramaCaptureMode | null>(null)

  const renderScene = useCallback(() => {
    const runtime = runtimeRef.current
    if (!runtime) return
    const phi = THREE.MathUtils.degToRad(90 - pitchRef.current)
    const theta = THREE.MathUtils.degToRad(yawRef.current)
    runtime.camera.fov = fovRef.current
    runtime.camera.updateProjectionMatrix()
    runtime.camera.lookAt(new THREE.Vector3(
      500 * Math.sin(phi) * Math.cos(theta),
      500 * Math.cos(phi),
      500 * Math.sin(phi) * Math.sin(theta),
    ))
    runtime.renderer.render(runtime.scene, runtime.camera)
  }, [])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    setLoaded(false)
    setError(null)

    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(72, 16 / 9, 0.1, 1200)
    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true })
    renderer.setClearColor(0x020617, 1)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    renderer.domElement.className = "block h-full w-full"
    container.appendChild(renderer.domElement)

    const runtime: PanoramaRuntime = { renderer, scene, camera }
    runtimeRef.current = runtime

    const resize = () => {
      const rect = container.getBoundingClientRect()
      const width = Math.max(320, Math.floor(rect.width))
      const height = Math.max(220, Math.floor(rect.height))
      renderer.setSize(width, height, false)
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      renderScene()
    }

    const textureLoader = new THREE.TextureLoader()
    textureLoader.setCrossOrigin("anonymous")
    textureLoader.load(
      resolveMediaUrl(src),
      (texture) => {
        texture.colorSpace = THREE.SRGBColorSpace
        const geometry = new THREE.SphereGeometry(500, 96, 48)
        const material = new THREE.MeshBasicMaterial({ map: texture, side: THREE.BackSide })
        const mesh = new THREE.Mesh(geometry, material)
        scene.add(mesh)
        runtime.texture = texture
        runtime.material = material
        runtime.mesh = mesh
        setLoaded(true)
        resize()
      },
      undefined,
      () => {
        setError("全景图加载失败")
      },
    )

    window.addEventListener("resize", resize)
    resize()

    let frame = 0
    const animate = () => {
      renderScene()
      frame = window.requestAnimationFrame(animate)
    }
    frame = window.requestAnimationFrame(animate)

    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener("resize", resize)
      runtime.mesh?.geometry.dispose()
      runtime.material?.dispose()
      runtime.texture?.dispose()
      renderer.dispose()
      renderer.domElement.remove()
      if (runtimeRef.current === runtime) runtimeRef.current = null
    }
  }, [renderScene, src])

  const captureCurrentView = useCallback((yaw: number, pitch: number) => {
    const runtime = runtimeRef.current
    if (!runtime || !loaded) throw new Error("全景图尚未加载完成")
    const previousYaw = yawRef.current
    const previousPitch = pitchRef.current
    yawRef.current = yaw
    pitchRef.current = clamp(pitch, -82, 82)
    renderScene()
    const dataUrl = runtime.renderer.domElement.toDataURL("image/png")
    yawRef.current = previousYaw
    pitchRef.current = previousPitch
    renderScene()
    return dataUrl
  }, [loaded, renderScene])

  const handleCapture = useCallback(async (mode: PanoramaCaptureMode) => {
    if (capturing) return
    setCapturing(mode)
    setError(null)
    try {
      if (mode === "single") {
        await onCapture(captureCurrentView(yawRef.current, pitchRef.current), mode)
      } else {
        const views = CAPTURE_VIEWS[mode]
        const dataUrls = views.map((view) => captureCurrentView(view.yaw, view.pitch))
        await onCapture(
          await composeCaptureGrid(dataUrls, mode === "four" ? 2 : 4, mode === "four" ? 2 : 2),
          mode,
        )
      }
    } catch (captureError) {
      setError(captureError instanceof Error ? captureError.message : String(captureError))
    } finally {
      setCapturing(null)
    }
  }, [captureCurrentView, capturing, onCapture])

  return (
    <div className="openreel-panorama-viewer fixed inset-0 z-[90] bg-[#03050a] text-white">
      <div className="studio-panorama-toolbar absolute left-4 top-4 z-10 flex max-w-[min(640px,calc(100vw-2rem))] items-center gap-3 rounded-xl border border-white/10 bg-black/50 px-3 py-2 backdrop-blur-md">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{title || "全景预览"}</div>
          <div className="text-[11px] text-zinc-400">拖拽转动视角，滚轮缩放</div>
        </div>
      </div>
      <div className="studio-panorama-toolbar absolute right-4 top-4 z-10 flex items-center gap-2 rounded-xl border border-white/10 bg-black/50 p-1.5 backdrop-blur-md">
        <button
          type="button"
          disabled={!loaded || Boolean(capturing)}
          onClick={() => void handleCapture("single")}
          className="h-8 rounded px-3 text-xs font-medium text-zinc-100 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-45"
        >
          {capturing === "single" ? "截图中" : "单视角"}
        </button>
        <button
          type="button"
          disabled={!loaded || Boolean(capturing)}
          onClick={() => void handleCapture("four")}
          className="h-8 rounded px-3 text-xs font-medium text-zinc-100 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-45"
        >
          {capturing === "four" ? "截图中" : "四视角"}
        </button>
        <button
          type="button"
          disabled={!loaded || Boolean(capturing)}
          onClick={() => void handleCapture("eight")}
          className="h-8 rounded px-3 text-xs font-medium text-zinc-100 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-45"
        >
          {capturing === "eight" ? "截图中" : "八视角"}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="h-8 rounded bg-white/10 px-3 text-xs font-semibold text-white transition hover:bg-white/16"
        >
          关闭
        </button>
      </div>

      <div
        ref={containerRef}
        className="h-full w-full cursor-grab touch-none active:cursor-grabbing"
        onPointerDown={(event) => {
          draggingRef.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY }
          event.currentTarget.setPointerCapture(event.pointerId)
        }}
        onPointerMove={(event) => {
          const dragging = draggingRef.current
          if (!dragging || dragging.pointerId !== event.pointerId) return
          const dx = event.clientX - dragging.x
          const dy = event.clientY - dragging.y
          draggingRef.current = { ...dragging, x: event.clientX, y: event.clientY }
          yawRef.current -= dx * 0.12
          pitchRef.current = clamp(pitchRef.current + dy * 0.12, -82, 82)
          renderScene()
        }}
        onPointerUp={(event) => {
          if (draggingRef.current?.pointerId === event.pointerId) draggingRef.current = null
        }}
        onPointerCancel={(event) => {
          if (draggingRef.current?.pointerId === event.pointerId) draggingRef.current = null
        }}
        onWheel={(event) => {
          event.preventDefault()
          fovRef.current = clamp(fovRef.current + event.deltaY * 0.04, 38, 96)
          renderScene()
        }}
      />

      {!loaded && !error && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-zinc-300">
          正在加载全景图…
        </div>
      )}
      {error && (
        <div className="absolute bottom-4 left-1/2 z-10 max-w-[min(560px,calc(100vw-2rem))] -translate-x-1/2 rounded-md border border-red-300/25 bg-red-950/85 px-3 py-2 text-sm text-red-100 shadow-2xl">
          {error}
        </div>
      )}
    </div>
  )
}
