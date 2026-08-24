"""工作流 CRUD、匯出 / 匯入、可用變數查詢。"""
import io
import json
import os
import zipfile
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import db
from api.runs import lenient_yaml_load
from engine.expression import find_referenced_vars
from engine.models import PipelineConfig
from engine.store import get_store

router = APIRouter()


class WorkflowRequest(BaseModel):
    name: str = "新工作流"
    canvas: Optional[dict] = None


class WorkflowUpdateRequest(BaseModel):
    name: Optional[str] = None
    canvas: Optional[dict] = None
    yaml: Optional[str] = None


@router.get("/workflows")
async def api_list_workflows():
    return db.list_workflows()


@router.post("/workflows")
async def api_create_workflow(req: WorkflowRequest):
    return db.create_workflow(name=req.name, canvas=req.canvas)


@router.get("/workflows/{wf_id}")
async def api_get_workflow(wf_id: str):
    wf = db.get_workflow(wf_id)
    if not wf:
        raise HTTPException(status_code=404, detail="找不到工作流")
    return wf


@router.put("/workflows/{wf_id}")
async def api_update_workflow(wf_id: str, req: WorkflowUpdateRequest):
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    # 只帶 yaml 不帶 canvas（外部 API / Telegram 遙控更新）→ 從 yaml 重建 canvas。
    # 不重建的話 DB 留著舊 canvas，前端下次載入畫布再自動存檔，就會把新 yaml 洗回舊內容。
    if "yaml" in patch and "canvas" not in patch:
        try:
            from yaml_to_canvas import yaml_to_canvas
            cv = yaml_to_canvas(patch["yaml"])
            if cv:
                patch["canvas"] = cv
        except Exception:
            pass   # 重建失敗不阻擋 yaml 更新
    wf = db.update_workflow(wf_id, patch)
    if not wf:
        raise HTTPException(status_code=404, detail="找不到工作流")
    return wf


@router.delete("/workflows/{wf_id}")
async def api_delete_workflow(wf_id: str):
    db.delete_workflow(wf_id)
    return {"deleted": True}


# ── 匯出 / 匯入 ──────────────────────────────────────────────────────
# 只打包 workflow.json（名稱 + canvas + yaml）。Atlas 另外會打包 recipes/，
# Atlas-Lite 沒有 recipe。錨點圖不進包 —— 它們動輒幾十 MB，而且換一台機器
# 螢幕解析度不同本來就要重錄。

@router.get("/workflows/{wf_id}/export")
async def api_export_workflow(wf_id: str):
    wf = db.get_workflow(wf_id)
    if not wf:
        raise HTTPException(status_code=404, detail="找不到工作流")

    payload = {"name": wf["name"], "canvas": wf["canvas"], "yaml": wf.get("yaml", "")}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("workflow.json", json.dumps(payload, ensure_ascii=False, indent=2))
    buf.seek(0)

    safe = wf["name"].replace(" ", "_").replace("/", "_")
    return StreamingResponse(buf, media_type="application/zip", headers={
        "Content-Disposition":
            f'attachment; filename="workflow.zip"; filename*=UTF-8\'\'{quote(safe)}.zip'})


@router.post("/workflows/import")
async def api_import_workflow(file: UploadFile = File(...)):
    content = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="無效的 ZIP 檔案")
    if "workflow.json" not in zf.namelist():
        raise HTTPException(status_code=400, detail="ZIP 裡找不到 workflow.json")

    data = json.loads(zf.read("workflow.json"))
    # create_workflow 自己會避重名
    wf = db.create_workflow(name=data.get("name", "匯入的工作流"),
                            canvas=data.get("canvas"))
    # create_workflow 一律把 yaml 初始化成空字串（yaml 由 canvas 重生），
    # 但匯出包有原始 yaml → 寫回去，否則匯入後到第一次存檔前 yaml 都是空的。
    imported_yaml = (data.get("yaml") or "").strip()
    if imported_yaml:
        db.update_workflow(wf["id"], {"yaml": imported_yaml})
        wf["yaml"] = imported_yaml

    # 提醒使用者：桌面自動化節點的錨點圖沒有跟著進來
    needs_rerecord = any(
        n.get("data", {}).get("computerUse") or n.get("type") == "computerUse"
        for n in data.get("canvas", {}).get("nodes", []))
    return {"workflow": wf, "needs_reanchor": needs_rerecord}


