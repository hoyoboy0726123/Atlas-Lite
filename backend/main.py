"""Atlas-Lite 後端。

啟動：uvicorn main:app --host 127.0.0.1 --port 8020
（前端 next.config.mjs 與 launch.bat 都指向 8020，改的話要一起改。）

路由本身全在 api/ 底下，這支只負責：DPI awareness、app 組裝、啟動 / 關閉。
Atlas 的 main.py 有 6981 行 —— 那是把所有端點都塞在一個檔案的結果。
"""
import sys

# Windows console 預設 cp950 印不出 emoji / 中文 → 啟動時強制 UTF-8。
# 不靠 PYTHONIOENCODING 環境變數，避免使用者沒設或 .bat 傳遞失效。
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── DPI awareness（Windows）─────────────────────────────────────────
# 必須在 import mss / pyautogui / 任何螢幕相關模組**之前**呼叫，
# 否則那些模組會 cache 住 DPI-unaware 的螢幕 metric，之後改不過來。
#
# 為什麼非做不可：DPI-unaware 的行程在高 DPI 螢幕上，Windows 會回「邏輯」
# 像素而不是實際像素。不同 scaling 的機器之間邏輯像素不一致 —— 同一組 (x, y)
# 在 150% 與 125% 的機器上對應到完全不同的實體位置，跨機器搬工作流會整個錯位。
#
# 三層 fallback：
#   PROCESS_PER_MONITOR_DPI_AWARE_V2 (-4)  Win10 1703+
#   PROCESS_PER_MONITOR_DPI_AWARE    (2)   Win8.1+
#   SetProcessDPIAware()                   Vista+
if sys.platform == "win32":
    try:
        import ctypes

        def _set_dpi_awareness() -> bool:
            user32 = ctypes.windll.user32
            # SetProcessDpiAwarenessContext 收的是 HANDLE（指標），一定要走
            # c_void_p —— ctypes 預設 c_int，傳整數會 silently fail。
            if hasattr(user32, "SetProcessDpiAwarenessContext"):
                user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
                for ctx in (-4, -3):   # v2 不支援時退 v1
                    if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(ctx)):
                        return True
            try:
                shcore = ctypes.windll.shcore
                if hasattr(shcore, "SetProcessDpiAwareness"):
                    if shcore.SetProcessDpiAwareness(2) == 0:   # 回 HRESULT，0 = S_OK
                        return True
            except Exception:
                pass
            try:
                return bool(user32.SetProcessDPIAware())
            except Exception:
                return False

        _set_dpi_awareness()
    except Exception:
        pass   # 設不到不致命，只是退回 DPI-unaware 行為

import asyncio  # noqa: E402
import logging  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from api import (chat, fsops, recorder, runs, settings_api,  # noqa: E402
                 triggers, workflows)
from scheduler.manager import shutdown as sched_shutdown  # noqa: E402
from scheduler.manager import start as sched_start  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")

app = FastAPI(title="Atlas-Lite", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    # 只開本機。這個後端能在使用者的電腦上執行任意腳本與操作桌面，
    # 允許任意 origin 等於讓任何網頁都能對這台機器下指令。
    allow_origins=["http://localhost:3020", "http://127.0.0.1:3020"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for _r in (settings_api, workflows, runs, recorder, fsops, triggers, chat):
    app.include_router(_r.router)

_folder_watch_task = None


@app.on_event("startup")
async def startup():
    from config import check_config
    from db import init_db

    init_db()
    print("✅ 資料庫已初始化")
    for warning in check_config():
        print(warning)

    await sched_start()
    print("✅ 排程已啟動")

    global _folder_watch_task
    _folder_watch_task = asyncio.create_task(triggers.folder_watch_poller())
    print("✅ 檔案夾監看輪詢已啟動")

    from settings import get_telegram_credentials
    token, chat_id = get_telegram_credentials()
    if token and chat_id:
        from telegram_handler import start_polling
        await start_polling()
        print("✅ Telegram 通知與按鈕回呼已啟動")
    else:
        print("ℹ️ 未設定 Telegram（設定頁可填）—— 人工確認節點改在網頁上按")


@app.on_event("shutdown")
async def shutdown():
    await sched_shutdown()
    try:
        from telegram_handler import stop_polling
        await stop_polling()
    except Exception:
        pass
    if _folder_watch_task is not None:
        _folder_watch_task.cancel()
    # 地端定位模型的推論服務是常駐子行程，不關會留在背景佔顯卡記憶體
    try:
        from engine import vlm_grounding
        vlm_grounding.shutdown()
    except Exception:
        pass
