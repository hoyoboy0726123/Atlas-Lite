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
        "⚠ **工具是你的，不是使用者的。** 使用者的畫面上沒有這些工具。",
        "所以絕對不要寫「請用 patch_node_actions 工具加上去」「你可以呼叫 xxx」——",
        "那對他毫無意義，等於你把該做的事推回去給他。要用工具就自己輸出 <tool> 標籤。",
        "",
        "範例 —— 使用者問「我有哪些工作流？」，你的完整輸出就是這兩行（沒有其他字）：",
        "<tool>list_workflows</tool>",
        "<input>{}</input>",
        "系統會把結果接回來，你再據此回答。",
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

**先查再問。** 使用者問的東西只要工具查得到（有哪些工作流 → list_workflows、
有哪些變數 → list_workflow_variables、上次為什麼失敗 → get_recent_runs + get_run_log、
某個工作流長怎樣 → get_workflow_yaml），就**先呼叫工具**拿到答案再回 ——
不要反問使用者「請告訴我你的工作流名稱」，那是你自己查得到的東西，
反問等於把查詢工作推回去給他。只有工具查不到的（他想做什麼、目標是什麼）才問。

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

## 什麼時候可以直接寫入
會動到工作流的工具有 confirm 參數。判準是**使用者有沒有授權這件事**：

- 他用祈使句直接叫你做（「幫我加上去」「直接改」「就這樣做」「刪掉它」）
  → 已經授權了，直接 confirm=True 做完。再問一次「要不要做」是把他剛講的話
  退回去給他，很煩。
- 他在問或在討論（「該怎麼做」「可以嗎」「有什麼方法」）
  → 先 confirm=False 給預覽，講清楚你要改什麼，等他說好再 confirm=True。
- 你不確定他要的到底是哪一種 → 走預覽。改錯東西比多問一句嚴重。

**一次提問內同樣的修改只做一次。** 做完就告訴他結果，不要再呼叫同一個工具。

## 改動作序列一律用 patch_node_actions
要加 / 改 / 刪某個步驟的動作時，用 patch_node_actions —— 它只動那一個步驟。
**不要**改用 save_workflow_yaml 整份重寫。整份重寫時你會憑印象重打其他步驟，
把步驟名改掉（「讀憑證金額」→「讀取金額」）或多寫出使用者沒要求的步驟；
而步驟名是跨節點引用 {{ steps.<步驟名>.output.<變數名> }} 的鍵，
改名等於把所有引用打斷 —— 使用者不會發現，直到工作流跑出錯值。
save_workflow_yaml 只在使用者明確說「重寫整個工作流」時才用。

## 回答精簡（使用者明確要求過）
- **只回答被問的事**。問「log 正確嗎」就看 log 直說「正確，17/17 成功」或指出
  哪一行有問題 —— 不要列「通常可能是以下幾種原因」的清單。
- 工具結果是什麼就說什麼；讀不到就明講「讀不到」，**不要推測補白**。
- 預設 3~5 句講完；使用者追問再展開。

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


# 「我在等工具 / 正在查詢」的旁白特徵。判斷要保守 —— 誤判會把一個正常的
# 簡短回答硬推回去重問，使用者要多等一輪（每輪 5-20 秒）。
_WAIT_WORDS = ("等待", "等候", "稍候", "稍等", "正在查詢", "正在讀取", "正在取得",
               "查詢中", "讀取中", "執行中", "請稍", "工具回傳", "工具結果",
               # 「我先查詢一下」這種也是旁白 —— 說要查卻沒輸出標籤，一樣沒人執行
               "查詢一下", "查一下", "先查")


def _looks_like_fake_wait(reply: str) -> bool:
    """回覆是「宣稱在等工具」的旁白，而不是真正的答案。"""
    s = (reply or "").strip()
    # 長度是主要判準，關鍵詞是次要的。旁白都極短（實測「（等待工具回傳變數
    # 列表）」14 字），而合法的回答就算含「查一下」也會帶上下文而變長
    # （「請你到 UIA 面板看一下那個欄位的 auto_id…」42 字）。
    # 門檻偏嚴是刻意的：誤判只是讓使用者多等一輪，但把一個有用的答案硬推回去
    # 重問，比漏抓一個旁白更糟 —— 漏抓時使用者至少還看得到一句話。
    if not s or len(s) > 40:
        return False
    return any(w in s for w in _WAIT_WORDS)


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
    # 已經成功寫入過的 (工具, 參數) 指紋 —— 見下方冪等防護
    done_writes: set[str] = set()
    nudged = False          # 「宣稱在等工具」只回推一次，避免來回鬼打牆

    for rnd in range(_MAX_ROUNDS):
        reply = llm.chat(convo, temperature=temperature)
        parsed = _parse_tool_call(reply)
        if not parsed:
            # ⚠ 模型有時會回「（等待工具回傳變數列表）」這種話 —— 它以為自己
            #   呼叫了工具，其實**沒有輸出 <tool> 標籤**，所以沒有人去執行。
            #   不處理的話這句話就成了最終答案：使用者看到一句莫名其妙的話，
            #   什麼也沒發生。實測 gpt-oss 約每三次會發生一次。
            if not nudged and _looks_like_fake_wait(reply):
                nudged = True
                logger_msg = reply.strip()[:60]
                log.info(f"[chat_agent] 模型宣稱在等工具卻沒輸出標籤，回推一次：{logger_msg!r}")
                convo.append({"role": "assistant", "content": reply})
                convo.append({"role": "user", "content": (
                    "你沒有真的輸出工具標籤，所以什麼都沒有執行 —— 系統只看得到 "
                    "<tool>…</tool><input>…</input> 這個格式，看不到你的旁白。"
                    "現在請直接輸出標籤；如果其實不需要工具，就直接回答使用者。")})
                continue
            return {"reply": reply.strip(), "tool_calls": calls, "rounds": rnd + 1}

        name, args, prefix = parsed
        emit({"type": "tool_start", "name": name, "args": args,
              "mutating": name in MUTATING})

        # ⚠ 冪等防護：同一次提問裡，同樣的**寫入**只做一次。
        #   實測(qwen3:8b)使用者說「好，你直接幫我加上去」之後，模型連續三輪
        #   都輸出同一個 patch_node_actions(confirm=True) —— 每輪都真的寫進去，
        #   使用者的步驟裡就多了三個一模一樣的 type_text。
        #   模型看到「已寫入」的結果卻沒認出事情已經做完，光靠提示詞防不住。
        #   只擋 confirm=True 的寫入；預覽重複無害。指紋只在這一次 run() 內有效，
        #   使用者下一輪真的想再加一次，是新的對話、新的指紋集合。
        wkey = ""
        if name in MUTATING and args.get("confirm") is True:
            wkey = json.dumps([name, args], ensure_ascii=False, sort_keys=True)
            if wkey in done_writes:
                result = (f"這個修改剛剛已經成功寫入了，不要重複執行。"
                          f"直接告訴使用者已經完成，或問他還要不要改別的。")
                emit({"type": "tool_end", "name": name, "result_preview": result})
                calls.append({"name": name, "args": args, "result_preview": result,
                              "skipped_duplicate": True})
                convo.append({"role": "assistant", "content": reply})
                convo.append({"role": "user", "content": f"[工具 {name} 的執行結果]\n{result}"})
                continue

        result = _run_tool(tools, name, args)
        # 只有**成功**才記指紋 —— 失敗的話模型該有機會改參數重試。
        # 兩個 mutating 工具寫入成功時都回「已寫入」開頭（見 chat_tools）。
        if wkey and result.startswith("已寫入"):
            done_writes.add(wkey)
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
