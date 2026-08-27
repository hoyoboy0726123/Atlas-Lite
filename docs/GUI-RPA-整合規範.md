# GUI 工具與 RPA（Atlas）整合規範

> 給「製作 GUI 工具」的開發者或 AI agent。
> 這個 GUI 會被 RPA 工具（UIA + 影像辨識 + 剪貼簿）自動操作，
> 請遵守以下規格，讓自動化穩定可靠。

## 背景：為什麼需要這份規範

Tkinter 沒有實作 UIA provider——RPA 從系統無障礙介面**讀不到** Tk 視窗裡的
任何欄位內容（整個視窗只是一塊空白 Pane）。因此資料交接走**剪貼簿**、
按鈕定位走**截圖比對（CV）**。這兩條路各有一個配合條件：

1. 剪貼簿交接 → 需要一顆「複製清單」按鈕
2. 截圖定位 → 需要視窗與按鈕的位置、外觀、文字固定

---

## 規格 1：「📋 複製清單」按鈕（RPA 的資料出口）

- 按下後把**已勾選**的項目寫入系統剪貼簿，**純文字、一行一筆**，
  不要加編號、逗號、引號或其他裝飾
- Tkinter 務必用以下寫法，缺 `update()` 的話剪貼簿內容
  **不會真正釋出給其他程式**（視窗關閉前別的程式都讀不到）：

```python
root.clipboard_clear()
root.clipboard_append("\n".join(selected_items))
root.update()   # ★ 必要
```

- 複製後在介面顯示回饋文字（例：「已複製 3 筆到剪貼簿」），
  RPA 不讀這段文字，但人要看得到有沒有成功
- 一筆都沒勾時**不要寫入空字串**，顯示提示即可
  （RPA 讀到空剪貼簿會判定為錯誤並停下——這是預期的防呆行為，
  空清單通常代表上游有異常，不該默默跑完）

## 規格 2：視窗與按鈕的位置、文字固定（RPA 的定位依據）

- 啟動時**固定視窗位置與大小**：

```python
root.geometry("420x560+60+60")   # 寬x高+X+Y,數字自訂但要固定
```

  不要記憶上次位置、不要依解析度置中（不同螢幕會算出不同座標）
- **視窗標題固定不變**（不要帶日期、版本號、筆數）——RPA 靠標題找視窗
- 「複製清單」按鈕放**頂部、橫跨整寬、高度至少兩行**，
  按鈕文字固定不變——RPA 靠按鈕的截圖找它，
  位置、配色、文字改版了 RPA 就找不到，要改請通知 RPA 維護者重錄
- **不要用 `messagebox` 或彈出視窗**顯示回饋——會蓋住畫面、
  需要人手動按掉，RPA 會被擋住。回饋一律用介面內的 Label

## 規格 3（選配、更進階）：直接呼叫 Atlas API

如果工具端可以發 HTTP 請求，可以加一顆「🚀 開始查詢」按鈕直接觸發工作流，
**連剪貼簿和 RPA 點擊都省掉**，清單直接進工作流：

```python
import urllib.request, json

def start_query():
    sel = [e.get().strip() for v, e in rows if v.get()]
    if not sel:
        status.config(text="請先勾選項目", fg="#c00"); return
    body = json.dumps({
        "workflow_id": "wf-xxxxxxxxxxxx",        # 目標工作流 id
        "yaml_content": None,                     # 用已存的 yaml 就留 None(依 API 版本)
        "input_params": {"品規清單": "\n".join(sel)},
    }).encode()
    req = urllib.request.Request("http://127.0.0.1:8020/pipeline/run",
                                 data=body, headers={"Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req).read())
    status.config(text=f"已啟動查詢({len(sel)} 筆) run={r['run_id'][:8]}", fg="#080")
```

工作流端把 `for_each` 的 `items` 改成 `{{ input.品規清單 }}` 即可。

---

## 完整最小範例（Tkinter）

```python
import tkinter as tk

root = tk.Tk()
root.title("品規清單產出工具")          # 標題固定
root.geometry("420x560+60+60")          # 位置大小固定

items = ["UM3405%", "UX3405%", "UX3407%", "UX3480%"]
rows = []

def copy_selected():
    sel = [e.get().strip() for v, e in rows if v.get() and e.get().strip()]
    if not sel:
        status.config(text="沒有勾選任何項目", fg="#c00")
        return
    root.clipboard_clear()
    root.clipboard_append("\n".join(sel))
    root.update()                        # ★ 必要
    status.config(text=f"已複製 {len(sel)} 筆到剪貼簿", fg="#080")

btn = tk.Button(root, text="📋 複製勾選清單", height=2, command=copy_selected)
btn.pack(fill="x", padx=10, pady=(10, 6))   # 頂部、整寬、夠高

for it in items:
    f = tk.Frame(root); f.pack(fill="x", padx=12, pady=1)
    v = tk.BooleanVar()
    tk.Checkbutton(f, variable=v).pack(side="left")
    e = tk.Entry(f, width=26); e.insert(0, it); e.pack(side="left")
    rows.append((v, e))

status = tk.Label(root, text="勾選後按上方按鈕複製", fg="#666")
status.pack(pady=8)
root.mainloop()
```

## 驗收清單

- [ ] 按「複製清單」後，開記事本 Ctrl+V 能貼出一行一筆的勾選項目
- [ ] 沒勾任何項目時按複製：剪貼簿內容**不變**、介面顯示提示
- [ ] 關掉工具重開，視窗出現在同一個位置、同樣大小
- [ ] 視窗標題與按鈕文字和上一版完全相同
- [ ] 全程沒有任何彈出視窗（messagebox）
