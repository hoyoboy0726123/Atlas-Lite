"""工作流 YAML 設定模型。

從 Atlas 的 pipeline/models.py 抽出（418 行 → 這裡剩四種節點）。
移除的節點型別：skill / subagent / visual_validation / outlook_automation /
web_crawler / mcp —— 全部依賴雲端 LLM，Atlas-Lite 不帶。

保留四種：
  script         batch 填一條命令（預設）
  computer_use   桌面自動化（錄製 / 手編動作序列）
  condition      IF / Switch 分支，用 Jinja2 求值，不碰 LLM
  human_confirm  人工確認（Telegram 通知 + 等待）

範例 YAML：
  pipeline:
    name: 每日報表
    steps:
      - name: 抓資料
        batch: python fetch.py
        timeout: 300
        output:
          path: data/raw.json
      - name: 資料夠多才繼續
        condition: true
        expression: "{{ steps.抓資料.output.rows | int > 100 }}"
        on_true: 產報表
        on_false: ""
      - name: 產報表
        batch: python report.py
"""
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


class StepOutput(BaseModel):
    """步驟輸出檔案的宣告。

    ⚠ 與 Atlas 的重大差異：`expect` **不再觸發 LLM 驗證**。
    Atlas 會把 expect 的自然語言送給 LLM 判斷「這個檔案對不對」；
    Atlas-Lite 沒有 LLM，expect 純粹是給人看的註解。
    要真正驗證輸出，用 `json_schema`（確定性、0 成本、錯在哪一欄講得清楚）。
    """
    path: Optional[str] = None
    expect: str = ""          # 人類可讀的說明，不參與驗證
    description: str = ""     # expect 的別名
    # 輸出為 .json 時的 schema 合約。不過 → 步驟直接 fail，並指出是哪個欄位錯。
    # 這是 Atlas-Lite 唯一的輸出驗證機制。
    json_schema: dict = {}

    def get_expect(self) -> str:
        return self.expect or self.description


