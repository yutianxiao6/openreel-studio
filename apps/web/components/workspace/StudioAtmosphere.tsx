"use client"

import { useEffect, useRef } from "react"

interface PointerState {
  x: number
  y: number
  opacity: number
}

export function StudioAtmosphere() {
  const auraRef = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    const aura = auraRef.current
    const shell = aura?.closest<HTMLElement>(".studio-shell")
    if (!aura || !shell || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return

    let bounds = shell.getBoundingClientRect()
    let frame = 0
    let lastX = bounds.width * 0.72
    let lastY = bounds.height * 0.18
    const current: PointerState = { x: lastX, y: lastY, opacity: 0.34 }
    const target: PointerState = { ...current }

    const updateBounds = () => {
      bounds = shell.getBoundingClientRect()
    }
    const onPointerMove = (event: PointerEvent) => {
      target.x = Math.max(0, Math.min(bounds.width, event.clientX - bounds.left))
      target.y = Math.max(0, Math.min(bounds.height, event.clientY - bounds.top))
      target.opacity = event.pointerType === "touch" ? 0.28 : 0.62
    }
    const onPointerLeave = () => {
      target.opacity = 0.24
    }
    const onPointerDown = () => {
      target.opacity = 0.82
    }
    const onPointerUp = () => {
      target.opacity = 0.58
    }

    const render = () => {
      current.x += (target.x - current.x) * 0.075
      current.y += (target.y - current.y) * 0.075
      current.opacity += (target.opacity - current.opacity) * 0.08
      const velocityX = current.x - lastX
      const velocityY = current.y - lastY
      const speed = Math.min(1, Math.hypot(velocityX, velocityY) / 12)
      const angle = Math.atan2(velocityY, velocityX) * (180 / Math.PI)
      aura.style.transform = `translate3d(${current.x}px, ${current.y}px, 0) translate(-50%, -50%) rotate(${angle}deg) scale(${1 + speed * 0.1}, ${1 - speed * 0.04})`
      aura.style.opacity = current.opacity.toFixed(3)
      lastX = current.x
      lastY = current.y
      frame = window.requestAnimationFrame(render)
    }

    const resizeObserver = new ResizeObserver(updateBounds)
    resizeObserver.observe(shell)
    shell.addEventListener("pointermove", onPointerMove, { passive: true })
    shell.addEventListener("pointerleave", onPointerLeave, { passive: true })
    shell.addEventListener("pointerdown", onPointerDown, { passive: true })
    shell.addEventListener("pointerup", onPointerUp, { passive: true })
    frame = window.requestAnimationFrame(render)

    return () => {
      resizeObserver.disconnect()
      window.cancelAnimationFrame(frame)
      shell.removeEventListener("pointermove", onPointerMove)
      shell.removeEventListener("pointerleave", onPointerLeave)
      shell.removeEventListener("pointerdown", onPointerDown)
      shell.removeEventListener("pointerup", onPointerUp)
    }
  }, [])

  return (
    <div className="studio-atmosphere" aria-hidden="true">
      <span ref={auraRef} className="studio-pointer-aura" />
      <span className="studio-atmosphere-mesh" />
    </div>
  )
}
