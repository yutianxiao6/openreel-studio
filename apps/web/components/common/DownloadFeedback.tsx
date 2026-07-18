"use client"

import { useEffect, useRef, useState } from "react"
import { MEDIA_DOWNLOAD_EVENT, type MediaDownloadEventDetail } from "@/lib/api"

interface DownloadNotice extends MediaDownloadEventDetail {
  updatedAt: number
}

export function DownloadFeedback() {
  const [notices, setNotices] = useState<DownloadNotice[]>([])
  const timers = useRef(new Map<string, number>())

  useEffect(() => {
    const activeTimers = timers.current
    const onDownload = (event: Event) => {
      const detail = (event as CustomEvent<MediaDownloadEventDetail>).detail
      if (!detail?.id) return

      const existingTimer = activeTimers.get(detail.id)
      if (existingTimer) {
        window.clearTimeout(existingTimer)
        activeTimers.delete(detail.id)
      }
      if (detail.status === "canceled") {
        setNotices((current) => current.filter((item) => item.id !== detail.id))
        return
      }

      setNotices((current) => {
        const next: DownloadNotice = { ...detail, updatedAt: Date.now() }
        return [...current.filter((item) => item.id !== detail.id), next].slice(-3)
      })

      if (detail.status === "completed" || detail.status === "failed") {
        const timeout = window.setTimeout(() => {
          setNotices((current) => current.filter((item) => item.id !== detail.id))
          activeTimers.delete(detail.id)
        }, detail.status === "failed" ? 6500 : 4200)
        activeTimers.set(detail.id, timeout)
      }
    }

    window.addEventListener(MEDIA_DOWNLOAD_EVENT, onDownload)
    return () => {
      window.removeEventListener(MEDIA_DOWNLOAD_EVENT, onDownload)
      for (const timeout of activeTimers.values()) window.clearTimeout(timeout)
      activeTimers.clear()
    }
  }, [])

  if (notices.length === 0) return null

  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[140] flex w-[min(360px,calc(100vw-32px))] flex-col gap-2" aria-live="polite">
      {notices.map((notice) => {
        const running = notice.status === "started"
        const failed = notice.status === "failed"
        const message = running
          ? "正在下载…"
          : failed
            ? notice.error || "下载失败"
            : notice.mode === "browser"
              ? "已交给浏览器下载"
              : "下载完成"
        return (
          <div
            key={notice.id}
            className={`overflow-hidden rounded-xl border px-3.5 py-3 shadow-2xl backdrop-blur-xl ${
              failed
                ? "border-red-300/20 bg-[#241418]/95 text-red-100"
                : "border-white/[0.12] bg-[#111821]/96 text-zinc-100"
            }`}
          >
            <div className="flex items-start gap-3">
              <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] ${
                running
                  ? "border-2 border-cyan-200/80 border-t-transparent animate-spin"
                  : failed
                    ? "bg-red-400/15 text-red-200"
                    : "bg-emerald-400/15 text-emerald-200"
              }`}>
                {running ? "" : failed ? "!" : "✓"}
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-xs font-semibold">{message}</div>
                <div className="mt-1 truncate text-[11px] text-zinc-400" title={notice.filename}>{notice.filename}</div>
                {notice.path && (
                  <div className="mt-1 truncate font-mono text-[10px] text-zinc-500" title={notice.path}>{notice.path}</div>
                )}
              </div>
            </div>
            {running && <div className="mt-2 h-0.5 w-full overflow-hidden rounded bg-white/[0.06]"><div className="h-full w-1/2 animate-pulse rounded bg-cyan-300/75" /></div>}
          </div>
        )
      })}
    </div>
  )
}
