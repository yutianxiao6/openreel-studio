"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { MarkdownView } from "@/components/common/MarkdownView"
import {
  api,
  type CodexBridgeStatus,
  type CodexChatMessageRecord,
  type CodexStreamEvent,
} from "@/lib/api"
import { useCodexBridgeStore } from "@/stores/codexBridgeStore"
import { useProjectStore } from "@/stores/projectStore"

interface LocalCodexMessage {
  id: string
  role: "user" | "assistant" | "system"
  content: string
  createdAt: string
}

function localId(prefix: string) {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return `${prefix}-${crypto.randomUUID()}`
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function fromRecord(message: CodexChatMessageRecord): LocalCodexMessage {
  return {
    id: message.id,
    role: message.role === "user" ? "user" : message.role === "assistant" ? "assistant" : "system",
    content: message.content,
    createdAt: message.created_at,
  }
}

function statusHelp(status: CodexBridgeStatus): string {
  if (status.state === "missing_cli") return "请先安装 Codex CLI；安装版 OpenReel 会自动发现它，无需填写端口。"
  if (status.state === "login_required") return "请先在本机运行 codex login，完成后点击重新连接。"
  if (status.detail) return status.detail
  return "启动 Codex 后重新连接。"
}

function CodexMark({ connected }: { connected: boolean }) {
  return (
    <div
      className={`relative grid h-10 w-10 shrink-0 place-items-center rounded-xl border text-[12px] font-black tracking-[-0.08em] transition-all duration-500 ${
        connected
          ? "border-emerald-300/25 bg-emerald-300/[0.08] text-emerald-100 shadow-[0_0_32px_rgba(52,211,153,.14)]"
          : "border-white/10 bg-white/[0.04] text-zinc-400"
      }`}
    >
      CX
      {connected && <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full border-2 border-[#0b0f16] bg-emerald-400" />}
    </div>
  )
}
export function CodexChatPanel() {
  const project = useProjectStore((state) => state.currentProject)
  const status = useCodexBridgeStore((state) => state.status)
  const checking = useCodexBridgeStore((state) => state.checking)
  const setStatus = useCodexBridgeStore((state) => state.setStatus)
  const setChecking = useCodexBridgeStore((state) => state.setChecking)
  const [messages, setMessages] = useState<LocalCodexMessage[]>([])
  const [input, setInput] = useState("")
  const [streaming, setStreaming] = useState(false)
  const [activity, setActivity] = useState("")
  const [historyLoading, setHistoryLoading] = useState(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const cancelStreamRef = useRef<(() => void) | null>(null)

  const refreshStatus = useCallback(async (restart = false) => {
    setChecking(true)
    try {
      const next = restart ? await api.connectCodex(true) : await api.getCodexStatus(true)
      setStatus(next)
    } catch (error) {
      setStatus({
        ok: false,
        connected: false,
        state: "error",
        label: "Codex 连接失败",
        detail: error instanceof Error ? error.message : String(error),
      })
    } finally {
      setChecking(false)
    }
  }, [setChecking, setStatus])

  useEffect(() => {
    void refreshStatus(false)
    const timer = window.setInterval(() => {
      void api.getCodexStatus(false).then(setStatus).catch(() => {})
    }, 10_000)
    return () => window.clearInterval(timer)
  }, [refreshStatus, setStatus])

  useEffect(() => {
    cancelStreamRef.current?.()
    cancelStreamRef.current = null
    setStreaming(false)
    setActivity("")
    if (!project?.id) {
      setMessages([])
      return
    }
    let cancelled = false
    setHistoryLoading(true)
    api.getCodexMessages(project.id)
      .then((history) => {
        if (!cancelled) setMessages(history.map(fromRecord))
      })
      .catch((error) => {
        if (!cancelled) {
          setMessages([{
            id: localId("history-error"),
            role: "system",
            content: `Codex 会话历史加载失败：${error instanceof Error ? error.message : String(error)}`,
            createdAt: new Date().toISOString(),
          }])
        }
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false)
      })
    return () => {
      cancelled = true
      cancelStreamRef.current?.()
      cancelStreamRef.current = null
    }
  }, [project?.id])

  useEffect(() => {
    const element = scrollRef.current
    if (!element) return
    element.scrollTo({ top: element.scrollHeight, behavior: streaming ? "auto" : "smooth" })
  }, [messages, activity, streaming])

  const setAssistantContent = useCallback((id: string, updater: (content: string) => string) => {
    setMessages((current) => current.map((message) => (
      message.id === id ? { ...message, content: updater(message.content) } : message
    )))
  }, [])

  const handleEvent = useCallback((assistantId: string, event: CodexStreamEvent) => {
    if (event.type === "connected") {
      setStatus({ ...status, ok: true, connected: true, state: "connected", label: "Codex 已连接", detail: null })
      return
    }
    if (event.type === "turn_started") {
      setActivity("Codex 正在理解项目与画布…")
      return
    }
    if (event.type === "delta") {
      setAssistantContent(assistantId, (content) => content + event.delta)
      return
    }
    if (event.type === "activity") {
      const suffix = event.status === "completed" || event.status === "success" ? "完成" : "执行中"
      setActivity(`${event.name || "OpenReel 工具"} · ${suffix}`)
      return
    }
    if (event.type === "done") {
      if (event.content) {
        setAssistantContent(assistantId, (content) => content.trim() ? content : event.content || "")
      }
      setStreaming(false)
      setActivity("")
      cancelStreamRef.current = null
      return
    }
    if (event.type === "error") {
      setAssistantContent(assistantId, (content) => (
        content.trim() ? `${content}\n\n连接错误：${event.message}` : `连接错误：${event.message}`
      ))
      setStreaming(false)
      setActivity("")
      cancelStreamRef.current = null
    }
  }, [setAssistantContent, setStatus, status])

  const handleSend = useCallback(async () => {
    if (!project?.id || !status.connected || streaming) return
    const message = input.trim()
    if (!message) return
    const userId = localId("codex-user")
    const assistantId = localId("codex-assistant")
    const now = new Date().toISOString()
    setMessages((current) => [
      ...current,
      { id: userId, role: "user", content: message, createdAt: now },
      { id: assistantId, role: "assistant", content: "", createdAt: now },
    ])
    setInput("")
    setStreaming(true)
    setActivity("正在交给 Codex…")
    try {
      cancelStreamRef.current = await api.codexChatStream(
        project.id,
        message,
        (event) => handleEvent(assistantId, event),
        userId,
      )
    } catch (error) {
      handleEvent(assistantId, {
        type: "error",
        message: error instanceof Error ? error.message : String(error),
      })
    }
  }, [handleEvent, input, project?.id, status.connected, streaming])

  const stop = useCallback(async () => {
    if (!project?.id) return
    try {
      await api.cancelCodexTurn(project.id)
    } catch {}
    cancelStreamRef.current?.()
    cancelStreamRef.current = null
    setStreaming(false)
    setActivity("")
  }, [project?.id])

  const suggestions = useMemo(() => [
    "检查当前画布，告诉我下一步最合理的制作动作",
    "根据当前项目直接搭建一条图片到视频的节点流程",
    "找出失败或未完成的节点，保留原节点并继续修复",
  ], [])

  return (
    <div className="studio-chat-surface relative flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-3 border-b border-white/[0.07] bg-black/10 px-4 py-3 backdrop-blur-xl">
        <CodexMark connected={status.connected} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-zinc-100">Codex</span>
            <span className="rounded border border-violet-300/15 bg-violet-400/[0.07] px-1.5 py-0.5 text-[8px] font-semibold uppercase tracking-[0.16em] text-violet-200/80">OpenReel Control</span>
          </div>
          <div className={`mt-0.5 flex items-center gap-1.5 text-[10px] ${status.connected ? "text-emerald-300" : "text-zinc-500"}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${status.connected ? "bg-emerald-400 shadow-[0_0_9px_rgba(52,211,153,.8)]" : checking ? "animate-pulse bg-amber-400" : "bg-zinc-600"}`} />
            {status.connected ? "Codex 已连接 · 直接操作当前画布" : checking ? "正在连接 Codex" : status.label}
          </div>
        </div>
        {!status.connected && (
          <button
            type="button"
            onClick={() => void refreshStatus(true)}
            disabled={checking}
            className="rounded-lg border border-white/10 bg-white/[0.04] px-2.5 py-1.5 text-[10px] text-zinc-300 transition hover:border-violet-300/25 hover:bg-violet-400/[0.08] disabled:opacity-50"
          >
            {checking ? "连接中" : "重新连接"}
          </button>
        )}
      </div>

      <div ref={scrollRef} className="studio-chat-messages min-h-0 flex-1 space-y-4 overflow-y-auto px-3 py-4 sm:px-5">
        {!status.connected && !checking && (
          <div className="mx-auto mt-6 max-w-sm rounded-2xl border border-amber-300/15 bg-amber-300/[0.045] p-4 text-xs leading-6 text-zinc-300 shadow-[0_24px_80px_rgba(0,0,0,.2)]">
            <div className="mb-1 font-semibold text-amber-100">{status.label}</div>
            <div className="text-zinc-500">{statusHelp(status)}</div>
            <div className="mt-3 border-t border-white/[0.06] pt-3 text-[10px] text-zinc-600">连接成功后，这里就是 Codex 会话；OpenReel 自带 Agent 不参与。</div>
          </div>
        )}

        {status.connected && !historyLoading && messages.length === 0 && (
          <div className="studio-empty-state text-center">
            <div className="mx-auto mb-4 grid h-16 w-16 place-items-center rounded-2xl border border-emerald-300/15 bg-gradient-to-br from-emerald-300/[0.09] to-violet-400/[0.08] text-lg font-black tracking-[-0.08em] text-emerald-100 shadow-[0_22px_60px_rgba(52,211,153,.08)]">CX</div>
            <p className="text-base font-semibold text-zinc-100">Codex 已接管创作会话</p>
            <p className="mx-auto mt-2 max-w-[300px] text-[11px] leading-5 text-zinc-500">你的消息会进入 Codex 线程。Codex 读取当前项目状态，并通过受限工具直接创建、修改和运行画布节点。</p>
            <div className="mt-5 grid gap-2">
              {suggestions.map((suggestion) => (
                <button key={suggestion} type="button" onClick={() => setInput(suggestion)} className="studio-suggestion-chip">
                  <span className="mr-2 text-emerald-300">↗</span>{suggestion}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((message) => (
          <div key={message.id} className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`}>
            <div className={`max-w-[92%] rounded-2xl border px-3.5 py-3 text-sm leading-6 shadow-[0_14px_40px_rgba(0,0,0,.16)] ${
              message.role === "user"
                ? "border-violet-300/15 bg-violet-500/[0.12] text-violet-50"
                : message.role === "assistant"
                  ? "border-white/[0.07] bg-white/[0.035] text-zinc-200"
                  : "border-amber-300/10 bg-amber-300/[0.04] text-amber-100/80"
            }`}>
              {message.role === "assistant" ? (
                message.content ? <MarkdownView compact>{message.content}</MarkdownView> : <span className="inline-flex items-center gap-2 text-xs text-zinc-500"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />Codex 正在思考…</span>
              ) : (
                <div className="whitespace-pre-wrap">{message.content}</div>
              )}
            </div>
          </div>
        ))}

        {activity && (
          <div className="flex items-center gap-2 px-1 text-[10px] text-zinc-500">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-cyan-400" />
            <span className="truncate">{activity}</span>
          </div>
        )}
      </div>

      <div className="studio-composer shrink-0 px-3 py-3 sm:px-4">
        <div className="mb-2 flex items-center justify-between text-[9px] uppercase tracking-[0.14em] text-zinc-600">
          <span>{status.connected ? "Codex direct canvas session" : "Codex offline"}</span>
          {streaming && (
            <button type="button" onClick={() => void stop()} className="normal-case tracking-normal text-red-300 transition hover:text-red-200">停止生成</button>
          )}
        </div>
        <div className="flex items-end gap-2">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault()
                void handleSend()
              }
            }}
            disabled={!project || !status.connected || streaming}
            placeholder={status.connected ? "和 Codex 对话，让它直接操作 OpenReel…" : "连接 Codex 后开始对话"}
            rows={1}
            className="studio-prompt-input min-h-[44px] max-h-40 flex-1 resize-none overflow-y-auto border px-3.5 py-2.5 text-sm leading-[22px] text-zinc-100 placeholder-zinc-600 focus:outline-none disabled:opacity-50"
          />
          <button
            type="button"
            onClick={() => void handleSend()}
            disabled={!project || !status.connected || streaming || !input.trim()}
            className="studio-send-button h-[44px] min-w-[68px] rounded-xl px-4 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-35"
          >
            发送
          </button>
        </div>
      </div>
    </div>
  )
}
