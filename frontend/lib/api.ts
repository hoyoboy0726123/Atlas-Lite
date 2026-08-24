import type { ScheduledTask, PipelineRun } from './types'

const BASE = '/api/backend'

/**
 * 長時間請求（如 LLM chat）專用的後端 URL：繞過 Next.js dev 的 rewrite proxy
 * 原因：Next.js rewrite 走 http-proxy，預設 socket timeout ~30s，超時就回 500
 * 只在瀏覽器端且後端在 localhost 時啟用；後端已配置 CORS 允許前端 origin
 */
const DIRECT_BASE = (() => {
  if (typeof window === 'undefined') return BASE
  const { hostname } = window.location
  // 後端埠:由 build-time 注入的 NEXT_PUBLIC_BACKEND_PORT 決定(Atlas 預設 8014、與 V5 8004 並存)
  const port = process.env.NEXT_PUBLIC_BACKEND_PORT || '8014'
  if (hostname === 'localhost' || hostname === '127.0.0.1') return `http://localhost:${port}`
  return BASE
})()

/**
 * fetch wrapper：對 5xx / network 錯誤做指數退避重試
 * 延遲序列 400ms → 1200ms → 2500ms（總等候 ~4s），覆蓋典型 uvicorn 熱重載空窗
 * 原因：Next.js dev proxy 在後端 .py 編輯觸發 uvicorn reload 的 2-5 秒內會回 500/502
 * 只對 idempotent 操作使用；4xx 不重試（客戶端錯誤）
 */
export async function fetchWithRetry(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const delays = [400, 1200, 2500]
  const tryOnce = async () => {
    try { return await fetch(input, init) } catch { return null }
  }
  let res = await tryOnce()
  for (const delay of delays) {
    if (res && res.ok) return res
    const status = res?.status ?? 0
    const shouldRetry = !res || (status >= 500 && status < 600)
    if (!shouldRetry) return res!
    await new Promise(r => setTimeout(r, delay))
    res = await tryOnce()
  }
  if (res) return res
  throw new Error('後端連線失敗（請確認 uvicorn 是否在運行）')
}

export async function fsBrowse(path = ''): Promise<{
  path: string
  parent: string | null
  items: { name: string; path: string; is_dir: boolean; ext: string }[]
}> {
  const res = await fetch(`${BASE}/fs/browse?path=${encodeURIComponent(path)}`)
  return res.json()
}

export async function fsCheckVenv(dir: string): Promise<{ has_venv: boolean; python_path: string | null; venv_dir_name: string | null }> {
  const res = await fetch(`${BASE}/fs/check-venv?dir=${encodeURIComponent(dir)}`)
  if (!res.ok) throw new Error('檢查失敗')
  return res.json()
}

// 在本機檔案總管開啟某工作流的輸出資料夾(OUTPUT_BASE_PATH/<工作流名稱>/)。
// 後端與使用者同機才有意義;找不到該資料夾 → 後端退回開 data/workflows 根並回 existed=false。
export async function openOutputFolder(name: string): Promise<{ opened: string; existed: boolean }> {
  const res = await fetch(`${BASE}/fs/open-output?name=${encodeURIComponent(name)}`)
  if (!res.ok) {
    const e = await res.json().catch(() => ({} as { detail?: string }))
    throw new Error(e.detail || '開啟輸出資料夾失敗')
  }
  return res.json()
}

// 開 OS 原生檔案對話框(本機部署、後端與使用者同一台)。mode: open/save/dir。
// 回 { path }(取消或無法開啟 → path=null,呼叫端可 fallback 到內建瀏覽 modal)。
export async function fsNativePick(opts: {
  mode?: 'open' | 'save' | 'dir'
  initial_dir?: string
  default_name?: string
  py_only?: boolean
} = {}): Promise<{ path: string | null; error?: string }> {
  const res = await fetch(`${BASE}/fs/native-pick`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(opts),
  })
  if (!res.ok) return { path: null, error: `HTTP ${res.status}` }
  return res.json()
}
async function _readErrorDetail(res: Response): Promise<string> {
  try {
    const data = await res.json()
    return data?.detail || `HTTP ${res.status}`
  } catch {
    return `HTTP ${res.status}`
  }
}
export async function getHealth(): Promise<{ status: string; warnings: string[] }> {
  const res = await fetch(`${BASE}/health`)
  return res.json()
}
export async function getPipelineRuns(): Promise<PipelineRun[]> {
  const res = await fetchWithRetry(`${BASE}/pipeline/runs`)
  if (!res.ok) return []
  const data = await res.json()
  return data.runs ?? []
}