# ── 可用變數 ─────────────────────────────────────────────────────────

def classify_node_type(step) -> str:
    if step.condition:
        return "condition"
    if step.human_confirm:
        return "human_confirm"
    if step.computer_use:
        return "computer_use"
    return "script"


@router.get("/workflows/{wf_id}/variables")
async def api_workflow_variables(wf_id: str):
    """列出這個工作流可用的變數 + 上次跑出來的實際值（給前端「插入變數」用）。

    回傳：
      available.steps[].fields[]  每個上游步驟提供的 output 欄位 + 上次的值
      available.input[]           這份 YAML 引用到的 input.X + 上次傳入值
      available.env[]             可用環境變數（過濾掉看起來像機密的）
      referenced                  整份 YAML 引用到的所有 dotted-path
    """
    wf = db.get_workflow(wf_id)
    if not wf:
        raise HTTPException(status_code=404, detail="找不到工作流")

    yaml_str = (wf.get("yaml") or "").strip()
    if not yaml_str:
        return {"available": {"steps": [], "input": [], "env": []},
                "referenced": [], "last_run_id": None}
    try:
        raw = lenient_yaml_load(yaml_str)
        config = PipelineConfig.from_dict(raw.get("pipeline", raw))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"工作流 YAML 解析失敗：{e}")

    # 撈最近幾次執行填 last_value。跨多次合併：某步的變數以「最近一次真的跑出
    # 該步變數的執行」為準 —— 這樣最新一次是半截 / 失敗時，之前的值也不會消失。
    last_run = None
    last_by_name: dict = {}
    last_input: dict = {}
    try:
        runs = get_store().list_by_workflow(wf_id, 10)   # 新→舊
        if runs:
            last_run = runs[0]
            last_input = runs[0].input_params or {}
        for r in runs:
            for sr in r.step_results:
                existing = last_by_name.get(sr.step_name)
                if existing is None:
                    last_by_name[sr.step_name] = sr
                elif not existing.step_vars and sr.step_vars:
                    # 先記到的那筆沒變數、這筆（較舊）有 → 用這筆把變數補回來
                    last_by_name[sr.step_name] = sr
    except Exception:
        pass

    # 1) 掃整份 YAML 找所有 {{ }} 引用
    referenced: set[str] = set()

    def scan(v):
        if isinstance(v, str) and v:
            referenced.update(find_referenced_vars(v))

    for step in config.steps:
        for fname in ("batch", "message", "uia_window", "working_dir"):
            scan(getattr(step, fname, ""))
        if step.output and step.output.path:
            scan(step.output.path)
        for a in (step.actions or []):
            for fname in ("text", "title", "title_contains", "vlm_prompt", "ocr_text"):
                scan(getattr(a, fname, ""))
            for v in (a.control or {}).values():
                scan(v)

    # 2) 上游步驟提供的欄位
    avail_steps = []
    for step in config.steps:
        if step.human_confirm:
            continue
        sr = last_by_name.get(step.name)
        fields: list[dict] = []

        if step.output and step.output.path:
            fields.append({"key": "path", "type": "string",
                           "last_value": (sr.actual_output_path if sr else "") or step.output.path,
                           "source": "output.path"})
        elif sr and sr.actual_output_path:
            fields.append({"key": "path", "type": "string",
                           "last_value": sr.actual_output_path, "source": "自動偵測"})
        if sr:
            fields += [
                {"key": "stdout", "type": "string",
                 "last_value": (sr.stdout_tail or "")[:200], "source": "stdout"},
                {"key": "exit_code", "type": "number",
                 "last_value": sr.exit_code, "source": "exit_code"},
                {"key": "status", "type": "string",
                 "last_value": sr.validation_status, "source": "驗證結果"},
            ]

        seen: set[str] = set()
        for a in (step.actions or []):
            if a.save_as and a.save_as not in seen:
                seen.add(a.save_as)
                last_v = (sr.step_vars.get(a.save_as, "") if sr and sr.step_vars else "")
                fields.append({"key": a.save_as, "type": "string",
                               "last_value": str(last_v) if last_v else "",
                               "source": f"save_as（{a.type}）"})

        # 腳本自動開放的變數：_step_export.json 與 .json 輸出欄位都落在 step_vars
        if sr and sr.step_vars:
            known = {f["key"] for f in fields}
            for k, v in sr.step_vars.items():
                if str(k) not in known:
                    is_num = isinstance(v, (int, float)) and not isinstance(v, bool)
                    fields.append({"key": str(k), "type": "number" if is_num else "string",
                                   "last_value": str(v), "source": "節點輸出"})

        avail_steps.append({"name": step.name,
                            "node_type": classify_node_type(step),
                            "fields": fields})

    # 3) input.X
    input_keys = sorted({ref.split(".", 1)[1] for ref in referenced
                         if ref.startswith("input.") and "." in ref})
    avail_input = [{"key": k, "last_value": last_input.get(k, ""), "required": True}
                   for k in input_keys]

    # 4) env（過濾掉名字看起來像機密的 —— 真的要用機密請走 {{ secrets.X }}）
    env_keys = {"ATLASLITE_DATA", "HOME", "USERPROFILE", "TIMEZONE"}
    env_keys.update(ref.split(".", 1)[1] for ref in referenced
                    if ref.startswith("env.") and "." in ref)

    def is_secretish(k: str) -> bool:
        return any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PWD"))

    avail_env = [{"key": k, "last_value": os.environ.get(k, ""), "is_secret": False}
                 for k in sorted(env_keys) if not is_secretish(k) and os.environ.get(k)]

    return {
        "available": {"steps": avail_steps, "input": avail_input, "env": avail_env},
        "referenced": sorted(referenced),
        "last_run_id": last_run.run_id if last_run else None,
    }


