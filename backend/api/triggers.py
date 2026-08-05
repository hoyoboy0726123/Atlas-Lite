"""觸發器：Webhook（外部 HTTP POST）與檔案夾監看（出現新檔就跑）。"""
import asyncio
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import db
from api.runs import PipelineRunRequest, start_pipeline

router = APIRouter()
log = logging.getLogger("atlas_lite.triggers")


# ── Webhook ──────────────────────────────────────────────────────────

def _webhook_url(request: Request, token: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/webhooks/{token}"


@router.post("/workflows/{workflow_id}/webhook")
async def create_workflow_webhook(workflow_id: str, request: Request):
    """建立 / 重新產生 webhook 觸發網址。每次呼叫換新 token，舊的立刻失效。"""
    if not db.get_workflow(workflow_id):
        raise HTTPException(status_code=404, detail="工作流不存在")
    hook = db.create_webhook(workflow_id)
    return {**hook, "url": _webhook_url(request, hook["token"])}


@router.get("/workflows/{workflow_id}/webhook")
async def get_workflow_webhook(workflow_id: str, request: Request):
    hook = db.get_webhook_by_workflow(workflow_id)
    if not hook:
        raise HTTPException(status_code=404, detail="這個工作流還沒設定 webhook")
    enabled = bool(hook["enabled"])
    return {**hook, "enabled": enabled,
            "url": _webhook_url(request, hook["token"]) if enabled else None}


@router.delete("/workflows/{workflow_id}/webhook")
async def delete_workflow_webhook(workflow_id: str):
    """停用 webhook（觸發端之後一律 404）。"""
    ok = db.disable_webhook(workflow_id)
    return {"ok": ok, "disabled": ok}


@router.post("/webhooks/{token}")
async def trigger_webhook(token: str, request: Request):
    """外部觸發入口。公開路徑，靠不可猜的 token 保護。

    ⚠ 這條路徑會讓外部的一個 HTTP 請求在使用者的電腦上執行工作流
    （含桌面自動化）。token 本身就是憑證 —— 洩漏等同把執行權交出去，
    所以只有使用者主動按「建立」才會有 token，且隨時可以重新產生。
    POST body（JSON 物件）會變成這次執行的 input_params。
    """
    hook = db.get_webhook_by_token(token)
    if not hook:
        raise HTTPException(status_code=404, detail="webhook 不存在或已停用")
    wf = db.get_workflow(hook["workflow_id"])
    if not wf:
        raise HTTPException(status_code=404, detail="綁定的工作流已不存在")
    # body → input_params；非 JSON 物件 / 空 body 都容忍
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {"payload": body}
    except Exception:
        body = {}
    db.mark_webhook_fired(token)
    result = await start_pipeline(PipelineRunRequest(
        yaml_content=wf["yaml"], workflow_id=hook["workflow_id"], input_params=body))
    return {"triggered": True, "workflow_id": hook["workflow_id"], **result}


# ── 檔案夾監看 ───────────────────────────────────────────────────────
# 輪詢式，不用 watchdog —— 少一個依賴，而且 8 秒的延遲對「有新檔就處理」
# 這種需求完全夠用。

_FOLDER_WATCH_INTERVAL = 8.0   # 秒


class FolderWatchRequest(BaseModel):
    folder_path: str
    pattern: str = "*"


@router.post("/workflows/{workflow_id}/folder-watch")
async def create_workflow_folder_watch(workflow_id: str, req: FolderWatchRequest):
    """設定「資料夾出現新檔就觸發」。只看設定之後的新檔，不對既有檔一次全轟。"""
    if not db.get_workflow(workflow_id):
        raise HTTPException(status_code=404, detail="工作流不存在")
    if not os.path.isdir(req.folder_path):
        raise HTTPException(status_code=400, detail=f"資料夾不存在：{req.folder_path}")
    return db.create_folder_watch(workflow_id, req.folder_path, req.pattern)


@router.get("/workflows/{workflow_id}/folder-watch")
async def get_workflow_folder_watch(workflow_id: str):
    w = db.get_folder_watch_by_workflow(workflow_id)
    if not w:
        raise HTTPException(status_code=404, detail="這個工作流還沒設定檔案夾監看")
    return {**w, "enabled": bool(w["enabled"])}


@router.delete("/workflows/{workflow_id}/folder-watch")
async def delete_workflow_folder_watch(workflow_id: str):
    ok = db.disable_folder_watch(workflow_id)
    return {"ok": ok, "disabled": ok}


async def folder_watch_poller():
    """背景輪詢：掃各個啟用中的監看資料夾，有新檔就觸發（檔案路徑進 input_params）。

    由 main.py 在 startup 時掛起來。單一 task 掃全部監看，不是一個監看一個 task。
    """
    from folder_watch import scan_new_files

    while True:
        try:
            for w in db.list_enabled_folder_watches():
                try:
                    new_files, max_mtime = scan_new_files(
                        w["folder_path"], w["pattern"], w["last_seen_mtime"])
                    if not new_files:
                        continue
                    wf = db.get_workflow(w["workflow_id"])
                    if not wf:
                        continue
                    fired = 0
                    for fp in new_files:
                        try:
                            await start_pipeline(PipelineRunRequest(
                                yaml_content=wf["yaml"], workflow_id=w["workflow_id"],
                                input_params={"file_path": fp,
                                              "file_name": os.path.basename(fp)}))
                            fired += 1
                        except Exception as e:
                            log.warning(f"觸發失敗 {fp}：{e}")
                    # 就算某幾個檔觸發失敗也要推進 mtime —— 否則下一輪又掃到同一批，
                    # 一個壞檔會讓整個監看無限重試。
                    db.update_folder_watch_progress(w["id"], max_mtime, fired)
                    if fired:
                        log.info(f"監看觸發：{w['workflow_id']} 新檔 {fired} 個")
                except Exception as e:
                    log.warning(f"監看 {w.get('workflow_id')} 掃描失敗：{e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"folder-watch 輪詢例外：{e}")
        try:
            await asyncio.sleep(_FOLDER_WATCH_INTERVAL)
        except asyncio.CancelledError:
            break