export async function getPipelineRun(runId: string): Promise<PipelineRun> {
  const res = await fetchWithRetry(`${BASE}/pipeline/runs/${runId}`)
  if (!res.ok) throw new Error('找不到 pipeline run')
  return res.json()
}

export async function startPipeline(
  yamlContent: string,
  workflowId?: string,
  inputParams?: Record<string, string>,
): Promise<{ run_id: string }> {
  const res = await fetch(`${BASE}/pipeline/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      yaml_content: yamlContent,
      workflow_id: workflowId ?? null,
      input_params: inputParams || {},
    }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail ?? '啟動失敗')
  }
  return res.json()
}

export async function deletePipelineRun(runId: string): Promise<void> {
  const res = await fetch(`${BASE}/pipeline/runs/${runId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('刪除失敗')
}

export async function resumePipeline(
  runId: string,
  decision: 'retry' | 'skip' | 'abort' | 'continue' | 'install_dep' | 'redo_prev',
  hint?: string,
): Promise<{ message: string }> {
  const body: Record<string, string> = { decision }
  if (hint) body.hint = hint
  const res = await fetch(`${BASE}/pipeline/runs/${runId}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error('Resume 失敗')
  return res.json()
}

export async function abortPipeline(runId: string): Promise<{ message: string }> {
  const res = await fetch(`${BASE}/pipeline/runs/${runId}/abort`, {
    method: 'POST',
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail ?? '中止失敗')
  }
  return res.json()
}
export async function getPipelineLog(runId: string): Promise<{ log: string }> {
  const res = await fetch(`${BASE}/pipeline/runs/${runId}/log`)
  if (!res.ok) throw new Error('取得 log 失敗')
  return res.json()
}

export async function getPipelineScheduled(): Promise<ScheduledTask[]> {
  const res = await fetchWithRetry(`${BASE}/pipeline/scheduled`)
  if (!res.ok) return []
  const data = await res.json()
  return data.tasks ?? []
}

export async function cancelPipelineSchedule(name: string): Promise<void> {
  const res = await fetch(`${BASE}/pipeline/scheduled/cancel-by-name/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error('取消排程失敗')
}

export async function createPipelineSchedule(req: {
  name: string
  yaml_content: string
  schedule_type: string
  schedule_expr: string
  workflow_id?: string
}): Promise<ScheduledTask> {
  const res = await fetch(`${BASE}/pipeline/scheduled`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.detail ?? '建立排程失敗')
  }
  const data = await res.json()
  return data.task
}

export async function deletePipelineSchedule(taskId: string): Promise<void> {
  const res = await fetch(`${BASE}/pipeline/scheduled/${taskId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('刪除排程失敗')
}
export interface EnvPaths {
  data_dir: string
  workflow_dir: string
  log_dir: string
  external_projects_dir: string
  timezone: string
}

export async function getEnvPaths(): Promise<EnvPaths> {
  const res = await fetchWithRetry(`${BASE}/env/paths`)
  if (!res.ok) throw new Error('讀取專案路徑失敗')
  return res.json()
}

// ── Computer Use 錄製 API ──────────────────────────────────────
export interface RecordingStatus {
  recording: boolean
  session_id?: string
  output_dir?: string
  action_count?: number
  duration_sec?: number
  stopped?: boolean
  latest_actions?: any[]
}

export async function startComputerUseRecording(sessionId: string, outputDir: string): Promise<RecordingStatus> {
  const res = await fetch(`${BASE}/computer-use/recording/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, output_dir: outputDir }),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`開始錄製失敗：${detail || res.status}`)
  }
  return res.json()
}

export async function stopComputerUseRecording(): Promise<RecordingStatus> {
  const res = await fetch(`${BASE}/computer-use/recording/stop`, { method: 'POST' })
  if (!res.ok) throw new Error('停止錄製失敗')
  return res.json()
}

export async function armComputerUseRecordingHotkey(sessionId: string, outputDir: string): Promise<{ armed: boolean; key: string }> {
  const res = await fetch(`${BASE}/computer-use/recording/arm-hotkey`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, output_dir: outputDir }),
  })
  if (!res.ok) throw new Error('arm 熱鍵失敗')
  return res.json()
}

