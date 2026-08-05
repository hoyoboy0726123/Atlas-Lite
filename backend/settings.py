"""使用者可調設定 —— 持久化到 JSON 檔。

Atlas 這支有 30 幾個鍵，其中 25 個是 LLM 相關（provider / model / thinking
模式 / 副模型 / 網路搜尋 / 長期記憶 / 自我修復）。Atlas-Lite 只剩下面這 6 個。
"""
import json
import threading
from typing import Optional

from config import OUTPUT_BASE_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

_SETTINGS_PATH = OUTPUT_BASE_PATH / "settings.json"
_lock = threading.Lock()

_DEFAULT = {
    # ── Telegram 通知 ──
    # 留空 → 讀 .env 的 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID。
    # 兩邊都沒有 → 人工確認節點仍然可用，只是不推播，要在網頁上按。
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    # 遠端遙控：開啟後 bot 會處理你發的指令訊息（/menu 列工作流、按按鈕啟動）。
    # 預設 OFF —— 等於把「誰能對這台電腦下指令」的門開了，要用再手動開。
    "telegram_remote_control": False,

    # ── 桌面自動化 ──
    # 含 computer_use 節點的工作流啟動時，自動縮小前景視窗（通常是瀏覽器）、
    # 結束後還原，避免視窗擋住要自動化的目標 app。
    # 預設 OFF —— 由使用者決定要不要打擾桌面。並發執行用 ref-count 處理。
    "auto_minimize_for_computer_use": False,

    # ── 地端 GUI 定位模型（plugins/vlm_grounding 外掛）──
    # 精度：auto（依可用顯卡記憶體自動選）/ fp16（需 ~11GB）/ int4（需 ~4.5GB）
    "grounding_precision": "auto",
    # 沒裝外掛時，前端的「直接定位」按鈕是否仍顯示（灰底 + 說明如何安裝）。
    "grounding_show_when_missing": True,
}

_cache: Optional[dict] = None


def _load_from_disk() -> dict:
    if _SETTINGS_PATH.exists():
        try:
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = dict(_DEFAULT)
            # 只吃認得的鍵 —— 從 Atlas 複製過來的 settings.json 帶了一堆 LLM 設定，
            # 照單全收會讓它們永遠留在檔案裡誤導人。
            merged.update({k: v for k, v in data.items() if k in _DEFAULT})
            return merged
        except (OSError, json.JSONDecodeError):
            pass
    return dict(_DEFAULT)


def get_settings() -> dict:
    """取得目前設定（含快取）。"""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_from_disk()
        return dict(_cache)


def update_settings(**patch) -> dict:
    """更新並寫入磁碟。只接受 _DEFAULT 裡有的鍵，型別要對得上。"""
    global _cache
    unknown = [k for k in patch if k not in _DEFAULT]
    if unknown:
        raise ValueError(f"未知的設定項：{', '.join(unknown)}")

    if "grounding_precision" in patch:
        v = (patch["grounding_precision"] or "auto").strip().lower()
        if v not in ("auto", "fp16", "int4"):
            raise ValueError(f"grounding_precision 只能是 auto / fp16 / int4，收到 {v!r}")
        patch["grounding_precision"] = v

    for key in ("telegram_remote_control", "auto_minimize_for_computer_use",
                "grounding_show_when_missing"):
        if key in patch:
            patch[key] = bool(patch[key])

    with _lock:
        current = dict(_cache if _cache is not None else _load_from_disk())
        current.update(patch)
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        _cache = current
        return dict(current)


def get_telegram_credentials() -> tuple[str, str]:
    """回 (bot_token, chat_id)。設定頁優先，沒填就用 .env。

    兩個都可能是空字串 —— 呼叫端要自己判斷「沒設定 = 不推播」，
    不要拿空 token 去打 API（會拿到看不懂的 401）。
    """
    s = get_settings()
    token = (s.get("telegram_bot_token") or "").strip() or TELEGRAM_BOT_TOKEN.strip()
    chat_id = str(s.get("telegram_chat_id") or "").strip() or TELEGRAM_CHAT_ID.strip()
    return token, chat_id