class ComputerUseAction(BaseModel):
    """單一桌面自動化動作。

    type 決定其餘欄位的解讀方式：
      - click_image：image 指定要尋找的錨點圖（相對 assets_dir），點中心
      - click_at：x/y 絕對座標點擊（錨點失效時的備援）
      - type_text：text 輸入純文字
      - hotkey：keys 為組合鍵陣列（如 ["ctrl", "c"]）
      - wait / wait_image：靜態等待 / 等某張圖出現
      - screenshot：存一張截圖到 assets_dir（除錯用，不影響流程）
      - drag / scroll：拖曳 / 捲動
      - assert_image / assert_text：驗證錨點圖可見 / OCR 驗證螢幕上有某段文字
      - activate_window：把指定標題的視窗切到前景
      - if_image_found：條件分支 — 找到 image 跑 then:，否則跑 else:
      - retry_until：重複跑 do: 直到 until: 成功
      - uia_*：UIA 控制型動作（見 cu_mode="uia"）

    ⚠ Atlas 的 `vlm_check`（雲端 VLM 判斷畫面）在 Atlas-Lite 未實作 ——
      執行到會明確報錯，請改用 assert_image / assert_text。
    """
    # YAML 會用 else: 這個 Python 保留字當 key，靠 pydantic alias 接回 else_
    model_config = ConfigDict(populate_by_name=True)

    type: str
    image: str = ""       # 主錨點圖檔名（相對 assets_dir）
    image2: str = ""      # 次錨點圖檔名（多錨點驗證用，選填）
    dx2: int = 0          # 次錨點相對點擊點的位移 x
    dy2: int = 0
    anchor_off_x: int = 0  # 點擊位置相對錨點影像中心的偏移（螢幕邊緣擷取時非 0）
    anchor_off_y: int = 0
    # 全螢幕截圖（錄製當下的虛擬桌面全景，供手動圈選參考）
    full_image: str = ""
    full_left: int = 0    # 虛擬桌面原點 X（副螢幕在左側時會是負值）
    full_top: int = 0
    x: int = 0
    y: int = 0
    x2: int = 0           # drag 終點
    y2: int = 0
    text: str = ""
    keys: list[str] = []
    seconds: float = 0.0
    timeout_sec: float = 10.0  # wait_image 的最大等待秒數
    dy: int = 0                # scroll：滾輪缺口數（正數上、負數下）
    confidence: float = 0.5    # 圖像比對相似度門檻；實測 0.5 對 DPI / 主題色 / hover 差異容忍度好
    button: str = "left"       # left / right / middle
    clicks: int = 1            # 1=單擊, 2=雙擊
    description: str = ""      # 給 UI 顯示的動作描述

    # ── 三層 fallback toggle（預設全 True = UIA → CV → 強制座標）──────
    # 全勾（預設）給不會設定的使用者最高命中率；進階使用者可只留其中一兩層：
    #   只勾 UIA / 只勾 CV → 嚴格模式，找不到立即 fail
    #   只勾 座標         → 直接點記錄座標
    # use_ocr=True 或 vlm_mode!=off 時，OCR/VLM 自帶 primary 邏輯，這三個不適用
    use_uia: bool = True
    use_cv: bool = True
    use_coord: bool = True
    hold_sec: float = 0.0      # > 0 會在回放時 mouseDown-sleep-mouseUp 取代瞬擊
    modifiers: list[str] = []  # click 時按著的修飾鍵（如 ["ctrl","shift"]）

    # ── OCR ──────────────────────────────────────────────────────
    use_ocr: bool = False      # True 且 ocr_text 有值才跑 OCR
    ocr_text: str = ""
    # OCR 搜尋範圍（虛擬桌面絕對座標）。width=0 表示沒自訂，
    # 回退用 cv_search_radius 以錄製座標為中心的預設區域
    ocr_box_left: int = 0
    ocr_box_top: int = 0
    ocr_box_width: int = 0
    ocr_box_height: int = 0
    # True = 框內找不到立即 fail（不退附近、不退全螢幕）。
    # 用於「目標必須在固定位置才合法」的場景（例：通知必須在右下角才能點）
    ocr_strict_region: bool = False

    # activate_window 專用：至少要填 title 或 title_contains 其一
    title: str = ""
    title_contains: str = ""   # 子字串比對（大小寫不敏感）

    # CV 搜尋矩形 [left, top, width, height]（虛擬桌面絕對座標）。
    # 給定時覆蓋「錄製座標 ±cv_search_radius」的預設範圍。
    # click_image / wait_image / assert_image 都支援
    search_region: list[int] = []
    cv_strict_region: bool = False  # True = 框內找不到立即 fail

    # ── 錨點獨特性（錄製後由 analyze_anchor_uniqueness 填，純提醒用）──
    # CV 找的是「最像的」。當畫面上有好幾個長得一樣的東西（視窗標題列那三顆
    # 按鈕、表格裡重複的圖示），回放時只要真目標跑掉一點，就可能挑中替身
    # 點下去 —— 實測假匹配分數可以到 0.95 以上，調門檻擋不住。
    #
    # ⚠ 這兩個欄位**不影響執行行為**，只給面板顯示警告用。
    #   試過依它自動鎖搜尋範圍，實測不可行（錄製當下的替身分佈預測不了
    #   回放當下的）。要防就請使用者自己勾 cv_search_only_near 或畫紅框。
    anchor_rivals: int = 0             # 除了真目標，錄製畫面上還有幾個相似位置
    anchor_nearest_rival_px: int = 0   # 最近的替身離目標多遠

    # ── 控制流巢狀動作（if_image_found / retry_until）───────────────
    # 刻意保留為 list[dict]，不做遞迴 pydantic 驗證 —— execute_action 收的就是
    # dict，巢狀動作在執行時才逐一 .get()。避免 pydantic 自我遞迴的 model_rebuild 麻煩。
    then: list[dict] = []
    else_: list[dict] = Field(default_factory=list, alias="else")
    do: list[dict] = []                # retry_until：要反覆執行的動作
    until: Optional[dict] = None       # retry_until：檢查條件
    max_attempts: int = 3
    wait_between_sec: float = 1.0

    # ── UIA action 專用（uia_click / uia_send_keys / uia_get_text 等）──
    # 上面 use_uia 那組是「錄製時自動抓的元素資訊」；這組是進階使用者
    # 透過 UIA Inspector 手動設定的，兩者是兩回事。
    ui: dict = {}                # 錄製時抓的 {"name","control_type","automation_id","window_title","rect"}
    # ── ocr_get_text 專用欄位(螢幕 OCR 抓「標籤旁邊的值」)──
    # ⚠ 這些欄位一定要宣告。ComputerUseAction 沒設 extra="allow",pydantic v2 預設
    #   extra="ignore" —— 未宣告的欄位在 runner 的 model_dump() 會**靜默消失**,
    #   面板試抓正常、YAML 存得下去,正式跑卻回「缺 label 欄位」;更糟的是
    #   direction / kind 掉光會降回預設值,讀到旁邊完全不同的數字還回報成功。
    label: str = ""              # 要找的標籤文字(例:總計金額)
    direction: str = "right"     # right 同列右側 / below 表格欄位在下方
    kind: str = "amount"         # amount 金額 / ident 單號 / taxid 統編(驗檢查碼) / any
    max_gap: int = 600           # 標籤與值的最大距離(px)
    lang_tag: str = ""           # OCR 語言標籤、留空走預設 zh-Hant-TW
    type_method: str = ""        # type_text: clipboard(預設、IME 免疫)/ keys(逐字打)

    control: dict = {}           # {"type":"Button","name":"儲存","auto_id":"save-btn","depth":10}
    save_as: str = ""            # uia_get_text 等把值存到此變數名，後續 step 可用 {{...}}
    row: int | str = 0           # uia_click_cell 用，可填 "{{row_count + 1}}" 延後解析
    column: int | str = 0
    check: str = ""              # uia_assert_state：exists / enabled / focused / checked
    window: str = ""             # action 層級 window 覆寫，支援 wildcard *
    rect: list[int] = []         # UIA picker 抓到的 [x,y,w,h]；control 沒 Name 也沒
                                 # AutomationId 時用 ControlFromPoint(rect 中心) 當 fallback

    # ── click_image 的 VLM 輔助模式 ────────────────────────────────
    #   "off"       → 不啟用（預設，走 UIA / CV / OCR / 座標）
    #   "grounding" → 地端 GUI 定位模型（Mano-CUA 系）直接回座標並點擊。
    #                 2026-08-03 實測 14/14 命中、誤差中位數 4.5px（網頁 /
    #                 Excel ribbon / 檔案總管都涵蓋）。失敗自動退回 CV → 座標。
    #                 注意：同一顆模型「讀畫面文字」會編造，所以只拿它定位、不讀字。
    #                 需要 plugins/vlm_grounding 外掛 + NVIDIA GPU。
    #   "description" → 視覺模型看圖回「目標實際顯示的文字」→ 交給 OCR 定位。
    #                 給「文字是動態的、錄製時不知道會是什麼字」的場景用
    #                 （訂單編號、當日日期、使用者名稱）。模型不碰座標，
    #                 所以不會出現「指到隔壁那顆」的問題。
    #                 需要在設定頁指定視覺模型（Ollama 地端 or 雲端 API key），
    #                 沒設定時前端反灰、執行到會明確報錯。
    # Atlas 另有 "anchor_pick"（雲端模型從多張候選錨點裡挑一張），Atlas-Lite 改用
    # 下面的 image_variants 純 CV 取代 —— 實測「最大化↔還原」兩態互測
    # 1.000 vs 0.707、0px 命中，比丟給模型判斷又快又準，而且免金鑰。
    vlm_mode: str = "off"
    vlm_prompt: str = ""         # grounding / description 模式：目標描述
    vlm_anchors: list[str] = []  # [僅相容用] anchor_pick 的候選錨點清單

    # ── 多形態錨點（純 CV，取代 anchor_pick）───────────────────────
    # 同一顆按鈕會隨狀態換樣子時（最大化↔還原、播放↔暫停、亮↔暗主題），
    # 錄製時把每種樣子各存一張，執行時每張都比一次、取分數最高的那張定位。
    # 空 = 只用主錨點 image。檔名相對 assets_dir，跟 image 同一個資料夾。
    image_variants: list[str] = []


