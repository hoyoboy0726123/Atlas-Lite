"""AI 助手的對話迴圈 —— 文字協議版。

## 為什麼是文字協議，不是 function calling
Ollama 的 /api/chat 有 tools 欄位，但 **AiHub 沒有** —— 它只吃單一 message
字串、回一段文字，連 messages 概念都沒有。與其為兩家寫兩套迴圈（其中一套
還得再處理「模型不支援 tools」的退路），統一走文字協議：教模型輸出

    <tool>工具名</tool>
    <input>{"參數": "值"}</input>

系統執行後把結果接回對話，再讓模型繼續。這套在 Atlas 已經驗證過
（訂閱 CLI 大腦沒有原生 FC，走的就是這個）。

## 迴圈為什麼要有上限
模型可能一直呼叫工具不收尾（實測最常見的是重複呼叫同一個讀取工具）。
沒有上限就會一直燒 token 且永遠不回答，所以硬性限制輪數，超過就把
「已經查到的東西」交給模型要求它直接作答。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger(__name__)

_TOOL_RE = re.compile(r"<tool>\s*(.*?)\s*</tool>\s*<input>\s*(.*?)\s*</input>", re.S)
# 只寫了 <tool> 沒給 <input>：常見於無參數工具，要當成空參數收下而不是報錯
_TOOL_ONLY_RE = re.compile(r"<tool>\s*(.*?)\s*</tool>", re.S)

_MAX_ROUNDS = 6          # 一次提問最多讓模型呼叫幾輪工具
_MAX_RESULT = 6000       # 單個工具結果塞回對話的上限


def _tools_block(tools: dict) -> str:
    """把工具表教成文字協議。

    ⚠ 說明要**整段**帶，不能只取第一行。兩步核准（confirm=False 先預覽）
      這條規則寫在 docstring 中段 —— 只取第一行的話模型看不到，
      就會直接 confirm=True 覆蓋使用者的工作流。
    """
    lines = [
        "",
        "## 🔧 工具呼叫方式（本環境沒有原生 function calling，使用文字協議）",
        "需要工具時，輸出以下格式後**立刻停止**（系統會執行並把結果接回對話）：",
        "<tool>工具名</tool>",
        '<input>{"參數名": "值"}</input>',
        "一次只呼叫一個工具；參數一律用 JSON 物件。",
        "標 `*` 的是必填，標 `=值` 的是可省略。",
        "不需要工具時直接回答使用者，**絕對不要**輸出 <tool> 標籤，",
        "也不要宣稱「正在查詢」卻沒有實際輸出標籤 —— 那樣不會有人去執行。",
        "",
        "可用工具：",
    ]
    for name, t in tools.items():
        doc = t["doc"]
        if len(doc) > 900:
            doc = doc[:900] + "…"
        doc = "\n  ".join(doc.splitlines())
        lines.append(f"- **{name}**({t['sig']})\n  {doc}")
    return "\n".join(lines)


_BASE_PROMPT = """你是 Atlas-Lite 的助手。Atlas-Lite 是一個桌面自動化編排工具：
使用者在畫布上排節點（腳本 / 桌面自動化 / 條件分支 / 人工確認），存成 YAML 後執行。

## 你的職責
使用者卡住的時候接話。他們通常是**邊做邊想**，做到某一步才發現不知道怎麼繼續 ——
所以直接接著他的進度講「下一步做什麼」，不要從頭複述整條流程。

**能直接做的就直接做，不能做的才解說。**
- 變數怎麼傳、YAML 欄位填什麼、動作序列怎麼排 → 用工具直接改好給他。
- 需要他本人動手的（去畫面上挑控制項、錄製滑鼠點擊、開啟某個 app）
  → 講清楚「在哪個面板、按哪個鈕、挑什麼」，不要只說「請自行設定」。

## 關於變數（最常卡住的地方）
- 取值動作（ocr_get_text / uia_get_text）要填 save_as，值才會存成變數。
- 同一個節點內用 {{變數名}}；跨節點用 {{ steps.<步驟名>.output.<變數名> }}。
- **取值動作一定要排在填值動作之前**，否則變數是空的 —— 而且系統會擋下來
  不讓字面的 {{變數名}} 被填進欄位。
- 不確定有哪些變數時先叫 list_workflow_variables，不要猜名字。

## 改東西之前要先問
會動到工作流的工具預設 confirm=False，只回預覽。
**等使用者明確說好，才用 confirm=True 呼叫第二次。** 不要自己決定要存。

## 改動作序列一律用 patch_node_actions
要加 / 改 / 刪某個步驟的動作時，用 patch_node_actions —— 它只動那一個步驟。
**不要**改用 save_workflow_yaml 整份重寫。整份重寫時你會憑印象重打其他步驟，
把步驟名改掉（「讀憑證金額」→「讀取金額」）或多寫出使用者沒要求的步驟；
而步驟名是跨節點引用 {{ steps.<步驟名>.output.<變數名> }} 的鍵，
改名等於把所有引用打斷 —— 使用者不會發現，直到工作流跑出錯值。
save_workflow_yaml 只在使用者明確說「重寫整個工作流」時才用。