export async function disarmComputerUseRecordingHotkey(): Promise<{ armed: boolean }> {
  const res = await fetch(`${BASE}/computer-use/recording/disarm-hotkey`, { method: 'POST' })
  if (!res.ok) return { armed: false }
  return res.json()
}

export async function duplicateCanvasAssets(src: string, dest: string): Promise<{ ok: boolean; copied_files: number; error?: string }> {
  const res = await fetch(`${BASE}/canvas/duplicate-assets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ src, dest }),
  })
  if (!res.ok) return { ok: false, copied_files: 0, error: `HTTP ${res.status}` }
  return res.json()
}

export async function getComputerUseRecordingStatus(): Promise<RecordingStatus> {
  const res = await fetchWithRetry(`${BASE}/computer-use/recording/status`)
  if (!res.ok) throw new Error('查詢錄製狀態失敗')
  return res.json()
}

export async function loadComputerUseRecording(outputDir: string): Promise<{ actions: any[]; meta: any; output_dir: string }> {
  const res = await fetchWithRetry(`${BASE}/computer-use/recording/load?output_dir=${encodeURIComponent(outputDir)}`)
  if (!res.ok) throw new Error('載入錄製結果失敗')
  return res.json()
}

export interface GroundingStatus {
  available: boolean
  sandbox_ok: boolean
  model_present: boolean
  gpu_ok: boolean
  vram_gb: number
  precision: string
  reason: string
  install_hint: string
  note: string
}

/** 這台機器能不能用 vlm_mode='grounding'（後端有 60s 快取）。 */
export async function getGroundingStatus(): Promise<GroundingStatus> {
  const res = await fetchWithRetry(`${BASE}/computer-use/grounding/status`)
  if (!res.ok) throw new Error('查詢 GUI 定位狀態失敗')
  return res.json()
}
export interface AnchorAnalysis {
  index: number
  checked: boolean
  /** 執行時「真的搆得到」的相似處數量（不是整張畫面的總數） */
  rivals: number
  nearest_rival_px: number
  /** 整張錄製畫面上掃到的相似處總數（含執行時搆不到的） */
  scanned?: number
  /** 各階段各有幾個搆得到，決定要給什麼建議 */
  phases?: { box: number; near: number; fullscreen: number }
  /** 錨點幾乎沒有特徵（純色）→ CV 與幻覺守門都對它無效，比有替身更嚴重 */
  flat?: boolean
  variance?: number
  /** 真目標的比對分數，以及「搶得走的」替身裡最高的那個 */
  target_score?: number
  best_rival_score?: number
  reason: string
}

/** 算每張錨點在錄製當下的畫面上「還能對到幾個地方」。>0 代表回放時 CV 可能挑錯。 */
export async function analyzeAnchors(
  assetsDir: string,
  actions: Record<string, unknown>[],
  cv?: { cv_search_radius?: number; cv_threshold?: number; cv_search_only_near?: boolean },
): Promise<{ results: AnchorAnalysis[] }> {
  // 一定要把步驟層級的 CV 設定帶過去 —— 分析要用「執行時真的會用的那組值」，
  // 否則會報一堆執行時根本碰不到的假警報。
  const res = await fetch(`${BASE}/computer-use/assets/analyze-anchors`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assets_dir: assetsDir, actions, ...(cv || {}) }),
  })
  if (!res.ok) throw new Error(await _readErrorDetail(res))
  return res.json()
}

/** 把描述餵給地端定位模型，看它會不會點回錄製時的位置。 */
export async function verifyGroundingDesc(
  assetsDir: string,
  action: Record<string, unknown>,
  description: string,
): Promise<{ verified: boolean; verify_px: number | null; verify_msg: string }> {
  const res = await fetch(`${BASE}/computer-use/grounding/verify`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assets_dir: assetsDir, action, description }),
  })
  if (!res.ok) throw new Error(await _readErrorDetail(res))
  return res.json()
}

// ── 視覺模型（vlm_mode='description' 用，選用）─────────────────────

export interface VlmSettings {
  vlm_provider: string
  vlm_model: string
  vlm_base_url: string
  vlm_api_key_set: boolean
  vlm_api_key_masked: string
  /** 目前這家的金鑰哪來的：'settings'（設定頁填的）/ 'env'（.env 讀到的）/ ''（沒有） */
  key_source: 'settings' | 'env' | ''
  /** 哪幾家在 .env 裡已經有金鑰，設定頁用來標記（不含金鑰本身） */
  env_keys: string[]
  /** false = 設定不完整 → 前端把「描述→OCR」反灰 */
  available: boolean
  reason: string
  hint: string
  /** true = Ollama 地端，圖片不出本機 */
  local: boolean
  providers: string[]
}

export async function getVlmSettings(): Promise<VlmSettings> {
  const res = await fetchWithRetry(`${BASE}/settings/vlm`)
  if (!res.ok) throw new Error('讀取視覺模型設定失敗')
  return res.json()
}

export async function saveVlmSettings(s: Partial<{
  vlm_provider: string; vlm_model: string; vlm_api_key: string; vlm_base_url: string
}>): Promise<VlmSettings> {
  const res = await fetch(`${BASE}/settings/vlm`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(s),
  })
  if (!res.ok) throw new Error(await _readErrorDetail(res))
  return res.json()
}

/** 真的打一次（左紅右藍小圖），確認金鑰 / 模型 / 看圖能力都沒問題。 */
export async function probeVlm(): Promise<{
  ok: boolean; answer?: string; reason: string; hint: string
}> {
  const res = await fetch(`${BASE}/settings/vlm/probe`, { method: 'POST' })
  if (!res.ok) throw new Error(await _readErrorDetail(res))
  return res.json()
}

/** 桌面自動化相關的使用者設定（自動縮小視窗、地端定位精度）。 */
export interface ComputerUseSettings {
  auto_minimize_for_computer_use: boolean
  grounding_precision: 'auto' | 'fp16' | 'int4'
  grounding_show_when_missing: boolean
}

export async function getComputerUseSettings(): Promise<ComputerUseSettings> {
  const res = await fetchWithRetry(`${BASE}/settings/computer-use`)
  if (!res.ok) throw new Error('讀取設定失敗')
  return res.json()
}

export async function saveComputerUseSettings(s: Partial<ComputerUseSettings>): Promise<ComputerUseSettings> {
  const res = await fetch(`${BASE}/settings/computer-use`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(s),
  })
  if (!res.ok) throw new Error(await _readErrorDetail(res))
  return res.json()
}

export async function deleteComputerUseAssets(dir: string): Promise<{ deleted: boolean; path: string; reason?: string }> {
  const res = await fetch(`${BASE}/computer-use/assets?dir=${encodeURIComponent(dir)}`, { method: 'DELETE' })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `刪除錨點資料夾失敗 (${res.status})`)
  }
  return res.json()
}

export function computerUseAssetImageUrl(dir: string, name: string): string {
  return `${BASE}/computer-use/assets/image?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(name)}`
}

export interface MonitorRect {
  left: number
  top: number
  width: number
  height: number
}

export async function getComputerUseMonitors(): Promise<{ monitors: MonitorRect[] }> {
  // monitors[0] = 虛擬桌面全景；monitors[1..N] = 每台實體螢幕
  const res = await fetch(`${BASE}/computer-use/monitors`)
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `讀取 monitor 清單失敗 (${res.status})`)
  }
  return res.json()
}

export interface CropAnchorReq {
  dir: string
  full_image: string
  click_x: number
  click_y: number
  full_left?: number
  full_top?: number
  crop_left: number
  crop_top: number
  crop_width: number
  crop_height: number
  save_as: string
}

export async function cropAnchorFromFull(req: CropAnchorReq): Promise<{
  image: string
  anchor_off_x: number
  anchor_off_y: number
  width: number
  height: number
  variance: number
}> {
  const res = await fetch(`${BASE}/computer-use/assets/crop`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ full_left: 0, full_top: 0, ...req }),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `裁切錨點失敗 (${res.status})`)
  }
  return res.json()
}

/** UIA element tree 節點(遞迴)。給 frontend tree picker 用。 */
export interface UiaElement {
  type: string                 // ControlType 名(Button / Edit / DataGrid 等)
  name: string
  auto_id: string              // AutomationId(可能是空)
  rect: number[]               // [x, y, w, h] 絕對桌面座標
  enabled: boolean
  offscreen: boolean
  children: UiaElement[]
}

export interface UiaInspectResult {
  ok: boolean
  window: { name: string; class: string; rect: number[]; process_id: number }
  tree: UiaElement
  error?: string
}

/** 檢視指定視窗的 UIA element tree(空 window = 當前 foreground)。 */
// ── AI 助手 ──────────────────────────────────────────────
/** 助手能不能用。不能用時 reason 會講清楚缺什麼。 */
export interface ChatStatus {
  available: boolean
  reason: string
  provider: string
  model: string
  /** 資料去向三態：local 本機 / internal 華碩內部 / external 外部廠商 */
  data_scope: string
  data_scope_label: string
  /** 只有真的不離開這台電腦才是 true（華碩自建的 gpt-oss 不算） */
  data_stays_local: boolean
}

export async function chatStatus(): Promise<ChatStatus> {
  const res = await fetch(`${BASE}/pipeline/chat/status`)
  if (!res.ok) throw new Error(`讀助手狀態失敗（HTTP ${res.status}）`)
  return res.json()
}

export interface ChatMessage { role: 'user' | 'assistant'; content: string }

/** 串流事件。注意串的是**工具事件**不是 token —— AiHub 沒有 streaming。 */
export type ChatEvent =
  | { type: 'tool_start'; name: string; args: Record<string, any>; mutating: boolean }
  | { type: 'tool_end'; name: string; result_preview: string }
  | { type: 'done'; reply: string; tool_calls: any[]; rounds: number; hit_round_limit?: boolean }
  | { type: 'error'; detail: string }

/**
 * 跑一輪對話，逐一把事件交給 onEvent。
 *
 * 一次提問可能跑好幾輪工具、每輪 5-20 秒，所以事件要即時吐給使用者看 ——
 * 全部做完才顯示等於讓人盯二十幾秒白屏。
 */
export async function chatStream(
  req: { messages: ChatMessage[]; extra_context?: string; temperature?: number },
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE}/pipeline/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
    signal,
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try { detail = (await res.json())?.detail || detail } catch { /* 保持原樣 */ }
    onEvent({ type: 'error', detail })
    return
  }
  if (!res.body) { onEvent({ type: 'error', detail: '沒有回應內容' }); return }

  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    // NDJSON：一行一個事件。最後一段可能被切斷，留在 buf 等下一塊。
    const lines = buf.split('\n')
    buf = lines.pop() ?? ''
    for (const line of lines) {
      const s = line.trim()
      if (!s) continue
      try { onEvent(JSON.parse(s)) } catch { /* 半行 JSON，丟掉 */ }
    }
  }
  const tail = buf.trim()
  if (tail) { try { onEvent(JSON.parse(tail)) } catch { /* 同上 */ } }
}

// ── 每工作流的對話歷史（跟著工作流走、切流就切對話）──
export interface WorkflowChatMessage {
  role: 'user' | 'assistant'
  content: string
  ts?: number
}

export async function getWorkflowChat(workflowId: string): Promise<WorkflowChatMessage[]> {
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/chat`)
  if (!res.ok) throw new Error('讀取工作流對話失敗')
  const data = await res.json()
  return data.messages || []
}

