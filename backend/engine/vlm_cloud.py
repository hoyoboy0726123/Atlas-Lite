"""可設定的視覺模型客戶端（選用）。

用途：`vlm_mode='description'` —— 畫面上的文字是動態的、錄製時不知道要找什麼，
就描述一下讓模型看圖告訴你目標的**實際文字**，再交給 OCR 定位。

## 為什麼不用 LangChain
Atlas 走 `build_llm()` → langchain-openai / langchain-anthropic / langchain-ollama…
那一整包是 Atlas-Lite 依賴從 1.4GB 降到 250MB 的主因。

實際上這裡只需要「送一張圖 + 一段文字，拿回一段文字」。多數供應商都提供
OpenAI 相容的 `/chat/completions`（含 Ollama 與 Gemini），所以一個 httpx 呼叫
就夠；只有 Anthropic 的訊息格式不同，另外處理。

## 三種狀態，要分清楚
  沒設定    → 前端把「描述→OCR」反灰，不是錯誤
  設了但壞  → 明確報錯（金鑰錯、模型不支援看圖、連不上）
  設了可用  → 正常運作

第二種絕不能靜默當成第一種 —— 使用者以為設好了，實際每次都沒看圖。
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Optional

log = logging.getLogger(__name__)

# 內建 base_url。使用者填了 vlm_base_url 就用他的（自架 / 代理 / 相容端點）。
_DEFAULT_BASE = {
    "ollama": "http://localhost:11434/v1",
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "anthropic": "https://api.anthropic.com/v1",
}

# 需要金鑰的供應商。Ollama 跑在本機，不需要。
_NEEDS_KEY = {"openai", "groq", "gemini", "anthropic"}

_TIMEOUT = 90.0


def _cfg() -> dict:
    """設定頁優先，沒填就退 .env。

    跟 Telegram 憑證同一套規則 —— 無頭機器 / 不想開網頁的人可以只靠 .env。

    金鑰是**跟著供應商走**的：.env 裡可以同時放 OPENAI_API_KEY 與
    GEMINI_API_KEY，設定頁切供應商時自動取對應那把，不會拿到上一家的金鑰。
    """
    from config import VLM_BASE_URL, VLM_MODEL, VLM_PROVIDER, vlm_env_key
    from settings import get_settings
    s = get_settings()
    provider = (s.get("vlm_provider") or "").strip().lower() or VLM_PROVIDER
    key = (s.get("vlm_api_key") or "").strip()
    key_src = "settings" if key else ""
    if not key and provider in _NEEDS_KEY:
        key = vlm_env_key(provider)
        key_src = "env" if key else ""
    return {
        "provider": provider,
        "model": (s.get("vlm_model") or "").strip() or VLM_MODEL,
        "api_key": key,
        "key_source": key_src,
        "base_url": (s.get("vlm_base_url") or "").strip() or VLM_BASE_URL,
    }


def capability(force: bool = False) -> dict:
    """這台機器能不能用 vlm_mode='description'。前端拿它決定按鈕亮不亮。

    只檢查「設定完不完整」，不打網路 —— 這支會被前端頻繁呼叫。
    真的連不連得上由 probe() 負責（使用者按「測試連線」才跑）。
    """
    c = _cfg()

    def _r(available: bool, reason: str = "", hint: str = "") -> dict:
        # key_source 每個分支都要帶 —— 「選了供應商但還沒填模型」時前端仍然
        # 要能顯示「金鑰已從 .env 讀到」，否則使用者會以為金鑰沒吃到又填一次。
        return {"available": available, "provider": c["provider"], "model": c["model"],
                "local": c["provider"] == "ollama", "key_source": c["key_source"],
                "reason": reason, "hint": hint}

    if not c["provider"]:
        return _r(False, "尚未設定視覺模型",
                  "到設定頁選一個供應商。裝了 Ollama 的話選它就好，不需要金鑰、圖片不出本機。")
    if c["provider"] not in _DEFAULT_BASE:
        return _r(False, f"不認得的供應商：{c['provider']}",
                  f"支援：{'、'.join(_DEFAULT_BASE)}")
    if not c["model"]:
        return _r(False, "沒填模型名稱",
                  "例如 Ollama 的 qwen2.5vl:7b、OpenAI 的 gpt-4o-mini")
    if c["provider"] in _NEEDS_KEY and not c["api_key"]:
        _envname = {"openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY",
                    "gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
        return _r(False, f"{c['provider']} 需要 API 金鑰但沒填",
                  f"在設定頁填入、或在 .env 設 {_envname[c['provider']]}，"
                  f"或改用 Ollama（地端、免金鑰）")
    return _r(True)


def _encode(img_bgr) -> Optional[str]:
    """BGR ndarray → base64 JPEG。品質 70 是體積與可讀性的折衷。"""
    import cv2
    ok, buf = cv2.imencode(".jpg", img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    if not ok:
        return None
    return base64.b64encode(buf.tobytes()).decode()


def ask_with_image(prompt: str, system: str, img_bgr,
                   logger: Optional[logging.Logger] = None) -> tuple[bool, str, str]:
    """送一張圖 + 一段文字給模型，回 (ok, 回應文字, 錯誤說明)。

    模型收不到圖就等於「沒看畫面就下判斷」—— 那比失敗更糟，所以任何一步
    出問題都明確回錯，絕不硬跑。
    """
    lg = logger or log
    cap = capability()
    if not cap["available"]:
        return (False, "", f"視覺模型未設定：{cap['reason']}")

    c = _cfg()
    provider = c["provider"]
    base = c["base_url"] or _DEFAULT_BASE[provider]
    b64 = _encode(img_bgr)
    if b64 is None:
        return (False, "", "截圖轉 JPEG 失敗")

    try:
        import httpx
    except ImportError:
        return (False, "", "缺少 httpx 套件（pip install httpx）")

    try:
        if provider == "anthropic":
            url = f"{base.rstrip('/')}/messages"
            headers = {"x-api-key": c["api_key"], "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
            payload = {
                "model": c["model"], "max_tokens": 512, "system": system,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64",
                                                 "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": prompt},
                ]}],
            }
        else:
            # OpenAI 相容：Ollama / OpenAI / Groq / Gemini 都走這條
            url = f"{base.rstrip('/')}/chat/completions"
            headers = {"content-type": "application/json"}
            if c["api_key"]:
                headers["authorization"] = f"Bearer {c['api_key']}"
            payload = {
                "model": c["model"], "temperature": 0, "max_tokens": 512,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]},
                ],
            }

        with httpx.Client(timeout=_TIMEOUT) as cli:
            r = cli.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            # 常見錯誤講人話，不要丟原始 JSON 給使用者自己猜。
            # 各家的狀態碼不一致（Ollama 連「模型不存在」都回 400），所以
            # 狀態碼與內容關鍵字都要看。
            body = r.text[:400]
            low = body.lower()
            if r.status_code in (401, 403):
                return (False, "", f"金鑰被拒（HTTP {r.status_code}）。"
                                   f"確認 {provider} 的 API 金鑰是否正確、是否還有額度。")
            if r.status_code == 429:
                return (False, "", f"{provider} 回報請求過於頻繁或額度用盡（HTTP 429）。")
            if "invalid model" in low or "not found" in low or r.status_code == 404:
                pull = f"，Ollama 請先執行：ollama pull {c['model']}" if provider == "ollama" else ""
                return (False, "", f"找不到模型「{c['model']}」{pull}")
            if "image" in low or "vision" in low or "multimodal" in low:
                return (False, "", f"模型「{c['model']}」不支援讀取圖片。"
                                   f"請改用多模態模型（Ollama：qwen2.5vl / llava；"
                                   f"OpenAI：gpt-4o）")
            return (False, "", f"HTTP {r.status_code}：{body[:160]}")
        data = r.json()
    except Exception as e:
        return (False, "", f"呼叫失敗（{type(e).__name__}: {e}）")

    # 抽回應文字
    try:
        if provider == "anthropic":
            parts = [b.get("text", "") for b in data.get("content", [])
                     if isinstance(b, dict)]
            text = "".join(parts).strip()
        else:
            text = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return (False, "", f"回應格式看不懂：{json.dumps(data)[:200]}")

    if not text:
        return (False, "", "模型回了空字串")
    lg.info(f"[vlm_cloud] {provider}/{c['model']} → {text[:80]!r}")
    return (True, text, "")


def probe() -> dict:
    """真的打一次（一張純色小圖），確認金鑰 / 模型 / 看圖能力都沒問題。

    使用者按「測試連線」才跑 —— 會花錢（雲端）或花幾秒（地端）。
    """
    import numpy as np

    cap = capability()
    if not cap["available"]:
        return {"ok": False, "reason": cap["reason"], "hint": cap["hint"]}

    # 一張左紅右藍的小圖：看不懂圖的模型答不出「右邊是什麼顏色」
    img = np.zeros((80, 160, 3), dtype=np.uint8)
    img[:, :80] = (0, 0, 255)      # BGR 左紅
    img[:, 80:] = (255, 0, 0)      # BGR 右藍
    ok, text, err = ask_with_image(
        "圖片右半邊是什麼顏色？只回一個詞。", "你是視覺助手，只回答問題本身。", img)
    if not ok:
        return {"ok": False, "reason": err, "hint": cap.get("hint", "")}
    saw = any(k in text.lower() for k in ("藍", "blue", "蓝"))
    return {
        "ok": saw, "answer": text[:60],
        "reason": "" if saw else
                  f"模型回「{text[:30]}」—— 看起來沒真的讀到圖片，"
                  f"這個模型可能不支援視覺輸入",
        "hint": "" if saw else "換一個多模態模型（Ollama：qwen2.5vl / llava；OpenAI：gpt-4o）",
    }
