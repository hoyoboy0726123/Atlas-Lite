"""環境設定。

核心功能不需要任何 API 金鑰。這裡只有 Telegram（選用）、時區、資料目錄，
以及視覺模型（選用，且可以完全跑地端）。`check_config()` 因此永遠回空清單，
不會擋啟動。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# 專案根目錄：backend/config.py → backend/ → Atlas-Lite/
_ROOT = Path(__file__).parent.parent.resolve()

# Telegram 通知（選用）。沒設 → 人工確認節點仍可用，只是不推播、要在網頁上按。
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEZONE = os.getenv("TIMEZONE", "Asia/Taipei")

# 視覺模型（選用，給 vlm_mode='description' 用）。
# 設定頁存的值優先；這裡是給無頭機器 / 不想開網頁的人用的後援。
# provider 填 ollama 就是地端，不需要金鑰、截圖不出本機。
# 三個都沒設 → 前端把「描述→OCR」反灰，其他功能完全不受影響。
VLM_PROVIDER = os.getenv("ATLASLITE_VLM_PROVIDER", "").strip().lower()
VLM_MODEL = os.getenv("ATLASLITE_VLM_MODEL", "").strip()
VLM_API_KEY = os.getenv("ATLASLITE_VLM_API_KEY", "").strip()
VLM_BASE_URL = os.getenv("ATLASLITE_VLM_BASE_URL", "").strip()

# 資料目錄。Atlas 用 repo_root/ai_output/ 同時放 DB、log、工作流產物；
# Atlas-Lite 統一收在 data/ 底下分子目錄（見下方 mkdir）。
#   data/atlas_lite.db      主資料庫
#   data/scheduler.db       APScheduler 的 job store
#   data/logs/              每次執行的 log
#   data/workflows/         工作流產物（每個工作流一個子夾）
#   data/_vlm_io/           地端定位模型的檔案交換區
# .env 設 ATLASLITE_DATA 可覆寫（相對路徑視為相對專案根目錄）。
_DATA_ENV = os.getenv("ATLASLITE_DATA", "").strip()
if _DATA_ENV:
    _p = Path(_DATA_ENV).expanduser()
    OUTPUT_BASE_PATH = _p if _p.is_absolute() else (_ROOT / _p).resolve()
else:
    OUTPUT_BASE_PATH = (_ROOT / "data").resolve()

SCHEDULER_DB_PATH = OUTPUT_BASE_PATH / "scheduler.db"
LOG_DIR = OUTPUT_BASE_PATH / "logs"
WORKFLOW_DIR = OUTPUT_BASE_PATH / "workflows"

for _d in (OUTPUT_BASE_PATH, LOG_DIR, WORKFLOW_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 使用者放自己的 Python 專案 / 腳本的標準位置。空資料夾 git 不追蹤，
# 全新 clone 不會有 → 啟動時確保建立 + 放一份說明。
EXTERNAL_PROJECTS_DIR = _ROOT / "external_projects"
EXTERNAL_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
_ext_readme = EXTERNAL_PROJECTS_DIR / "README.txt"
if not _ext_readme.exists():
    try:
        _ext_readme.write_text(
            "把你自己的 Python 專案 / 腳本放在這個資料夾底下\n"
            "（例：external_projects\\my_tool\\main.py），\n"
            "然後在腳本節點的命令欄填 python external_projects/my_tool/main.py。\n\n"
            "腳本節點跑的是系統全域 Python，不是 Atlas-Lite 後端的 venv ——\n"
            "你的專案有自己的 venv 時，在命令欄寫該 venv 的 python 絕對路徑即可。\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def check_config() -> list[str]:
    """啟動時的設定檢查。

    Atlas-Lite 沒有任何必填設定（不需要 API 金鑰），永遠回空清單。
    保留這個函式是為了讓 main.py 的啟動流程與 Atlas 一致。
    """
    return []