export async function appendWorkflowChat(
  workflowId: string,
  role: 'user' | 'assistant',
  content: string,
): Promise<WorkflowChatMessage[]> {
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role, content }),
  })
  if (!res.ok) throw new Error('追加對話訊息失敗')
  const data = await res.json()
  return data.messages || []
}

export async function setWorkflowChat(
  workflowId: string,
  messages: Array<{ role: 'user' | 'assistant'; content: string }>,
): Promise<WorkflowChatMessage[]> {
  // 覆寫整份（用於把 localStorage scratch 一次灌進新建立的工作流）
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/chat`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages }),
  })
  if (!res.ok) throw new Error('覆寫對話訊息失敗')
  const data = await res.json()
  return data.messages || []
}

export async function clearWorkflowChat(workflowId: string): Promise<void> {
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/chat`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error('清空對話失敗')
}

// ── LLM 設定 ─────────────────────────────────────────────
export interface LlmSettings {
  llm_provider: string
  llm_model: string
  llm_base_url: string
  llm_aihub_env: string
  /** 金鑰哪來的：'vault' / 'env' / ''（沒有）。永遠不回金鑰本身。 */
  key_source: string
  available: boolean
  reason: string
  data_scope: string
  data_scope_label: string
  data_stays_local: boolean
  providers: string[]
  aihub_allowed_models: string[]
  aihub_envs: string[]
}

