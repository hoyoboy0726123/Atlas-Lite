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
  serverUpdatedAt?: number   // 伺服器版本戳（秒）—— PUT 帶上，被別處改過會 409
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
    // 樂觀鎖用：只從伺服器回應更新（本地編輯會動 updatedAt，不能拿它當 base）
    serverUpdatedAt: d.updated_at,
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

  // ── AI 助手 ──
  // 「問 AI」按鈕把當下節點的狀態摘要丟進來，助手要接著這個講。
  // 為什麼放在 store 而不是用 props 串：按鈕在節點面板深處，
  // 助手側欄掛在頁面最外層，中間隔了五六層元件。
  assistantOpen: boolean
  askAiContext: string        // 節點狀態摘要
  askAiQuestion: string       // 打開側欄後要自動送出的第一句
  openAssistant: (ctx?: string, question?: string) => void
  closeAssistant: () => void
  clearAskAiSeed: () => void  // 送出後清掉，避免重開側欄又送一次
  // 使用者當前檢視/最後執行的 run —— 助手靠它知道「這次」是指哪次執行，
  // 不用去猜最近清單裡的哪一筆
  currentRunId: string | null
  setCurrentRunId: (id: string | null) => void
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
  entry.timer = setTimeout(() => {
    _pendingUpdates.delete(id)
    void _sendUpdate(id, entry.patch)
  }, 500)
}

/** 實際送 PUT。帶樂觀鎖 base；被別處改過（409）就重載最新版並明講。 */
async function _sendUpdate(id: string, patch: Record<string, any>, opts?: { keepalive?: boolean }) {
  const st = useWorkflowStore.getState()
  const base = st.workflows.find(w => w.id === id)?.serverUpdatedAt
  try {
    const fresh = await updateWorkflowApi(
      id, base !== undefined ? { ...patch, base_updated_at: base } : patch, opts)
    // 成功 → 版本戳前進，之後的存檔以這版為基準
    useWorkflowStore.setState(s => ({
      workflows: s.workflows.map(w =>
        w.id === id ? { ...w, serverUpdatedAt: fresh.updated_at } : w),
    }))
  } catch (e) {
    if (e instanceof Error && e.message.startsWith('CONFLICT:')) {
      // ⚠ 不能靜默重試：這代表 AI 助手或另一個分頁剛改過這條工作流，
      //   硬存會把那些修改整份蓋掉（實測發生過 —— 助手改好的動作被舊分頁清空）。
      //   放棄這次存檔、拉最新版，並明講使用者最後一次改動要重做。
      const { toast } = await import('sonner')
      toast.warning(e.message.slice('CONFLICT:'.length), { duration: 9000 })
      void useWorkflowStore.getState().fetchWorkflows()
      return
    }
    // 其他失敗維持原行為：本地已更新，下次 fetchWorkflows 會同步
  }
}

/** 立刻送出所有還在防抖等待的更新。
 *
 * ⚠ 為什麼需要：防抖 500ms 內若發生「切換工作流 → fetchWorkflows 重抓」或
 *   「重新整理 / dev 熱更新」，還沒送出的修改就永遠消失 —— 實測使用者
 *   設定到一半的動作序列就是這樣不見的。切換與關頁前都要先 flush。 */
function _flushPendingUpdates() {
  for (const [id, entry] of _pendingUpdates) {
    clearTimeout(entry.timer)
    _pendingUpdates.delete(id)
    // keepalive：關頁時普通 fetch 會被瀏覽器取消，keepalive 的請求會被送完
    void _sendUpdate(id, entry.patch, { keepalive: true })
  }
}

// 關頁 / 重新整理 / dev 熱更新前，把還沒送出的修改送掉
if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', _flushPendingUpdates)
  window.addEventListener('beforeunload', _flushPendingUpdates)
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

    assistantOpen: false,
    askAiContext: '',
    askAiQuestion: '',
    openAssistant: (ctx, question) =>
      set({ assistantOpen: true, askAiContext: ctx ?? '', askAiQuestion: question ?? '' }),
    closeAssistant: () => set({ assistantOpen: false }),
    // 連 context 一起清 —— context 只該附在自動送出的那一句上，
    // 留著的話之後每句手打的訊息都會重複帶同一包節點狀態
    clearAskAiSeed: () => set({ askAiQuestion: '', askAiContext: '' }),
    currentRunId: null,
    setCurrentRunId: (id) => set({ currentRunId: id }),

    setActive: (id) => {
      // 先把上一條工作流還沒送出的修改 flush 掉，再切 —— 否則切換後的
      // 重抓會用 DB 裡的舊資料蓋掉記憶體，半途的設定就消失了
      _flushPendingUpdates()
      set({ activeId: id })
    },

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

