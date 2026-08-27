'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow, Background, Controls, MiniMap, Panel,
  addEdge, useNodesState, useEdgesState,
  BackgroundVariant, MarkerType, NodeToolbar, Position,
  type Connection, type Edge, type ReactFlowInstance, type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import InsertableEdge from './_insertableEdge'

import {
  Play, Clock, Code2, Plus, Square, Zap,
  Loader2, CheckCircle2, XCircle, Workflow, Terminal, X,
  UserCheck, MousePointer2,
} from 'lucide-react'
import { toast } from 'sonner'
import { Toaster } from 'sonner'

import ScriptStepNode              from './_scriptNode'
import HumanConfirmNodeComponent   from './_humanConfirmNode'
import ComputerUseNodeComponent    from './_computerUseNode'
import ConditionNodeComponent      from './_conditionNode'
import ScriptConfigPanel           from './_scriptPanel'
import HumanConfirmPanel           from './_humanConfirmPanel'
import ComputerUsePanel            from './_computerUsePanel'
import ConditionPanel               from './_conditionPanel'
import TriggerPanel                 from './_triggerPanel'
import HoverScrollRow               from './_hoverScrollRow'
import Sidebar                from './_sidebar'
import {
  type AppNode, type StepData, type HumanConfirmData, type ComputerUseData, type ConditionData,
  type ScriptNode, type HumanConfirmNode, type ComputerUseNode, type ConditionNode,
  newStepData, newHumanConfirmData, newComputerUseData, dedupeComputerUseName, newConditionData,
  stepsToFlow, flowToSteps, stepsToYaml, parseYaml,
} from './_helpers'
import { useWorkflowStore } from './_store'
import {
  startPipeline, getPipelineRun, resumePipeline, abortPipeline,
  createPipelineSchedule, getPipelineLog, getPipelineRuns,
  deleteComputerUseAssets, openOutputFolder,
  validateWorkflowYaml,
  applyWorkflowYaml,
} from '@/lib/api'
import type { PipelineRun } from '@/lib/types'
import { useRunStatusStore } from './_runStatus'

const nodeTypes = {
  scriptStep: ScriptStepNode,
  humanConfirmation: HumanConfirmNodeComponent,
  computerUse: ComputerUseNodeComponent,
  condition: ConditionNodeComponent,
}

// Edge 類型：全部用 InsertableEdge — hover 出 + / 🗑️ 按鈕（n8n 風格）
const edgeTypes = {
  insertable: InsertableEdge,
}

// 新 edge 的共同設定：箭頭 + indigo 顏色 + insertable type
const DEFAULT_EDGE_OPTIONS = {
  type: 'insertable' as const,
  style: { stroke: '#6366f1', strokeWidth: 2 },
  markerEnd: { type: MarkerType.ArrowClosed, color: '#6366f1', width: 18, height: 18 },
  selectable: true,
}

