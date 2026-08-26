"""AI 助手的按需知識庫。

## 為什麼不寫進系統提示
系統提示每輪都吃 token；節點細節（欄位名、語法、組合範例）只有在使用者
問到該主題時才需要。所以提示裡只放一句「遇到 X 主題先 read_help_doc(X)」，
細節放這裡按需載入 —— 跟 Atlas 的 help_docs 同一套哲學，內容裁成 Lite
實際有的功能（沒有 LLM 節點、沒有 subagent、沒有 MCP/Outlook）。

## 寫作準則
每個主題 ≤ 2KB。寫「怎麼組出能跑的設定」，不寫行銷句。
欄位名必須跟 engine/models.py 一字不差 —— 模型會照抄進 YAML。
"""
from __future__ import annotations

_DOCS: dict[str, str] = {}

_DOCS["script"] = """# script 節點（Python / Shell 命令）

步驟只要有 `batch` 就是 script 節點。欄位：

```yaml
- name: 跑報表
  batch: "C:\\proj\\venv\\Scripts\\python.exe" main.py --date {{ input.date }}
  working_dir: C:\\proj          # 命令的 cwd（相對路徑都以它為準）
  timeout: 300                   # 秒
  retry: 1
  output:
    path: ai_output/報表.xlsx    # 宣告產出檔 → 下游用 {{ steps.跑報表.output.path }}
    json_schema: '{"type":"object","required":["rows"]}'   # 選填：確定性驗證 JSON 產出
```

要點：
- **腳本跑在系統 Python，不是後端的 venv** —— 依賴要自己裝在使用者環境
  （或用專案自己的 venv python 絕對路徑當前綴，見 project 主題）。
- `batch` 支援 `{{ }}` 變數（跨節點 steps.X.output.Y、secrets.名稱）。
- `.bat` 檔可直接當 batch 跑：`batch: "C:\\proj\\start.bat"`（配 working_dir）。
- output.path 若是 .json，欄位會自動攤平到 output.<key> 給下游條件用。
- `expect` 在 Lite **只是註解**（沒有 LLM 驗證）；要驗證用 json_schema。

## 背景服務（啟動後不等它結束）
```yaml
  background: true             # 不等結束、直接跑下一步
  ready_after_seconds: 5       # 啟動後等 N 秒當作 ready
  background_keep: true        # 工作流結束後保留行程（false = 順手收掉）
```
用途：先起一個本機伺服器 / GUI，後續 computer_use 步驟去操作它。
"""

_DOCS["project"] = """# 幫使用者接上既有的 Python 專案（SOP）

使用者貼專案路徑（「我的專案在 C:\\...」「幫我啟動我的程式」）時，照順序做：

1. **inspect_project(路徑)** —— 不要憑空猜。回傳：
   - venv.has_venv / venv.python_path：有 venv 就把 python_path 當 batch 前綴
   - entry_candidates：main.py / app.py / run.py / start.bat 等入口候選
   - dependency_files、top_level_tree
2. **read_project_file** 讀入口檔（看 argparse / input() / 常數設定）與 README，
   判斷怎麼跑、需要哪些參數。**這一步不可跳過** —— 參數格式（--date 這種
   flag 名、位置參數順序）猜錯，命令執行就直接炸。
3. 跟使用者確認入口與參數（一句話即可），然後用 patch_step_fields 組節點：

```yaml
# 有 venv：
batch: "C:\\proj\\venv\\Scripts\\python.exe" main.py
working_dir: C:\\proj
# 沒 venv：batch 用 python 開頭，並提醒「依賴可能缺、建議先 pip install -r requirements.txt」
# 有 .bat：直接 batch: "C:\\proj\\start.bat"（.bat 內容可先 read_project_file 看一眼）
```

- GUI / 伺服器類 → background: true + ready_after_seconds，後面接 computer_use 操作它。
- 會寫輸出檔的 → 補 output.path，下游才能引用。
- ⚠ .env / 金鑰檔讀不到（工具會拒讀）——不要嘗試、也不要叫使用者貼內容。
"""

_DOCS["condition"] = """# condition 節點（條件分支）

步驟有 `condition: true` 就是分支節點。兩種模式：

## 二元分支（expression）
```yaml
- name: 檢查筆數
  condition: true
  expression: "{{ steps.跑報表.output.rows | int > 100 }}"
  on_true: 發通知        # 成立跳去的步驟名
  on_false: 人工確認     # 不成立跳去的（留空 = 流程結束）
```

## 多路分支（switch / cases）
```yaml
- name: 依狀態分流
  condition: true
  switch: "{{ steps.api.output.status }}"
  cases:
    "200": 繼續處理
    "404": 重抓一次
  default: 人工確認      # 沒命中任何 case（留空 = 結束）
```

要點：
- expression 是 Jinja：`|int`、`|float`、`in`、`and/or` 都可用。
  例：`{{ "錯誤" in steps.檢查.output.stdout }}`。
- on_true/on_false/cases 的值/default **必須是存在的步驟名** —— 打錯字執行時才炸，
  用 patch_step_fields 改會先驗證。
- 一般步驟也有 `next:` 可以無條件跳轉（做迴圈要小心別造成死循環）。
"""

