import { create } from 'zustand'
import type { Edge } from '@xyflow/react'
import type { AppNode } from './_helpers'
import {
  listWorkflows, createWorkflowApi, updateWorkflowApi, deleteWorkflowApi,
  type WorkflowData,
} from '@/lib/api'

// ── 一個工作流的完整資料 ─────────────────────────────────────────────────────
export interface Workflow {
  id: string
  name: string
  nodes: AppNode[]
  edges: Edge[]
  updatedAt: number
}

/** 遷移舊節點類型：pipelineStep → scriptStep。
 *
 * Atlas 這裡還會依 skillMode 分流到 skillStep —— Atlas-Lite 沒有那種節點，
 * 一律當腳本節點。真的是 AI 技能的舊節點載入後會是空指令，使用者看得到。
 * 其他節點型別（humanConfirmation / computerUse / condition）不需遷移。
 */
function migrateNodes(nodes: AppNode[]): AppNode[] {
  return nodes.map(n =>
    n.type === 'pipelineStep' ? { ...n, type: 'scriptStep' as const } : n
  )
}

function apiToWorkflow(d: WorkflowData): Workflow {
  return {
    id: d.id,
    name: d.name,
    nodes: migrateNodes((d.canvas?.nodes ?? []) as AppNode[]),
    edges: (d.canvas?.edges ?? []) as Edge[],
    updatedAt: d.updated_at * 1000,  // backend uses seconds, frontend uses ms
  }
}

// ── Store ────────────────────────────────────────────────────────────────────
interface WorkflowStore {
  workflows: Workflow[]
  activeId:  string | null
  loaded:    boolean         // 是否已從 API 載入

  // CRUD (all async, hit backend API)
  fetchWorkflows: () => Promise<void>
  createWorkflow: (name?: string) => Promise<string>   // returns new id
  updateWorkflow: (id: string, patch: Partial<Omit<Workflow, 'id'>>) => void
  removeWorkflow: (id: string) => Promise<void>
  setActive:      (id: string) => void
  getActive:      () => Workflow | undefined

  // 儲存目前畫布狀態（debounced by caller）。
  // 同時帶上 yaml 一起存，讓 TG 遠端遙控等不經過前端 getYaml() 的入口
  // 也能直接讀到對應的 YAML（不再因為 yaml 欄位空而拒絕啟動）。
  saveCanvas: (id: string, nodes: AppNode[], edges: Edge[], yaml?: string) => void
}

// 防抖佇列：合併多次快速 saveCanvas / updateWorkflow 呼叫
const _pendingUpdates = new Map<string, { timer: ReturnType<typeof setTimeout>; patch: Record<string, any> }>()

function _debouncedApiUpdate(id: string, patch: Record<string, any>) {
  const existing = _pendingUpdates.get(id)
  if (existing) {
    clearTimeout(existing.timer)
    Object.assign(existing.patch, patch)
  } else {
    _pendingUpdates.set(id, { timer: 0 as any, patch: { ...patch } })
  }
  const entry = _pendingUpdates.get(id)!
  entry.timer = setTimeout(async () => {
    _pendingUpdates.delete(id)
    try {
      await updateWorkflowApi(id, entry.patch)
    } catch {
      // 靜默失敗 — 本地狀態已更新，下次 fetchWorkflows 會同步
    }
  }, 500)
}

export const useWorkflowStore = create<WorkflowStore>()(
  (set, get) => ({
    workflows: [],
    activeId:  null,
    loaded:    false,

    fetchWorkflows: async () => {
      try {
        const data = await listWorkflows()
        const workflows = data.map(apiToWorkflow)
        const { activeId } = get()
        const active = activeId && workflows.find(w => w.id === activeId)
          ? activeId
          : (workflows[0]?.id ?? null)
        set({ workflows, activeId: active, loaded: true })
      } catch {
        set({ loaded: true })
      }
    },

    createWorkflow: async (name) => {
      const data = await createWorkflowApi(name ?? '新工作流')
      const wf = apiToWorkflow(data)
      set(s => ({ workflows: [...s.workflows, wf], activeId: wf.id }))
      return wf.id
    },

    updateWorkflow: (id, patch) => {
      // 立即更新本地狀態
      set(s => ({
        workflows: s.workflows.map(w =>
          w.id === id ? { ...w, ...patch, updatedAt: Date.now() } : w
        ),
      }))
      // 異步 debounced 更新後端
      const apiPatch: Record<string, any> = {}
      if (patch.name !== undefined) apiPatch.name = patch.name
      if (Object.keys(apiPatch).length > 0) {
        _debouncedApiUpdate(id, apiPatch)
      }
    },

    removeWorkflow: async (id) => {
      set(s => {
        const ws = s.workflows.filter(w => w.id !== id)
        const activeId = s.activeId === id ? (ws[ws.length - 1]?.id ?? null) : s.activeId
        return { workflows: ws, activeId }
      })
      try {
        await deleteWorkflowApi(id)
      } catch {
        // 靜默
      }
    },

    setActive: (id) => set({ activeId: id }),

    getActive: () => {
      const { workflows, activeId } = get()
      return workflows.find(w => w.id === activeId)
    },

    saveCanvas: (id, nodes, edges, yaml) => {
      // 立即更新本地狀態
      set(s => ({
        workflows: s.workflows.map(w =>
          w.id === id ? { ...w, nodes, edges, updatedAt: Date.now() } : w
        ),
      }))
      // 異步 debounced 更新後端 — 帶 yaml 一起存（給 TG 遠端遙控用）
      const patch: Record<string, any> = { canvas: { nodes, edges } }
      if (typeof yaml === 'string') patch.yaml = yaml
      _debouncedApiUpdate(id, patch)
    },
  })
)

