import { create } from "zustand"
import type { CodexBridgeStatus } from "@/lib/api"

export type ChatAgentMode = "openreel" | "codex"

export const INITIAL_CODEX_STATUS: CodexBridgeStatus = {
  ok: false,
  connected: false,
  state: "disconnected",
  label: "正在连接 Codex",
  detail: null,
}

interface CodexBridgeStore {
  mode: ChatAgentMode
  status: CodexBridgeStatus
  checking: boolean
  setMode: (mode: ChatAgentMode) => void
  setStatus: (status: CodexBridgeStatus) => void
  setChecking: (checking: boolean) => void
}

export const useCodexBridgeStore = create<CodexBridgeStore>((set) => ({
  mode: "openreel",
  status: INITIAL_CODEX_STATUS,
  checking: false,
  setMode: (mode) => set({ mode }),
  setStatus: (status) => set({ status }),
  setChecking: (checking) => set({ checking }),
}))