export async function getLlmSettings(): Promise<LlmSettings> {
  const res = await fetch(`${BASE}/settings/llm`)
  if (!res.ok) throw new Error(`讀 LLM 設定失敗（HTTP ${res.status}）`)
  return res.json()
}

export async function updateLlmSettings(patch: Partial<{
  llm_provider: string; llm_model: string
  llm_base_url: string; llm_aihub_env: string
}>): Promise<LlmSettings> {
  const res = await fetch(`${BASE}/settings/llm`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new Error(`存 LLM 設定失敗（HTTP ${res.status}）`)
  return res.json()
}

export async function listLlmModels(): Promise<{
  ok: boolean; models: string[]; source?: string; note?: string; error?: string
}> {
  const res = await fetch(`${BASE}/settings/llm/models`)
  if (!res.ok) throw new Error(`讀模型清單失敗（HTTP ${res.status}）`)
  return res.json()
}

export async function probeLlm(): Promise<{
  ok: boolean; provider: string; model: string; key_source: string
  error?: string; elapsed_ms?: number; reply?: string
  data_scope?: string; data_scope_label?: string; data_stays_local?: boolean
}> {
  const res = await fetch(`${BASE}/settings/llm/probe`, { method: 'POST' })
  if (!res.ok) throw new Error(`測試連線失敗（HTTP ${res.status}）`)
  return res.json()
}


/** 螢幕 OCR 試抓結果。找不到時分兩種：標籤沒找到 / 標籤找到但旁邊沒有合格式的值。 */
export interface OcrProbeResult {
  ok: boolean
  error?: string
  word_count?: number
  found?: boolean
  value?: string
  label_read_as?: string
  label_score?: number
  direction?: string
  box?: number[]
  label_found?: boolean
  reason?: string
  candidates?: string[]
}

/** 立刻對當下螢幕試抓一次，讓使用者在設定時就看到會抓到什麼。 */
export async function ocrProbe(req: {
  label: string
  direction?: 'right' | 'below'
  kind?: 'amount' | 'ident' | 'taxid' | 'any'
  region?: number[]
}): Promise<OcrProbeResult> {
  const res = await fetch(`${BASE}/computer-use/ocr/probe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      label: req.label,
      direction: req.direction ?? 'right',
      kind: req.kind ?? 'amount',
      ...(req.region ? { region: req.region } : {}),
    }),
  })
  if (!res.ok) throw new Error(`OCR 試抓失敗（HTTP ${res.status}）`)
  return res.json()
}

export async function uiaInspect(req: {
  window?: string
  max_depth?: number
  max_children_per_node?: number
}): Promise<UiaInspectResult> {
  const res = await fetch(`${BASE}/computer-use/uia/inspect`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      window: req.window ?? '',
      max_depth: req.max_depth ?? 6,
      max_children_per_node: req.max_children_per_node ?? 50,
    }),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `UIA inspect 失敗 (${res.status})`)
  }
  return res.json()
}

/** 桌面對應位置畫紅框 outline、ttl_ms 後自動消失。 */
export async function uiaHighlight(req: {
  x: number; y: number; width: number; height: number; ttl_ms?: number
}): Promise<void> {
  await fetch(`${BASE}/computer-use/uia/highlight`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...req, ttl_ms: req.ttl_ms ?? 1500 }),
  }).catch(() => {})  // hover 失敗不打擾使用者
}

/** 立即清除紅框 outline。 */
export async function uiaHighlightClear(): Promise<void> {
  await fetch(`${BASE}/computer-use/uia/highlight/clear`, {
    method: 'POST',
  }).catch(() => {})
}

export interface UiaWindowInfo {
  name: string
  class: string
  rect: number[]
  is_offscreen: boolean
}

/** 列當下所有可見的 top-level 視窗。 */
export async function uiaListWindows(): Promise<{ ok: boolean; windows: UiaWindowInfo[] }> {
  const res = await fetch(`${BASE}/computer-use/uia/windows`)
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `列視窗失敗 (${res.status})`)
  }
  return res.json()
}

/** UIA Live Picker — 滑鼠 hover 桌面選元素 + F8 確認、F9 取消 */
export interface UiaPickerStatus {
  running: boolean
  hovered: UiaElement | null
  confirmed: UiaElement | null
  error: string | null
}

export async function uiaPickerStart(): Promise<{ ok: boolean; started: boolean; running: boolean }> {
  const res = await fetch(`${BASE}/computer-use/uia/picker/start`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}
export async function uiaPickerPoll(): Promise<UiaPickerStatus> {
  const res = await fetch(`${BASE}/computer-use/uia/picker/poll`)
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}
export async function uiaPickerConsume(): Promise<{ ok: boolean; element: UiaElement | null }> {
  const res = await fetch(`${BASE}/computer-use/uia/picker/consume`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}
export async function uiaPickerStop(): Promise<{ ok: boolean; was_running: boolean }> {
  const res = await fetch(`${BASE}/computer-use/uia/picker/stop`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}
export async function uiaPickerConfirm(): Promise<{ ok: boolean; element?: UiaElement; error?: string }> {
  const res = await fetch(`${BASE}/computer-use/uia/picker/confirm`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

/** 把 base64 PNG 直接存到 assets_dir(VLM 錨點立即截圖用、瀏覽器內裁切後上傳) */
export async function saveAnchorPng(req: {
  dir: string
  name: string
  png_b64: string
}): Promise<{ image: string; width: number; height: number; variance: number; size_bytes: number }> {
  const res = await fetch(`${BASE}/computer-use/assets/save-png`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(detail || `存錨點 PNG 失敗 (${res.status})`)
  }
  return res.json()
}
export interface WebhookInfo {
  token: string; workflow_id: string; enabled: boolean
  created_at: number; last_fired_at: number; fire_count: number
  url: string | null
}
export interface FolderWatchInfo {
  id: string; workflow_id: string; folder_path: string; pattern: string
  enabled: boolean; created_at: number; last_seen_mtime: number; trigger_count: number
}

export async function getWebhook(workflowId: string): Promise<WebhookInfo | null> {
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/webhook`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error('讀取 webhook 失敗')
  return res.json()
}

export async function createWebhook(workflowId: string): Promise<WebhookInfo> {
  const res = await fetch(`${BASE}/workflows/${workflowId}/webhook`, { method: 'POST' })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '建立 webhook 失敗')
  return data
}

export async function disableWebhook(workflowId: string): Promise<void> {
  const res = await fetch(`${BASE}/workflows/${workflowId}/webhook`, { method: 'DELETE' })
  if (!res.ok) throw new Error('停用 webhook 失敗')
}

export async function getFolderWatch(workflowId: string): Promise<FolderWatchInfo | null> {
  const res = await fetchWithRetry(`${BASE}/workflows/${workflowId}/folder-watch`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error('讀取檔案夾監看失敗')
  return res.json()
}

export async function createFolderWatch(workflowId: string, folderPath: string, pattern: string): Promise<FolderWatchInfo> {
  const res = await fetch(`${BASE}/workflows/${workflowId}/folder-watch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder_path: folderPath, pattern }),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '建立監看失敗')
  return data
}

export async function disableFolderWatch(workflowId: string): Promise<void> {
  const res = await fetch(`${BASE}/workflows/${workflowId}/folder-watch`, { method: 'DELETE' })
  if (!res.ok) throw new Error('停用監看失敗')
}

// ── Secrets Vault(值永不回傳;工作流以 {{ secrets.名稱 }} 引用)──────────
export interface SecretMeta { name: string; created_at: number; updated_at: number }

export async function listSecrets(): Promise<SecretMeta[]> {
  const res = await fetchWithRetry(`${BASE}/settings/secrets`)
  if (!res.ok) throw new Error('讀取 secrets 失敗')
  return (await res.json()).secrets
}

export async function setSecret(name: string, value: string): Promise<{ ok: boolean; name: string }> {
  const res = await fetch(`${BASE}/settings/secrets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, value }),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? '儲存失敗')
  return data
}

