"use client"

import { useEffect, useState } from "react"
import {
  chooseDesktopMediaDownloadDirectory,
  getDesktopMediaDownloadDirectory,
} from "@/lib/api"

export function DownloadTab() {
  const [directory, setDirectory] = useState("")
  const [loading, setLoading] = useState(true)
  const [choosing, setChoosing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    void getDesktopMediaDownloadDirectory()
      .then((value) => {
        if (active) setDirectory(value)
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [])

  const chooseDirectory = async () => {
    if (choosing) return
    setChoosing(true)
    setError(null)
    try {
      const result = await chooseDesktopMediaDownloadDirectory()
      if (!result.canceled && result.directory) setDirectory(result.directory)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setChoosing(false)
    }
  }

  return (
    <div className="space-y-4">
      <section className="rounded-xl border border-white/[0.08] bg-white/[0.025] p-4">
        <div className="text-sm font-semibold text-zinc-100">媒体下载位置</div>
        <p className="mt-1 text-[11px] leading-5 text-zinc-500">
          安装版的图片、视频和资产会直接保存到这里。同名文件会自动编号，不覆盖已有文件。
        </p>
        <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-center">
          <div
            className="min-w-0 flex-1 truncate rounded-lg border border-white/[0.08] bg-black/20 px-3 py-2.5 font-mono text-[11px] text-zinc-300"
            title={directory}
          >
            {loading ? "读取中…" : directory || "首次下载时选择文件夹"}
          </div>
          <button
            type="button"
            onClick={() => void chooseDirectory()}
            disabled={loading || choosing}
            className="h-9 shrink-0 rounded-lg border border-violet-300/20 bg-violet-400/[0.08] px-4 text-[11px] font-medium text-violet-100 transition hover:bg-violet-400/[0.14] disabled:cursor-wait disabled:opacity-50"
          >
            {choosing ? "选择中…" : directory ? "更改文件夹" : "选择文件夹"}
          </button>
        </div>
        {error && (
          <div className="mt-3 rounded-lg border border-red-400/20 bg-red-500/10 px-3 py-2 text-[11px] text-red-200">
            {error}
          </div>
        )}
      </section>
    </div>
  )
}
