"""設定頁 API：健康檢查、環境路徑、Telegram 通知、桌面自動化選項、Secrets。

檔名不叫 settings.py 是為了不跟 backend/settings.py 撞名 —— 那支才是設定的
真正儲存層，這裡只是它的 HTTP 外殼。
"""
import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import secrets_vault
from config import (EXTERNAL_PROJECTS_DIR, LOG_DIR, OUTPUT_BASE_PATH,
                    TIMEZONE, WORKFLOW_DIR)
from settings import get_settings, update_settings

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "app": "Atlas-Lite"}


@router.get("/env/paths")
async def get_env_paths():
    """前端「這些東西存在哪」要顯示的路徑。"""
    return {
        "data_dir": str(OUTPUT_BASE_PATH),
        "workflow_dir": str(WORKFLOW_DIR),
        "log_dir": str(LOG_DIR),
        "external_projects_dir": str(EXTERNAL_PROJECTS_DIR),
        "timezone": TIMEZONE,
    }


# ── Telegram 通知 ────────────────────────────────────────────────────

class NotificationSettingsRequest(BaseModel):
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_remote_control: Optional[bool] = None


def _notification_payload() -> dict:
    s = get_settings()
    token = s.get("telegram_bot_token", "") or ""
    return {
        # 永不回傳完整 token —— 設定頁只需要知道「有沒有設」跟認得出是哪一個
        "telegram_bot_token_set": bool(token),
        "telegram_bot_token_masked": (token[:8] + "…" + token[-4:]) if len(token) > 14 else "",
        "telegram_chat_id": s.get("telegram_chat_id", ""),
        "telegram_remote_control": bool(s.get("telegram_remote_control", False)),
    }


@router.get("/settings/notifications")
async def get_notification_settings():
    return _notification_payload()


@router.put("/settings/notifications")
async def put_notification_settings(req: NotificationSettingsRequest):
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    if patch.get("telegram_remote_control") and not (
            patch.get("telegram_bot_token") or get_settings().get("telegram_bot_token")):
        raise HTTPException(status_code=400,
                            detail="要開遠端遙控，得先填 Telegram bot token 與 chat id")
    if patch:
        update_settings(**patch)
    return _notification_payload()


# ── 桌面自動化 ───────────────────────────────────────────────────────

class ComputerUseSettingsRequest(BaseModel):
    auto_minimize_for_computer_use: Optional[bool] = None
    grounding_precision: Optional[str] = None
    grounding_show_when_missing: Optional[bool] = None


@router.get("/settings/computer-use")
async def get_computer_use_settings():
    s = get_settings()
    return {k: s[k] for k in ("auto_minimize_for_computer_use", "grounding_precision",
                              "grounding_show_when_missing")}


@router.put("/settings/computer-use")
async def put_computer_use_settings(req: ComputerUseSettingsRequest):
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    if patch:
        try:
            update_settings(**patch)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    # 精度改了要讓推論服務重載入，否則舊行程還在用舊精度
    if "grounding_precision" in patch:
        from engine import vlm_grounding
        os.environ["ATLASLITE_GROUNDING_PRECISION"] = patch["grounding_precision"]
        vlm_grounding.shutdown()
    return await get_computer_use_settings()


# ── 視覺模型（vlm_mode='description' 用，選用）────────────────────────

_VLM_PROVIDERS = ["ollama", "openai", "groq", "gemini", "anthropic"]


class VlmSettingsRequest(BaseModel):
    vlm_provider: Optional[str] = None
    vlm_model: Optional[str] = None
    vlm_api_key: Optional[str] = None
    vlm_base_url: Optional[str] = None


def _vlm_payload() -> dict:
    import config
    from engine import vlm_cloud
    s = get_settings()
    key = s.get("vlm_api_key", "") or ""
    cap = vlm_cloud.capability()
    provider = s.get("vlm_provider", "") or config.VLM_PROVIDER
    return {
        "vlm_provider": provider,
        "vlm_model": s.get("vlm_model", "") or config.VLM_MODEL,
        "vlm_base_url": s.get("vlm_base_url", "") or config.VLM_BASE_URL,
        # 跟 Telegram token 一樣：只回「有沒有設」與遮罩，不回完整金鑰
        "vlm_api_key_set": bool(key),
        "vlm_api_key_masked": (key[:6] + "…" + key[-4:]) if len(key) > 12 else "",
        # 目前這家的金鑰哪來的：'settings' / 'env' / ''（沒有）。
        # 從 .env 讀到就別再叫使用者填一次 —— 那是最容易讓人以為壞掉的地方。
        "key_source": cap.get("key_source", ""),
        # 哪幾家在 .env 裡已經有金鑰了，讓設定頁能標出來（不回金鑰本身）
        "env_keys": sorted(p for p in _VLM_PROVIDERS if config.vlm_env_key(p)),
        "available": cap["available"],
        "reason": cap["reason"],
        "hint": cap["hint"],
        "local": cap.get("local", False),
        "providers": _VLM_PROVIDERS,
    }


@router.get("/settings/vlm")
async def get_vlm_settings():
    return _vlm_payload()


@router.put("/settings/vlm")
async def put_vlm_settings(req: VlmSettingsRequest):
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    if patch:
        try:
            update_settings(**patch)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return _vlm_payload()


@router.post("/settings/vlm/probe")
async def probe_vlm():
    """真的打一次（左紅右藍小圖問「右半邊什麼顏色」）。

    分得出「設定沒填」「金鑰壞了」「模型看不懂圖」三種 —— 最後一種最陰險，
    設定看起來完好但模型其實沒讀到圖，等於每次都在瞎猜。
    """
    import asyncio

    from engine import vlm_cloud
    return await asyncio.get_running_loop().run_in_executor(None, vlm_cloud.probe)


# ── Secrets Vault ────────────────────────────────────────────────────
# 值永遠不會被回傳，也永遠不會進 log。工作流用 {{ secrets.名稱 }} 引用。

class SecretRequest(BaseModel):
    name: str
    value: str


@router.get("/settings/secrets")
async def secrets_list():
    return {"secrets": secrets_vault.list_secret_names()}


@router.post("/settings/secrets")
async def secrets_set(req: SecretRequest):
    if not (req.value or "").strip():
        raise HTTPException(status_code=400, detail="值不能是空的")
    try:
        secrets_vault.set_secret(req.name, req.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "name": req.name.strip()}


@router.delete("/settings/secrets/{name}")
async def secrets_delete(name: str):
    if not secrets_vault.delete_secret(name):
        raise HTTPException(status_code=404, detail=f"找不到 secret：{name}")
    return {"ok": True}