// ── 排程對話框 ────────────────────────────────────────────────────────────────
function ScheduleDialog({ yaml, pipelineName, workflowId, onClose }: {
  yaml: string; pipelineName: string; workflowId: string | null; onClose: () => void
}) {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const todayStr = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
  const timeStr  = `${pad(now.getHours() + 1)}:00`

  const [mode, setMode]     = useState<'once' | 'cron'>('once')
  const [onceDate, setDate] = useState(todayStr)
  const [onceTime, setTime] = useState(timeStr)
  const [cronExpr, setCron] = useState('0 9 * * 1-5')
  const [loading, setLoading] = useState(false)

  const handleSave = async () => {
    setLoading(true)
    try {
      let expr = ''
      if (mode === 'once') {
        expr = `${onceDate}T${onceTime}:00`
      } else {
        expr = cronExpr.trim()
        if (!expr) { toast.error('請輸入 cron 表達式'); setLoading(false); return }
      }
      await createPipelineSchedule({
        name: pipelineName || 'my-pipeline',
        yaml_content: yaml,
        schedule_type: mode,
        schedule_expr: expr,
        workflow_id: workflowId ?? undefined,
      })
      toast.success('排程已建立')
      onClose()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '建立失敗')
    } finally { setLoading(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-2xl w-96 overflow-hidden">
        <div className="flex items-center gap-3 px-5 py-4 border-b">
          <Clock className="w-4 h-4 text-indigo-600" />
          <span className="font-semibold text-gray-800">設定排程</span>
        </div>
        <div className="p-5 space-y-4">
          <div>
            <label className="text-xs font-medium text-gray-500 mb-2 block">排程類型</label>
            <div className="flex gap-2">
              {(['once', 'cron'] as const).map(m => (
                <button key={m} onClick={() => setMode(m)}
                  className={`flex-1 py-1.5 rounded-lg text-sm font-medium border transition-colors
                    ${mode === m ? 'bg-indigo-600 text-white border-indigo-600' : 'text-gray-600 border-gray-200 hover:border-indigo-400'}`}
                >{m === 'once' ? '一次性' : '週期（Cron）'}</button>
              ))}
            </div>
          </div>
          {mode === 'once' ? (
            <div className="flex gap-2">
              <input type="date" value={onceDate} onChange={e => setDate(e.target.value)} min={todayStr}
                className="flex-1 border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none focus:border-indigo-400" />
              <input type="time" value={onceTime} onChange={e => setTime(e.target.value)}
                className="flex-1 border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none focus:border-indigo-400" />
            </div>
          ) : (
            <div>
              <input value={cronExpr} onChange={e => setCron(e.target.value)}
                placeholder="0 9 * * 1-5"
                className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono outline-none focus:border-indigo-400" />
              <p className="text-xs text-gray-400 mt-1">分 時 日 月 週。範例：0 9 * * 1-5 = 週一到五早上 9 點</p>
            </div>
          )}
          <p className="text-xs text-gray-400 leading-relaxed">
            排程觸發時沒有人在旁邊 —— 含人工確認節點的工作流會停在那裡等你回應
            （設了「超時自動行動」才會自己往下走）。
          </p>
        </div>
        <div className="px-5 py-4 border-t flex gap-2 justify-end">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:bg-gray-50 rounded-lg transition-colors">取消</button>
          <button onClick={handleSave} disabled={loading}
            className="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-60 flex items-center gap-2 transition-colors"
          >
            {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Clock className="w-3.5 h-3.5" />}
            建立排程
          </button>
        </div>
      </div>
    </div>
  )
}

// ── 執行對話框（填啟動參數）───────────────────────────────────────────────────
function RunDialog({ workflowId, onRun, onClose }: {
  workflowId?: string
  onRun: (inputParams: Record<string, string>) => void
  onClose: () => void
}) {
  // 掃這個工作流引用了哪些 input.X，把上次的值預填回去
  const [inputKeys, setInputKeys] = useState<string[]>([])
  const [inputParams, setInputParams] = useState<Record<string, string>>({})
  useEffect(() => {
    if (!workflowId) return
    let cancelled = false
    import('@/lib/api').then(({ getWorkflowVariables }) =>
      getWorkflowVariables(workflowId)
        .then((r) => {
          if (cancelled) return
          setInputKeys(r.available.input.map((i) => i.key))
          const init: Record<string, string> = {}
          for (const i of r.available.input) init[i.key] = String(i.last_value ?? '')
          setInputParams(init)
        })
        .catch(() => {}),
    )
    return () => { cancelled = true }
  }, [workflowId])

  const setQuickDate = (k: string, kind: 'today' | 'yesterday' | 'tomorrow') => {
    const d = new Date()
    if (kind === 'yesterday') d.setDate(d.getDate() - 1)
    else if (kind === 'tomorrow') d.setDate(d.getDate() + 1)
    setInputParams((p) => ({ ...p, [k]: d.toISOString().slice(0, 10) }))
  }

  const missing = inputKeys.filter((k) => !(inputParams[k] || '').trim())

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div className="bg-white rounded-2xl shadow-2xl w-[460px] max-h-[88vh] overflow-hidden flex flex-col" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 px-5 py-4 border-b">
          <Play className="w-4 h-4 text-indigo-600" />
          <span className="font-semibold text-gray-800">執行工作流</span>
        </div>
        <div className="p-5 space-y-3 overflow-y-auto">
          {inputKeys.length > 0 ? (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 space-y-2.5">
              <div className="text-xs font-semibold text-amber-800">📌 這個工作流需要啟動參數</div>
              {inputKeys.map((k) => (
                <div key={k}>
                  <label className="text-[11px] text-gray-600 block mb-0.5 font-mono">input.{k}</label>
                  <input
                    value={inputParams[k] ?? ''}
                    onChange={(e) => setInputParams((p) => ({ ...p, [k]: e.target.value }))}
                    placeholder={k.toLowerCase().includes('date') ? '2026-05-10' : ''}
                    className="w-full border border-gray-200 rounded-md px-2 py-1 text-xs font-mono outline-none focus:border-indigo-400 bg-white"
                  />
                  {k.toLowerCase().includes('date') && (
                    <div className="flex gap-1.5 mt-1">
                      {(['today', 'yesterday', 'tomorrow'] as const).map((kind) => (
                        <button
                          key={kind}
                          type="button"
                          onClick={() => setQuickDate(k, kind)}
                          className="text-[10px] px-1.5 py-0.5 rounded border border-gray-200 text-gray-500 hover:bg-indigo-50 hover:border-indigo-300 hover:text-indigo-700"
                        >{kind === 'today' ? '今天' : kind === 'yesterday' ? '昨天' : '明天'}</button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
              {missing.length > 0 && (
                <p className="text-[11px] text-amber-700">⚠ 還缺：<span className="font-mono">{missing.join(', ')}</span></p>
              )}
            </div>
          ) : (
            <p className="text-sm text-gray-500">這個工作流不需要啟動參數，可以直接執行。</p>
          )}
          <button
            onClick={() => onRun(inputParams)}
            disabled={missing.length > 0}
            className={`w-full p-3 rounded-xl text-sm font-medium border-2 transition-all flex items-center justify-center gap-2 ${
              missing.length === 0
                ? 'border-indigo-200 text-indigo-700 hover:border-indigo-400 hover:bg-indigo-50 cursor-pointer'
                : 'border-gray-100 bg-gray-50 text-gray-400 cursor-not-allowed'
            }`}
          >
            <Play className="w-4 h-4" /> 開始執行
          </button>
        </div>
        <div className="px-5 py-3 border-t bg-gray-50 flex justify-end">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700 transition-colors">取消</button>
        </div>
      </div>
    </div>
  )
}

// ── YAML Panel（Terminal 風格）─────────────────────────────────────────────────
function YamlPanel({ yaml, onImport, onClose }: { yaml: string; onImport: (y: string) => void; onClose: () => void }) {
  const [draft, setDraft] = useState(yaml)
  useEffect(() => setDraft(yaml), [yaml])
  return (
    <div className="absolute top-0 right-0 h-full w-[460px] bg-gray-950 shadow-2xl border-l border-gray-800 z-40 flex flex-col">
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
        <div className="flex items-center gap-2">
          <Terminal className="w-4 h-4 text-green-400" />
          <span className="font-semibold text-sm text-gray-300 font-mono">YAML</span>
        </div>
        <div className="flex gap-2">
          <button onClick={async () => {
              try { await navigator.clipboard.writeText(draft); toast.success('已複製 YAML') }
              catch { toast.error('複製失敗，請手動選取') }
            }}
            className="px-3 py-1 text-xs border border-gray-600 text-gray-300 rounded-lg hover:bg-gray-800 transition-colors font-mono">
            複製
          </button>
          <button onClick={() => { onImport(draft); toast.success('已從 YAML 更新流程') }}
            className="px-3 py-1 text-xs bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors font-mono">
            套用
          </button>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-lg leading-none">×</button>
        </div>
      </div>
      <textarea
        value={draft}
        onChange={e => setDraft(e.target.value)}
        className="flex-1 p-4 text-xs font-mono text-green-400 bg-gray-950 resize-none outline-none leading-relaxed caret-green-400"
        style={{ caretColor: '#4ade80' }}
        spellCheck={false}
      />
    </div>
  )
}

// ── Empty State ───────────────────────────────────────────────────────────────
function EmptyState({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
      <div className="pointer-events-auto flex flex-col items-center gap-4 text-center">
        <div className="w-16 h-16 rounded-2xl bg-indigo-50 flex items-center justify-center">
          <Workflow className="w-8 h-8 text-indigo-400" />
        </div>
        <div>
          <p className="text-gray-600 font-medium mb-1">尚未建立任何步驟</p>
          <p className="text-gray-400 text-sm">點擊下方按鈕新增第一個步驟</p>
        </div>
        <button
          onClick={onAdd}
          className="flex items-center gap-2 px-5 py-2.5 bg-indigo-600 text-white rounded-xl shadow-lg hover:bg-indigo-700 transition-colors font-medium text-sm"
        >
          <Plus className="w-4 h-4" />
          新增步驟
        </button>
      </div>
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────
export default function PipelinePage() {
  const [nodes, setNodes, onNodesChange] = useNodesState<AppNode>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  // 滑鼠停留節點 id(顯示 hover 浮動複製按鈕用)— 用 ref 計時器延遲清除、給滑鼠時間移到 toolbar
  const [hoveredId, setHoveredId] = useState<string | null>(null)
  const hoverLeaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const cancelHoverClear = useCallback(() => {
    if (hoverLeaveTimerRef.current) {
      clearTimeout(hoverLeaveTimerRef.current)
      hoverLeaveTimerRef.current = null
    }
  }, [])
  const clearHoverDelayed = useCallback(() => {
    cancelHoverClear()
    hoverLeaveTimerRef.current = setTimeout(() => setHoveredId(null), 200)
  }, [cancelHoverClear])
  const [pipelineName, setPipelineName] = useState('my-pipeline')
  const [showYaml, setShowYaml]   = useState(false)
  const [showSchedule, setShowSchedule] = useState(false)
  const [showRunDialog, setShowRunDialog] = useState(false)
  const [showTriggers, setShowTriggers] = useState(false)   // 觸發器面板(Webhook / 檔案夾監看)
  const [running, setRunning]     = useState(false)
  const [runStatus, _setRunStatus] = useState<'idle' | 'running' | 'success' | 'failed' | 'awaiting'>('idle')
  const runStatusRef = useRef(runStatus)
  const setRunStatus = (v: typeof runStatus) => { runStatusRef.current = v; _setRunStatus(v) }
  const [awaitingRunId, setAwaitingRunId] = useState<string | null>(null)
  const [awaitingType, setAwaitingType] = useState<'failure' | 'confirm' | 'missing_dep'>('failure')
  // Phase 3 自我修復回寫:修復成功跑完後,問是否把修好的 YAML 存回存檔工作流
  const [awaitingMessage, setAwaitingMessage] = useState('')
  const [awaitingSuggestion, setAwaitingSuggestion] = useState('')
  const [installing, setInstalling] = useState(false)   // 缺套件「安裝並繼續」進行中（顯示安裝中、disable 按鈕）
  const [showLog, setShowLog]       = useState(false)
  const [logLines, setLogLines]     = useState<string[]>([])
  const logEndRef  = useRef<HTMLDivElement>(null)
  const logContainerRef = useRef<HTMLDivElement>(null)
  const logAutoScrollRef = useRef(true)

  // ── Log panel 高度調整 ─────────────────────────────────────
  const LOG_HEIGHT_KEY = 'pipeline-log-height'
  const LOG_MIN_HEIGHT = 150
  const LOG_DEFAULT_HEIGHT = 256  // 原本的 h-64
  const [logHeight, setLogHeight] = useState(LOG_DEFAULT_HEIGHT)
  const [logResizing, setLogResizing] = useState(false)
  useEffect(() => {
    const saved = Number(localStorage.getItem(LOG_HEIGHT_KEY))
    if (saved >= LOG_MIN_HEIGHT) setLogHeight(saved)
  }, [])
  useEffect(() => {
    if (!logResizing) return
    const onMove = (e: MouseEvent) => {
      // 從視窗底往上算 → 拖曳越上寬，面板越高
      const maxHeight = Math.floor(window.innerHeight / 2)  // 最多占一半螢幕
      const fromBottom = window.innerHeight - e.clientY
      const h = Math.min(maxHeight, Math.max(LOG_MIN_HEIGHT, fromBottom))
      setLogHeight(h)
    }
    const onUp = () => {
      setLogResizing(false)
      try { localStorage.setItem(LOG_HEIGHT_KEY, String(logHeight)) } catch {}
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [logResizing, logHeight])
  const rfInstanceRef = useRef<ReactFlowInstance<AppNode, Edge> | null>(null)
  const [editingName, setEditingName] = useState(false)
  const runIdRef   = useRef<string | null>(null)
  // latestRunId 鏡像 runIdRef.current 給 useEffect 依賴用 — 切完成工作流時 Trace 視圖能即時 re-fetch、
  // 不用等 3 秒 interval。ref 仍是 source of truth、所有寫入都用 setRunId helper 雙寫
  const [latestRunId, setLatestRunId] = useState<string | null>(null)
  const setRunId = (v: string | null) => { runIdRef.current = v; setLatestRunId(v) ; useWorkflowStore.getState().setCurrentRunId(v) }
  const pollRef    = useRef<ReturnType<typeof setInterval> | null>(null)
  const savingRef  = useRef(false)  // 防止切換工作流時觸發 auto-save

  // ── Workflow Store ────────────────────────────────────────────────────────
  const { activeId, workflows, updateWorkflow, saveCanvas, createWorkflow } = useWorkflowStore()

  // 當 activeId 改變時，載入對應工作流（defer 避免 render-time setState）
  useEffect(() => {
    if (!activeId) return
    const wf = workflows.find(w => w.id === activeId)
    if (!wf) return
    savingRef.current = true
    // 切換工作流前：清除上一個工作流的執行狀態
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
    setRunId(null)
    setRunning(false)
    useRunStatusStore.getState().resetAll()
    const timer = setTimeout(() => {
      setNodes(wf.nodes as AppNode[])
      setEdges(wf.edges)
      setPipelineName(wf.name)
      setSelectedId(null)
      setRunStatus('idle')
      setAwaitingRunId(null)
      setTimeout(() => {
        savingRef.current = false
        rfInstanceRef.current?.fitView({ padding: 0.3, duration: 300 })
        // 一次性 yaml backfill — 對舊工作流(DB yaml 欄位空)很關鍵、
        // 因為單純點開不修改不會觸發 auto-save。idempotent、重複載入也只會覆寫成相同值。
        // 這也是 TG 遠端遙控能讀到 yaml 的最後一道保險。
        //
        // ⚠ 跳過 nodes 為空的 workflow:capture 的 `wf` 是 1 秒前的 snapshot。
        // 若這 1 秒內有 importYaml('new') 寫入 reddit canvas、backfill 用舊 capture
        // 會用「空 nodes」蓋掉 backend 已存的好資料。導致 user 從 hero 套用 YAML 後、
        // 切回工作流發現 canvas 變空。empty workflow 也沒 yaml 可 backfill、skip 安全。
        try {
          if (wf.nodes && wf.nodes.length > 0) {
            const yaml = stepsToYaml(wf.name, flowToSteps(wf.nodes as AppNode[], wf.edges))
            saveCanvas(activeId, wf.nodes as AppNode[], wf.edges, yaml)
          }
        } catch { /* 解析失敗就放過、下次編輯時 auto-save 會補 */ }
      }, 1000)
    }, 30)
    return () => clearTimeout(timer)
  }, [activeId]) // eslint-disable-line

  // 自動偵測背景執行中的 pipeline（排程觸發等），每 3 秒輪詢
  const bgDetectRef = useRef<ReturnType<typeof setInterval> | null>(null)
  useEffect(() => {
    if (bgDetectRef.current) clearInterval(bgDetectRef.current)
    if (!pipelineName) return

    // 切 workflow 時 reset 跑 / log，避免上一個 workflow 的狀態殘留蓋過去
    // (這個 reset 不影響 awaiting_human 訊息，因為下面 detect 會立即接管 active run)
    setRunId(null)
    setLogLines([])
    setRunning(false)
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }

    // initial fallback 旗標：第一輪 detect 找不到 active 時、改載 latest run 的 log
    // 切 workflow 一次只 fallback 一次，後續 detect 只負責偵測新 active(user 啟動 / TG / 排程觸發)
    let initialDone = false

    const detect = async () => {
      try {
        const runs = await getPipelineRuns()
        const active = runs.find(
          r => (r.status === 'running' || r.status === 'awaiting_human') && r.pipeline_name === pipelineName
        )
        // 偵測 active(running / awaiting)— 接管條件:有 active 且不是當前已偵測過的同一個 run
        if (active && runIdRef.current !== active.run_id) {
          setRunId(active.run_id)
          setRunning(true)
          if (active.status === 'awaiting_human') {
            setRunStatus('awaiting')
            setAwaitingRunId(active.run_id)
            const at = (active as any).awaiting_type
            const mapped = at === 'human_confirm' ? 'confirm' : at === 'missing_dependency' ? 'missing_dep' : 'failure'
            setAwaitingType(mapped)
            setAwaitingMessage((active as any).awaiting_message || '')
            setAwaitingSuggestion((active as any).awaiting_suggestion || '')
          } else {
            setRunStatus('running')
          }
          setShowLog(true)
          toast.info(`偵測到排程執行中`)
          pollStatus(active.run_id)
          pollRef.current = setInterval(() => pollStatus(active.run_id), 1500)
          initialDone = true
          return
        }
        // 沒 active：第一輪做 fallback — 找該 workflow 最新一筆 run、載入該 run 的 log
        // 讓使用者切過去就能看到上次跑的結果(不用按執行)；trace 視圖也會吃到同個 runIdRef
        if (!initialDone) {
          initialDone = true
          const latest = runs.find(r => r.pipeline_name === pipelineName)
          if (latest) {
            setRunId(latest.run_id)
            try {
              const data = await getPipelineLog(latest.run_id)
              setLogLines((data.log || '').split('\n'))
            } catch { /* ignore */ }
          }
        }
      } catch { /* ignore */ }
    }

    detect()
    bgDetectRef.current = setInterval(detect, 3000)
    return () => { if (bgDetectRef.current) clearInterval(bgDetectRef.current) }
  }, [pipelineName]) // eslint-disable-line

  // Auto-save 到 store（防抖 800ms）— 同時把 YAML 帶下去，
  // 讓 DB 的 yaml 欄位永遠跟畫布同步（TG 遠端遙控啟動會直接讀 yaml）
  const autoSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (savingRef.current || !activeId) return
    if (autoSaveTimer.current) clearTimeout(autoSaveTimer.current)
    autoSaveTimer.current = setTimeout(() => {
      const yaml = stepsToYaml(pipelineName, flowToSteps(nodes as AppNode[], edges))
      saveCanvas(activeId, nodes as AppNode[], edges, yaml)
    }, 800)
    return () => { if (autoSaveTimer.current) clearTimeout(autoSaveTimer.current) }
  }, [nodes, edges, pipelineName]) // eslint-disable-line

  // 同步名稱到 store
  useEffect(() => {
    if (savingRef.current || !activeId) return
    updateWorkflow(activeId, { name: pipelineName })
  }, [pipelineName]) // eslint-disable-line

  const selectedNode = nodes.find(n => n.id === selectedId)

  // ── 從 runStatus store 讀取 edges 動畫狀態 ─────────────────────────────────
  // 只讓「正在跑的節點」的進入線有動畫 —— 動畫跟著執行進度走,
  // 還沒跑到 / 不會跑(分支沒選到)的連線維持靜止,跟實際執行相符。
  // 執行狀態存在 runStatus store(以 step name 為 key),不在 node.data。
  const edgesAnimated = useRunStatusStore(s => s.edgesAnimated)
  const stepStatuses = useRunStatusStore(s => s.stepStatuses)
  const displayEdges = useMemo(() => {
    if (!edgesAnimated) return edges
    const runningNodeIds = new Set(
      nodes
        .filter(n => stepStatuses[(n.data as { name?: string })?.name ?? '']?.status === 'running')
        .map(n => n.id),
    )
    return edges.map(e => ({ ...e, animated: runningNodeIds.has(e.target) } as Edge))
  }, [edges, edgesAnimated, nodes, stepStatuses])

  // ── 穩定化 ReactFlow callbacks（避免每次 render 產生新函式觸發 ReactFlow 內部 setState）
  const onNodeClick = useCallback((_: React.MouseEvent, node: { id: string }) => setSelectedId(node.id), [])
  const onPaneClick = useCallback(() => setSelectedId(null), [])
  const onInit      = useCallback((inst: ReactFlowInstance<AppNode, Edge>) => {
    rfInstanceRef.current = inst
    setTimeout(() => inst.fitView({ padding: 0.3 }), 0)
  }, [])
  const miniMapNodeColor = useCallback((n: { type?: string }) => {
    if (n.type === 'humanConfirmation') return '#10b981'
    if (n.type === 'computerUse') return '#9333ea'
    return '#3b82f6'
  }, [])

  // ── Derive YAML ──────────────────────────────────────────────────────────
  const getYaml = useCallback(() => {
    const steps = flowToSteps(nodes, edges)
    return stepsToYaml(pipelineName, steps)
  }, [nodes, edges, pipelineName])

  // ── Add script step ────────────────────────────────────────────────────────
  // 改動：新增節點不再自動連到前一個節點（n8n 風格），由使用者自己拉線
  const addScriptStep = useCallback(() => {
    const count = nodes.length
    const id   = `step-${Date.now()}`
    const data  = newStepData(count)
    const lastNode = [...nodes].sort((a, b) => b.position.x - a.position.x)[0]
    const x = lastNode ? lastNode.position.x + 320 : 100
    const y = lastNode ? lastNode.position.y : 160

    const newNode: AppNode = {
      id, type: 'scriptStep',
      position: { x, y },
      data,
    }
    setNodes(ns => [...ns, newNode])
    setSelectedId(id)
  }, [nodes, setNodes])

  // ── 節點複製貼上 — Ctrl+C / Ctrl+V / hover 按鈕 / 跨 workflow ─────────────
  // clipboard 存在 localStorage(跨 workflow / 分頁 / refresh 都保留)
  const CLIPBOARD_KEY = 'pipeline_canvas_clipboard_v1'
  const copyNode = useCallback((nodeId: string) => {
    const n = nodes.find(x => x.id === nodeId)
    if (!n) return
    try {
      localStorage.setItem(CLIPBOARD_KEY, JSON.stringify({
        type: n.type,
        data: n.data,
        copiedAt: Date.now(),
      }))
      const displayName = (n.data as { name?: string })?.name || n.type
      toast.success(`📋 已複製:${displayName}`, {
        description: '在任意處按 Ctrl+V 貼上(可跨工作流)',
      })
    } catch (e) {
      toast.error(`複製失敗:${(e as Error).message}`)
    }
  }, [nodes])

  const pasteNode = useCallback(async () => {
    let raw: string | null
    try { raw = localStorage.getItem(CLIPBOARD_KEY) } catch { raw = null }
    if (!raw) {
      toast.info('剪貼簿是空的')
      return
    }
    let payload: { type: string; data: Record<string, unknown> }
    try { payload = JSON.parse(raw) } catch {
      toast.error('剪貼簿資料損毀')
      return
    }
    const newId = `${payload.type}-${Date.now()}`
    // 算位置:有 hoveredId / selectedId 就在它旁邊、不然在 viewport 中心
    const ref = nodes.find(n => n.id === (hoveredId || selectedId))
    const pos = ref
      ? { x: ref.position.x + 60, y: ref.position.y + 60 }
      : { x: 100 + nodes.length * 20, y: 200 }
    // 名稱去重:加 _copy 或 _copy2 ...
    const newData: Record<string, unknown> = { ...payload.data, index: nodes.length }
    const baseName = String((payload.data as { name?: string }).name || payload.type)
    const existingNames = new Set(
      nodes.map(n => String((n.data as { name?: string }).name || ''))
    )
    let candidate = `${baseName}_copy`
    let n = 2
    while (existingNames.has(candidate)) candidate = `${baseName}_copy${n++}`
    newData.name = candidate
    newData.status = 'idle'
    newData.errorMsg = ''
    // assets 處理(computer_use 節點)— 整份資料夾 deep copy、避免兩節點共用同一份
    const srcAssetsDir = String((payload.data as { assetsDir?: string }).assetsDir || '')
    if (payload.type === 'computerUse' && srcAssetsDir) {
      try {
        const { duplicateCanvasAssets } = await import('@/lib/api')
        // 新資料夾名稱:原本最後一段(e.g. "桌面自動化 1_assets")換掉
        const newAssetsDir = srcAssetsDir.replace(/[^/\\]+_assets$/, `${candidate}_assets`)
                                          .replace(/[^/\\]+$/, `${candidate}_assets`)
        // 若 srcAssetsDir 不是 _assets 結尾,簡單在後面接 candidate 名:fallback
        const finalDest = newAssetsDir.includes(candidate)
          ? newAssetsDir
          : `${srcAssetsDir.replace(/\/$/, '')}_${Date.now()}`
        const r = await duplicateCanvasAssets(srcAssetsDir, finalDest)
        if (r.ok) {
          newData.assetsDir = finalDest
          if (r.copied_files > 0) {
            toast.success(`📋 貼上、assets 連同 ${r.copied_files} 個檔案一起複製`)
          }
        } else {
          // 失敗就讓新節點共用舊 assets(警告)
          toast.warning(`assets 複製失敗(${r.error})、新節點暫時共用舊資料夾、跑前請手動處理`)
        }
      } catch (e) {
        console.warn('duplicate assets failed:', e)
      }
    }
    const newNode: AppNode = {
      id: newId,
      type: payload.type as AppNode['type'],
      position: pos,
      data: newData,
    } as AppNode
    setNodes(ns => [...ns, newNode])
    setSelectedId(newId)
    toast.success(`📋 已貼上:${candidate}`)
  }, [nodes, hoveredId, selectedId, setNodes])

  // ── 單節點 self-run — 雙向 DFS 收這個節點 + 沿線連到的「整個連通子圖」(前 + 後)
  // 沒拉線:只跑這個;有連線:跑全部相連的、讓使用者測試 2-N 個串接的小區塊
  const selfRunNode = useCallback(async (nodeId: string) => {
    // DFS 雙向(incoming + outgoing edges)收集所有連通節點
    const connectedIds = new Set<string>([nodeId])
    const stack = [nodeId]
    while (stack.length) {
      const cur = stack.pop()!
      for (const e of edges) {
        // 上游:source → target、cur 是 target、加 source
        if (e.target === cur && !connectedIds.has(e.source)) {
          connectedIds.add(e.source); stack.push(e.source)
        }
        // 下游:cur 是 source、加 target
        if (e.source === cur && !connectedIds.has(e.target)) {
          connectedIds.add(e.target); stack.push(e.target)
        }
      }
    }
    const subset = nodes.filter(n => connectedIds.has(n.id))
    if (subset.length === 0) {
      toast.error('找不到節點'); return
    }
    const subsetEdges = edges.filter(e => connectedIds.has(e.source) && connectedIds.has(e.target))
    let steps: ReturnType<typeof flowToSteps>
    try {
      steps = flowToSteps(subset, subsetEdges)
    } catch (e) {
      toast.error(`生 YAML 失敗:${(e as Error).message}`); return
    }
    if (steps.length === 0) {
      toast.error('subset 沒有可執行 step'); return
    }
    const yamlText = stepsToYaml(`${pipelineName}_selfrun`, steps)
    try {
      const { startPipeline } = await import('@/lib/api')
      const r = await startPipeline(yamlText, activeId ?? undefined)
      setRunId(r.run_id)
      setRunStatus('running')
      setRunning(true)
      // 啟動 polling、走跟正常 Run 同一條 path 才會看到 log + 自動轉回 idle
      pollStatus(r.run_id)
      if (pollRef.current) clearInterval(pollRef.current)
      pollRef.current = setInterval(() => pollStatus(r.run_id), 1500)
      toast.success(`▶ 單節點測試啟動(${steps.length} step、下方 Log 會顯示)`)
    } catch (e) {
      toast.error(`啟動失敗:${(e as Error).message}`)
    }
  }, [nodes, edges, pipelineName, activeId])  // eslint-disable-line react-hooks/exhaustive-deps

  // 鍵盤監聽:Ctrl+C / Ctrl+V — 排除 input / textarea / contenteditable 等輸入元件
  useEffect(() => {
    const isTypingTarget = (t: EventTarget | null): boolean => {
      if (!(t instanceof HTMLElement)) return false
      const tag = t.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
      if (t.isContentEditable) return true
      return false
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return
      if (isTypingTarget(e.target)) return
      const k = e.key.toLowerCase()
      if (k === 'c' && selectedId) {
        e.preventDefault()
        copyNode(selectedId)
      } else if (k === 'v') {
        e.preventDefault()
        pasteNode()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [selectedId, copyNode, pasteNode])

  // ── Add human confirmation node ──────────────────────────────────────────
  const addHumanConfirm = useCallback(() => {
    const id = `confirm-${Date.now()}`
    const data = newHumanConfirmData(nodes.length)
    const lastNode = [...nodes].sort((a, b) => b.position.x - a.position.x)[0]
    const x = lastNode ? lastNode.position.x + 320 : 100
    const y = lastNode ? lastNode.position.y : 160
    setNodes(ns => [...ns, { id, type: 'humanConfirmation', position: { x, y }, data }])
    setSelectedId(id)
  }, [nodes, setNodes])

  // ── Add computer_use（桌面自動化）節點 ──────────────────────────────────
  const addComputerUse = useCallback(() => {
    const id = `computer-use-${Date.now()}`
    const data = newComputerUseData(nodes.length)
    const lastNode = [...nodes].sort((a, b) => b.position.x - a.position.x)[0]
    const x = lastNode ? lastNode.position.x + 320 : 100
    const y = lastNode ? lastNode.position.y : 160
    setNodes(ns => {
      // 防呆:用當前節點清單確保名稱唯一(計數器頁面重整會歸零、避免兩節點共用同一 _assets 夾)
      data.name = dedupeComputerUseName(data.name, new Set(ns.map(n => String((n.data as { name?: string }).name || ''))))
      return [...ns, { id, type: 'computerUse', position: { x, y }, data }]
    })
    setSelectedId(id)
  }, [nodes, setNodes])

  // ── Add Condition 節點(IF / Switch 控制流)── Ticket 2 ─────────────
  const addCondition = useCallback(() => {
    const id = `condition-${Date.now()}`
    const data = newConditionData(nodes.length)
    const lastNode = [...nodes].sort((a, b) => b.position.x - a.position.x)[0]
    const x = lastNode ? lastNode.position.x + 280 : 100
    const y = lastNode ? lastNode.position.y : 160
    setNodes(ns => [...ns, { id, type: 'condition', position: { x, y }, data }])
    setSelectedId(id)
  }, [nodes, setNodes])

  // ── Edge 上的 ➕ 按鈕：在指定 edge 中間插入新節點 ──────────────────────────
  // _insertableEdge.tsx dispatch 'pipeline-insert-node-on-edge' CustomEvent
  // detail = { edgeId, source, target, nodeType, labelX, labelY }
  // 我們在這裡接：建新節點放在中點 + 把舊 edge 拆成兩段
  useEffect(() => {
    const handler = (e: Event) => {
      const ev = e as CustomEvent
      const { edgeId, source, target, nodeType, labelX, labelY } = ev.detail || {}
      if (!edgeId || !source || !target || !nodeType) return
      // 用 reactflow viewport 的 project 把螢幕座標轉到 flow 座標
      // labelX/Y 已經是 flow 座標（EdgeLabelRenderer 給的就是），直接用
      const id = `${nodeType}-${Date.now()}`
      let data: any
      switch (nodeType) {
        case 'scriptStep':         data = newStepData(0); break
        case 'humanConfirmation':  data = newHumanConfirmData(0); break
        case 'computerUse':        data = newComputerUseData(0); break
        case 'condition':          data = newConditionData(0); break
        default: return
      }
      setNodes(ns => {
        if (nodeType === 'computerUse') {
          data.name = dedupeComputerUseName(data.name, new Set(ns.map(n => String((n.data as { name?: string }).name || ''))))
        }
        return [...ns, { id, type: nodeType, position: { x: labelX - 100, y: labelY - 50 }, data }]
      })
      setEdges(es => [
        ...es.filter(x => x.id !== edgeId),
        { id: `e-${source}-${id}`, source, target: id, ...DEFAULT_EDGE_OPTIONS },
        { id: `e-${id}-${target}`, source: id, target, ...DEFAULT_EDGE_OPTIONS },
      ])
      setSelectedId(id)
    }
    window.addEventListener('pipeline-insert-node-on-edge', handler)
    return () => window.removeEventListener('pipeline-insert-node-on-edge', handler)
  }, [setNodes, setEdges])

  // ── 刪一條連線(edge 上的 🗑️ 按鈕)──────────────────────────────────────
  // 若這條線是從 condition 節點拉出去的、連帶把對應的分支設定清掉,
  // 避免畫布上線沒了、但 onTrue / cases 還殘留舊目標。
  useEffect(() => {
    const handler = (e: Event) => {
      const { edgeId, source, target } = (e as CustomEvent).detail || {}
      if (!edgeId) return
      setNodes(ns => {
        const srcNode = ns.find(n => n.id === source)
        const tgtNode = ns.find(n => n.id === target)
        if (srcNode?.type !== 'condition' || !tgtNode || !('name' in (tgtNode.data ?? {}))) return ns
        const targetName = (tgtNode.data as any).name as string
        return ns.map(n => {
          if (n.id !== source) return n
          const d = n.data as ConditionData
          let patch: Partial<ConditionData> | null = null
          if (d.mode === 'if') {
            if (d.onTrue === targetName && d.onFalse === targetName) patch = { onTrue: '', onFalse: '' }
            else if (d.onTrue === targetName) patch = { onTrue: '' }
            else if (d.onFalse === targetName) patch = { onFalse: '' }
          } else {
            const cases = { ...(d.cases || {}) }
            let changed = false
            for (const [k, v] of Object.entries(cases)) {
              if (v === targetName) { delete cases[k]; changed = true }
            }
            if (changed) patch = { cases }
          }
          return patch ? ({ ...n, data: { ...n.data, ...patch } } as AppNode) : n
        })
      })
      setEdges(es => es.filter(x => x.id !== edgeId))
    }
    window.addEventListener('pipeline-delete-edge', handler)
    return () => window.removeEventListener('pipeline-delete-edge', handler)
  }, [setNodes, setEdges])

  // ── 面板 → 畫布 同步:condition 的分支設定改了、出線跟著反映 ───────────────
  // 使用者在面板改 onTrue / onFalse / cases 後,condition 節點的出線要指到正確
  // 的目標節點(不殘留指向舊目標的線)。拖拉 / 刪線那兩條路徑會同時更新 edge 跟
  // 分支欄位、所以這裡會是 no-op;只有面板改欄位時才真的補 / 刪 edge。
  useEffect(() => {
    const conditionNodes = nodes.filter(n => n.type === 'condition')
    if (conditionNodes.length === 0) return

    // step name → node id(condition 分支存的是名稱字串)
    const nameToId = new Map<string, string>()
    for (const n of nodes) {
      const nm = (n.data as any)?.name
      if (typeof nm === 'string' && nm && !nameToId.has(nm)) nameToId.set(nm, n.id)
    }

    let nextEdges = edges
    let changed = false
    for (const cond of conditionNodes) {
      const d = cond.data as ConditionData
      // 此 condition 想要的目標 node id 集合
      const wantNames = d.mode === 'if'
        ? [d.onTrue, d.onFalse]
        : [...Object.values(d.cases || {}), d.default]
      const wantIds = new Set<string>()
      for (const nm of wantNames) {
        if (!nm) continue
        const id = nameToId.get(nm)
        if (id && id !== cond.id) wantIds.add(id)
      }
      const curOut = nextEdges.filter(e => e.source === cond.id)
      const curTargets = new Set(curOut.map(e => e.target))
      // 刪掉指向「已不在分支設定裡」的出線
      const stale = curOut.filter(e => !wantIds.has(e.target))
      if (stale.length > 0) {
        const staleIds = new Set(stale.map(e => e.id))
        nextEdges = nextEdges.filter(e => !staleIds.has(e.id))
        changed = true
      }
      // 補上「分支設定有、但畫布還沒線」的出線
      for (const tid of wantIds) {
        if (!curTargets.has(tid)) {
          nextEdges = [...nextEdges, {
            id: `e-${cond.id}-${tid}`,
            source: cond.id,
            target: tid,
            ...DEFAULT_EDGE_OPTIONS,
          }]
          changed = true
        }
      }
    }
    if (changed) setEdges(nextEdges)
  }, [nodes, edges, setEdges])

  // ── Delete step（刪除任何節點時自動重新連線前後節點）──────────────────────────
  const deleteStep = useCallback((id: string) => {
    // 若刪的是 computer_use 節點，順便把磁碟上的錨點資料夾清掉避免殘留
    const target = nodes.find(n => n.id === id)
    if (target && target.type === 'computerUse') {
      const d = target.data as ComputerUseData
      const assets = d.assetsDir ||
        `workflows/${pipelineName || 'pipeline'}/${d.name}_assets`
      // fire-and-forget：失敗也不中斷刪除流程
      deleteComputerUseAssets(assets).catch(() => {/* ignore */})
    }

    const inEdge  = edges.find(e => e.target === id)
    const outEdge = edges.find(e => e.source === id)
    setEdges(es => {
      let filtered = es.filter(e => e.source !== id && e.target !== id)
      if (inEdge && outEdge) {
        filtered = [...filtered, {
          id: `e-${inEdge.source}-${outEdge.target}`,
          source: inEdge.source,
          target: outEdge.target,
          ...DEFAULT_EDGE_OPTIONS,
        }]
      }
      return filtered
    })
    setNodes(ns => ns.filter(n => n.id !== id))
    setSelectedId(null)
  }, [nodes, edges, setNodes, setEdges, pipelineName])

  // ── Update step data (works for both scriptStep and skillStep) ─────────────
  // 步驟名稱不能有空白:`{{ steps.<名稱>.output }}` 點號語法遇空白會 Jinja 語法錯而崩。
  // 在中央更新點即時把名稱空白轉底線 → 使用者手動改名打空白也會自動變底線、無法殘留。
  const updateStep = useCallback((id: string, patch: Partial<StepData> | Partial<ConditionData>) => {
    const p = patch as { name?: unknown }
    if (typeof p.name === 'string' && /\s/.test(p.name)) {
      patch = { ...patch, name: p.name.replace(/\s+/g, '_') } as typeof patch
      // 固定 id → 連打空白時只刷新同一則、不會疊一堆
      toast.info('名稱的空白已自動改為底線（變數引用 {{ steps.名稱 }} 不允許空白）', { id: 'name-space-fix' })
    }
    setNodes(ns => ns.map(n =>
      n.id === id ? ({ ...n, data: { ...n.data, ...patch } } as AppNode) : n
    ))
  }, [setNodes])

  // ── Update AI validation node data ─────────────────────────────────────

  // ── Connect ───────────────────────────────────────────────────────────────
  // 從 condition 節點拉線 = 直接設定分支:
  //   IF 模式  → 第一條線寫進 onTrue、第二條寫進 onFalse
  //   Switch 模式 → 每條線在 cases 加一筆(佔位 key「情況N」、value = 子節點名稱)
  // 其他節點維持原本「單純連線」行為。
  const onConnect = useCallback((connection: Connection) => {
    const edge: Edge = {
      ...connection,
      id: `e-${connection.source}-${connection.target}`,
      ...DEFAULT_EDGE_OPTIONS,
    }

    const srcNode = nodes.find(n => n.id === connection.source)
    const tgtNode = nodes.find(n => n.id === connection.target)

    // 一般步驟只能接「一個」下一步 — 只有「條件判斷」節點能分多條路。
    // 其他節點若已有出線、再拉第二條 → 擋下、不建立、並說明該怎麼做。
    if (srcNode && srcNode.type !== 'condition'
        && edges.some(e => e.source === srcNode.id)) {
      toast.error('一般步驟只能接一個「下一步」。要分成多條路,請改用「條件判斷」節點。')
      return
    }

    if (srcNode?.type === 'condition' && tgtNode && 'name' in (tgtNode.data ?? {})) {
      const cond = srcNode.data as ConditionData
      const targetName = (tgtNode.data as any).name as string
      // 已連到這個子節點的出線數(算上即將加入的這條)
      const existingOut = edges.filter(e => e.source === srcNode.id && e.target !== connection.target)
      if (cond.mode === 'if') {
        // 依「目前已有幾條出線」決定這條填 onTrue 還是 onFalse
        if (existingOut.length === 0 && !cond.onTrue) {
          updateStep(srcNode.id, { onTrue: targetName } as Partial<ConditionData>)
        } else if (!cond.onFalse) {
          updateStep(srcNode.id, { onFalse: targetName } as Partial<ConditionData>)
        } else {
          // 兩條都滿了 — 覆寫 onTrue(使用者大概想重設)
          updateStep(srcNode.id, { onTrue: targetName } as Partial<ConditionData>)
        }
      } else {
        // Switch:拉線時問使用者「當值等於什麼」當作這個 case 的比對值。
        const cases = { ...(cond.cases || {}) }
        // 若這個子節點已經是某個 case 的 value,就不重複加(直接建線即可)
        if (!Object.values(cases).includes(targetName)) {
          // 還沒設「要依哪個值分流」→ 先請使用者設好,不要在這裡硬問值
          if (!cond.switch?.trim()) {
            toast.error('請先點開這個條件節點、設定「要依哪一個值來分流」,再拉分支線。')
            return
          }
          // 取出分流依據的好讀名稱(steps.X.output.Y → 「X 的 Y」)
          const sm = cond.switch.match(/\{\{\s*([\s\S]+?)\s*\}\}/)
          const inner = (sm ? sm[1] : cond.switch).trim()
          const sm2 = inner.match(/^steps\.(.+)\.output\.(.+)$/)
          const depName = sm2 ? `${sm2[1]} 的「${sm2[2]}」` : inner
          const answer = window.prompt(
            `你這個節點是依「${depName}」來分流。\n` +
            `當「${depName}」的內容等於什麼的時候,要走到「${targetName}」這條路?\n` +
            `請填那個值實際可能出現的內容(可留空、之後再到節點設定填)。`,
            '',
          )
          if (answer === null) return   // 使用者按取消 → 不建立這條連線
          let key = answer.trim()
          if (!key || cases[key] !== undefined) {
            // 留空 或 與現有 case 撞名 → 退回不重複的「情況N」佔位
            let n = Object.keys(cases).length + 1
            while (cases[`情況${n}`] !== undefined) n++
            key = `情況${n}`
          }
          cases[key] = targetName
          updateStep(srcNode.id, { cases } as Partial<ConditionData>)
        }
      }
    }

    setEdges(es => addEdge(edge, es))
  }, [setEdges, nodes, edges, updateStep])

  // ── Import from YAML ──────────────────────────────────────────────────────
  // mode: 'new' = 建立新工作流（不碰目前的）；'overwrite' = 覆蓋目前工作流
  /** 套用 YAML 到畫布。mode='new' 時回傳新建立的 workflow id、否則回傳 null。
   *  回傳 id 是給 Hero 用的:Hero 建完工作流後要把「當初為什麼這樣設計」的對話
   *  灌進那條工作流。 */
  const importYaml = useCallback(async (yaml: string, mode: 'new' | 'overwrite' = 'overwrite'): Promise<string | null> => {
    // 改走後端解析(完整 YAML 解析器):一行式與多行巢狀動作都支援。
    // 先前是前端手寫逐行 parser、只認「- {...} 一行一動作」,多行巢狀會被
    // **靜默丟掉**(實測整個 actions 消失)。後端驗證失敗會明講哪裡錯、擋在套用前。
    let v: { ok: boolean; error?: string; name?: string }
    try {
      v = await validateWorkflowYaml(yaml)
    } catch {
      toast.error('後端無法連線，無法套用 YAML')
      return null
    }
    if (!v.ok) {
      toast.error(`YAML 有誤，未套用：${v.error || '未知錯誤'}`, { duration: 10000 })
      return null
    }

    const loadFromServer = (wf: { name: string; canvas?: { nodes?: unknown[]; edges?: unknown[] } | null }) => {
      const cv = wf.canvas || { nodes: [], edges: [] }
      savingRef.current = true
      setPipelineName(wf.name)
      setNodes((cv.nodes || []) as AppNode[])
      setEdges((cv.edges || []) as never[])
      setTimeout(() => { savingRef.current = false }, 800)
    }

    if (mode === 'new') {
      // 名字衝突自動加 " 2" / " 3" …
      const existing = useWorkflowStore.getState().workflows
      let name = v.name || '新工作流'
      if (existing.some(w => w.name === name)) {
        let i = 2
        while (existing.some(w => w.name === `${name} ${i}`)) i++
        name = `${name} ${i}`
      }
      const newId = await createWorkflow(name)   // store 會把 activeId 切到新 workflow
      try {
        const wf = await applyWorkflowYaml(newId, yaml)
        // activeId useEffect 會在 30ms 後把（剛建立的空）新 workflow 載入畫布，
        // 晚於它才寫入、不然會被空畫布覆蓋
        setTimeout(() => {
          loadFromServer({ ...wf, name })
          const cv = wf.canvas || { nodes: [], edges: [] }
          saveCanvas(newId, (cv.nodes || []) as AppNode[], (cv.edges || []) as never[], yaml)
        }, 150)
      } catch (e) {
        toast.error(`套用失敗：${e instanceof Error ? e.message : String(e)}`)
        return null
      }
      toast.success(`已建立新工作流「${name}」`)
      setShowYaml(false)
      return newId
    } else {
      if (!activeId) { toast.error('沒有選取的工作流'); return null }
      try {
        const wf = await applyWorkflowYaml(activeId, yaml)
        loadFromServer(wf)
        // 同步 store + 後端 —— store 裡的舊 nodes 不更新的話,切走再切回來會變回舊內容
        const cv = wf.canvas || { nodes: [], edges: [] }
        saveCanvas(activeId, (cv.nodes || []) as AppNode[], (cv.edges || []) as never[], yaml)
        toast.success('已套用')
      } catch (e) {
        toast.error(`套用失敗：${e instanceof Error ? e.message : String(e)}`)
        return null
      }
    }
    setShowYaml(false)
    return null
  }, [setNodes, setEdges, createWorkflow, saveCanvas, activeId])

  // ── Run pipeline ──────────────────────────────────────────────────────────
  const handleRunClick = async () => {
    const stepNodes = nodes.filter(n => n.type === 'scriptStep' || n.type === 'humanConfirmation' || n.type === 'computerUse')
    if (stepNodes.length === 0) { toast.error('請先新增步驟'); return }
    const steps = flowToSteps(nodes, edges)
    // 空步驟檢查：排除有自己 schema 的節點類型（不靠 batch 跑的）
    //   condition / computer_use / human_confirm 都不需要 batch，各自有檢查
    const emptyStep = steps.find(s =>
      !s.batch?.trim() && !s.condition && !s.humanConfirm && !s.computerUse
    )
    if (emptyStep) {
      toast.error(`步驟「${emptyStep.name}」尚未設定執行指令，請點擊該步驟方塊填入`)
      return
    }
    // computer_use 節點若沒動作，明確提示
    const emptyCu = steps.find(s => s.computerUse && (!s.computerUseActions || s.computerUseActions.length === 0))
    if (emptyCu) {
      toast.error(`桌面自動化節點「${emptyCu.name}」尚未錄製動作，請開啟節點面板點「開始錄製」`)
      return
    }
    // 「節點有多個出邊」偵測：使用者插中間節點忘記刪原連線常見坑
    // flowToSteps 改 multimap + DFS 找最長路徑後不會丟掉中間節點，
    // 但仍提醒使用者去把多餘連線清掉、避免將來架構變化又踩雷
    // 注意:condition 條件節點本來就會分多條路、有多條出線是正常的、不警告
    {
      const stepNodeIds = new Set(nodes
        .filter(n => n.type === 'scriptStep' || n.type === 'humanConfirmation'
          || n.type === 'computerUse' || n.type === 'condition')
        .map(n => n.id))
      const branchNames: string[] = []
      for (const n of stepNodes) {
        if (n.type === 'condition') continue   // 條件節點多出線是預期行為
        const out = edges.filter(e => e.source === n.id && stepNodeIds.has(e.target))
        if (out.length > 1) branchNames.push((n.data as any).name || n.id)
      }
      if (branchNames.length > 0) {
        toast.error(
          `一般步驟只能接一個「下一步」,但這些步驟接了多條:${branchNames.join('、')}。` +
          `請滑到多餘的連線上點 🗑️ 刪掉;若你要分成多條路,請改用「條件判斷」節點。`,
          { duration: 9000 },
        )
        return
      }
    }
    // condition 節點未設定判斷條件偵測:有出線但 expression(IF)/ switch(Switch)是空的
    // → 明確擋下、不靜默跑通(後端 runner 也會報錯、前端提早讓使用者看到)
    {
      const unsetConditions: string[] = []
      for (const n of nodes) {
        if (n.type !== 'condition') continue
        const hasOut = edges.some(e => e.source === n.id)
        if (!hasOut) continue
        const d = n.data as ConditionData
        const isUnset = d.mode === 'if'
          ? !d.expression?.trim()
          : !d.switch?.trim()
        if (isUnset) unsetConditions.push(d.name || n.id)
      }
      if (unsetConditions.length > 0) {
        toast.error(
          `這些條件節點還沒設定判斷條件:${unsetConditions.join('、')}。` +
          `請點開節點、設定要判斷的內容後再執行。`,
          { duration: 8000 },
        )
        return
      }
    }
    setShowRunDialog(true)
  }

  const handleRunConfirm = async (inputParams: Record<string, string> = {}) => {
    setShowRunDialog(false)
    const yaml = getYaml()
    setRunning(true)
    setRunStatus('running')
    useRunStatusStore.getState().resetAll()
    try {
      const res = await startPipeline(yaml, activeId ?? undefined, inputParams)
      setRunId(res.run_id)
      toast.success(`已啟動（ID: ${res.run_id}）`)
      pollStatus(res.run_id)
      pollRef.current = setInterval(() => pollStatus(res.run_id), 1500)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '啟動失敗')
      setRunning(false)
      setRunStatus('failed')
    }
  }

  // 中止後：拉取最終 log 與節點狀態，然後延遲重啟背景偵測
  const finalizeAbort = async (rid: string) => {
    // 等一下讓後端處理完中止
    await new Promise(r => setTimeout(r, 1500))
    try {
      const [data, logRes] = await Promise.all([
        getPipelineRun(rid).catch(() => null),
        getPipelineLog(rid).catch(() => null),
      ])
      // 更新 log 面板
      if (logRes?.log) setLogLines(logRes.log.split('\n'))
      // 更新節點狀態
      if (data) {
        const statusMap: Record<string, { status: 'idle' | 'running' | 'success' | 'failed'; errorMsg: string }> = {}
        const steps = data.config_dict?.steps ?? []
        for (const step of steps) {
          const result = data.step_results?.find((s: any) => s.step_name === step.name)
          if (result) {
            statusMap[step.name] = {
              status: result.validation_status === 'failed' ? 'failed' : 'success',
              errorMsg: result.validation_reason ?? '',
            }
          } else {
            // 未完成的步驟標記為 idle（中止後不再 running）
            statusMap[step.name] = { status: 'idle', errorMsg: '' }
          }
        }
        useRunStatusStore.getState().setBulkStatus(statusMap)
      }
      useRunStatusStore.getState().setEdgesAnimated(false)
    } catch { /* ignore — UI 已設為 failed */ }
    // 延遲重啟背景偵測
    setTimeout(() => {
      if (!bgDetectRef.current && pipelineName) {
        const detect = async () => {
          if (runIdRef.current) return
          try {
            const runs = await getPipelineRuns()
            const active = runs.find(
              (r: any) => (r.status === 'running' || r.status === 'awaiting_human') && r.pipeline_name === pipelineName
            )
            if (active && !runIdRef.current) {
              setRunId(active.run_id)
              setRunning(true)
              if (active.status === 'awaiting_human') {
                setRunStatus('awaiting')
                setAwaitingRunId(active.run_id)
                const at = (active as any).awaiting_type
                const mapped = at === 'human_confirm' ? 'confirm' : at === 'missing_dependency' ? 'missing_dep' : 'failure'
                setAwaitingType(mapped)
                setAwaitingMessage((active as any).awaiting_message || '')
                setAwaitingSuggestion((active as any).awaiting_suggestion || '')
              } else {
                setRunStatus('running')
              }
              setShowLog(true)
              toast.info('偵測到排程執行中')
              pollStatus(active.run_id)
              pollRef.current = setInterval(() => pollStatus(active.run_id), 1500)
            }
          } catch { /* ignore */ }
        }
        bgDetectRef.current = setInterval(detect, 3000)
      }
    }, 3500)
  }

  const handleAbort = async () => {
    const rid = runIdRef.current
    if (!rid) return
    // 立即清除所有 UI 狀態（避免 in-flight poll 覆蓋）
    // 注意:只清 ref(停掉 active poll)、保留 latestRunId 給 trace/log 視圖繼續顯示這次的結果
    runIdRef.current = null
    if (pollRef.current) clearInterval(pollRef.current)
    if (bgDetectRef.current) { clearInterval(bgDetectRef.current); bgDetectRef.current = null }
    setRunning(false)
    setRunStatus('failed')
    setAwaitingRunId(null)
    toast.dismiss('awaiting')
    try {
      // 執行中（running）用 force abort（/abort），才能 kill 子進程
      const res = await abortPipeline(rid)
      toast.info(res.message)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '中止失敗')
    }
    finalizeAbort(rid)
  }

  const pollStatus = async (runId: string) => {
    // 若 polling 已被中止（abort/清除），直接丟棄此次回應
    if (!runIdRef.current) return
    try {
      const [data, logRes] = await Promise.all([
        getPipelineRun(runId),
        getPipelineLog(runId).catch(() => null),
      ])

      // 再次確認：收到回應後若 runId 已被清除，不處理
      if (!runIdRef.current) return

      // 每次 poll 同步更新 log
      if (logRes?.log) setLogLines(logRes.log.split('\n'))

      // 透過外部 store 更新節點狀態（避免 setNodes 觸發 ReactFlow ForwardRef 衝突）
      const currentStepName = data.config_dict?.steps?.[data.current_step]?.name
      const statusMap: Record<string, { status: 'idle' | 'running' | 'success' | 'failed'; errorMsg: string }> = {}
      const steps = data.config_dict?.steps ?? []
      for (const step of steps) {
        if ((data.status === 'running' || data.status === 'awaiting_human') && step.name === currentStepName) {
          statusMap[step.name] = { status: 'running', errorMsg: '' }
          continue
        }
        const result = data.step_results?.find(s => s.step_name === step.name)
        if (result) {
          statusMap[step.name] = {
            status: result.validation_status === 'failed' ? 'failed' : 'success',
            errorMsg: result.validation_reason ?? '',
          }
        }
      }
      useRunStatusStore.getState().setBulkStatus(statusMap)
      useRunStatusStore.getState().setEdgesAnimated(data.status === 'running')

      // 等待人工決策（繼續 polling，這樣 Telegram 確認後前端也能偵測到）
      if (data.status === 'awaiting_human') {
        const firstEntry = runStatusRef.current !== 'awaiting'
        const at = data.awaiting_type
        const mapped = at === 'human_confirm' ? 'confirm' : at === 'missing_dependency' ? 'missing_dep' : 'failure'
        if (firstEntry) {
          setRunning(false)
          setRunStatus('awaiting')
          setAwaitingRunId(runId)
        }
        // 內容(type/message/suggestion)每次輪詢都同步 — 不只首次。
        // 否則「已在 awaiting 期間 awaiting_suggestion 變了」(例:按安裝後 missing_dep
        // 仍是 awaiting、但 suggestion 換成帶 manual_hint 的新 JSON)不會反映、要重整才出現。
        // setState 傳相同值 React 會自動跳過 re-render,連續輪詢同值無成本。
        setAwaitingType(mapped)
        setAwaitingMessage(data.awaiting_message || '')
        setAwaitingSuggestion(data.awaiting_suggestion || '')
        if (firstEntry) {
          // toast 只在首次進入 awaiting 顯示、避免每次輪詢洗版
        }
        return
      }
      // 如果之前在 awaiting，現在狀態改變了（Telegram 確認了 / 前端按繼續了）→ 重新同步
      if (runStatusRef.current === 'awaiting') {
        setAwaitingRunId(null)
        setAwaitingSuggestion('')
        toast.dismiss('awaiting')
        // 如果後端已是 completed/failed/aborted，不設 idle，讓下方 done 分支處理
        if (data.status === 'running') {
          setRunStatus('running')
          setRunning(true)
          toast.success('Pipeline 已恢復執行')
        }
      }

      const done = data.status === 'completed' || data.status === 'failed' || data.status === 'aborted'
      if (done) {
        clearInterval(pollRef.current!)
        runIdRef.current = null
        setRunning(false)
        toast.dismiss('awaiting')
        const success = data.status === 'completed'
        setRunStatus(success ? 'success' : 'failed')
        setAwaitingRunId(null)
        toast[success ? 'success' : 'error'](success ? 'Pipeline 執行完成 ✓' : data.status === 'aborted' ? 'Pipeline 已中止' : 'Pipeline 執行失敗')
      }
    } catch (e) {
      // 忽略「找不到 pipeline run」的 404（背景任務可能尚未註冊），下次 poll 會自動重試
      const msg = e instanceof Error ? e.message : String(e)
      if (msg.includes('找不到')) { console.warn('[pollStatus] run 尚未註冊，等待下次 poll'); return }
      console.error('[pollStatus]', e)
      toast.error(`Poll 錯誤: ${msg}`)
    }
  }

  // 人工決策後繼續 polling

  const handleDecision = async (decision: 'retry' | 'skip' | 'abort' | 'continue' | 'install_dep' | 'redo_prev', hint?: string) => {
    if (!awaitingRunId) return
    const rid = awaitingRunId

    if (decision === 'abort') {
      // 立即清除 UI 狀態
      setRunStatus('failed')
      setRunning(false)
      setAwaitingRunId(null)
      runIdRef.current = null
      toast.dismiss('awaiting')
      if (pollRef.current) clearInterval(pollRef.current)
      if (bgDetectRef.current) { clearInterval(bgDetectRef.current); bgDetectRef.current = null }
      try {
        // 走和重試相同的 /resume 路徑（已支援 decision='abort'），避免 /abort 端點問題
        await resumePipeline(rid, 'abort')
        toast.info('Pipeline 已中止')
      } catch (e) {
        toast.error(e instanceof Error ? e.message : '中止失敗（後端狀態可能已變更）')
      }
      finalizeAbort(rid)
      return
    }

    // 缺套件「安裝並繼續」會同步阻塞整個 pip install（大依賴可能數分鐘）→ 期間顯示「安裝中」、
    // disable 按鈕,避免使用者以為當機。大型依賴後端會秒回手動指引、不會真的卡住。
    if (decision === 'install_dep') setInstalling(true)
    try {
      await resumePipeline(rid, decision, hint)
      // 安裝可能失敗並停在 missing_dependency（大型依賴 → 回手動指引）→ 讓 poll 重新同步、
      // 由後端狀態決定 modal 是消失(成功往下跑)還是更新成手動指引,不在這裡硬清。
      if (decision === 'install_dep') {
        setTimeout(() => pollStatus(rid), 300)
        return
      }
      // Guard：poll 可能在 await 期間已完成 pipeline（例如最後一步是人工確認）
      // 此時 runIdRef.current 已被 poll 的 done 分支清空，不可再覆寫狀態
      setAwaitingRunId(null)
      toast.dismiss('awaiting')
      if (runIdRef.current) {
        setRunStatus('running')
        setRunning(true)
        // 立即觸發一次 poll，捕捉「最後一步是人工確認 → 直接完成」的情境
        setTimeout(() => pollStatus(rid), 500)
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失敗')
    } finally {
      if (decision === 'install_dep') setInstalling(false)
    }
  }

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])

  // log 自動捲到底（僅在用戶未手動上捲時）
  useEffect(() => {
    if (showLog && logAutoScrollRef.current) logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logLines, showLog])

  // 開啟 log 時重置 auto-scroll
  useEffect(() => { if (showLog) logAutoScrollRef.current = true }, [showLog])

  // ── Editable pipeline name ────────────────────────────────────────────────
  const RunStatusIcon = runStatus === 'running' ? <Loader2 className="w-4 h-4 animate-spin" />
    : runStatus === 'success' ? <CheckCircle2 className="w-4 h-4 text-green-500" />
    : runStatus === 'failed'  ? <XCircle className="w-4 h-4 text-red-500" />
    : null

  return (
    <div className="h-screen flex overflow-hidden bg-gray-50" style={{ fontFamily: "'Inter', 'Noto Sans TC', sans-serif" }}>
      <Toaster richColors position="top-right" />


      {/* ── Left Sidebar ── */}
      <Sidebar onYamlApply={importYaml} />

      {/* ── Right: Toolbar + Canvas ── */}
      <div className="flex-1 flex flex-col overflow-hidden">

      {/* ── Toolbar ── */}
      <header className="h-14 bg-white border-b border-gray-200 flex items-center px-4 gap-3 shrink-0 z-20 shadow-sm">
        <div className="w-px h-6 bg-gray-200 shrink-0 hidden" />

        {/* Pipeline name */}
        {editingName ? (
          <input
            autoFocus
            value={pipelineName}
            onChange={e => setPipelineName(e.target.value)}
            onBlur={() => setEditingName(false)}
            onKeyDown={e => e.key === 'Enter' && setEditingName(false)}
            className="text-sm font-medium border-b-2 border-indigo-400 outline-none bg-transparent text-gray-800 min-w-0 flex-1 max-w-[500px]"
          />
        ) : (
          <button onClick={() => setEditingName(true)}
            title={pipelineName}
            className="text-sm font-medium text-gray-800 hover:text-indigo-600 transition-colors whitespace-nowrap shrink-0">
            {pipelineName}
          </button>
        )}

        {RunStatusIcon && <span>{RunStatusIcon}</span>}
        <div className="flex-1" />

        {/* 輸出資料夾 — 在本機檔案總管開啟此工作流的輸出資料夾 */}
        <button
          onClick={async () => {
            try {
              const r = await openOutputFolder(pipelineName)
              if (!r.existed) alert('此工作流尚無輸出,已開啟 data/workflows 根目錄。')
            } catch (e) {
              alert('開啟輸出資料夾失敗:' + (e as Error).message)
            }
          }}
          title="在檔案總管開啟此工作流的輸出資料夾,方便查看產出結果"
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm border border-gray-200 text-gray-600 hover:border-indigo-300 hover:text-indigo-600 transition-colors"
        >
          📂 輸出資料夾
        </button>


        {/* YAML */}
        <button
          onClick={() => setShowYaml(!showYaml)}
          className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm border transition-colors
            ${showYaml ? 'bg-indigo-50 border-indigo-300 text-indigo-700' : 'border-gray-200 text-gray-600 hover:border-indigo-300 hover:text-indigo-600'}`}
        >
          <Code2 className="w-3.5 h-3.5" /> YAML
        </button>

        {/* Log */}
        <button
          onClick={() => setShowLog(!showLog)}
          className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm border transition-colors
            ${showLog ? 'bg-gray-900 border-gray-700 text-gray-100' : 'border-gray-200 text-gray-600 hover:border-gray-400 hover:text-gray-800'}`}
        >
          <Terminal className="w-3.5 h-3.5" /> Log
          {running && <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />}
        </button>

        {/* Schedule */}
        <button
          onClick={() => setShowSchedule(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm border border-gray-200 text-gray-600 hover:border-indigo-300 hover:text-indigo-600 transition-colors"
        >
          <Clock className="w-3.5 h-3.5" /> 排程
        </button>

        {/* 觸發器(Webhook / 檔案夾監看)— 要先存成工作流才有 id 可綁 */}
        <button
          onClick={() => {
            if (!activeId) { toast.error('請先儲存工作流(觸發器綁定工作流 ID)'); return }
            setShowTriggers(true)
          }}
          title="事件觸發:外部 POST 網址觸發(Webhook)/ 資料夾新檔觸發(監看)"
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm border border-gray-200 text-gray-600 hover:border-teal-400 hover:text-teal-700 transition-colors"
        >
          <Zap className="w-3.5 h-3.5" /> 觸發器
        </button>

        {/* Run / Stop */}
        {running ? (
          <button
            onClick={handleAbort}
            className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-sm bg-red-600 text-white hover:bg-red-700 transition-colors font-medium shadow-sm"
          >
            <Square className="w-3.5 h-3.5" /> 停止
          </button>
        ) : (
          <button
            onClick={handleRunClick}
            disabled={nodes.filter(n => n.type === 'scriptStep' || n.type === 'humanConfirmation' || n.type === 'computerUse').length === 0}
            className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-sm bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 transition-colors font-medium shadow-sm"
          >
            <Play className="w-3.5 h-3.5" /> 執行
          </button>
        )}
      </header>

      {/* ── Canvas area ── */}
      <div className="flex-1 relative overflow-hidden">
        <ReactFlow
          nodes={nodes}
          edges={displayEdges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeClick={onNodeClick}
          onPaneClick={onPaneClick}
          onNodeMouseEnter={(_e, n) => { cancelHoverClear(); setHoveredId(n.id) }}
          onNodeMouseLeave={clearHoverDelayed}
          onInit={onInit}
          minZoom={0.2}
          maxZoom={2}
          deleteKeyCode={['Delete', 'Backspace']}
          defaultEdgeOptions={DEFAULT_EDGE_OPTIONS}
        >
          {/* hover 浮動工具列 — 跟著滑鼠所在節點顯示;hover 在 toolbar 上會 cancel 延遲清除、不會消失 */}
          {hoveredId && (
            <NodeToolbar nodeId={hoveredId} isVisible position={Position.Top} offset={0}>
              <div
                onMouseEnter={cancelHoverClear}
                onMouseLeave={clearHoverDelayed}
                className="inline-flex items-center gap-0.5 px-1 py-1 rounded-lg bg-white border border-gray-200 shadow-md"
              >
                <button
                  onClick={() => copyNode(hoveredId)}
                  title="複製此節點(Ctrl+C)"
                  className="px-2 py-1 rounded text-indigo-600 hover:bg-indigo-50 transition-colors text-sm"
                >
                  📋
                </button>
                <button
                  onClick={() => selfRunNode(hoveredId)}
                  disabled={running}
                  title="單節點測試:只跑這個節點 + 沿線往前的所有上游"
                  className="px-2 py-1 rounded text-emerald-600 hover:bg-emerald-50 transition-colors text-sm disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  ▶
                </button>
                <button
                  onClick={() => deleteStep(hoveredId)}
                  title="刪除此節點"
                  className="px-2 py-1 rounded text-red-500 hover:bg-red-50 transition-colors text-sm"
                >
                  🗑
                </button>
              </div>
            </NodeToolbar>
          )}
          {/* Dotted grid background */}
          <Background
            variant={BackgroundVariant.Dots}
            gap={20}
            size={1.5}
            color="#d1d5db"
          />
          <Controls position="bottom-left" showInteractive={false} />
          <MiniMap
            position="bottom-right"
            nodeColor={miniMapNodeColor}
            style={{ background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 8 }}
          />

          {/* Add node buttons (top-left of canvas) */}
          {/* HoverScrollRow:小螢幕時按鈕列超過畫面寬度,滑鼠停在左右邊緣會自動橫向捲動,不壓縮按鈕寬度 */}
          <Panel position="top-left">
            <HoverScrollRow>
              <button
                onClick={addScriptStep}
                title="新增一個執行 Python 腳本/指令的步驟"
                className="flex items-center gap-1.5 px-3 py-2 bg-white border border-blue-200 rounded-xl text-sm text-blue-600 hover:border-blue-400 hover:bg-blue-50 shadow-sm transition-colors"
              >
                <Plus className="w-3.5 h-3.5" /> <Code2 className="w-3.5 h-3.5" /> Python腳本
              </button>
              <button
                onClick={addHumanConfirm}
                title="新增人工確認節點（暫停等待確認後繼續）"
                className="flex items-center gap-1.5 px-3 py-2 bg-white border border-emerald-200 rounded-xl text-sm text-emerald-600 hover:border-emerald-400 hover:bg-emerald-50 shadow-sm transition-colors"
              >
                <Plus className="w-3.5 h-3.5" /> <UserCheck className="w-3.5 h-3.5" /> 人工確認
              </button>
              <button
                onClick={addComputerUse}
                title="新增桌面自動化節點（錄製滑鼠鍵盤操作後重播）"
                className="flex items-center gap-1.5 px-3 py-2 bg-white border border-fuchsia-200 rounded-xl text-sm text-fuchsia-700 hover:border-fuchsia-400 hover:bg-fuchsia-50 shadow-sm transition-colors"
              >
                <Plus className="w-3.5 h-3.5" /> <MousePointer2 className="w-3.5 h-3.5" /> 桌面自動化
              </button>
              <button
                onClick={addCondition}
                title="新增 Condition 控制流節點(IF / Switch — 求值表達式後跳到指定 step)"
                className="flex items-center gap-1.5 px-3 py-2 bg-white border border-orange-200 rounded-xl text-sm text-orange-700 hover:border-orange-400 hover:bg-orange-50 shadow-sm transition-colors"
              >
                <Plus className="w-3.5 h-3.5" /> 🔀 條件分支
              </button>
            </HoverScrollRow>
          </Panel>
        </ReactFlow>


        {/* Awaiting human decision banner */}
        {runStatus === 'awaiting' && awaitingRunId && awaitingType === 'failure' && (
          <div className="absolute top-4 left-1/2 -translate-x-1/2 z-40 bg-amber-50 border border-amber-200 rounded-2xl shadow-lg px-5 py-3 space-y-2 max-w-[600px] w-[95%]">
            {/* 標題列 + 操作按鈕 */}
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-amber-600 font-medium text-sm whitespace-nowrap">⚠️ 步驟失敗,請選擇處理方式</span>
              <div className="flex items-center gap-1.5 ml-auto flex-wrap">
                <button onClick={() => handleDecision('retry')} className="px-3 py-1.5 bg-blue-600 text-white rounded-lg text-xs font-medium hover:bg-blue-700 whitespace-nowrap">🔄 重試</button>
                <button
                  onClick={() => handleDecision('skip')}
                  title="跳過此步、直接跑下一步(原失敗 step 不再執行)"
                  className="px-3 py-1.5 bg-amber-500 text-white rounded-lg text-xs font-medium hover:bg-amber-600 whitespace-nowrap"
                >⏩ 跳過</button>
                <button
                  onClick={() => handleDecision('redo_prev')}
                  title="認為失敗是因為上一步沒做好;清掉上一步 + 當前步結果、從上一步重跑"
                  className="px-3 py-1.5 bg-teal-600 text-white rounded-lg text-xs font-medium hover:bg-teal-700 whitespace-nowrap"
                >↩ 重做上一步</button>
                <button onClick={() => handleDecision('abort')} className="px-3 py-1.5 bg-red-600 text-white rounded-lg text-xs font-medium hover:bg-red-700 whitespace-nowrap">🛑 中止</button>
              </div>
            </div>
            {/* 失敗原因 */}
            {awaitingMessage && (
              <div className="bg-amber-100 border border-amber-200 rounded-lg px-3 py-2">
                <p className="text-xs font-semibold text-amber-700 mb-0.5">失敗原因</p>
                <p className="text-xs text-amber-800 leading-relaxed">{awaitingMessage}</p>
              </div>
            )}
            {/* AI 解決建議 */}
            {awaitingSuggestion && (
              <div className="bg-blue-50 border border-blue-200 rounded-lg px-3 py-2">
                <p className="text-xs font-semibold text-blue-700 mb-0.5">💡 AI 建議</p>
                <p className="text-xs text-blue-800 leading-relaxed">{awaitingSuggestion}</p>
              </div>
            )}
          </div>
        )}
        {/* Human confirmation banner */}
        {runStatus === 'awaiting' && awaitingRunId && awaitingType === 'confirm' && (
          <div className="absolute top-4 left-1/2 -translate-x-1/2 z-40 bg-emerald-50 border border-emerald-200 rounded-2xl shadow-lg px-5 py-3 space-y-2 max-w-[560px]">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-emerald-700 font-medium text-sm whitespace-nowrap">✋ 等待人工確認</span>
              <span className="text-emerald-600 text-xs max-w-[200px] truncate">{awaitingMessage}</span>
              <div className="flex items-center gap-2 ml-auto">
                <button onClick={() => handleDecision('continue')} className="px-3 py-1.5 bg-emerald-600 text-white rounded-lg text-xs font-medium hover:bg-emerald-700 whitespace-nowrap">✅ 繼續</button>
                <button onClick={() => handleDecision('abort')} className="px-3 py-1.5 bg-red-600 text-white rounded-lg text-xs font-medium hover:bg-red-700 whitespace-nowrap">🛑 中止</button>
              </div>
            </div>
          </div>
        )}


        {/* missing_dependency banner — skill 跑到一半發現缺套件 */}
        {runStatus === 'awaiting' && awaitingRunId && awaitingType === 'missing_dep' && (() => {
          let meta: { packages?: string[]; stderr_tail?: string; manual_hint?: string; is_large?: boolean; install_failed?: boolean } = {}
          try { meta = awaitingSuggestion ? JSON.parse(awaitingSuggestion) : {} } catch { /* ignore */ }
          const pkgs = meta.packages || []
          const manualHint = meta.manual_hint || ''
          // 大型/裝太久(is_large=big_or_slow)→ 走「終端機手動安裝」模式(改按鈕文案、隱藏去設定頁)。
          // 一般失敗保留 app 內重試/去設定頁,但下方仍會顯示 manual_hint 指令框當額外幫助。
          const manualMode = !!meta.is_large
          const installLabel = installing ? '⏳ 安裝中…' : (manualMode ? '✅ 我已裝好，繼續' : '✅ 安裝並繼續')
          return (
            <div className="absolute top-4 left-1/2 -translate-x-1/2 z-40 bg-blue-50 border border-blue-200 rounded-2xl shadow-lg px-5 py-3 space-y-2 max-w-[640px] w-[95%]">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-blue-700 font-medium text-sm whitespace-nowrap">{manualMode ? '📦 需在終端機安裝大型套件' : '📦 需要安裝套件'}</span>
                {awaitingMessage && <span className="text-blue-600 text-xs max-w-[260px] truncate">{awaitingMessage}</span>}
                <div className="flex items-center gap-2 ml-auto">
                  <button
                    onClick={() => handleDecision('install_dep', pkgs.join(','))}
                    disabled={pkgs.length === 0 || installing}
                    title={manualMode ? '在終端機裝好後按這裡，系統會偵測到已安裝並往下跑' : '在 app 內安裝後自動繼續'}
                    className="px-3 py-1.5 bg-blue-600 text-white rounded-lg text-xs font-medium hover:bg-blue-700 disabled:opacity-50 whitespace-nowrap flex items-center gap-1"
                  >{installing && <span className="inline-block w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin" />}{installLabel}</button>
                  {!manualMode && (
                    <a
                      href="/settings"
                      target="_blank"
                      rel="noopener noreferrer"
                      title="去設定頁手動安裝"
                      className="px-3 py-1.5 bg-white border border-blue-200 text-blue-700 rounded-lg text-xs font-medium hover:bg-blue-100 whitespace-nowrap"
                    >⚙️ 去設定頁</a>
                  )}
                  <button onClick={() => handleDecision('abort')} disabled={installing} className="px-3 py-1.5 bg-red-600 text-white rounded-lg text-xs font-medium hover:bg-red-700 disabled:opacity-50 whitespace-nowrap">🛑 中止</button>
                </div>
              </div>
              {pkgs.length > 0 && (
                <div className="bg-blue-100 border border-blue-200 rounded-lg px-3 py-2">
                  <p className="text-xs font-semibold text-blue-700 mb-1">缺少：</p>
                  <div className="flex flex-wrap gap-1.5">
                    {pkgs.map(p => (
                      <code key={p} className="text-xs bg-white border border-blue-200 text-blue-800 rounded px-1.5 py-0.5 font-mono">{p}</code>
                    ))}
                  </div>
                </div>
              )}
              {/* 大型依賴 / 安裝失敗 → 顯示終端機手動安裝指令(可選取複製) */}
              {manualHint && (
                <div className="bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                  <p className="text-xs font-semibold text-amber-800 mb-1">⚠️ 請到終端機手動安裝</p>
                  <pre className="bg-white/80 rounded p-2 overflow-auto max-h-48 text-[11px] text-gray-800 whitespace-pre-wrap select-text font-mono leading-relaxed">{manualHint}</pre>
                </div>
              )}
              {meta.stderr_tail && (
                <details className="text-xs text-blue-700/80">
                  <summary className="cursor-pointer hover:text-blue-800">stderr 片段</summary>
                  <pre className="mt-1 bg-white/60 rounded p-2 overflow-auto max-h-32 text-[11px] text-gray-700 whitespace-pre-wrap">{meta.stderr_tail}</pre>
                </details>
              )}
            </div>
          )
        })()}


        {/* Empty state */}
        {nodes.filter(n => n.type === 'scriptStep' || n.type === 'humanConfirmation' || n.type === 'computerUse').length === 0 && <EmptyState onAdd={addScriptStep} />}

        {/* Node config panel */}
        {selectedNode && selectedNode.type === 'computerUse' ? (
          <ComputerUsePanel
            node={selectedNode as ComputerUseNode}
            pipelineName={pipelineName}
            onUpdate={patch => updateStep(selectedNode.id, patch as Partial<StepData>)}
            onClose={() => setSelectedId(null)}
            onDelete={() => deleteStep(selectedNode.id)}
            workflowId={activeId ?? undefined}
          />
        ) : selectedNode && selectedNode.type === 'humanConfirmation' ? (
          <HumanConfirmPanel
            node={selectedNode as HumanConfirmNode}
            onUpdate={patch => updateStep(selectedNode.id, patch as Partial<StepData>)}
            onClose={() => setSelectedId(null)}
            onDelete={() => deleteStep(selectedNode.id)}
            workflowId={activeId ?? undefined}
          />
        ) : selectedNode && selectedNode.type === 'condition' ? (
          <ConditionPanel
            node={selectedNode as ConditionNode}
            onUpdate={patch => updateStep(selectedNode.id, patch as Partial<StepData>)}
            onClose={() => setSelectedId(null)}
            onDelete={() => deleteStep(selectedNode.id)}
            workflowId={activeId ?? undefined}
            availableStepNames={nodes
              .filter(n => 'name' in (n.data ?? {}) && (n.data as any).name)
              .map(n => (n.data as any).name as string)}
          />
        ) : selectedNode && selectedNode.type === 'scriptStep' ? (
          <ScriptConfigPanel
            node={selectedNode as ScriptNode}
            onUpdate={patch => updateStep(selectedNode.id, patch)}
            onClose={() => setSelectedId(null)}
            onDelete={() => deleteStep(selectedNode.id)}
            workflowId={activeId ?? undefined}
            aiExpectText={
              (() => {
                const outEdge = edges.find(e => e.source === selectedNode.id)
                if (!outEdge) return undefined
                const nextNode = nodes.find(n => n.id === outEdge.target)
                return nextNode?.type === 'aiValidation'
                  ? undefined
                  : undefined
              })()
            }
          />
        ) : null}

        {/* YAML panel */}
        {showYaml && (
          <YamlPanel
            yaml={getYaml()}
            onImport={importYaml}
            onClose={() => setShowYaml(false)}
          />
        )}

        {/* Dry-run 預覽 modal */}

        {/* Terminal log panel */}
        {showLog && (
          <div
            className="absolute bottom-0 left-0 right-0 bg-gray-950 border-t border-gray-700 flex flex-col z-30"
            style={{ height: logHeight, userSelect: logResizing ? 'none' : undefined }}
          >
            {/* Resize handle（上邊緣） */}
            <div
              onMouseDown={(e) => { e.preventDefault(); setLogResizing(true) }}
              onDoubleClick={() => setLogHeight(LOG_DEFAULT_HEIGHT)}
              title="拖曳調整高度・雙擊還原"
              className={`absolute top-0 left-0 right-0 h-1 cursor-row-resize z-10 transition-colors ${
                logResizing ? 'bg-indigo-500' : 'hover:bg-indigo-400'
              }`}
            />
            <div className="flex items-center gap-2 px-4 py-2 border-b border-gray-800 shrink-0">
              <Terminal className="w-3.5 h-3.5 text-gray-400" />
              <span className="text-xs text-gray-400 font-mono">執行 Log</span>
              {running && <span className="text-xs text-green-400 animate-pulse">● 執行中</span>}
              {!running && latestRunId && <span className="text-xs text-gray-500">Run: {latestRunId}</span>}
              <div className="flex-1" />
              <button
                onClick={async () => {
                  if (!logLines.length) { toast.info('目前沒有 log 可複製'); return }
                  try { await navigator.clipboard.writeText(logLines.join('\n')); toast.success(`已複製 ${logLines.length} 行 log`) }
                  catch { toast.error('複製失敗，請手動選取') }
                }}
                className="text-xs text-gray-500 hover:text-gray-300 px-2"
                title="複製完整 log 內容到剪貼簿"
              >複製</button>
              <button onClick={() => setLogLines([])} className="text-xs text-gray-500 hover:text-gray-300 px-2">清除</button>
              <button onClick={() => setShowLog(false)} className="text-gray-500 hover:text-gray-300">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
            <div ref={logContainerRef} className="flex-1 overflow-y-auto p-3 font-mono text-xs leading-5"
              onScroll={() => {
                const el = logContainerRef.current
                if (!el) return
                logAutoScrollRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 30
              }}>
              {logLines.length === 0 && (
                <span className="text-gray-600">尚無 log — 請先執行 Pipeline</span>
              )}
              {logLines.map((line, i) => (
                <div key={i} className={
                  /\[ERROR\s*\]|Traceback|exit code: [1-9]/i.test(line) ? 'text-red-400' :
                  /\[WARN/i.test(line) ? 'text-yellow-400' :
                  /success|完成|✓/i.test(line) ? 'text-green-400' :
                  'text-gray-300'
                }>{line || '\u00a0'}</div>
              ))}
              <div ref={logEndRef} />
            </div>
          </div>
        )}
      </div>

      {/* Schedule dialog */}
      {showSchedule && (
        <ScheduleDialog yaml={getYaml()} pipelineName={pipelineName} workflowId={activeId ?? null} onClose={() => setShowSchedule(false)} />
      )}

      {/* Run Diff */}

      {/* 觸發器面板(Webhook / 檔案夾監看) */}
      {showTriggers && activeId && (
        <TriggerPanel
          workflowId={activeId}
          workflowName={pipelineName}
          onClose={() => setShowTriggers(false)}
        />
      )}

      {/* Run dialog */}
      {showRunDialog && (
        <RunDialog
          workflowId={activeId ?? undefined}
          onRun={handleRunConfirm}
          onClose={() => setShowRunDialog(false)}
        />
      )}

      </div>{/* end right column */}

    </div>
  )
}
