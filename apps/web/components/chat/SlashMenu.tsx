"use client"

import { useEffect, useMemo, useRef } from "react"

export interface SlashCommandDef {
  name: string
  description: string
  usage?: string
  insertText?: string
  insertOnly?: boolean
  searchText?: string
}

export const SLASH_COMMANDS: SlashCommandDef[] = [
  { name: "/help", description: "显示帮助" },
  { name: "/plan", description: "进入只读 Plan Mode", usage: "/plan [目标|execute|exit]" },
  { name: "/workflow", description: "进入工作流搭建模式", usage: "/workflow [exit]" },
  { name: "/reset", description: "清理失败节点或确认重置", usage: "/reset [failed|full|confirm|cancel]" },
  { name: "/doctor", description: "项目诊断快照" },
  { name: "/status", description: "系统状态(模型/工具/MCP)" },
  { name: "/config", description: "LLM/图片/视频/Key 配置总览" },
  { name: "/model", description: "当前模型配置" },
  { name: "/mcp", description: "MCP 连接状态" },
  { name: "/clear", description: "清空对话、任务和流程运行态" },
]

export const SLASH_COMMAND_COMPLETIONS: SlashCommandDef[] = [
  { name: "/plan", description: "进入 Plan Mode", insertOnly: true },
  { name: "/plan execute", description: "执行最近的 proposed plan", insertOnly: true },
  { name: "/plan exit", description: "退出 Plan Mode", insertOnly: true },
  { name: "/workflow", description: "进入工作流搭建模式", insertOnly: true },
  { name: "/workflow exit", description: "退出工作流搭建模式", insertOnly: true },
  { name: "/reset failed", description: "清理失败且无产出的节点", insertOnly: true },
  { name: "/reset full", description: "请求全量重置确认", insertOnly: true },
  { name: "/reset confirm", description: "确认全量重置", insertOnly: true },
  { name: "/reset cancel", description: "取消全量重置", insertOnly: true },
]

interface SlashMenuProps {
  query: string
  extraCompletions?: SlashCommandDef[]
  selectedIndex: number
  onSelect: (cmd: SlashCommandDef) => void
  onHover: (index: number) => void
}

export function filterSlashCommands(query: string, extraCompletions: SlashCommandDef[] = []): SlashCommandDef[] {
  const firstLine = query.split("\n", 1)[0] ?? ""
  if (!firstLine.startsWith("/")) return []

  const q = firstLine.toLowerCase().replace(/\s+/g, " ")
  const trimmed = q.trimEnd()
  if (!trimmed || trimmed === "/") return SLASH_COMMANDS

  if (!/\s/.test(q)) {
    return SLASH_COMMANDS.filter((c) =>
      c.name.toLowerCase().startsWith(trimmed) ||
      c.description.toLowerCase().includes(trimmed.slice(1)),
    )
  }

  const commandName = trimmed.split(/\s+/, 1)[0]
  const queryForMatch = q.endsWith(" ") ? q : trimmed
  const queryWords = trimmed.slice(1).split(/\s+/).filter(Boolean)
  return [...SLASH_COMMAND_COMPLETIONS, ...extraCompletions].filter((c) => {
    const name = c.name.toLowerCase()
    if (!name.startsWith(`${commandName} `)) return false
    if (q.endsWith(" ") && name === trimmed) return false
    if (name.startsWith(queryForMatch)) return true
    if (!c.searchText) return false
    const haystack = `${name} ${c.searchText}`.toLowerCase()
    return queryWords.every((word) => haystack.includes(word))
  })
}

export function SlashMenu({ query, extraCompletions = [], selectedIndex, onSelect, onHover }: SlashMenuProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const items = useMemo(() => filterSlashCommands(query, extraCompletions), [query, extraCompletions])

  useEffect(() => {
    const el = containerRef.current?.children[selectedIndex] as HTMLElement | undefined
    el?.scrollIntoView({ block: "nearest" })
  }, [selectedIndex])

  if (items.length === 0) return null

  return (
    <div className="absolute bottom-full left-0 right-0 mb-2 max-h-64 overflow-y-auto rounded-xl border border-violet-300/15 bg-[#0d121c]/96 shadow-[0_24px_70px_rgba(0,0,0,.52)] backdrop-blur-xl animate-[studio-enter_.18s_ease-out]">
      <div ref={containerRef}>
        {items.map((cmd, i) => {
          const active = i === selectedIndex
          return (
            <button
              key={cmd.name}
              type="button"
              onMouseEnter={() => onHover(i)}
              onClick={() => onSelect(cmd)}
              className={`flex w-full items-center gap-3 px-3 py-2 text-left text-xs transition-colors ${
                active ? "bg-gradient-to-r from-violet-500/25 to-cyan-400/10 text-white" : "text-gray-200 hover:bg-white/[0.045]"
              }`}
            >
              <span className="min-w-[150px] whitespace-nowrap font-mono font-semibold text-violet-300">{cmd.name}</span>
              <span className="flex-1 truncate text-gray-400">{cmd.description}</span>
              {cmd.usage && active && (
                <span className="text-[10px] text-gray-500 font-mono">{cmd.usage}</span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}
