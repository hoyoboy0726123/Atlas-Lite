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
