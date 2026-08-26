import type { Node, Edge } from '@xyflow/react'

// ── 資料型別 ─────────────────────────────────────────────────────────────────

/** 腳本節點：執行用戶寫好的腳本或指令 */
export interface StepData extends Record<string, unknown> {
  name: string
  batch: string
  workingDir: string
  outputPath: string
  expect: string
  humanConfirm?: boolean           // optional — 人工確認步驟
  humanConfirmMessage?: string     // optional — 確認訊息
  humanConfirmNotifyTelegram?: boolean  // optional — 是否 Telegram 通知
  humanConfirmScreenshot?: boolean     // optional — 是否自動截圖
  humanConfirmPreview?: boolean        // optional — 是否 render 上一步驟輸出檔案預覽
  humanConfirmSendPrevOutput?: boolean // optional — 抵達節點時自動把上一步輸出檔當 document 傳到 TG
  hcOnTimeout?: 'wait' | 'pass' | 'reject' | 'abort'  // 超時後行動,預設 wait = 永遠等
  // 背景模式(Script 開 daemon / GUI 用):啟動後不等 exit、立即下一步、subprocess 由 runner 接管
  background?: boolean
  background_keep?: boolean   // 背景模式下:workflow 結束「不」自動 kill、進程留在桌面供手動操作
  readyAfterSeconds?: number   // 背景啟動後等 N 秒讓 daemon ready 再下一步,預設 0
  // 桌面自動化節點（computer_use）
  computerUse?: boolean                  // optional — 桌面自動化步驟
  computerUseActions?: ComputerUseAction[]  // optional — 動作序列
  computerUseAssetsDir?: string          // optional — 錨點圖片資料夾
  computerUseFailFast?: boolean          // optional — 遇錯立即中止
  cvThreshold?: number                   // CV 比對門檻：0.50 寬鬆 / 0.80 標準 / 0.90 嚴格
  cvSearchOnlyNear?: boolean             // true = 只搜錄製座標附近
  cvSearchRadius?: number                // 附近搜尋半徑（px），預設 400
  cvTriggerHover?: boolean               // true = 比對前先觸發 hover 效果（匹配錄製時的 hover 狀態）
  cvHoverWaitMs?: number                 // hover 等待時間（ms）：200 或 400
  cvCoordFallback?: boolean              // true = CV 失敗時退回錄製座標硬點（預設 false = 失敗就停）
  ocrThreshold?: number                  // OCR 最小 conf 門檻（預設 0.6）
  ocrCvFallback?: boolean                // true = OCR 失敗接著 CV 比對（預設 false = 失敗就停）
  // UIA 模式
  cuMode?: 'pixel' | 'uia'                // 預設 pixel
  uiaWindow?: string                       // 視窗 title pattern(支援 *)
  // 輸出 JSON Schema 合約(output.json_schema、inline JSON 字串)
  jsonSchemaText?: string
  // Condition 節點(Ticket 2 控制流)— 純 metadata、runner 跳轉用、不執行命令
  condition?: boolean
  expression?: string                    // IF mode
  onTrue?: string
  onFalse?: string
  switch?: string                        // Switch mode
  cases?: Record<string, string>
  default?: string
  // 跳轉:任意 step 跑完跳指定下一步(end / __end__ / 空 = 線性)
  next?: string
  // 此節點用哪份 LLM 設定:"primary"(預設、走主模型)或 "secondary"(走副模型)
  // 副模型在設定頁設;副模型未設則自動 fallback 主
  timeout: number
  retry: number
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

export interface ConditionData extends Record<string, unknown> {
  name: string
  mode: 'if' | 'switch'      // UI 顯示用、後端只看 expression / switch 哪個非空
  expression: string         // IF 模式:Jinja2 boolean expression
  onTrue: string             // IF 模式:條件為真跳的 step name
  onFalse: string            // IF 模式:條件為假跳的 step name(留空 = end)
  switch: string             // Switch 模式:Jinja2 expression、求值後 str(value)
  cases: Record<string, string>  // Switch 模式:case_value → step_name
  default: string            // Switch 模式:沒命中時跳的 step name(留空 = end)
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

/** 人工確認節點：暫停 Pipeline 等待人為確認 */
export interface HumanConfirmData extends Record<string, unknown> {
  name: string
  message: string          // 自訂確認訊息
  notifyTelegram: boolean  // 是否透過 Telegram 通知
  screenshot: boolean      // 是否自動截圖並傳送到 Telegram
  previewPrevOutput: boolean  // 是否 render 上一步驟輸出檔案成 PNG 傳 TG
  sendPrevOutput: boolean  // 是否自動把上一步輸出檔當 document 傳到 TG（手機可下載）
  timeout: number          // 超時秒數(超時行動 != wait 時有效)
  hcOnTimeout: 'wait' | 'pass' | 'reject' | 'abort'   // 超時後的行動,預設 'wait' = 永遠等
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

// 桌面自動化動作（對應 backend ComputerUseAction）
export interface ComputerUseAction {
  type: 'click_image' | 'click_at' | 'type_text' | 'hotkey' | 'wait' | 'wait_image' | 'screenshot' | 'scroll' | 'drag'
      | 'assert_image' | 'assert_text' | 'activate_window' | 'if_image_found' | 'retry_until'
      | 'uia_click' | 'uia_send_keys' | 'uia_get_text' | 'uia_get_table_rowcount' | 'uia_click_cell'
      | 'uia_wait_enabled' | 'uia_assert_state' | 'uia_close_window' | 'uia_set_clipboard'
      | 'uia_select' | 'uia_wait'
      | 'if_element_found' | 'wait_download' | 'wait_text' | 'if_text_found' | 'for_each'
      | 'ocr_get_text'
  // ── ocr_get_text 專用：讀「標籤旁邊的值」存成變數 ──
  label?: string        // 要找的標籤文字（例：總計金額）
  direction?: 'right' | 'below'                // 值在標籤的哪一側
  kind?: 'amount' | 'ident' | 'taxid' | 'any'  // 值的格式約束；taxid 會驗檢查碼
  max_gap?: number      // 標籤與值的最大距離（px）
  lang_tag?: string     // OCR 語言標籤，留空走預設 zh-Hant-TW
  type_method?: string  // type_text：clipboard（預設、IME 免疫）/ keys（逐字打）
  // save_as 沿用下方 UIA action 的同名欄位（同樣是「值存進哪個變數」）