export async function deleteSecret(name: string): Promise<void> {
  const res = await fetch(`${BASE}/settings/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('刪除失敗')
}
export interface WorkflowData {
  id: string
  name: string
  yaml: string
  canvas: { nodes: any[]; edges: any[] }
  created_at: number
  updated_at: number
}

export async function listWorkflows(): Promise<WorkflowData[]> {
  const res = await fetchWithRetry(`${BASE}/workflows`)
  if (!res.ok) throw new Error('讀取工作流失敗')
  return res.json()
}

export async function createWorkflowApi(name: string = '新工作流', canvas?: { nodes: any[]; edges: any[] }): Promise<WorkflowData> {
  const res = await fetchWithRetry(`${BASE}/workflows`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, canvas }),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`建立工作流失敗 (${res.status}): ${detail || '後端暫時無回應，請稍後再試'}`)
  }
  return res.json()
}

export async function updateWorkflowApi(id: string, patch: { name?: string; canvas?: any; yaml?: string }): Promise<WorkflowData> {
  const res = await fetchWithRetry(`${BASE}/workflows/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new Error('更新工作流失敗')
  return res.json()
}

export async function deleteWorkflowApi(id: string): Promise<void> {
  const res = await fetchWithRetry(`${BASE}/workflows/${id}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('刪除工作流失敗')
}

export function exportWorkflowUrl(id: string): string {
  return `${BASE}/workflows/${id}/export`
}

export async function importWorkflow(file: File): Promise<{
  workflow: WorkflowData
  needs_reanchor: boolean
}> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${BASE}/workflows/import`, { method: 'POST', body: form })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail ?? '匯入失敗')
  }
  return res.json()
}
export interface NotificationSettings {
  /** 後端永遠不回傳完整 token，只說有沒有設、以及遮罩後的樣子。 */
  telegram_bot_token_set: boolean
  telegram_bot_token_masked: string
  telegram_chat_id: string
  telegram_remote_control: boolean
}

