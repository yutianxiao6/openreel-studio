"use client"

import { motion, AnimatePresence } from "framer-motion"
import { useChatStore, type ChecklistItem, type UnfinishedNode } from "@/stores/chatStore"

const STATUS_LABEL: Record<string, string> = {
  pending: "待执行",
  in_progress: "进行中",
  completed: "完成",
  failed: "失败",
}

const STATUS_DOT: Record<string, string> = {
  pending: "bg-zinc-500",
  in_progress: "bg-blue-400",
  completed: "bg-emerald-400",
  failed: "bg-red-400",
}

function ProgressBar({ done, total, failed }: { done: number; total: number; failed: number }) {
  const pct = total > 0 ? Math.round((done / total) * 100) : 0
  const failPct = total > 0 ? Math.round((failed / total) * 100) : 0
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
        <div className="flex h-full">
          <div
            className="h-full bg-green-500 transition-all duration-500"
            style={{ width: `${pct}%` }}
          />
          {failPct > 0 && (
            <div
              className="h-full bg-red-500 transition-all duration-500"
              style={{ width: `${failPct}%` }}
            />
          )}
        </div>
      </div>
      <span className="text-[10px] text-gray-400 whitespace-nowrap">
        {done}/{total}
      </span>
    </div>
  )
}

function UnfinishedWarning({ nodes }: { nodes: UnfinishedNode[] }) {
  if (!nodes.length) return null
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: "auto" }}
      className="bg-red-900/30 border border-red-700/50 rounded-lg px-3 py-2 mb-2"
    >
      <div className="text-xs font-medium text-red-300 mb-1">
        画布上有 {nodes.length} 个未完成节点，处理完前禁止创建新节点
      </div>
      <ul className="text-[10px] text-red-400/80 space-y-0.5 max-h-20 overflow-y-auto">
        {nodes.slice(0, 5).map((n) => (
          <li key={n.node_id} className="truncate">
            <span className="text-red-300">{n.title}</span>
            {" — "}{n.reason}
          </li>
        ))}
        {nodes.length > 5 && (
          <li className="text-red-500">… 及其他 {nodes.length - 5} 个</li>
        )}
      </ul>
    </motion.div>
  )
}

function actionLabel(tool: string): string {
  if (tool === "task.create") return "创建任务"
  if (tool === "task.update") return "更新任务"
  if (tool === "task.complete") return "完成任务"
  if (tool === "task.list") return "检查任务"
  if (tool === "node.create") return "创建节点"
  if (tool === "node.run") return "生成内容"
  if (tool === "node.update") return "更新节点"
  if (tool === "canvas.delete") return "删除节点"
  return "执行"
}

function ChecklistRow({ item, isActive }: { item: ChecklistItem; isActive: boolean }) {
  const nodeLabel = item.actual_node_id
    ? "已绑定"
    : item.status === "completed"
      ? "已完成"
      : "待产出"
  return (
    <motion.tr
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: 1, x: 0 }}
      className={
        "border-b border-gray-800/60 text-xs transition-colors " +
        (isActive
          ? "bg-indigo-900/30 border-l-2 border-l-indigo-400"
          : item.status === "completed"
            ? "opacity-70"
            : "")
      }
    >
      <td className="py-1.5 px-2 text-center text-[10px] text-gray-500">
        {item.step}
      </td>
      <td className="py-1.5 px-1 text-center" title={STATUS_LABEL[item.status] || item.status}>
        <motion.span
          animate={isActive ? { opacity: [1, 0.35, 1], scale: [1, 0.86, 1] } : undefined}
          transition={isActive ? { duration: 1.2, repeat: Infinity } : undefined}
          className={`mx-auto block h-2 w-2 rounded-full ${STATUS_DOT[item.status] || STATUS_DOT.pending}`}
        />
      </td>
      <td
        className={
          "py-1.5 px-2 max-w-[160px] truncate " +
          (item.status === "completed"
            ? "text-green-300/70 line-through decoration-green-700/40"
            : item.status === "failed"
              ? "text-red-300"
              : "text-gray-200")
        }
      >
        {item.title}
      </td>
      <td className="py-1.5 px-2 text-[10px] text-gray-500 max-w-[140px] truncate">
        {actionLabel(item.tool)}
      </td>
      <td className="py-1.5 px-2 text-[10px] text-gray-600">
        {nodeLabel}
      </td>
    </motion.tr>
  )
}

export function ChecklistPanel() {
  const checklist = useChatStore((s) => s.activeChecklist)
  const unfinished = useChatStore((s) => s.unfinishedNodes)

  if (!checklist.length && !unfinished.length) return null

  const done = checklist.filter((s) => s.status === "completed").length
  const failed = checklist.filter((s) => s.status === "failed").length
  const total = checklist.length
  const activeIndex = checklist.findIndex(
    (s) => s.status === "in_progress" || s.status === "pending",
  )

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0, y: -12 }}
        animate={{ opacity: 1, y: 0 }}
        className="border-b border-gray-800/60 bg-gray-950/80 backdrop-blur"
      >
        {/* 未完成节点警告 */}
        <UnfinishedWarning nodes={unfinished} />

        {/* 清单表格 */}
        {checklist.length > 0 && (
          <div className="px-2 pb-2">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[11px] font-medium text-gray-400">
                任务
              </span>
              <span className="text-[10px] text-gray-500">
                {done}/{total} 完成{failed > 0 ? `, ${failed} 失败` : ""}
              </span>
            </div>
            <ProgressBar done={done} total={total} failed={failed} />
            <div className="mt-1.5 max-h-[180px] overflow-y-auto scrollbar-thin">
              <table className="w-full">
                <thead>
                  <tr className="text-[10px] text-gray-500 border-b border-gray-800/40">
                    <th className="py-1 px-2 text-center w-6">#</th>
                    <th className="py-1 px-1 text-center w-6"></th>
                    <th className="py-1 px-2 text-left">步骤</th>
                    <th className="py-1 px-2 text-left">动作</th>
                    <th className="py-1 px-2 text-left w-16">产物</th>
                  </tr>
                </thead>
                <tbody>
                  {checklist.map((item, i) => (
                    <ChecklistRow
                      key={item.step_id}
                      item={item}
                      isActive={i === activeIndex}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </motion.div>
    </AnimatePresence>
  )
}
