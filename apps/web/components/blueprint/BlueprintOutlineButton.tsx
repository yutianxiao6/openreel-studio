"use client"

import { useCallback, useState } from "react"
import { BlueprintViewer } from "@/components/blueprint/BlueprintViewer"
import { useBlueprintStore } from "@/stores/blueprintStore"

export function BlueprintOutlineButton({ projectId }: { projectId?: string | null }) {
  const [open, setOpen] = useState(false)
  const status = useBlueprintStore((state) => state.status)
  const loading = useBlueprintStore((state) => state.loading)
  const load = useBlueprintStore((state) => state.load)

  const handleOpen = useCallback(() => {
    setOpen(true)
    if (projectId) void load(projectId)
  }, [load, projectId])

  return (
    <>
      <button
        type="button"
        onClick={handleOpen}
        disabled={!projectId}
        className="rounded-md border border-white/10 bg-white/[0.04] px-3 py-1 text-xs text-zinc-300 transition-colors hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40"
        title={status === "missing" ? "查看项目蓝图" : `查看项目蓝图：${status}`}
      >
        {loading ? "蓝图..." : "蓝图"}
      </button>
      <BlueprintViewer open={open} projectId={projectId} onClose={() => setOpen(false)} />
    </>
  )
}
