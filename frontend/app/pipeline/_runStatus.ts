/**
 * 輕量 Zustand store：追蹤 pipeline 執行時的步驟狀態。
 * 與 ReactFlow nodes 完全分離，避免 setNodes 觸發 ForwardRef 渲染衝突。
 */
import { create } from 'zustand'

export interface StepStatus {
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

interface RunStatusStore {
  /** key = step name, value = runtime status */
  stepStatuses: Record<string, StepStatus>
  edgesAnimated: boolean

  setStepStatus: (name: string, s: StepStatus) => void
  setBulkStatus: (map: Record<string, StepStatus>) => void
  setEdgesAnimated: (v: boolean) => void
  resetAll: () => void
}

export const useRunStatusStore = create<RunStatusStore>((set) => ({
  stepStatuses: {},
  edgesAnimated: false,

  setStepStatus: (name, s) =>
    set((state) => ({
      stepStatuses: { ...state.stepStatuses, [name]: s },
    })),

  setBulkStatus: (map) => set({ stepStatuses: map }),

  setEdgesAnimated: (v) => set({ edgesAnimated: v }),

  resetAll: () => set({ stepStatuses: {}, edgesAnimated: false }),
}))
