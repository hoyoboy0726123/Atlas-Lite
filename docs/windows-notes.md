# Windows 開發備忘

`.gitattributes` 引用了本檔。這些是實測踩過的坑,不是文件轉述。

## `.bat` 必須純 ASCII,不可內嵌中文

`cmd.exe` 是**依系統 ANSI 代碼頁逐位元組解析**批次檔。英文 locale 的系統
代碼頁是 1252,表達不了中文 —— `.bat` 裡只要出現中文,該行就會被打爛成
無效指令。

- 症狀極具誤導性:`'XXX' is not recognized as an internal or external command`,
  看起來像少裝東西,實際是該行被代碼頁截斷。
- **`chcp 65001` 救不了** —— 它只改主控台輸出編碼,不改 `.bat` 本身的解析。
- 換行符不是原因:純 LF 的 `.bat` 可以正常執行,但本 repo 仍統一 CRLF
  (`.gitattributes` 強制),避免部分編輯器顯示異常。

**寫法規則**(`launch.bat` 遵守這些):

1. repo 內路徑一律用 `%~dp0` 相對路徑
2. 提示訊息全部用英文
3. 要執行會印中文的 Python 腳本時,不要繼承空的 `PYTHONUTF8`
   (空值會讓 Python 直接 fatal;`launch.bat` 啟動 uvicorn 前明確清掉它)

## `NoDefaultCurrentDirectoryInExePath`

部分環境設有 `NoDefaultCurrentDirectoryInExePath=1`,此時 `cmd /c start.bat`
不會從當前目錄找檔案 —— 要用 `.\start.bat` 或絕對路徑。

## Python 版本

pydantic 2.9.x 沒有 Python 3.14 的 wheel,裸 `python` 若是 3.14 會退回從
Rust 原始碼編譯 pydantic-core 然後噴一整面 cargo 錯誤。`launch.bat` 依序
嘗試 `py -3.13` → `py -3.12` → `py -3.11`,都沒有才退回 `python`。