class PipelineStep(BaseModel):
    name: str
    batch: str = ""        # Shell 命令
    working_dir: str = ""  # 命令的 cwd
    timeout: int = 300     # 秒
    output: Optional[StepOutput] = None
    retry: int = 1         # 自動重試次數

    # ── 人工確認節點 ───────────────────────────────────────────────
    human_confirm: bool = False
    message: str = ""             # 自訂訊息
    notify_telegram: bool = True
    screenshot: bool = False      # 暫停前自動截圖，附到 Telegram
    send_prev_output: bool = False  # 抵達時自動把上一步輸出傳到 Telegram
                                    # False = 不自動傳，但 inline keyboard 仍有「📎 上一步輸出」按鈕
    # 超時自動行動（超時秒數沿用 step.timeout）：
    # wait（預設，永遠等）/ pass（當作通過）/ reject（跳回上一步）/ abort（終止）
    hc_on_timeout: str = "wait"

    # ── 背景 step（daemon / GUI app）────────────────────────────────
    # True = 啟動 subprocess 後不等它 exit、直接跑下一個 step。
    # 用途：腳本開了一個永不結束的 GUI / server，但後續 step 需要它活著
    #       （例如接下來要用 computer_use 點那個視窗）。
    background: bool = False
    ready_after_seconds: int = 0   # 啟動後等 N 秒讓它 ready
    # workflow 正常結束時是否「保留」行程。預設 True = 留在桌面供手動操作。
    # 手動中止一律強制 kill，不受此旗標影響。
    background_keep: bool = True

    # ── 控制流：顯式跳轉 ───────────────────────────────────────────
    # 一般 step 跑完依 YAML 順序前進；設了 next 就跳到指定 step name。
    # 特殊值 "end" / "__end__" → 結束流程。主要用途是讓 condition 的兩個分支
    # 各自跑完後跳到 end，避免線性掉進對方的 branch。
    next: str = ""

    # ── 控制流節點：condition（IF / Switch）───────────────────────
    # 純 metadata 節點，不執行任何命令。runner 用 Jinja2 求值（不是 eval、
    # 不是 LLM），依結果跳到指定的下游 step。MAX_VISITS=1000 防無限迴圈。
    #
    # IF 模式：expression + on_true / on_false
    #   expression: "{{ steps.X.output.rows | int > 100 }}"
    # Switch 模式：switch + cases（忽略 expression）
    #   switch: "{{ steps.api.output.status }}"
    #   cases: { "200": ok_step, "404": retry_step }
    # 註：output.<欄位> 撞名時（status / path / stdout / stderr / exit_code）
    #     以「你輸出的值」為準；要取步驟自己的執行狀態請寫 output.step.status。
    condition: bool = False
    expression: str = ""
    on_true: str = ""
    on_false: str = ""     # 留空 = 流程結束
    switch: str = ""
    cases: dict = {}
    default: str = ""      # 沒命中任何 case 時跳的 step（留空 = 結束）

    # ── 桌面自動化節點 ─────────────────────────────────────────────
    computer_use: bool = False
    cu_mode: str = "pixel"   # "pixel" = 錄製座標 + CV/OCR/VLM（預設）
                             # "uia"   = UIA 控制；兩種模式共用 actions[]，
                             #           實際分派依 action.type 走
    uia_window: str = ""     # cu_mode=uia 時的視窗 title 比對（支援 wildcard *），空 = foreground
    actions: list[ComputerUseAction] = []
    assets_dir: str = ""     # 錨點圖片資料夾（相對工作流目錄）
    fail_fast: bool = True   # True = 任一動作失敗立即中止

    # ── CV 比對設定（套用到本節點所有 click_image / drag）────────────
    cv_threshold: float = 0.5      # 0.50 寬鬆 / 0.80 標準 / 0.90 嚴格
    cv_search_only_near: bool = False  # True = 只在錄製座標附近搜尋
    cv_search_radius: int = 400    # 附近搜尋半徑（px）；實際範圍 (2r × 2r)
    cv_trigger_hover: bool = True  # 比對前先把游標移到錄製座標，讓 Windows hover 效果出現
    cv_hover_wait_ms: int = 200    # 200（快）/ 400（保險，部分動畫較慢）
    cv_coord_fallback: bool = False  # True = CV 找不到時退回錄製座標硬點；False（預設）= FAIL 不亂點

    # ── OCR 比對設定 ───────────────────────────────────────────────
    ocr_threshold: float = 0.6     # 低於此 confidence 視為沒匹配到
                                   # 1.0 精確 / 0.9 target⊆word / 0.8 跨詞行層級 / 0.6 模糊
    ocr_cv_fallback: bool = False  # True = OCR 失敗時退到 CV 比對鏈