_DOCS["human_confirm"] = """# human_confirm 節點（人工確認）

步驟有 `human_confirm: true` 就會暫停等人。欄位：

```yaml
- name: 送出前確認
  human_confirm: true
  message: 請確認金額無誤再繼續     # 顯示給使用者的訊息（支援 {{ }} 變數）
  notify_telegram: true             # 推播到 Telegram（沒設 bot 就只在網頁上等）
  screenshot: true                  # 暫停前截圖附到 Telegram（桌面操作前後很有用）
  send_prev_output: false           # 把上一步的輸出檔一併傳到 Telegram
  hc_on_timeout: wait               # wait = 一直等；skip / abort = 超時後自動處理
```

典型用法：
- **放在危險動作前**：填表 → 人工確認（附截圖）→ 按送出。
- 使用者在 Telegram / 網頁按「繼續 / 跳過 / 中止」決定走向。
- 跟 condition 組合：人工確認只擋「條件判斷有疑慮」的分支，正常路直接過。
"""

_DOCS["computer_use"] = """# computer_use 節點（桌面自動化）補充

動作序列的細節（動作型別、ops 格式）在 patch_node_actions 的說明裡。這裡是節點層設定：

```yaml
- name: 操作表單
  computer_use: true
  cu_mode: uia                 # uia = 讀 GUI 結構（推薦）；pixel = 錄製座標 + CV/OCR
  uia_window: "*BK簽呈*"       # 目標視窗（支援 * 萬用；動作可用自己的 window 覆蓋）
  fail_fast: true              # 任一動作失敗立即中止
  actions: [ ... ]
```

要點：
- **視窗 pattern 用 `*關鍵字*`**，別綁完整標題 —— Edge 標題含「和其他 N 個頁面」，
  分頁數一變就找不到。
- 跨視窗流程：各動作自帶 window（讀 A 視窗、填 B 視窗）。
- 讀值選「裝著值的元素」（Edit），不是標籤 Text；填值走 ValuePattern 不經輸入法。
- 查詢/匯出時間不固定時用 `uia_wait` 等畫面狀態，不要寫死 wait 秒數：
  `{type: uia_wait, control: {auto_id: loading}, until: disappear, timeout_sec: 120}`（等「查詢中」遮罩消失）
  `{type: uia_wait, control: {auto_id: result}, until: text_contains, text: "已匯出", timeout_sec: 120}`
  until 可用 appear / disappear / text_contains / text_equals；逾時誠實失敗。
  遮罩常沒有 auto_id、只有「資料處理中...」文字 —— control 的 name 支援
  萬用字元:`control: {type: Text, name: "資料處理中*"}`。
  ⚠ 只等 disappear 有競態:按下按鈕後遮罩還沒 render 就檢查、會誤判「已消失」。
  保險寫法是兩段:先 `until: appear`(短逾時)等它出現、再 `until: disappear` 等它消失；
  或改等結果元素 `text_contains` 關鍵字。
- 匯出後「可能跳對話框、可能直接下載」的分歧用 `if_element_found`（UIA 版條件分支）
  搭配 `wait_download`（等下載資料夾出現寫完的新檔案）：
  ```yaml
  - {type: uia_click, control: {name: "匯出/Export"}}
  - type: if_element_found          # 「查無資料」對話框有出現嗎？
    control: {type: Button, name: "確定"}
    timeout_sec: 5
    then:                            # 出現 → 按確定、繼續下一筆
      - {type: uia_click, control: {type: Button, name: "確定"}}
    else:                            # 沒出現 → 等下載完成
      - {type: wait_download, pattern: "PP_Component*.xlsx", timeout_sec: 300, save_as: 下載檔}
  ```
  wait_download 只認「動作開始後新出現、且寫完」的檔案（排除 .crdownload 半成品、
  大小穩定才算完成）；save_as 存完整路徑。dir 空值 = 使用者的 Downloads 資料夾。
- 下拉選單用 `uia_select` 動作（text = 選項文字，支援 {{變數}}）：
  `{type: uia_select, control: {type: ComboBoxControl, auto_id: month}, text: "{{ now.month }}"}`
  背景 pattern 選取 + 回讀驗證，選項文字要一字不差（08 不是 8）。
- 點擊三層 fallback：UIA（有 name/auto_id 才可能中）→ CV 錨點圖 → 錄製座標。
  匿名元素（無 name 無 auto_id）純 UIA 必定失敗，要保留 CV 層。
"""