/** 寫入用（token 只有在使用者真的重填時才送）。 */
export interface NotificationSettingsInput {
  telegram_bot_token?: string
  telegram_chat_id?: string
  telegram_remote_control?: boolean
}

export async function getNotificationSettings(): Promise<NotificationSettings> {
  const res = await fetchWithRetry(`${BASE}/settings/notifications`)
  if (!res.ok) throw new Error('讀取通知設定失敗')
  return res.json()
}

export async function saveNotificationSettings(s: NotificationSettingsInput): Promise<NotificationSettings> {
  const res = await fetch(`${BASE}/settings/notifications`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(s),
  })
  if (!res.ok) throw new Error('儲存通知設定失敗')
  return res.json()
}
export interface ScreenSnapshot {
  origin_x: number    // 虛擬桌面左上絕對座標 X（多螢幕配置可能負值）
  origin_y: number    // 虛擬桌面左上絕對座標 Y
  width: number       // 截圖寬（像素）
  height: number      // 截圖高（像素）
  image_b64: string   // PNG base64
}

export async function getScreenSnapshot(): Promise<ScreenSnapshot> {
  const res = await fetchWithRetry(`${BASE}/screen/snapshot`)
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`螢幕擷取失敗（${res.status}）：${detail}`)
  }
  return res.json()
}

