"use client"

import { useCallback } from "react"
import { api } from "@/lib/api"
import { useCodexBridgeStore, type ChatAgentMode } from "@/stores/codexBridgeStore"
import { ChatPanel } from "./ChatPanel"
import { CodexChatPanel } from "./CodexChatPanel"

export function OptionalAgentChatPanel() {
  const mode = useCodexBridgeStore((state) => state.mode)
  const setMode = useCodexBridgeStore((state) => state.setMode)
  const setStatus = useCodexBridgeStore((state) => state.setStatus)
  const setChecking = useCodexBridgeStore((state) => state.setChecking)

  const selectMode = useCallback((next: ChatAgentMode) => {
    if (next === mode) return
    setMode(next)
    if (next !== "openreel") return
    setChecking(false)
    void api.disconnectCodex()
      .then(setStatus)
      .catch(() => {
        setStatus({
          ok: false,
          connected: false,
          state: "disconnected",
          label: "Codex 未连接",
          detail: null,
        })
      })
  }, [mode, setChecking, setMode, setStatus])

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-white/[0.07] bg-black/15 px-3 py-2">
        <span className="mr-auto text-[9px] font-semibold uppercase tracking-[0.16em] text-zinc-600">聊天引擎</span>
        <button
          type="button"
          onClick={() => selectMode("openreel")}
          className={`rounded-lg border px-2.5 py-1.5 text-[10px] font-medium transition-all ${
            mode === "openreel"
              ? "border-violet-300/25 bg-violet-400/[0.12] text-violet-100 shadow-[0_0_20px_rgba(139,92,246,.08)]"
              : "border-transparent text-zinc-500 hover:border-white/10 hover:bg-white/[0.04] hover:text-zinc-300"
          }`}
        >
          OpenReel Agent
        </button>
        <button
          type="button"
          onClick={() => selectMode("codex")}
          className={`rounded-lg border px-2.5 py-1.5 text-[10px] font-medium transition-all ${
            mode === "codex"
              ? "border-emerald-300/25 bg-emerald-300/[0.1] text-emerald-100 shadow-[0_0_20px_rgba(52,211,153,.08)]"
              : "border-transparent text-zinc-500 hover:border-white/10 hover:bg-white/[0.04] hover:text-zinc-300"
          }`}
          title="可选功能：启用后由本机 Codex 接管当前聊天和画布操作"
        >
          Codex 插件
        </button>
      </div>
      <div className="min-h-0 flex-1">
        {mode === "codex" ? <CodexChatPanel /> : <ChatPanel />}
      </div>
    </div>
  )
}
