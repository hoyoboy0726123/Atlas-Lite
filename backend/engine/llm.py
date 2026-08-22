"""LLM 客戶端 —— 只有兩個供應商：地端 Ollama、華碩 AiHub。

## 為什麼不搬 Atlas 的 llm_factory
Atlas 支援 6 個供應商，走 langchain-openai / -anthropic / -ollama 那一整包，
是 Atlas-Lite 依賴從 1.4GB 降到 250MB 的主因。這裡的需求只是
「送一段文字、拿回一段文字」，httpx 就夠。風格對齊隔壁的 vlm_cloud.py。

## 兩個供應商的定位不同
  ollama   地端，不需金鑰，**資料不出本機**。個人 / 開發 / 敏感資料。
  aihub    華碩雲端閘道。內部帳號、免自備 API key，但要注意資料去向（見下）。

## AiHub 的資安分級是**接入方**的責任
AiHub 底下掛了多個實際模型，只有 `local/gpt-oss` 是華碩自建部署，
其餘 alias（azure / google / amazon）的請求內容都會送到外部雲端廠商。
本檔在程式層寫死白名單並預設鎖 gpt-oss —— 不讓使用者從設定頁隨手切到
會把內部資料送出去的模型。要放寬得改這裡的原始碼，是刻意的摩擦。

## 三種狀態，要分清楚（跟 vlm_cloud 同一套規矩）
  沒設定    → 前端把助手反灰，不是錯誤
  設了但壞  → 明確報錯（金鑰錯、Ollama 沒開、模型沒 pull）
  設了可用  → 正常運作
第二種絕不能靜默當成第一種。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── AiHub 端點 ──────────────────────────────────────────────
_AIHUB_BASE = {
    "stage": "https://stage-iotapi.asus.com/aoccgpt2/v1/openapi",
    "prod": "https://aoccaihub.asus.com/aoccgpt2/v1/openapi",
}

# alias → (service, version, 資料是否離開華碩)
# ⚠ 未登錄的名稱**不猜服務商** —— 直接拋錯，比靜默送錯模型好。
_AIHUB_MODELS: dict[str, tuple[str, str, bool]] = {
    "gpt-oss":         ("local",  "gpt-oss",         False),   # 華碩自建 120B
    "gpt41":           ("azure",  "gpt41",           True),
    "gpt41-mini":      ("azure",  "gpt41-mini",      True),
    "gemini2.5pro":    ("google", "gemini2.5pro",    True),
    "gemini2.5flash":  ("google", "gemini2.5flash",  True),
    "gemini2.0":       ("google", "gemini2.0",       True),
    "claude45":        ("amazon", "claude45",        True),
    "claude37":        ("amazon", "claude37",        True),
    "claude35":        ("amazon", "claude35",        True),
}

# 預設只允許資料不外流的那一個。要開放請改這裡，並自行承擔資料去向。
_AIHUB_ALLOWED = {"gpt-oss"}

# 坑 9：短輸出 5-20 秒、長答案 ~21 秒、偶發更久。逾時要放寬，不是效能問題。
_AIHUB_TIMEOUT = 600.0
_OLLAMA_TIMEOUT = 300.0

# 坑 6：文件一處寫 30 天、一處寫 24 小時。取較短者再打折。
_TOKEN_TTL = 12 * 3600
# 坑 7：new_session 平均 1.55 秒，每題開新的等於白付 10-30% 延遲。
_POOL_SIZE = 4
_SESSION_MAX_USES = 200


class LlmError(RuntimeError):
    """LLM 呼叫失敗。訊息要能直接顯示給使用者看，講清楚下一步怎麼做。"""


# ── 設定 ────────────────────────────────────────────────────
def _cfg() -> dict:
    """設定頁優先，沒填就退 .env（跟 vlm_cloud / Telegram 同一套規則）。"""
    from config import (LLM_AIHUB_API_KEY, LLM_AIHUB_ENV, LLM_BASE_URL,
                        LLM_MODEL, LLM_PROVIDER)
    from settings import get_settings
    s = get_settings()
    provider = (s.get("llm_provider") or "").strip().lower() or LLM_PROVIDER

    # ⚠ 金鑰**不從 settings 讀**。settings.json 是明文檔，金鑰只走加密保險箱
    #   或 .env —— 這是刻意的，不要為了方便加一個明文欄位回來。
    key, key_src = "", ""
    if provider == "aihub":
        try:
            from secrets_vault import get_secret
            key = (get_secret("AIHUB_API_KEY") or "").strip()
            key_src = "vault" if key else ""
        except Exception as e:
            log.debug(f"[llm] 讀 secrets_vault 失敗:{e}")
        if not key:
            key = LLM_AIHUB_API_KEY
            key_src = "env" if key else ""

    return {
        "provider": provider,
        "model": (s.get("llm_model") or "").strip() or LLM_MODEL,
        "api_key": key,
        "key_source": key_src,
        "base_url": (s.get("llm_base_url") or "").strip() or LLM_BASE_URL,
        "aihub_env": (s.get("llm_aihub_env") or "").strip().lower() or LLM_AIHUB_ENV,
    }


def capability(cfg: Optional[dict] = None) -> dict:
    """能不能用？不能的話缺什麼？前端拿這個決定助手要不要反灰。"""
    c = cfg or _cfg()
    p = c["provider"]
    if not p:
        return {"available": False, "reason": "尚未設定 LLM 供應商",
                "provider": "", "model": ""}
    if p not in ("ollama", "aihub"):
        return {"available": False,
                "reason": f"不認得的供應商「{p}」—— 目前只支援 ollama 與 aihub",
                "provider": p, "model": c["model"]}
    if not c["model"]:
        return {"available": False,
                "reason": ("尚未指定模型"
                           + ("（例：qwen3:8b —— 先 ollama pull）" if p == "ollama"
                              else "（例：gpt-oss）")),
                "provider": p, "model": ""}
    if p == "aihub":
        if not c["api_key"]:
            return {"available": False,
                    "reason": "AiHub 需要 API KEY —— 放到 .env 的 AIHUB_API_KEY 或設定頁的保險箱",
                    "provider": p, "model": c["model"]}
        if c["model"] not in _AIHUB_MODELS:
            return {"available": False,
                    "reason": (f"不認得的 AiHub 模型「{c['model']}」。"
                               f"可用：{'、'.join(sorted(_AIHUB_MODELS))}"),
                    "provider": p, "model": c["model"]}
        if c["model"] not in _AIHUB_ALLOWED:
            svc, _, external = _AIHUB_MODELS[c["model"]]
            return {"available": False,
                    "reason": (f"「{c['model']}」走 {svc}，請求內容會離開華碩。"
                               f"本專案在程式層鎖定 {'、'.join(sorted(_AIHUB_ALLOWED))}"),
                    "provider": p, "model": c["model"]}
    return {"available": True, "reason": "", "provider": p, "model": c["model"],
            "data_stays_local": p == "ollama" or c["model"] == "gpt-oss"}


# ── 訊息攤平（AiHub 坑 5）───────────────────────────────────
def flatten_messages(messages: list[dict]) -> str:
    """多段 messages → 單一字串。

    AiHub 沒有 messages 概念、只吃一個 `message` 欄位，要自己攤平。
    順序保留、system 放最前、\\n\\n 串接。
    """
    sys_parts, rest = [], []
    for m in messages:
        role = (m.get("role") or "user").lower()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            sys_parts.append(content)
        elif role == "assistant":
            rest.append(f"[助手先前的回覆]\n{content}")
        else:
            rest.append(content)
    return "\n\n".join(sys_parts + rest)


# ── 寬鬆 JSON 解析（AiHub 坑 3：沒有 JSON mode）───────────────
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def loads_loose(text: str) -> Any:
    """從模型回覆裡挖出 JSON。

    沒有 `format=json` 這種保證，回覆常帶前後說明或 ``` 圍籬。
    三段：剝圍籬 → 直接 loads → 取第一個 { 到最後一個 } 切片。
    真的挖不出來就拋錯 —— 不回 None，免得呼叫端把「解析失敗」當成
    「模型說沒有」。
    """
    s = (text or "").strip()
    if not s:
        raise LlmError("模型回了空字串")
    m = _FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()
    if (v := _try_parse(s)) is not _MISS:
        return v
    for lo, hi in ((s.find("{"), s.rfind("}")), (s.find("["), s.rfind("]"))):
        if lo != -1 and hi > lo:
            if (v := _try_parse(s[lo:hi + 1])) is not _MISS:
                return v
    raise LlmError(f"模型的回覆不是 JSON，也挖不出 JSON 片段：{text[:200]}")


# 哨兵。不能用 None 代表「解析失敗」—— 合法的 JSON `null` 就會被誤判。
_MISS = object()
_JSON_LIT_RE = re.compile(r"(?<![\"'\w])(true|false|null)(?![\"'\w])")


def _try_parse(s: str) -> Any:
    """json.loads，失敗再試 Python 字面值。解不出來回 _MISS。"""
    try:
        return json.loads(s)
    except Exception:
        pass
    # 模型很常送單引號的「JSON」（{'op':'append'}）。literal_eval 只吃字面值、
    # 不執行任何程式碼，拿來救這種輸出是安全的。
    import ast
    try:
        return ast.literal_eval(s)
    except Exception:
        pass
    # 還是不行才動 true/false/null。⚠ 這步會改到字串**內容**裡的同名字
    #   （{'text':'true'} → {'text':True}），所以放最後 —— 原樣能解就不該走到這。
    try:
        return ast.literal_eval(_JSON_LIT_RE.sub(
            lambda m: {"true": "True", "false": "False", "null": "None"}[m.group(1)], s))
    except Exception:
        return _MISS


def embed(texts: list[str]) -> list[list[float]]:
    """向量化。

    AiHub **沒有 embedding 端點**（坑 4）—— 直接拋錯，不靜默退化成別的東西。
    要做向量檢索請走地端 Ollama 的 /api/embeddings。
    """
    raise LlmError("AiHub 沒有 embedding 端點；向量化請用地端 Ollama")


# ── AiHub：token 與 session 池 ──────────────────────────────
class _AiHubState:
    """token 快取 + session 池。跨請求共用，所以要上鎖。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.token = ""
        self.token_at = 0.0
        self.token_key = ""       # 綁哪把 API KEY 拿的 —— 換金鑰要作廢
        self.token_base = ""      # 綁哪個環境 —— stage/prod 的 token 不通用
        self.sessions: list[tuple[str, int]] = []   # (session_id, 已用次數)


_aihub = _AiHubState()


def _aihub_token(client, base: str, api_key: str, force: bool = False) -> str:
    with _aihub.lock:
        fresh = (_aihub.token
                 and _aihub.token_key == api_key
                 and _aihub.token_base == base
                 and time.time() - _aihub.token_at < _TOKEN_TTL)
        if fresh and not force:
            return _aihub.token
    r = client.get(f"{base}/auth", headers={"Authorization": api_key})
    if r.status_code in (401, 403):
        raise LlmError("AiHub 認證失敗 —— API KEY 不對或已停用")
    r.raise_for_status()
    tok = (r.json() or {}).get("token") or ""
    if not tok:
        raise LlmError("AiHub /auth 回了 200，但沒有 token 欄位")
    with _aihub.lock:
        _aihub.token, _aihub.token_at = tok, time.time()
        _aihub.token_key, _aihub.token_base = api_key, base
        # 換 token 後舊 session 一律作廢（坑 6）
        _aihub.sessions.clear()
    return tok


def _aihub_session(client, base: str, token: str) -> tuple[str, int]:
    with _aihub.lock:
        if _aihub.sessions:
            return _aihub.sessions.pop()
    r = client.post(f"{base}/new_session", headers={"Authorization": token})
    r.raise_for_status()
    sid = (r.json() or {}).get("session_id") or ""
    if not sid:
        raise LlmError("AiHub /new_session 回了 200，但沒有 session_id 欄位")
    return sid, 0


def _aihub_return_session(sid: str, uses: int) -> None:
    """成功才放回池。用滿次數就丟掉、下次開新的。"""
    if uses >= _SESSION_MAX_USES:
        return
    with _aihub.lock:
        if len(_aihub.sessions) < _POOL_SIZE:
            _aihub.sessions.append((sid, uses))


def _aihub_chat(cfg: dict, prompt: str, temperature: float) -> str:
    import httpx

    model = cfg["model"]
    if model not in _AIHUB_MODELS:
        raise LlmError(f"不認得的 AiHub 模型「{model}」——"
                       f"可用：{'、'.join(sorted(_AIHUB_MODELS))}")
    if model not in _AIHUB_ALLOWED:
        svc, _, _ = _AIHUB_MODELS[model]
        raise LlmError(f"「{model}」走 {svc}，請求內容會離開華碩；"
                       f"本專案鎖定 {'、'.join(sorted(_AIHUB_ALLOWED))}")
    service, version, _ = _AIHUB_MODELS[model]
    base = cfg["base_url"] or _AIHUB_BASE.get(cfg["aihub_env"] or "prod", _AIHUB_BASE["prod"])

    with httpx.Client(timeout=_AIHUB_TIMEOUT) as client:
        last_err = None
        for attempt in (1, 2):
            token = _aihub_token(client, base, cfg["api_key"], force=(attempt == 2))
            sid, uses = _aihub_session(client, base, token)
            try:
                r = client.post(f"{base}/chat",
                                headers={"Authorization": token},
                                json={"session_id": sid,
                                      "response_type": "normal",
                                      "assistant_id": "",
                                      "service": service,
                                      "version": version,
                                      "message": prompt,
                                      "temperature": temperature})
            except httpx.TimeoutException:
                raise LlmError(f"AiHub 逾時（{int(_AIHUB_TIMEOUT)} 秒）—— "
                               f"長答案偶發會更久，可以重試")
            if r.status_code == 401 and attempt == 1:
                # token 失效 → 重新認證重試一次（只一次，避免無限打認證）
                last_err = "401"
                continue
            r.raise_for_status()
            data = r.json() or {}
            err = data.get("error")
            # 坑 8：error 可能是字串 "null"/"None"，不能直接當真值判斷
            if err and str(err).strip().lower() not in ("null", "none", ""):
                raise LlmError(f"AiHub 回報錯誤：{err}")
            _aihub_return_session(sid, uses + 1)
            return data.get("textResponse") or ""
        raise LlmError(f"AiHub 重新認證後仍失敗（{last_err}）")


# ── Ollama ─────────────────────────────────────────────────
def _ollama_chat(cfg: dict, messages: list[dict], temperature: float) -> str:
    import httpx

    base = (cfg["base_url"] or "http://localhost:11434").rstrip("/")
    try:
        with httpx.Client(timeout=_OLLAMA_TIMEOUT) as client:
            r = client.post(f"{base}/api/chat",
                            json={"model": cfg["model"],
                                  "messages": messages,
                                  "stream": False,
                                  "options": {"temperature": temperature}})
    except httpx.ConnectError:
        raise LlmError(f"連不上 Ollama（{base}）—— 先確認 `ollama serve` 有在跑")
    except httpx.TimeoutException:
        raise LlmError(f"Ollama 逾時（{int(_OLLAMA_TIMEOUT)} 秒）——"
                       f"第一次載入大模型會很慢，可以重試")
    if r.status_code >= 400:
        # ⚠ 不能只看狀態碼。實測「模型沒 pull」Ollama 回的是 **400 不是 404**，
        #   原始 HTTPStatusError 只會顯示「400 Bad Request」——使用者完全不知道
        #   要去 pull。真正的原因在 body 的 error 欄位裡。
        detail = ""
        try:
            detail = ((r.json() or {}).get("error") or "").strip()
        except Exception:
            detail = (r.text or "").strip()[:200]
        if "not found" in detail.lower() or "try pulling" in detail.lower():
            raise LlmError(f"Ollama 找不到模型「{cfg['model']}」——"
                           f"先執行：ollama pull {cfg['model']}")
        raise LlmError(f"Ollama 回 HTTP {r.status_code}：{detail or '(沒有錯誤說明)'}")
    data = r.json() or {}
    if data.get("error"):
        raise LlmError(f"Ollama 回報錯誤：{data['error']}")
    return ((data.get("message") or {}).get("content") or "")


# ── 對外 ────────────────────────────────────────────────────
def chat(messages: list[dict], temperature: float = 0.2,
         cfg: Optional[dict] = None) -> str:
    """送 messages、拿回整段文字。

    ⚠ **沒有串流**。AiHub v0.9 移除了 streaming（實測改 response_type 五個模型
    全部回同一個伺服器端錯誤），為了兩邊行為一致，Ollama 這邊也不開。
    長答案要等 ~20 秒 —— 呼叫端要自己處理 UX（先出骨架、別讓人盯白屏）。

    ⚠ **temperature=0 不等於可重現**。AiHub 收得下 0 但不報錯也不保證同樣輸出
    （閘道夾限或 gpt-oss 本身 MoE 不確定）。任何建立在「0 = 可重現」上的
    評測比較都會被雜訊騙 —— 單次結果不要下結論。
    """
    c = cfg or _cfg()
    cap = capability(c)
    if not cap["available"]:
        raise LlmError(cap["reason"])
    if not messages:
        raise LlmError("chat() 收到空的 messages")

    if c["provider"] == "ollama":
        return _ollama_chat(c, messages, temperature)
    return _aihub_chat(c, flatten_messages(messages), temperature)


def probe(cfg: Optional[dict] = None) -> dict:
    """連線測試：真的打一次最小請求，回「現在能不能用」。

    只看設定填沒填是不夠的 —— 金鑰過期、Ollama 沒開、模型沒 pull
    都只有真的打過去才知道。
    """
    c = cfg or _cfg()
    cap = capability(c)
    out = {"provider": c["provider"], "model": c["model"],
           "key_source": c["key_source"]}
    if not cap["available"]:
        return {**out, "ok": False, "error": cap["reason"]}
    t0 = time.time()
    try:
        reply = chat([{"role": "user", "content": "回覆 OK 兩個字，不要其他內容。"}],
                     temperature=0.2, cfg=c)
    except LlmError as e:
        return {**out, "ok": False, "error": str(e)}
    except Exception as e:
        return {**out, "ok": False, "error": f"{type(e).__name__}: {e}"}
    return {**out, "ok": True, "elapsed_ms": int((time.time() - t0) * 1000),
            "reply": (reply or "").strip()[:120],
            "data_stays_local": cap.get("data_stays_local", False)}


def list_models(cfg: Optional[dict] = None) -> dict:
    """列出可選模型。Ollama 問本機、AiHub 回白名單（沒有列表端點）。"""
    c = cfg or _cfg()
    if c["provider"] == "aihub":
        return {"ok": True, "source": "白名單",
                "models": sorted(_AIHUB_ALLOWED),
                "note": (f"AiHub 另有 {len(_AIHUB_MODELS) - len(_AIHUB_ALLOWED)} 個模型"
                         f"會把請求送到外部廠商，已在程式層擋掉")}
    import httpx
    base = (c["base_url"] or "http://localhost:11434").rstrip("/")
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{base}/api/tags")
        r.raise_for_status()
    except Exception as e:
        return {"ok": False, "models": [],
                "error": f"問不到 Ollama 的模型列表（{base}）：{e}"}
    names = [m.get("name", "") for m in (r.json() or {}).get("models", [])]
    return {"ok": True, "source": base, "models": [n for n in names if n]}
