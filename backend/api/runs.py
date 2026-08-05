"""執行相關的 API：啟動 / 查詢 / 決策 / 中止 / log / 排程。"""
import asyncio
import re
import uuid
from dataclasses import asdict
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from engine.logger import create_run_logger, find_run_log
from engine.models import PipelineConfig
from engine.runner import (force_abort, register_task, request_abort,
                           resume_pipeline, run_pipeline)
from engine.store import PipelineRun, get_store

router = APIRouter()


class PipelineRunRequest(BaseModel):
    yaml_content: str
    workflow_id: Optional[str] = None
    # 啟動時傳入的參數，render 階段以 `{{ input.<key> }}` 引用。
    # YAML 寫死的欄位照舊，只有寫了 {{ input.X }} 的欄位才需要這裡帶值。
    input_params: dict = {}


class PipelineDecisionRequest(BaseModel):
    decision: str
    hint: Optional[str] = None


_VALID_DECISIONS = ("retry", "skip", "abort", "continue", "redo_prev", "install_dep")


# ── YAML 容錯：雙引號包 Windows 路徑自動轉正 ──────────────────────────
# `path: "C:\Users\..."` 的雙引號內，\U \x \n 會被 YAML 當 escape sequence
# → ScannerError 整份解析失敗。這裡只在初次解析失敗時，把「雙引號內含反斜線」
# 的純量改成單引號再重試（單引號 YAML 不解析 escape）。本來就正常的 YAML 零影響。
_WIN_DQUOTE_RE = re.compile(r'(?m)([:\-]\s+)"([^"\n]*\\[^"\n]*)"(\s*(?:#[^\n]*)?)$')


def _sanitize_windows_paths(text: str) -> str:
    def fix(m):
        prefix, val, tail = m.group(1), m.group(2), m.group(3)
        if "'" in val:   # 含單引號才需特殊處理；Windows 路徑通常沒有 → 保守跳過
            return m.group(0)
        return f"{prefix}'{val}'{tail}"
    return _WIN_DQUOTE_RE.sub(fix, text)


def lenient_yaml_load(text: str):
    """寬鬆解析：先正常 load，失敗則嘗試修雙引號 Windows 路徑後重試。"""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        fixed = _sanitize_windows_paths(text)
        if fixed != text:
            return yaml.safe_load(fixed)   # 仍失敗就讓例外往上拋
        raise


def _parse_config(yaml_content: str) -> PipelineConfig:
    try:
        data = lenient_yaml_load(yaml_content)
        return PipelineConfig.from_dict(data.get("pipeline", data))
    except ValueError as e:
        # from_dict 對不支援的節點型別拋 ValueError，訊息已經寫給使用者看了
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"YAML 解析失敗：{e}")


@router.post("/pipeline/run")
async def start_pipeline(req: PipelineRunRequest):
    config = _parse_config(req.yaml_content)

    # 先建立執行紀錄再開背景 task，確保前端立刻查得到
    run_id = str(uuid.uuid4())[:12]
    _, log_path = create_run_logger(run_id, config.name)
    config_d = config.model_dump()
    config_d["_workflow_id"] = req.workflow_id
    run = PipelineRun(
        run_id=run_id, pipeline_name=config.name, config_dict=config_d,
        telegram_chat_id=0, log_path=log_path, workflow_id=req.workflow_id,
        input_params=req.input_params or {},
    )
    get_store().save(run)

    # 背景執行（runner 看到已存在的 run_id 會走恢復路徑）
    task = asyncio.create_task(run_pipeline(config_d, chat_id=0, run_id=run_id))
    register_task(run_id, task)
    return {"run_id": run_id, "message": f"工作流「{config.name}」已啟動"}


@router.get("/pipeline/runs")
async def list_pipeline_runs():
    return {"runs": [run_to_dict(r) for r in get_store().list_recent(20)]}


@router.get("/pipeline/runs/{run_id}")
async def get_pipeline_run(run_id: str):
    run = get_store().load(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="找不到這次執行")
    return run_to_dict(run)


@router.delete("/pipeline/runs/{run_id}")
async def delete_pipeline_run(run_id: str):
    if get_store().delete(run_id):
        return {"message": f"執行 {run_id} 已刪除"}
    raise HTTPException(status_code=404, detail="找不到這次執行")