// ── 列 assets_dir 內的錨點 PNG 檔（給 VLM 挑錨點 file picker 用）
export interface AssetFileEntry {
  name: string
  size: number
  mtime: number
}
export async function listAssetFiles(dir: string): Promise<{ dir: string; files: AssetFileEntry[] }> {
  const url = `${BASE}/computer-use/assets/list?dir=${encodeURIComponent(dir)}`
  const res = await fetchWithRetry(url)
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`列檔案失敗（${res.status}）：${detail}`)
  }
  return res.json()
}

// 直接給縮圖 URL（讓 <img src=...> 載入；瀏覽器會自動 GET）
export function assetImageUrl(dir: string, name: string): string {
  return `${BASE}/computer-use/assets/image?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(name)}`
}
export interface VariableField {
  key: string
  type: string
  last_value: string | number
  source?: string
}

export interface VariableStepInfo {
  name: string
  node_type: string
  fields: VariableField[]
}

export interface WorkflowVariablesResult {
  available: {
    steps: VariableStepInfo[]
    input: { key: string; last_value: string; required: boolean }[]
    env: { key: string; last_value: string; is_secret: boolean }[]
  }
  referenced: string[]
  last_run_id: string | null
}
export async function getWorkflowVariables(wfId: string): Promise<WorkflowVariablesResult> {
  const res = await fetchWithRetry(`${BASE}/workflows/${wfId}/variables`)
  if (!res.ok) throw new Error(`getWorkflowVariables 失敗:${res.status}`)
  return res.json()
}
