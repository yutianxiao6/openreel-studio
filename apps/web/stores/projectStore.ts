import { create } from "zustand"

export interface ProjectRecord {
  id: string
  title: string
  description?: string | null
  genre?: string | null
  format?: string | null
  episode_count?: number
  duration_per_episode?: number
  budget_level?: string | null
  status?: string
  state_json?: string | Record<string, unknown> | null
  created_at?: string
  updated_at?: string
}

interface ProjectStore {
  currentProject: ProjectRecord | null
  projects: ProjectRecord[]
  isLoading: boolean
  setCurrentProject: (project: ProjectRecord | null) => void
  setProject: (project: ProjectRecord | null) => void
  setProjects: (projects: ProjectRecord[]) => void
  setLoading: (loading: boolean) => void
  updateCurrentProject: (updates: Partial<ProjectRecord>) => void
}

export const useProjectStore = create<ProjectStore>((set) => ({
  currentProject: null,
  projects: [],
  isLoading: false,
  setCurrentProject: (project) => set({ currentProject: project }),
  setProject: (project) => set({ currentProject: project }),
  setProjects: (projects) => set({ projects }),
  setLoading: (isLoading) => set({ isLoading }),
  updateCurrentProject: (updates) =>
    set((state) => ({
      currentProject: state.currentProject
        ? { ...state.currentProject, ...updates }
        : null,
      projects: state.currentProject
        ? state.projects.map((project) =>
            project.id === state.currentProject?.id ? { ...project, ...updates } : project,
          )
        : state.projects,
    })),
}))