# ── 每工作流的 AI 對話歷史 ───────────────────────────────
# 切工作流就切對話。跟 Atlas 同一套端點與行為。

class ChatMessageIn(BaseModel):
    role: str
    content: str


class ChatBulkSetRequest(BaseModel):
    messages: list[ChatMessageIn]


@router.get("/workflows/{wf_id}/chat")
async def get_workflow_chat_api(wf_id: str):
    """載入指定工作流的對話歷史。"""
    msgs = db.get_workflow_chat(wf_id)
    if msgs is None:
        raise HTTPException(status_code=404, detail=f"找不到工作流：{wf_id}")
    return {"messages": msgs}


@router.post("/workflows/{wf_id}/chat")
async def append_workflow_chat_api(wf_id: str, msg: ChatMessageIn):
    """追加一則訊息（user 或 assistant）。回更新後的完整陣列。"""
    if msg.role not in ("user", "assistant"):
        raise HTTPException(status_code=400, detail="role 必須是 'user' 或 'assistant'")
    result = db.append_workflow_chat(wf_id, msg.role, msg.content)
    if result is None:
        raise HTTPException(status_code=404, detail=f"找不到工作流：{wf_id}")
    return {"messages": result}


@router.put("/workflows/{wf_id}/chat")
async def set_workflow_chat_api(wf_id: str, req: ChatBulkSetRequest):
    """整批覆寫（建新工作流時把 scratch 對話帶過去用）。"""
    msgs = [{"role": m.role, "content": m.content} for m in req.messages]
    if not db.set_workflow_chat(wf_id, msgs):
        raise HTTPException(status_code=404, detail=f"找不到工作流：{wf_id}")
    return {"messages": db.get_workflow_chat(wf_id)}


@router.delete("/workflows/{wf_id}/chat")
async def clear_workflow_chat_api(wf_id: str):
    """清空對話歷史（使用者按「清除對話」）。"""
    if not db.clear_workflow_chat(wf_id):
        raise HTTPException(status_code=404, detail=f"找不到工作流：{wf_id}")
    return {"messages": []}
