import { create } from "zustand"

export type ViewMode = "canvas" | "panel"

interface ViewModeState {
  mode: ViewMode
  setMode: (m: ViewMode) => void
  toggle: () => void
}

export const useViewModeStore = create<ViewModeState>((set, get) => ({
  mode: "canvas",
  setMode: (mode) => set({ mode }),
  toggle: () => set({ mode: get().mode === "canvas" ? "panel" : "canvas" }),
}))