## 語言
一律用繁體中文回答。程式碼與 YAML 保持原樣。
"""


def build_system_prompt(tools: dict, extra: str = "") -> str:
    parts = [_BASE_PROMPT, _tools_block(tools)]
    if extra.strip():
        parts.append("\n## 使用者當前的狀態\n" + extra.strip())
    return "\n".join(parts)


def _parse_tool_call(text: str) -> Optional[tuple[str, dict, str]]:
    """從模型回覆裡挖工具呼叫。回 (工具名, 參數, 標籤前的文字) 或 None。"""
    m = _TOOL_RE.search(text or "")
    if m:
        name = m.group(1).strip()
        raw = (m.group(2) or "").strip()
        try:
            args = json.loads(raw) if raw else {}
        except Exception:
            # 模型常把 JSON 包在 ``` 裡，或前後多帶說明字 —— 寬鬆挖一次再放棄
            try:
                from engine.llm import loads_loose
                args = loads_loose(raw)
            except Exception:
                return (name, {"__parse_error__": raw[:300]}, text[:m.start()].strip())
        if not isinstance(args, dict):
            return (name, {"__parse_error__": raw[:300]}, text[:m.start()].strip())
        return (name, args, text[:m.start()].strip())
    m2 = _TOOL_ONLY_RE.search(text or "")
    if m2:
        return (m2.group(1).strip(), {}, text[:m2.start()].strip())
    return None


def _run_tool(tools: dict, name: str, args: dict) -> str:
    t = tools.get(name)
    if not t:
        # 列出實際有哪些 —— 只回「沒這個工具」模型會一直猜同一個錯名字
        return f"沒有「{name}」這個工具。可用的是：{'、'.join(tools)}"
    if "__parse_error__" in args:
        return (f"<input> 裡的內容不是合法的 JSON 物件，沒有執行。"
                f"收到的是：{args['__parse_error__']}")
    try:
        return str(t["fn"](**args))
    except TypeError as e:
        # 參數給錯（漏必填、多給不認得的）—— 把正確簽名回給模型讓它自己修
        return f"參數不對：{e}。正確用法：{name}({t['sig']})"
    except Exception as e:
        log.warning(f"[chat_agent] 工具 {name} 失敗:{e}", exc_info=True)
        return f"工具 {name} 執行失敗：{type(e).__name__}: {e}"


def run(messages: list[dict], tools: Optional[dict] = None,
        extra_context: str = "", temperature: float = 0.3,
        on_event: Optional[Callable[[dict], None]] = None) -> dict:
    """跑一輪對話（可能含多次工具呼叫）。回 {reply, tool_calls, rounds}。

    on_event 給呼叫端推進度用（工具開始 / 結束）。沒有串流可以推 token ——
    AiHub v0.9 移除了 streaming，為了兩家行為一致，這裡也不做。
    長答案要等 ~20 秒，所以工具事件是唯一能給使用者的「還活著」訊號，
    呼叫端最好用上。
    """
    from chat_tools import MUTATING, TOOLS
    from engine import llm

    tools = tools if tools is not None else TOOLS
    convo = [{"role": "system",
              "content": build_system_prompt(tools, extra_context)}] + list(messages)

    def emit(ev: dict) -> None:
        if on_event:
            try:
                on_event(ev)
            except Exception as e:
                log.debug(f"[chat_agent] on_event 失敗:{e}")

    calls: list[dict] = []
    for rnd in range(_MAX_ROUNDS):
        reply = llm.chat(convo, temperature=temperature)
        parsed = _parse_tool_call(reply)
        if not parsed:
            return {"reply": reply.strip(), "tool_calls": calls, "rounds": rnd + 1}

        name, args, prefix = parsed
        emit({"type": "tool_start", "name": name, "args": args,
              "mutating": name in MUTATING})
        result = _run_tool(tools, name, args)
        if len(result) > _MAX_RESULT:
            result = result[:_MAX_RESULT] + f"\n…（結果太長，只帶前 {_MAX_RESULT} 字）"
        calls.append({"name": name, "args": args, "result_preview": result[:200]})
        emit({"type": "tool_end", "name": name, "result_preview": result[:200]})

        convo.append({"role": "assistant", "content": reply})
        convo.append({"role": "user",
                      "content": f"[工具 {name} 的執行結果]\n{result}"})

    # 輪數用盡：不要再讓它呼叫工具，要它拿手上的東西直接作答。
    # 這裡回空字串或「還在查」對使用者毫無價值 —— 寧可要一個不完整的答案。
    convo.append({"role": "user",
                  "content": (f"已經呼叫了 {_MAX_ROUNDS} 次工具，不要再呼叫了。"
                              f"用你目前查到的資訊直接回答使用者；"
                              f"還缺什麼就明講缺什麼。")})
    final = llm.chat(convo, temperature=temperature)
    # 模型可能還是硬輸出 <tool> —— 剝掉，不然使用者會看到標籤
    final = _TOOL_RE.sub("", final)
    final = _TOOL_ONLY_RE.sub("", final).strip()
    return {"reply": final or "查了幾輪還是沒能給出答案，請換個問法或講得更具體。",
            "tool_calls": calls, "rounds": _MAX_ROUNDS,
            "hit_round_limit": True}