  image?: string
  image2?: string        // 次錨點（多錨點驗證）
  dx2?: number           // 次錨點相對點擊點的 X 位移
  dy2?: number           // 次錨點相對點擊點的 Y 位移
  x?: number
  y?: number
  x2?: number
  y2?: number
  dy?: number
  text?: string
  keys?: string[]
  seconds?: number
  timeout_sec?: number
  confidence?: number
  button?: 'left' | 'right' | 'middle'
  clicks?: number
  description?: string
  // 三層 fallback toggle (UIA / CV / 強制座標), 預設全 True = UIA → CV → 強制座標
  // 對應 backend ComputerUseAction.{use_uia, use_cv, use_coord}; 細節見 panel 註解
  use_uia?: boolean     // UIA element 結構定位(預設 True)
  use_cv?: boolean      // CV 圖像比對(預設 True)
  use_coord?: boolean   // 強制座標最終 fallback(預設 True、舊欄位、語意改成「最終座標 fallback 啟用」)
  hold_sec?: number     // click 長按時間（>0 時回放走 mouseDown→sleep→mouseUp）
  modifiers?: string[]  // click 時按著的修飾鍵（如 ["ctrl"]、["ctrl","shift"]）
  use_ocr?: boolean     // click_image 顯式 OCR 啟用（勾選才跑 OCR，避免 silent 填字但沒觸發）
  ocr_text?: string     // OCR 目標文字（跟 use_ocr=true 搭配才生效）
  // OCR 搜尋範圍（藍框，絕對桌面座標；width=0 = 未設定，回退 near_xy+cv_search_radius）
  ocr_box_left?: number
  ocr_box_top?: number
  ocr_box_width?: number
  ocr_box_height?: number
  // 嚴格鎖定範圍：true = 框內找不到立即 fail（不退附近、不退全螢幕）
  ocr_strict_region?: boolean
  // UIA action 專用(uia_click / uia_send_keys / uia_get_text / uia_get_table_rowcount / uia_click_cell / uia_wait_enabled / uia_assert_state)
  control?: { type?: string; name?: string; auto_id?: string; depth?: number }
  save_as?: string
  row?: number | string                                    // 字串支援 {{var}} 替換
  column?: number | string
  check?: 'exists' | 'enabled' | 'focused' | 'checked'
  window?: string                                          // action 層級 window 覆寫(空 → 用 step.uiaWindow)
  rect?: number[]                                          // UIA picker 抓到的 element rect[x,y,w,h]、給 backend ControlFromPoint fallback 用
  anchor_off_x?: number // 點擊相對錨點影像中心的偏移 x
  anchor_off_y?: number // 點擊相對錨點影像中心的偏移 y
  full_image?: string   // 全螢幕截圖檔名（手動圈選編輯錨點時用）
  full_left?: number    // 全螢幕截圖對應的虛擬桌面原點 X（可能是負值）
  full_top?: number     // 全螢幕截圖對應的虛擬桌面原點 Y
  // search_region：CV / OCR 搜尋矩形（紅框，絕對桌面座標 [l,t,w,h]）
  search_region?: number[]
  // CV 嚴格鎖定範圍：true = 紅框內找不到立即 fail（不退附近、不退全螢幕、不退錄製座標）
  cv_strict_region?: boolean
  // 錨點獨特性（錄製後分析填入，純提醒用、不影響執行）：
  // 這張錨點在錄製當下的畫面上還對得到幾個地方。>0 代表回放時 CV 可能挑錯。
  anchor_rivals?: number
  anchor_nearest_rival_px?: number
  // 視覺輔助模式：
  //   grounding   → 地端 GUI 定位模型直接給座標（連 CV 都點不準時用；失敗自動退回 CV）
  //   description → 模型讀出目標的實際文字，座標交給 OCR（文字每次都不同時用）
  vlm_mode?: 'off' | 'grounding' | 'description'
  vlm_prompt?: string   // grounding / description 模式要找的目標描述
  // 多形態錨點：同一顆按鈕的不同樣子（最大化↔還原、亮↔暗主題），
  // 執行時每張都比一次取最高分。取代 Atlas 靠雲端模型挑圖的 anchor_pick。
  image_variants?: string[]
  // 控制流：if_image_found / retry_until 用（unknown[] 因為遞迴 dict 巢狀）
  then?: ComputerUseAction[]
  else?: ComputerUseAction[]
  do?: ComputerUseAction[]
  dir?: string      // wait_download：下載資料夾（空 = Downloads）
  pattern?: string  // wait_download：檔名 glob
  items?: string | string[]  // for_each：清單或逗號/換行分隔字串
  continue_on_error?: boolean  // for_each：某筆失敗跳下一筆
  until?: ComputerUseAction | string  // retry_until 巢狀動作 / uia_wait 條件字串(appear/disappear/text_contains/text_equals)
  max_attempts?: number
  wait_between_sec?: number
  // activate_window 用
  title?: string
  title_contains?: string
}

export interface ComputerUseData extends Record<string, unknown> {
  name: string
  actions: ComputerUseAction[]
  assetsDir: string         // 錨點圖片資料夾（相對工作流）
  failFast: boolean         // 遇錯立即中止
  cvThreshold: number       // CV 比對門檻：0.50 寬鬆 / 0.80 標準 / 0.90 嚴格
  cvSearchOnlyNear: boolean // true = 只搜錄製座標附近（找不到直接 FAIL）
  cvSearchRadius: number    // 附近搜尋半徑（px），預設 400
  cvTriggerHover: boolean   // true = 比對前先 moveTo 錄製座標觸發 hover
  cvHoverWaitMs: number     // hover 等待 ms：200（快）/ 400（保險）
  cvCoordFallback: boolean  // true = CV 失敗時退回錄製座標硬點。預設 false（失敗就停，不亂點）
  ocrThreshold: number      // OCR 最小 conf 門檻（1.0/0.9/0.8/0.6 分級；預設 0.6）
  ocrCvFallback: boolean    // true = OCR 失敗時繼續試 CV 比對鏈。預設 false（失敗就停）
  // UIA 模式(預設 pixel、向後相容)
  cuMode: 'pixel' | 'uia'   // 'pixel' = 錄製座標(現況);'uia' = UIA tree 控制
  uiaWindow: string         // UIA 模式視窗 title pattern(支援 *)、空字串 = foreground
  timeout: number           // 秒（執行上限）
  retry: number
  index: number
  status: 'idle' | 'running' | 'success' | 'failed'
  errorMsg: string
}

/** 視覺驗證節點：用 Settings 主模型（必須支援視覺）判斷某個圖像是否符合預期 */
export type ScriptNode = Node<StepData>
export type HumanConfirmNode = Node<HumanConfirmData>
export type ComputerUseNode = Node<ComputerUseData>
export type ConditionNode = Node<ConditionData>
export type AppNode = Node<StepData | HumanConfirmData | ComputerUseData | ConditionData>

let _conditionCounter = 0

export function newConditionData(index = 0): ConditionData {
  _conditionCounter++
  return {
    name: `條件_${_conditionCounter}`,
    mode: 'if',
    expression: '',
    onTrue: '',
    onFalse: '',
    switch: '',
    cases: {},
    default: '',
    index,
    status: 'idle',
    errorMsg: '',
  }
}

let _confirmCounter = 0
export function newHumanConfirmData(index = 0): HumanConfirmData {
  _confirmCounter++
  return {
    name: `人工確認_${_confirmCounter}`,
    message: '',
    notifyTelegram: true,
    screenshot: false,
    previewPrevOutput: false,
    sendPrevOutput: false,
    timeout: 3600,
    hcOnTimeout: 'wait',
    index,
    status: 'idle',
    errorMsg: '',
  }
}

// 防呆:新增桌面自動化節點時,確保名稱不與現有節點撞名(計數器頁面重整後會歸零、
// 撞名會讓兩節點共用同一個 _assets 夾 → 互相覆蓋錨點圖)。回傳一個目前沒被用到的 桌面自動化_N。
export function dedupeComputerUseName(name: string, existing: Set<string>): string {
  if (!existing.has(name)) return name
  let k = 1
  while (existing.has(`桌面自動化_${k}`)) k++
  return `桌面自動化_${k}`
}

let _computerUseCounter = 0
export function newComputerUseData(index = 0): ComputerUseData {
  _computerUseCounter++
  return {
    name: `桌面自動化_${_computerUseCounter}`,
    actions: [],
    assetsDir: '',
    failFast: true,
    cvThreshold: 0.5,
    cvSearchOnlyNear: false,
    cvSearchRadius: 400,
    cvTriggerHover: true,
    cvHoverWaitMs: 200,
    cvCoordFallback: false,
    ocrThreshold: 0.6,
    ocrCvFallback: false,
    cuMode: 'pixel',
    uiaWindow: '',
    timeout: 300,
    retry: 0,
    index,
    status: 'idle',
    errorMsg: '',
  }
}

let _counter = 0
export function newStepData(index = 0): StepData {
  _counter++
  return {
    name: `Python腳本_${_counter}`,
    batch: '',
    workingDir: '',
    outputPath: '',
    expect: '',
    timeout: 300,
    // 與 backend models.py PipelineStep.retry default 對齊；
    // 讓失敗有一次自我修正的機會（節點失敗後 LLM 會看到 reason 重試）。
    // 不想要重試的步驟在 UI 改成 0 即可。
    retry: 1,
    index,
    status: 'idle',
    errorMsg: '',
  }
}

let _skillCounter = 0
const COLORS = ['#6366f1','#0ea5e9','#10b981','#f59e0b','#ec4899','#8b5cf6','#14b8a6','#f97316']
export const stepColor = (index: number) => COLORS[index % COLORS.length]

// ── 扇形版面 ──────────────────────────────────────────────────────────────────
// condition(IF / Switch)分支會「扇形」攤開:主線一排、分支對稱往上下展、
// 每條分支各自往右接成一列。讓使用者一眼看懂「這裡分流、各走各的」。
const FAN_COL_W = 360
const FAN_ROW_H = 200
const FAN_Y0 = 160

// 從 steps 算出每個節點的出邊。
// condition → 依 cases(Switch)/ onTrue·onFalse(IF) 連到具名目標;
// 其他節點:next: end → 不連(分支終點);next: <名稱> → 連該節點;無 next → 線性 i→i+1。
function buildFlowGraph(steps: StepData[]): { out: number[][]; edges: { i: number; j: number }[] } {
  const n = steps.length
  const nameToIdx = new Map<string, number>()
  steps.forEach((s, i) => { if (s.name && !nameToIdx.has(s.name)) nameToIdx.set(s.name, i) })
  const out: number[][] = steps.map(() => [])
  const edges: { i: number; j: number }[] = []
  steps.forEach((s, i) => {
    const targets: number[] = []
    if (s.condition) {
      if (s.switch) {
        for (const tgt of Object.values(s.cases || {})) {
          const j = nameToIdx.get(String(tgt))
          if (j !== undefined) targets.push(j)
        }
        if (s.default && nameToIdx.has(s.default)) targets.push(nameToIdx.get(s.default)!)
      } else {
        if (s.onTrue && nameToIdx.has(s.onTrue)) targets.push(nameToIdx.get(s.onTrue)!)
        if (s.onFalse && nameToIdx.has(s.onFalse)) targets.push(nameToIdx.get(s.onFalse)!)
      }
    } else {
      const nxt = s.next
      if (nxt && nxt.trim().toLowerCase() === 'end') {
        // 分支終點、不連
      } else if (nxt) {
        const j = nameToIdx.get(nxt)
        if (j !== undefined) targets.push(j)
      } else if (i + 1 < n) {
        targets.push(i + 1)
      }
    }
    // 同一目標去重:default 與某 case 指向同一節點時只連一條(避免畫面重複箭頭)
    const seen = new Set<number>()
    for (const j of targets) { if (seen.has(j)) continue; seen.add(j); out[i].push(j); edges.push({ i, j }) }
  })
  return { out, edges }
}

// 算出每個節點的扇形座標:欄(x)= 最長路徑深度;列(y)= DFS,condition 多出邊對稱攤開。
function fanLayout(n: number, out: number[][]): { x: number; y: number }[] {
  if (n === 0) return []
  const col = new Array(n).fill(0)
  for (let pass = 0; pass < n; pass++) {
    let changed = false
    for (let i = 0; i < n; i++) for (const j of out[i]) if (col[j] < col[i] + 1) { col[j] = col[i] + 1; changed = true }
    if (!changed) break
  }
  const lane = new Map<number, number>()
  const dfs = (i: number, l: number) => {
    if (lane.has(i)) return
    lane.set(i, l)
    const ch = out[i]
    if (ch.length <= 1) { for (const c of ch) dfs(c, l) }
    else { const k = ch.length; ch.forEach((c, idx) => dfs(c, l + (idx - (k - 1) / 2))) }
  }
  dfs(0, 0)
  for (let i = 0; i < n; i++) if (!lane.has(i)) lane.set(i, 0)
  // 匯流置中:多條分支收斂回同一節點時,擺在各前驅的垂直中點、其後單線子節點跟著移。
  const preds: number[][] = new Array(n).fill(0).map(() => [])
  for (let i = 0; i < n; i++) for (const j of out[i]) preds[j].push(i)
  const byCol = [...Array(n).keys()].sort((a, b) => col[a] - col[b])
  for (const j of byCol) {
    if (preds[j].length >= 2) {
      lane.set(j, preds[j].reduce((s, p) => s + lane.get(p)!, 0) / preds[j].length)
      let cur = j
      while (out[cur].length === 1 && preds[out[cur][0]].length < 2) {
        const nx = out[cur][0]; lane.set(nx, lane.get(cur)!); cur = nx
      }
    }
  }
  return [...Array(n).keys()].map(i => ({ x: col[i] * FAN_COL_W, y: FAN_Y0 + lane.get(i)! * FAN_ROW_H }))
}

// ── Steps → ReactFlow nodes + edges ──────────────────────────────────────────
export function stepsToFlow(steps: StepData[]): { nodes: AppNode[]; edges: Edge[] } {
  const nodes: AppNode[] = steps.map((s, i) => {
    if (s.computerUse) {
      return {
        id: `step-${i}`,
        type: 'computerUse' as const,
        position: { x: i * 320, y: 160 },
        data: {
          name: s.name,
          actions: s.computerUseActions || [],
          assetsDir: s.computerUseAssetsDir || '',
          failFast: s.computerUseFailFast ?? true,
          cvThreshold: s.cvThreshold ?? 0.5,
          cvSearchOnlyNear: s.cvSearchOnlyNear ?? false,
          cvSearchRadius: s.cvSearchRadius ?? 400,
          cvTriggerHover: s.cvTriggerHover ?? true,
          cvHoverWaitMs: s.cvHoverWaitMs ?? 200,
          cvCoordFallback: s.cvCoordFallback ?? false,
          ocrThreshold: s.ocrThreshold ?? 0.6,
          ocrCvFallback: s.ocrCvFallback ?? false,
          cuMode: s.cuMode ?? 'pixel',
          uiaWindow: s.uiaWindow ?? '',
          timeout: s.timeout,
          retry: s.retry,
          // expect / json_schema:節點 data 必須帶著,否則「套用 YAML → 畫布 → autosave」
          // 這一圈就把 YAML 寫好的驗證閘洗掉(與 flowToSteps 讀回端成對)
          expectText: s.expect || '',
          jsonSchemaText: s.jsonSchemaText || '',
          index: i,
          status: 'idle' as const,
          errorMsg: '',
        } as ComputerUseData,
      }
    }
    if (s.humanConfirm) {
      return {
        id: `step-${i}`,
        type: 'humanConfirmation' as const,
        position: { x: i * 320, y: 160 },
        data: {
          name: s.name,
          message: s.humanConfirmMessage || '',
          notifyTelegram: s.humanConfirmNotifyTelegram ?? true,
          screenshot: s.humanConfirmScreenshot ?? false,
          previewPrevOutput: s.humanConfirmPreview ?? false,
          sendPrevOutput: s.humanConfirmSendPrevOutput ?? false,
          timeout: s.timeout || 3600,
          hcOnTimeout: (s.hcOnTimeout as HumanConfirmData['hcOnTimeout']) ?? 'wait',
          index: i,
          status: 'idle' as const,
          errorMsg: '',
        } as HumanConfirmData,
      }
    }
    if (s.condition) {
      // YAML 來源:有寫 expression → IF 模式;有寫 switch → Switch 模式
      const inferredMode: 'if' | 'switch' = s.switch ? 'switch' : 'if'
      return {
        id: `step-${i}`,
        type: 'condition' as const,
        position: { x: i * 320, y: 160 },
        data: {
          name: s.name,
          mode: inferredMode,
          expression: s.expression || '',
          onTrue: s.onTrue || '',
          onFalse: s.onFalse || '',
          switch: s.switch || '',
          cases: (s.cases as Record<string, string>) || {},
          default: s.default || '',
          index: i,
          status: 'idle' as const,
          errorMsg: '',
        } as ConditionData,
      }
    }
    return {
      id: `step-${i}`,
      type: 'scriptStep' as const,
      position: { x: i * 320, y: 160 },
      data: { ...s, index: i },
    }
  })

  // 分支感知:condition 依 cases/onTrue·onFalse 連線、其餘照 next / 線性。
  // 同步把節點排成扇形(主線一排、分支對稱攤開),不再全擠成單排線性鏈。
  const { out, edges: rawEdges } = buildFlowGraph(steps)
  const positions = fanLayout(steps.length, out)
  nodes.forEach((node, i) => { node.position = positions[i] })

  // 用 insertable type — hover 出 + / 🗑️；箭頭由 ReactFlow defaultEdgeOptions 統一處理
  const edges: Edge[] = rawEdges.map(({ i, j }) => ({
    id: `e-${i}-${j}`,
    source: `step-${i}`,
    target: `step-${j}`,
    type: 'insertable',
    animated: steps[i].status === 'running',
    style: { stroke: stepColor(i), strokeWidth: 2 },
    markerEnd: { type: 'arrowclosed' as any, color: stepColor(i), width: 18, height: 18 },
  }))

  return { nodes, edges }
}

// ── ReactFlow nodes → ordered steps（只包含有邊連接的節點）──────────────────────
export function flowToSteps(nodes: AppNode[], edges: Edge[]): StepData[] {
  // 過濾出可執行節點。condition 不執行命令但要進 YAML、由 runner 求值跳轉。
  const execNodeIds = new Set<string>()
  const execNodes: AppNode[] = []
  for (const n of nodes) {
    if (n.type === 'scriptStep' || n.type === 'humanConfirmation'
        || n.type === 'condition' || n.type === 'computerUse') {
      execNodeIds.add(n.id)
      execNodes.push(n)
    }
  }
  if (execNodes.length === 0) return []

  const virtualEdges: Edge[] = edges.filter(
    e => execNodeIds.has(e.source) && execNodeIds.has(e.target))

  // 找起點（無入邊的節點）
  const hasIncoming = new Set(virtualEdges.map(e => e.target))
  const starts = execNodes.filter(n => !hasIncoming.has(n.id))
  if (!starts.length) return []

  // 沿邊走、收集有連接的節點。
  // 之前用 Map<source,target>（單一 target）→ 同 source 多條出邊只保留最後一條、
  // 後寫覆蓋前寫；使用者「插入中間節點忘記刪舊邊」會看運氣決定走不走中間節點，
  // 而且中間節點會被當「孤立節點」靜默丟掉（user 收不到任何警告）。
  // 改成 multimap + DFS 找最長路徑：插入新節點即使保留舊邊、新路徑也會被選中。
  const adjMulti = new Map<string, string[]>()
  for (const e of virtualEdges) {
    if (!adjMulti.has(e.source)) adjMulti.set(e.source, [])
    adjMulti.get(e.source)!.push(e.target)
  }
  // DFS 找最長路徑；visited 防 cycle、子探索用 set copy 不互相污染
  const longestFrom = (node: string, visited: Set<string>): string[] => {
    if (visited.has(node)) return []
    const next = new Set(visited); next.add(node)
    const targets = adjMulti.get(node) || []
    if (targets.length === 0) return [node]
    let best: string[] = []
    for (const t of targets) {
      const sub = longestFrom(t, next)
      if (sub.length > best.length) best = sub
    }
    return [node, ...best]
  }
  const orderIds = longestFrom(starts[0].id, new Set<string>())
  const ordered: AppNode[] = []
  const seen = new Set<string>()
  for (const id of orderIds) {
    const node = execNodes.find(n => n.id === id)
    if (node) { ordered.push(node); seen.add(id) }
  }

  // 主路徑外的「分支子節點」也要進 YAML — condition 節點會分多條路、
  // 不在最長路徑上的分支(例如 onFalse 那條)若被丟掉、runner 就跳不到、
  // 會報「找不到 step」。所以 BFS 把所有從起點可達的 exec 節點都收進來、
  // 附在主路徑後面(順序不影響 — runner 靠 name 跳轉、不靠陣列順序)。
  const reachable: string[] = []
  {
    const queue = [starts[0].id]
    const visited = new Set<string>()
    while (queue.length) {
      const cur = queue.shift()!
      if (visited.has(cur)) continue
      visited.add(cur)
      for (const t of adjMulti.get(cur) || []) {
        if (!visited.has(t)) queue.push(t)
      }
    }
    for (const id of visited) {
      if (!seen.has(id)) reachable.push(id)
    }
  }
  for (const id of reachable) {
    const node = execNodes.find(n => n.id === id)
    if (node) ordered.push(node)
  }

  // 孤立節點不加入（邊驅動執行）

  const result = ordered.map((n, i) => {

    if (n.type === 'computerUse') {
      const d = n.data as ComputerUseData
      return {
        name: d.name,
        batch: '',
        workingDir: '',
        outputPath: '',
        // expect / json_schema 由後端 yaml_to_canvas 放進 base_data,這裡必須帶回,
        // 否則 YAML 寫好的驗證閘會在 canvas round-trip(autosave)時被洗掉
        expect: (d as any).expectText || '',
        jsonSchemaText: (d as any).jsonSchemaText || '',
        computerUse: true,
        computerUseActions: d.actions,
        computerUseAssetsDir: d.assetsDir,
        computerUseFailFast: d.failFast,
        cvThreshold: d.cvThreshold,
        cvSearchOnlyNear: d.cvSearchOnlyNear,
        cvSearchRadius: d.cvSearchRadius,
        cvTriggerHover: d.cvTriggerHover,
        cvHoverWaitMs: d.cvHoverWaitMs,
        ocrThreshold: d.ocrThreshold,
        ocrCvFallback: d.ocrCvFallback,
        cvCoordFallback: d.cvCoordFallback,
        cuMode: d.cuMode,
        uiaWindow: d.uiaWindow,
        timeout: d.timeout,
        retry: d.retry,
        index: i,
        status: d.status,
        errorMsg: d.errorMsg,
      } as StepData
    }

    if (n.type === 'humanConfirmation') {
      const d = n.data as HumanConfirmData
      return {
        name: d.name,
        batch: '',
        workingDir: '',
        outputPath: '',
        expect: '',
        humanConfirm: true,
        humanConfirmMessage: d.message,
        humanConfirmNotifyTelegram: d.notifyTelegram,
        humanConfirmScreenshot: d.screenshot,
        humanConfirmPreview: d.previewPrevOutput,
        humanConfirmSendPrevOutput: d.sendPrevOutput,
        timeout: d.timeout,
        hcOnTimeout: d.hcOnTimeout || 'wait',
        retry: 0,
        index: i,
        status: d.status,
        errorMsg: d.errorMsg,
      } as StepData
    }

    if (n.type === 'condition') {
      const d = n.data as ConditionData
      return {
        name: d.name,
        batch: '',
        workingDir: '',
        outputPath: '',
        expect: '',
        condition: true,
        // IF 跟 Switch 用同一份 model 欄位、依 d.mode 把該寫的寫進去、不該寫的留空
        expression: d.mode === 'if' ? (d.expression || '') : '',
        onTrue: d.mode === 'if' ? (d.onTrue || '') : '',
        onFalse: d.mode === 'if' ? (d.onFalse || '') : '',
        switch: d.mode === 'switch' ? (d.switch || '') : '',
        cases: d.mode === 'switch' ? (d.cases || {}) : {},
        default: d.mode === 'switch' ? (d.default || '') : '',
        timeout: 5,
        retry: 0,
        index: i,
        status: d.status,
        errorMsg: d.errorMsg,
      } as StepData
    }




    const d = n.data as StepData
    return {
      name: d.name,
      batch: d.batch,
      workingDir: d.workingDir || '',
      outputPath: d.outputPath,
      expect: d.expect || (d as any).expectText || '',
      jsonSchemaText: (d as StepData).jsonSchemaText || '',
      timeout: d.timeout,
      retry: d.retry,
      next: d.next || '',
      // 背景模式(daemon / GUI app):不等 exit、立刻下一步
      background: !!(d as { background?: boolean }).background,
      background_keep: (d as { background_keep?: boolean }).background_keep !== false,  // 預設 true(保留)
      readyAfterSeconds: (d as { readyAfterSeconds?: number }).readyAfterSeconds || 0,
      index: i,
      status: d.status,
      errorMsg: d.errorMsg,
    } as StepData
  })

  // ── 依「邊 + 最終陣列順序」重算每步的 next,確保 YAML round-trip 不掉拍 ──
  // 排序是 [最長路徑]+[分支節點],可能把「終點節點」排在分支節點前面。若不顯式標 next,
  // 重新匯入時終點會「順序 fallthrough」掉到後面的分支節點 → 多一條錯誤箭頭(merge 工作流必踩)。
  // 規則(condition 走 cases/onTrue/onFalse、不動):
  //   - 0 出邊(終點):不是陣列最後一個 → next: end;是最後一個 → 留空。
  //   - 1 出邊:目標剛好是陣列下一個 → 線性留空;否則顯式 next: <目標步驟名>。
  const nameById = new Map(execNodes.map(n => [n.id, ((n.data as { name?: string }).name) || n.id]))
  result.forEach((step, i) => {
    const node = ordered[i]
    if (node.type === 'condition') return
    const outs = adjMulti.get(node.id) || []
    if (outs.length === 0) {
      step.next = (i < result.length - 1) ? 'end' : ''
    } else if (outs.length === 1) {
      step.next = (outs[0] === ordered[i + 1]?.id) ? '' : (nameById.get(outs[0]) || '')
    }
  })
  return result
}

// ── Steps → YAML string ───────────────────────────────────────────────────────
/** 序列化 output 區塊(path + expect + skill_mode)— 所有節點型別共用。
 * (同 V5 2dd3810:hero 套用後驗證閘被靜默拔掉)。 */
function emitOutputBlock(lines: string[], s: StepData) {
  if (!(s.outputPath || s.expect || s.jsonSchemaText)) return
  lines.push(`    output:`)
  if (s.outputPath) lines.push(`      path: ${s.outputPath}`)
  if (s.expect) {
    if (s.expect.includes('\n') || s.expect.length > 80) {
      lines.push(`      expect: |`)
      for (const dl of s.expect.split('\n')) {
        lines.push(`        ${dl}`)
      }
    } else {
      lines.push(`      expect: "${s.expect.replace(/"/g, '\\"')}"`)
    }
  }
  // 輸出 JSON Schema 合約(inline JSON = 合法 YAML flow style,單行輸出)
  if (s.jsonSchemaText && s.jsonSchemaText.trim()) {
    let oneLine = s.jsonSchemaText.trim()
    try { oneLine = JSON.stringify(JSON.parse(oneLine)) } catch { oneLine = oneLine.split('\n').map(x => x.trim()).join(' ') }
    lines.push(`      json_schema: ${oneLine}`)
  }
}

export function stepsToYaml(name: string, steps: StepData[]): string {
  const lines: string[] = [
    `name: ${name || 'my-pipeline'}`,
    ``,
    `steps:`,
  ]
  for (const s of steps) {
    lines.push(`  - name: ${s.name}`)
    if (s.humanConfirm) {
      lines.push(`    human_confirm: true`)
      if (s.humanConfirmMessage) lines.push(`    message: "${s.humanConfirmMessage.replace(/"/g, '\\"')}"`)
      if (s.humanConfirmNotifyTelegram === false) lines.push(`    notify_telegram: false`)
      if (s.humanConfirmScreenshot) lines.push(`    screenshot: true`)
      if (s.humanConfirmPreview) lines.push(`    preview_prev_output: true`)
      if (s.humanConfirmSendPrevOutput) lines.push(`    send_prev_output: true`)
      if (s.timeout && s.timeout !== 3600) lines.push(`    timeout: ${s.timeout}`)
      if (s.hcOnTimeout && s.hcOnTimeout !== 'wait') lines.push(`    hc_on_timeout: ${s.hcOnTimeout}`)
      if (s.next) lines.push(`    next: ${s.next}`)
      continue
    }
    if (s.condition) {
      // Condition 節點:純 metadata、不需 batch / output / timeout
      lines.push(`    condition: true`)
      if (s.expression) lines.push(`    expression: "${s.expression.replace(/"/g, '\\"')}"`)
      if (s.onTrue) lines.push(`    on_true: ${s.onTrue}`)
      if (s.onFalse) lines.push(`    on_false: ${s.onFalse}`)
      if (s.switch) lines.push(`    switch: "${s.switch.replace(/"/g, '\\"')}"`)
      if (s.cases && Object.keys(s.cases).length > 0) {
        const inline = Object.entries(s.cases)
          .map(([k, v]) => `"${k}": ${v}`)
          .join(', ')
        lines.push(`    cases: { ${inline} }`)
      }
      if (s.default) lines.push(`    default: ${s.default}`)
      if (s.next) lines.push(`    next: ${s.next}`)
      continue
    }
    if (s.computerUse) {
      lines.push(`    computer_use: true`)
      if (s.computerUseAssetsDir) lines.push(`    assets_dir: ${s.computerUseAssetsDir}`)
      if (s.computerUseFailFast === false) lines.push(`    fail_fast: false`)
      if (s.cvThreshold !== undefined && s.cvThreshold !== 0.5) lines.push(`    cv_threshold: ${s.cvThreshold}`)
      if (s.cvSearchOnlyNear) lines.push(`    cv_search_only_near: true`)
      if (s.cvSearchRadius !== undefined && s.cvSearchRadius !== 400) lines.push(`    cv_search_radius: ${s.cvSearchRadius}`)
      if (s.cvTriggerHover === false) lines.push(`    cv_trigger_hover: false`)
      if (s.cvHoverWaitMs !== undefined && s.cvHoverWaitMs !== 200) lines.push(`    cv_hover_wait_ms: ${s.cvHoverWaitMs}`)
      // cv_coord_fallback 預設 false → 只在 true 時寫入
      if (s.cvCoordFallback === true) lines.push(`    cv_coord_fallback: true`)
      if (s.ocrThreshold !== undefined && s.ocrThreshold !== 0.6) lines.push(`    ocr_threshold: ${s.ocrThreshold}`)
      if (s.ocrCvFallback === true) lines.push(`    ocr_cv_fallback: true`)
      // UIA 模式 — 預設 pixel、空 window
      if (s.cuMode && s.cuMode !== 'pixel') lines.push(`    cu_mode: ${s.cuMode}`)
      if (s.uiaWindow) lines.push(`    uia_window: ${JSON.stringify(s.uiaWindow)}`)
      if (s.computerUseActions && s.computerUseActions.length > 0) {
        // 以 JSON 陣列寫入 actions（一行一動作，夠精簡又能 yaml parse）
        lines.push(`    actions:`)
        for (const a of s.computerUseActions) {
          // 用 flow 寫法把每個 action 壓成一行 JSON
          const compact = JSON.stringify(a)
          lines.push(`      - ${compact}`)
        }
      }
      emitOutputBlock(lines, s)
      if (Number.isFinite(Number(s.timeout)) && Number(s.timeout) > 0 && Number(s.timeout) !== 300) lines.push(`    timeout: ${Number(s.timeout)}`)
      // computer_use 一定寫 retry(即使是 0),因為 backend PipelineStep 預設 retry=1
      // 對 UI 自動化來說 retry 從動作 #1 重跑會重複點擊造成副作用,所以預期是 retry=0
      lines.push(`    retry: ${Number(s.retry) || 0}`)
      continue
    }
    if (s.workingDir) lines.push(`    working_dir: ${s.workingDir}`)
    if (s.batch) {
      if (s.batch.includes('\n') || s.batch.length > 80) {
        lines.push(`    batch: |`)
        for (const bl of s.batch.split('\n')) {
          lines.push(`      ${bl}`)
        }
      } else {
        lines.push(`    batch: ${s.batch}`)
      }
    }
    if (s.background) {
      lines.push(`    background: true`)
      lines.push(`    background_keep: ${s.background_keep === false ? 'false' : 'true'}`)  // 預設 true(保留)
      if (s.readyAfterSeconds && s.readyAfterSeconds > 0) {
        lines.push(`    ready_after_seconds: ${s.readyAfterSeconds}`)
      }
    }
    if (s.outputPath || s.expect || s.jsonSchemaText) {
      lines.push(`    output:`)
      if (s.outputPath) lines.push(`      path: ${s.outputPath}`)
      if (s.expect) {
        if (s.expect.includes('\n') || s.expect.length > 80) {
          lines.push(`      expect: |`)
          for (const dl of s.expect.split('\n')) {
            lines.push(`        ${dl}`)
          }
        } else {
          lines.push(`      expect: "${s.expect.replace(/"/g, '\\"')}"`)
        }
      }
      // output.skill_mode 只在 script 節點 + AI 驗證節點勾深度時寫；skill 節點不寫
      // 輸出 JSON Schema 合約(inline JSON = 合法 YAML flow style,單行輸出)
      if (s.jsonSchemaText && s.jsonSchemaText.trim()) {
        lines.push(`      json_schema: ${s.jsonSchemaText.trim().replace(/\n\s*/g, ' ')}`)
      }
    }
    if (Number.isFinite(Number(s.timeout)) && Number(s.timeout) > 0 && Number(s.timeout) !== 300) lines.push(`    timeout: ${Number(s.timeout)}`)
    // retry 的後端 default 是 1，只要不等於 1 都得寫出來（包含使用者明確設 0）
    if (String(s.retry ?? '') !== '' && Number.isFinite(Number(s.retry)) && Number(s.retry) !== 1) lines.push(`    retry: ${Number(s.retry)}`)
    // next 跳轉(condition 分支用、空字串 = 線性、不寫)
    if (s.next)            lines.push(`    next: ${s.next}`)
    // llm_role(預設 primary、不寫;只在 secondary 時寫)
  }
  return lines.join('\n')
}

// ── YAML string → steps ───────────────────────────────────────────────────────
export function parseYaml(raw: string): { name: string; validate: boolean; steps: StepData[] } | null {
  try {
    const lines = raw.split('\n')
    let stepIndent = 2
    for (const line of lines) {
      const m = line.match(/^(\s*)- name:/)
      if (m) { stepIndent = m[1].length; break }
    }

    let name = 'my-pipeline'
    let validate = false
    const steps: StepData[] = []
    let cur: Partial<StepData> | null = null
    let inOutput = false
    let multilineTarget: 'batch' | 'expect' | 'vv_prompt' | 'wc_cookies' | null = null
    let multilineIndent = 0
    let multilineLines: string[] = []

    const flushMultiline = () => {
      if (multilineTarget && cur && multilineLines.length > 0) {
        const text = multilineLines.join('\n').replace(/\n+$/, '')
        if (multilineTarget === 'batch') cur.batch = text
        else cur.expect = text
      }
      multilineTarget = null
      multilineLines = []
      multilineIndent = 0
    }

    for (let li = 0; li < lines.length; li++) {
      const line = lines[li]
      const t = line.trim()

      if (multilineTarget) {
        if (t === '') { multilineLines.push(''); continue }
        const leadingSpaces = line.match(/^(\s*)/)?.[1].length ?? 0
        if (leadingSpaces >= multilineIndent) {
          multilineLines.push(line.slice(multilineIndent))
          continue
        }
        flushMultiline()
      }

      if (!t || t.startsWith('#') || t === 'pipeline:' || t === 'steps:') continue

      if (/^name:/.test(t) && !cur) {
        name = t.replace(/^name:\s*/, '')
      } else if (/^validate:/.test(t) && !cur) {
        validate = /true/.test(t)
      } else if (/^- name:/.test(t)) {
        flushMultiline()
        if (cur) steps.push(buildStep(cur, steps.length))
        cur = { name: t.replace(/^-\s*name:\s*/, '') }
        inOutput = false
      } else if (/^working_dir:/.test(t) && cur) {
        cur.workingDir = t.replace(/^working_dir:\s*/, '')
        inOutput = false
      } else if (/^batch:/.test(t) && cur) {
        const val = t.replace(/^batch:\s*/, '')
        if (val === '|' || val === '>') {
          multilineTarget = 'batch'
          const nextLine = lines[li + 1]
          multilineIndent = nextLine ? (nextLine.match(/^(\s*)/)?.[1].length ?? 0) : 0
        } else {
          cur.batch = val
        }
        inOutput = false
      } else if (/^output:/.test(t) && cur) {
        inOutput = true
      } else if (/^path:/.test(t) && cur && inOutput) {
        cur.outputPath = t.replace(/^path:\s*/, '')
      } else if (/^(expect|description):/.test(t) && cur && inOutput) {
        const val = t.replace(/^(expect|description):\s*/, '').replace(/^"|"$/g, '')
        if (val === '|' || val === '>') {
          multilineTarget = 'expect'
          const nextLine = lines[li + 1]
          multilineIndent = nextLine ? (nextLine.match(/^(\s*)/)?.[1].length ?? 0) : 0
        } else {
          cur.expect = val
        }
      } else if (/^json_schema:/.test(t) && cur && inOutput) {
        // 輸出 JSON Schema 合約:inline JSON(單行)直接存;block 巢狀原樣收集(同 mcp_tool_args)
        const rawJs = t.replace(/^json_schema:\s*/, '').trim()
        if (rawJs) {
          cur.jsonSchemaText = rawJs
        } else {
          const baseIndent = line.match(/^(\s*)/)?.[1].length ?? 0
          const collected: string[] = []
          let lj = li + 1
          let minIndent = Infinity
          while (lj < lines.length) {
            const sub = lines[lj]
            if (!sub.trim()) { collected.push(''); lj++; continue }
            const subIndent = sub.match(/^(\s*)/)?.[1].length ?? 0
            if (subIndent <= baseIndent) break
            collected.push(sub)
            if (subIndent < minIndent) minIndent = subIndent
            lj++
          }
          const strip = minIndent === Infinity ? 0 : minIndent
          cur.jsonSchemaText = collected.map(cl => cl ? cl.slice(strip) : '').join('\n').replace(/\s+$/, '')
          li = lj - 1
        }
      } else if (/^ai_validation:/.test(t) && cur && inOutput) {
        // ai_validation 是後端 model 上的死欄位，這裡單純忽略；
        // 解析時不再以它觸發任何狀態（避免「YAML 寫但行為不變」的假設）
      } else if (/^human_confirm:/.test(t) && cur) {
        cur.humanConfirm = /true/.test(t)
      } else if (/^message:/.test(t) && cur) {
        cur.humanConfirmMessage = t.replace(/^message:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^notify_telegram:/.test(t) && cur) {
        cur.humanConfirmNotifyTelegram = /true/.test(t)
      } else if (/^screenshot:/.test(t) && cur) {
        cur.humanConfirmScreenshot = /true/.test(t)
      } else if (/^preview_prev_output:/.test(t) && cur) {
        cur.humanConfirmPreview = /true/.test(t)
      } else if (/^send_prev_output:/.test(t) && cur) {
        cur.humanConfirmSendPrevOutput = /true/.test(t)
      } else if (/^hc_on_timeout:/.test(t) && cur) {
        const v = t.replace(/^hc_on_timeout:\s*/, '').replace(/^"|"$/g, '').trim()
        if (v === 'wait' || v === 'pass' || v === 'reject' || v === 'abort') cur.hcOnTimeout = v
      } else if (/^background_keep:/.test(t) && cur) {
        cur.background_keep = /true/.test(t)
      } else if (/^background:/.test(t) && cur) {
        cur.background = /true/.test(t)
      } else if (/^ready_after_seconds:/.test(t) && cur) {
        cur.readyAfterSeconds = parseInt(t.replace(/^ready_after_seconds:\s*/, '')) || 0
      } else if (/^computer_use:/.test(t) && cur) {
        cur.computerUse = /true/.test(t)
      } else if (/^assets_dir:/.test(t) && cur) {
        cur.computerUseAssetsDir = t.replace(/^assets_dir:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^fail_fast:/.test(t) && cur) {
        cur.computerUseFailFast = /true/.test(t)
      } else if (/^cv_threshold:/.test(t) && cur) {
        const v = parseFloat(t.replace(/^cv_threshold:\s*/, '')); if (!isNaN(v)) cur.cvThreshold = v
      } else if (/^cv_search_only_near:/.test(t) && cur) {
        cur.cvSearchOnlyNear = /true/.test(t)
      } else if (/^cv_search_radius:/.test(t) && cur) {
        const v = parseInt(t.replace(/^cv_search_radius:\s*/, '')); if (!isNaN(v)) cur.cvSearchRadius = v
      } else if (/^cv_trigger_hover:/.test(t) && cur) {
        cur.cvTriggerHover = /true/.test(t)
      } else if (/^cv_hover_wait_ms:/.test(t) && cur) {
        const v = parseInt(t.replace(/^cv_hover_wait_ms:\s*/, '')); if (!isNaN(v)) cur.cvHoverWaitMs = v
      } else if (/^cv_coord_fallback:/.test(t) && cur) {
        cur.cvCoordFallback = /true/.test(t)
      } else if (/^ocr_threshold:/.test(t) && cur) {
        const v = parseFloat(t.replace(/^ocr_threshold:\s*/, '')); if (!isNaN(v)) cur.ocrThreshold = v
      } else if (/^ocr_cv_fallback:/.test(t) && cur) {
        cur.ocrCvFallback = /true/.test(t)
      } else if (/^cu_mode:/.test(t) && cur) {
        const v = t.replace(/^cu_mode:\s*/, '').replace(/^"|"$/g, '').trim()
        cur.cuMode = (v === 'uia' ? 'uia' : 'pixel')
      } else if (/^uia_window:/.test(t) && cur) {
        const raw = t.replace(/^uia_window:\s*/, '').trim()
        // 寫出端用 JSON.stringify → 多半帶引號,剝掉;也容忍裸字串
        try { cur.uiaWindow = JSON.parse(raw) } catch { cur.uiaWindow = raw.replace(/^"|"$/g, '') }
      } else if (/^actions:/.test(t) && cur) {
        cur.computerUseActions = []
      } else if (/^- \{/.test(t) && cur && Array.isArray(cur.computerUseActions)) {
        // actions 的 JSON 陣列項目(一行一動作)
        try {
          const obj = JSON.parse(t.replace(/^-\s*/, ''))
          cur.computerUseActions.push(obj)
        } catch { /* ignore */ }
      } else if (/^condition:/.test(t) && cur) {
        cur.condition = /true/.test(t)
      } else if (/^expression:/.test(t) && cur) {
        cur.expression = t.replace(/^expression:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^on_true:/.test(t) && cur) {
        cur.onTrue = t.replace(/^on_true:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^on_false:/.test(t) && cur) {
        cur.onFalse = t.replace(/^on_false:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^switch:/.test(t) && cur) {
        cur.switch = t.replace(/^switch:\s*/, '').replace(/^"|"$/g, '')
      } else if (/^cases:/.test(t) && cur) {
        // inline JSON-ish:cases: { "200": ok, "404": retry }
        const m = t.match(/cases:\s*\{(.+)\}\s*$/)
        if (m) {
          const cases: Record<string, string> = {}
          // 切 key/value pairs:`"200": ok, "404": retry` → entries
          for (const pair of m[1].split(',')) {
            const p = pair.trim()
            const kv = p.match(/^"?([^"]*?)"?\s*:\s*(.+)$/)
            if (kv) cases[kv[1].trim()] = kv[2].trim()
          }
          cur.cases = cases
        }
      } else if (/^default:/.test(t) && cur) {
        cur.default = t.replace(/^default:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^next:/.test(t) && cur) {
        cur.next = t.replace(/^next:\s*/, '').replace(/^"|"$/g, '').trim()
      } else if (/^timeout:/.test(t) && cur) {
        cur.timeout = parseInt(t.replace(/^timeout:\s*/, '')) || 300
        inOutput = false
      } else if (/^retry:/.test(t) && cur) {
        cur.retry = parseInt(t.replace(/^retry:\s*/, '')) || 0
        inOutput = false
      } else if (cur && t && !t.startsWith('-')) {
        // 不匹配任何 key 的行 → 追加到 batch（處理長文字被換行的情況）
        if (cur.batch && !inOutput) {
          cur.batch += ' ' + t
        } else if (cur.expect && inOutput) {
          cur.expect += ' ' + t
        }
      }
    }
    flushMultiline()
    if (cur) steps.push(buildStep(cur, steps.length))
    return { name, validate, steps }
  } catch { return null }
}

function buildStep(partial: Partial<StepData>, index: number): StepData {
  // 防呆:弱 LLM 常把「桌面自動化步驟」漏寫 `computer_use: true`、只留一個名字
  //（如「RPA操作」「自動化點擊」）→ 解析後會變成空白 script 節點、不是 computer_use。
  // 若某步「完全沒有任何類型欄位、也沒有 batch」且名字明顯是桌面自動化/RPA 意圖,
  // 就補正成空白 computer_use 節點(actions 留給使用者在畫布錄製)。
  // 對齊 system prompt「桌面自動化節點」與「啟動既有專案第 0 點」的設計。
  const _hasAnyType = !!(
    (partial.batch && partial.batch.trim())
    || partial.humanConfirm || partial.condition || partial.computerUse
  )
  if (!_hasAnyType && partial.name
      && /RPA|computer[\s_-]?use|點擊|點按|滑鼠|鍵盤|錄製|桌面自動化|UI\s?自動化|自動化操作|自動化點擊|操作工具|操作視窗|視窗操作/i.test(partial.name)) {
    partial.computerUse = true
  }
  return {
    name: partial.name ?? `步驟 ${index + 1}`,
    batch: partial.batch ?? '',
    workingDir: partial.workingDir ?? '',
    outputPath: partial.outputPath ?? '',
    expect: partial.expect ?? '',
    humanConfirm: partial.humanConfirm ?? false,
    humanConfirmMessage: partial.humanConfirmMessage ?? '',
    humanConfirmNotifyTelegram: partial.humanConfirmNotifyTelegram ?? true,
    humanConfirmScreenshot: partial.humanConfirmScreenshot ?? false,
    humanConfirmPreview: partial.humanConfirmPreview ?? false,
    humanConfirmSendPrevOutput: partial.humanConfirmSendPrevOutput ?? false,
    // computer_use 節點(稽查 F)— 跟 condition 一樣的漏洞:buildStep 原本完全沒列這些欄位,
    // parseYaml 設好 cur.computerUse=true 等、進 buildStep 後全被丟掉 → YAML round-trip 丟失桌面自動化節點。
    // 預設值對齊 newComputerUseData。
    computerUse: partial.computerUse ?? false,
    computerUseActions: partial.computerUseActions ?? [],
    computerUseAssetsDir: partial.computerUseAssetsDir ?? '',
    computerUseFailFast: partial.computerUseFailFast ?? true,
    cvThreshold: partial.cvThreshold ?? 0.5,
    cvSearchOnlyNear: partial.cvSearchOnlyNear ?? false,
    cvSearchRadius: partial.cvSearchRadius ?? 400,
    cvTriggerHover: partial.cvTriggerHover ?? true,
    cvHoverWaitMs: partial.cvHoverWaitMs ?? 200,
    cvCoordFallback: partial.cvCoordFallback ?? false,
    ocrThreshold: partial.ocrThreshold ?? 0.6,
    ocrCvFallback: partial.ocrCvFallback ?? false,
    cuMode: partial.cuMode ?? 'pixel',
    uiaWindow: partial.uiaWindow ?? '',
    // condition / 分支控制(YAML 來源:condition: true + expression+on_true/on_false 或 switch+cases)
    // 之前 buildStep 漏寫這 8 個欄位 → parseYaml 設好 cur.condition=true 等、進 buildStep 後全被丟掉、
    // stepsToYaml 一看 s.condition===undefined 就完全不寫 condition 區塊 → YAML round-trip 丟失條件節點
    condition: partial.condition ?? false,
    expression: partial.expression ?? '',
    onTrue: partial.onTrue ?? '',
    onFalse: partial.onFalse ?? '',
    switch: partial.switch ?? '',
    cases: partial.cases ?? {},
    default: partial.default ?? '',
    next: partial.next ?? '',
    // 輸出 JSON Schema 合約
    jsonSchemaText: partial.jsonSchemaText ?? '',
    // human_confirm 超時行為(wait/pass/reject/abort)
    hcOnTimeout: partial.hcOnTimeout ?? 'wait',
    timeout: partial.timeout ?? (partial.humanConfirm ? 3600 : 300),
    // YAML 沒寫 retry 時的 fallback，跟 newStepData 與後端 PipelineStep.retry 一致（都是 1）。
    retry: partial.retry ?? 1,
    index,
    status: 'idle',
    errorMsg: '',
  }
}