class PipelineConfig(BaseModel):
    name: str
    steps: list[PipelineStep]

    @classmethod
    def from_yaml(cls, path: str) -> "PipelineConfig":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        # 支援頂層有 "pipeline:" 或直接是 {name, steps}
        raw = data.get("pipeline", data)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, data: dict) -> "PipelineConfig":
        """從 dict 建立設定。會擋掉 Atlas 才有的 LLM 節點型別。

        Atlas 匯出的 YAML 會帶一堆 Atlas-Lite 沒有的欄位（skill_mode、
        subagent、mcp_*…）。這裡的策略：
          - 旗標**是開的** → 直接拋錯，講清楚是哪個 step 的哪種節點不支援。
            靜默丟掉會讓一個 skill 節點退化成「空命令的腳本節點」，
            跑起來 exit 0 什麼都沒做 —— 那比載入失敗糟糕得多。
          - 旗標是關的 / 純粹是多餘欄位 → 安靜濾掉，不擋使用者匯入。
        """
        # 旗標名 → 給使用者看的說明
        UNSUPPORTED = {
            "skill_mode": "LLM Skill 節點",
            "subagent": "Subagent 節點",
            "visual_validation": "視覺驗證節點",
            "outlook_automation": "Outlook 自動化節點",
            "web_crawler": "網頁爬蟲節點",
            "mcp": "MCP 節點",
        }
        known = set(PipelineStep.model_fields) | {"else"}
        cleaned = dict(data)
        steps = cleaned.get("steps")
        if isinstance(steps, list):
            new_steps = []
            for i, s in enumerate(steps):
                if not isinstance(s, dict):
                    new_steps.append(s)
                    continue
                for flag, label in UNSUPPORTED.items():
                    if s.get(flag):
                        raise ValueError(
                            f"步驟「{s.get('name') or f'#{i + 1}'}」是{label}，"
                            f"Atlas-Lite 不支援（它需要雲端 LLM）。"
                            f"請改用腳本節點、桌面自動化節點、condition 或人工確認節點。"
                        )
                new_steps.append({k: v for k, v in s.items() if k in known})
            cleaned["steps"] = new_steps
        filtered = {k: v for k, v in cleaned.items()
                    if not k.startswith("_") and k in cls.model_fields}
        return cls(**filtered)