@router.post("/pipeline/runs/{run_id}/resume")
async def resume_pipeline_run(run_id: str, req: PipelineDecisionRequest):
    if req.decision not in _VALID_DECISIONS:
        raise HTTPException(status_code=400,
                            detail=f"decision 必須是：{' / '.join(_VALID_DECISIONS)}")
    msg = await resume_pipeline(run_id, req.decision, hint=req.hint or "")
    return {"message": msg}


@router.post("/pipeline/runs/{run_id}/abort")
async def abort_pipeline_run(run_id: str):
    run = get_store().load(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="找不到這次執行")
    if run.status not in ("running", "awaiting_human"):
        return {"message": f"目前狀態是 {run.status}，不需要中止"}
    request_abort(run_id)
    await force_abort(run_id)
    return {"message": "已中止"}


@router.get("/pipeline/runs/{run_id}/log")
async def get_pipeline_log(run_id: str):
    from pathlib import Path

    run = get_store().load(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="找不到這次執行")
    p = Path(run.log_path) if run.log_path else None
    if not (p and p.is_file()):
        p = find_run_log(run_id)   # log_path 失效時靠 run_id 前綴回頭找
    if not p:
        return {"log": "（尚無 log 檔案）"}
    return {"log": p.read_text(encoding="utf-8", errors="replace")}


# ── 排程 ─────────────────────────────────────────────────────────────

class PipelineScheduleRequest(BaseModel):
    name: str
    yaml_content: str
    schedule_type: str = "cron"
    schedule_expr: str = "0 8 * * *"
    workflow_id: Optional[str] = None


@router.get("/pipeline/scheduled")
async def list_pipeline_scheduled():
    from scheduler.manager import list_tasks
    return {"tasks": list_tasks()}


@router.post("/pipeline/scheduled")
async def create_pipeline_schedule(req: PipelineScheduleRequest):
    from scheduler.manager import add_pipeline_task

    config = _parse_config(req.yaml_content)   # 先驗證，別排一個跑不起來的
    config_d = config.model_dump()
    if req.workflow_id:
        config_d["_workflow_id"] = req.workflow_id
    yaml_to_save = yaml.dump({"pipeline": config_d}, allow_unicode=True,
                             default_flow_style=False)
    try:
        info = add_pipeline_task(name=req.name, schedule_type=req.schedule_type,
                                 schedule_expr=req.schedule_expr,
                                 yaml_content=yaml_to_save)
        return {"task": asdict(info)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/pipeline/scheduled/cancel-by-name/{name}")
async def cancel_pipeline_schedule(name: str):
    from scheduler.manager import remove_task_by_name
    if not remove_task_by_name(name):
        raise HTTPException(status_code=404, detail="找不到這個名稱的排程")
    return {"status": "ok"}


@router.delete("/pipeline/scheduled/{task_id}")
async def delete_pipeline_schedule(task_id: str):
    from scheduler.manager import remove_task
    if remove_task(task_id):
        return {"message": f"排程 {task_id} 已刪除"}
    raise HTTPException(status_code=404, detail="找不到這個排程")


# ── 序列化 ───────────────────────────────────────────────────────────

def run_to_dict(r) -> dict:
    """把 PipelineRun 轉成前端吃的 dict。

    （Atlas 這裡還會算 token 成本 —— Atlas-Lite 一毛錢都不花，沒有這一段。）
    """
    return {
        "run_id": r.run_id,
        "pipeline_name": r.pipeline_name,
        "status": r.status,
        "current_step": r.current_step,
        "total_steps": len(r.config_dict.get("steps", [])),
        "started_at": r.started_at,
        "ended_at": r.ended_at,
        "step_results": [
            {"step_index": s.step_index, "step_name": s.step_name,
             "exit_code": s.exit_code,
             "validation_status": s.validation_status,
             "validation_reason": s.validation_reason,
             "validation_suggestion": s.validation_suggestion,
             "retries_used": s.retries_used,
             "stdout_tail": s.stdout_tail, "stderr_tail": s.stderr_tail,
             "actual_output_path": s.actual_output_path or "",
             "step_vars": s.step_vars or {},
             "started_at": s.started_at or "", "ended_at": s.ended_at or ""}
            for s in r.step_results
        ],
        "config_dict": r.config_dict,
        "log_path": r.log_path,
        "awaiting_type": r.awaiting_type or "",
        "awaiting_message": r.awaiting_message or "",
        "awaiting_suggestion": r.awaiting_suggestion or "",
        "input_params": r.input_params or {},
        "workflow_id": r.workflow_id,
    }