_DOCS["variables"] = """# 變數與傳值

## 同一個節點內
取值動作設 `save_as: 金額` → 之後的動作用 `{{金額}}`。
取值動作必須排在使用它的動作**之前**。

## 跨節點
`{{ steps.<步驟名>.output.<變數名> }}` —— 步驟名一字不差（改名會全斷）。
可用來源：
- computer_use 的 save_as 變數
- script 節點的 output.path、stdout；JSON 輸出檔的欄位會攤平成 output.<key>
- `{{ steps.X.output.status }}`（ok / failed）

## 其他命名空間
- `{{ secrets.名稱 }}`：加密保險箱（設定頁 Secrets），值不會出現在 log。
- `{{ input.名稱 }}`：啟動工作流時傳入的參數 —— 「這次選 08、下次選 09」
  就是每次啟動帶不同 input。
- `{{ now.* }}`：當前日期（排程報表選當月用）。全部是補零字串，
  下拉選單的選項文字直接對得上：
  `now.year`(2026)、`now.month`(08)、`now.day`、`now.date`(2026-08-24)、
  `now.prev_month`(07)、`now.next_month`(09)、`now.prev_month_year` / `next_month_year`
  （跨年時給對應年份）。

## 常見錯誤（工具會擋，但要知道為什麼）
- 變數名只能中英數與底線：`40,425` 這種「值當名字」替換不了。
- 單層大括號 `{金額}` 無效，會被字面填進欄位。
- 同節點內不要用跨節點語法（該步驟還沒跑完、output 不存在）。
"""

_DOCS["patterns"] = """# 綜合應用範例（節點怎麼接）

## 範例：讀值 → 人工確認 → 分支 → 填值
```yaml
name: 憑證核銷
steps:
  - name: 讀憑證
    computer_use: true
    cu_mode: uia
    actions:
      - {type: uia_get_text, control: {type: EditControl, auto_id: total},
         window: "*BK簽呈*", save_as: 金額}

  - name: 金額確認
    human_confirm: true
    message: "讀到金額 {{ steps.讀憑證.output.金額 }}，正確嗎？"
    screenshot: true

  - name: 檢查金額
    condition: true
    expression: "{{ steps.讀憑證.output.金額 | int > 0 }}"
    on_true: 填進系統
    on_false: 金額異常通知

  - name: 填進系統
    computer_use: true
    cu_mode: uia
    actions:
      - {type: uia_send_keys, control: {type: EditControl, auto_id: amount},
         window: "*目標系統*", text: "{{ steps.讀憑證.output.金額 }}"}

  - name: 金額異常通知
    human_confirm: true
    message: "金額讀到 0 或讀取失敗，請人工處理"
```

## 範例：啟動既有專案 → 操作它
```yaml
  - name: 起服務
    batch: "C:\\proj\\venv\\Scripts\\python.exe" app.py
    working_dir: C:\\proj
    background: true
    ready_after_seconds: 5
  - name: 操作介面
    computer_use: true
    ...
```

要點：分支目標、跳轉（next / on_true / on_false / cases / default）都要指向
**存在的步驟名**；線性流程不用寫 next（照順序跑）。
"""

TOPICS = {
    "script": "script 節點：batch / working_dir / output / 背景服務",
    "project": "接上既有 Python 專案的 SOP（venv 偵測、入口、bat）",
    "condition": "條件分支：expression / on_true / switch / cases",
    "human_confirm": "人工確認：message / Telegram / 截圖 / 超時行為",
    "computer_use": "桌面自動化節點層設定：模式 / 視窗 pattern / 三層 fallback",
    "variables": "變數與傳值：同節點 / 跨節點 / secrets / input",
    "patterns": "綜合應用範例：多節點串接的完整 YAML",
}


def get_help_doc(topic: str) -> str:
    """取主題文件。空字串或不認得的主題 → 列出可選清單。"""
    key = (topic or "").strip().lower()
    if key in _DOCS:
        return _DOCS[key]
    lines = ["可用的主題（read_help_doc(topic) 讀取）："]
    for k, desc in TOPICS.items():
        lines.append(f"- {k}: {desc}")
    if key:
        lines.insert(0, f"沒有「{topic}」這個主題。")
    return "\n".join(lines)
