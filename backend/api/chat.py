"""AI 助手的 HTTP 端點。

薄殼 —— 實際邏輯在 engine/chat_agent.py。

## 為什麼是 NDJSON 串流而不是一次回
AiHub v0.9 沒有 streaming（實測改 response_type 五個模型全部回伺服器端錯誤），
所以**文字沒辦法逐字吐**。但工具呼叫可以：一次提問可能跑好幾輪工具，
每輪 5-20 秒，全部做完才回等於讓使用者盯二十幾秒白屏。
這裡串的是「工具事件」，讓使用者看得到助手正在查什麼 —— 這是沒有 token
串流時唯一能給的「還活著」訊號。
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter()
log = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    role: str          # user | assistant
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    # 使用者當下在編輯哪個節點 —— 「問 AI」按鈕會帶這段狀態摘要進來。
    # 助手要接著他的進度講，沒有這個就只能從頭問一遍。
    extra_context: str = ""
    # 聊天視窗綁定的工作流。⚠ 一定要帶：不帶的話使用者說「幫我加抓值」，
    # 助手只能反問「請問是哪一個工作流？」—— 明明聊天視窗上就寫著綁定誰。
    workflow_id: str = ""
    temperature: float = 0.3


def _workflow_state_block(wf_id: str) -> str:
    """綁定工作流的狀態摘要，接進系統提示。

    對齊 Atlas 的 _workflow_state_block —— 移植聊天 UI 時漏了這層，
    實測後果：對話綁定「_runner通路驗證」，使用者說「我接下來要抓找補金額」，
    助手呼叫 list_workflows 後反問「請問您要在哪一個工作流加步驟？」。
    """
    import db
    try:
        import yaml as _yaml
        wf = db.get_workflow(wf_id)
    except Exception:
        return ""
    if not wf:
        return ""
    lines = [
        f"## 目前綁定的工作流：{wf['name']}",
        "使用者的聊天視窗綁著這條工作流。他說「這個工作流」「幫我加」「抓某某值」",
        "都是指它 —— 工具的 query 參數直接用上面的名稱，**不要再問他是哪一個工作流**。",
    ]
    steps = []
    try:
        spec = _yaml.safe_load(wf.get("yaml") or "") or {}
        steps = spec.get("steps") or []
    except Exception:
        pass
    if steps:
        lines.append("步驟：")
        for i, s in enumerate(steps, 1):
            if not isinstance(s, dict):
                continue
            kind = ("桌面自動化" if s.get("computer_use")
                    else "條件分支" if s.get("condition")
                    else "人工確認" if s.get("human_confirm")
                    else "腳本")
            acts = s.get("actions") or []
            sa = [a.get("save_as") for a in acts
                  if isinstance(a, dict) and a.get("save_as")]
            lines.append(f"  {i}. {s.get('name', '?')}（{kind}，{len(acts)} 個動作"
                         + (f"，存變數：{'、'.join(sa)}" if sa else "") + "）")
    else:
        n = len((wf.get("canvas") or {}).get("nodes") or [])
        lines.append(f"（還沒有 YAML；畫布上有 {n} 個節點，可能尚未存檔）")
    return "\n".join(lines)


def _merged_context(req: "ChatRequest") -> str:
    parts = []
    if req.workflow_id:
        blk = _workflow_state_block(req.workflow_id)
        if blk:
            parts.append(blk)
    if req.extra_context.strip():
        parts.append(req.extra_context.strip())
    return "\n\n".join(parts)


@router.get("/pipeline/chat/status")
async def chat_status():
    """助手能不能用。前端拿這個決定要不要把「問 AI」反灰。"""
    from engine import llm
    cap = llm.capability()
    return {"available": cap["available"], "reason": cap["reason"],
            "provider": cap.get("provider", ""), "model": cap.get("model", ""),
            # 三態：local 本機 / internal 華碩內部 / external 外部廠商
            "data_scope": cap.get("data_scope", ""),
            "data_scope_label": cap.get("data_scope_label", ""),
            "data_stays_local": cap.get("data_stays_local", False)}


@router.post("/pipeline/chat")
async def chat(req: ChatRequest):
    """跑一輪對話（可能含多次工具呼叫），一次回完整結果。"""
    from engine import chat_agent, llm
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages 不能是空的")
    msgs = [m.model_dump() for m in req.messages]
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, lambda: chat_agent.run(msgs, extra_context=_merged_context(req),
                                         temperature=req.temperature))
    except llm.LlmError as e:
        # LlmError 的訊息是寫給使用者看的（缺什麼、下一步怎麼做）——
        # 原樣送出去，不要包成「內部錯誤」
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/pipeline/chat/stream")
async def chat_stream(req: ChatRequest):
    """同上，但把工具事件即時串出來（NDJSON）。

    事件：
      {"type":"tool_start","name":...,"args":{...},"mutating":bool}
      {"type":"tool_end","name":...,"result_preview":"..."}
      {"type":"done","reply":"...","tool_calls":[...],"hit_round_limit":bool}
      {"type":"error","detail":"..."}
    """
    from engine import chat_agent, llm
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages 不能是空的")
    msgs = [m.model_dump() for m in req.messages]

    # agent 迴圈是同步的（httpx 同步 client），跑在工作執行緒；
    # 事件透過 queue 交回 event loop。哨兵 None 表示結束。
    q: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            out = chat_agent.run(msgs, extra_context=_merged_context(req),
                                 temperature=req.temperature,
                                 on_event=q.put)
            q.put({"type": "done", **out})
        except llm.LlmError as e:
            q.put({"type": "error", "detail": str(e)})
        except Exception as e:
            log.warning(f"[chat] 失敗:{e}", exc_info=True)
            q.put({"type": "error", "detail": f"{type(e).__name__}: {e}"})
        finally:
            q.put(None)

    async def gen():
        threading.Thread(target=worker, daemon=True).start()
        loop = asyncio.get_running_loop()
        while True:
            ev = await loop.run_in_executor(None, q.get)
            if ev is None:
                break
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")
