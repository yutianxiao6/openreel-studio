import { create } from "zustand"
import type { CodexBridgeStatus } from "@/lib/api"

export const INITIAL_CODEX_STATUS: CodexBridgeStatus = {
  ok: false,
  connected: false,
  state: "disconnected",
  label: "正在连接 Codex",
  detail: null,
}

interface CodexBridgeStore {
  status: CodexBridgeStatus
  checking: boolean
  setStatus: (status: CodexBridgeStatus) => void
  setChecking: (checking: boolean) => void
}

export const useCodexBridgeStore = create<CodexBridgeStore>((set) => ({
  status: INITIAL_CODEX_STATUS,
  checking: true,
  setStatus: (status) => set({ status }),
  setChecking: (checking) => set({ checking }),
}))
