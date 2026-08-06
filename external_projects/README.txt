把你自己的 Python 腳本專案放在這裡。

工作流的「Python 腳本」節點可以直接引用這裡的檔案，例如：
    batch: python external_projects/my_tool/main.py --input data.csv

注意：
- 腳本在「系統全域 Python」執行，不是 Atlas-Lite 的 venv。
  要用特定虛擬環境請在 batch 寫絕對路徑：
    batch: C:\path\to\your\.venv\Scripts\python.exe main.py
- 腳本可以透過環境變數 PIPELINE_OUTPUT_DIR 取得本次執行的輸出目錄。
- 要傳資料給下游步驟，把扁平的 JSON dict 寫到：
    %PIPELINE_OUTPUT_DIR%\_step_export.json
  下游就能用 {{ steps.<步驟名>.output.<欄位> }} 引用。
  （注意中間有 .output. —— 少了它會 render 失敗。）

  欄位名如果剛好叫 status / path / stdout / stderr / exit_code，
  它們跟步驟中繼資料同名：output.<欄位> 取到的是**你的值**，
  步驟自己的執行狀態要寫 output.step.<欄位>。

本目錄內容不進版控（見 .gitignore）。
