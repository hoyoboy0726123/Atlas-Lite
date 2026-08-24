"""AI 助手的工具。

## 為什麼不搬 Atlas 的 chat_tools
Atlas 那支 2630 行、35 個工具，一大半是 Lite 根本沒有的功能
（subagent、MCP、Outlook、長期記憶、web search、Telegram 傳檔）。
搬過來只會讓模型看到一堆呼叫了就報錯的工具。這裡只留 Lite 真的有的。

## 沒有原生 function calling
Ollama 有 tools 欄位、AiHub **沒有**（它只吃一個字串、回一段文字）。
與其為兩家寫兩套迴圈，統一走文字協議 —— 見 engine/chat_agent.py。
所以工具在這裡是「名字 → 簽名 + 說明 + 函式」的純資料，不綁任何框架。

## 兩步核准
會改東西的工具（save_workflow_yaml / patch_node_actions）一律 confirm=False
先回預覽，等使用者明確同意才 confirm=True 真寫。這條規則寫在每個工具的
說明裡 —— 文字協議下模型只看得到說明，說明沒寫模型就會直接覆蓋。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional

import db

log = logging.getLogger(__name__)

# 一次回給模型的上限。工作流 YAML 可能很長，全塞進去會擠掉對話歷史。
_MAX_YAML = 20000
_MAX_LOG = 12000


def _resolve_workflow(query: str) -> tuple[Optional[dict], str]:
    """用名稱或 id 找工作流。回 (工作流, 錯誤訊息)。

    ⚠ 找不到時**列出實際有哪些** —— 只回「找不到」模型會一直猜同一個錯名字。
    """
    q = (query or "").strip()
    if not q:
        return None, "要指定工作流名稱或 id"
    all_wf = db.list_workflows()
    if not all_wf:
        return None, "目前一個工作流都還沒有"
    for w in all_wf:                                   # id 完全相符
        if w["id"] == q:
            return db.get_workflow(w["id"]), ""
    exact = [w for w in all_wf if w["name"] == q]
    if len(exact) == 1:
        return db.get_workflow(exact[0]["id"]), ""
    part = [w for w in all_wf if q.lower() in (w["name"] or "").lower()]
    if len(part) == 1:
        return db.get_workflow(part[0]["id"]), ""
    if len(part) > 1:
        names = "、".join(w["name"] for w in part[:8])
        return None, f"「{q}」對到多個工作流：{names} —— 講明確一點"
    names = "、".join(w["name"] for w in all_wf[:10])
    return None, f"找不到「{q}」。目前有：{names}"


# ── 讀 ──────────────────────────────────────────────────────
def list_workflows() -> str:
    """列出所有工作流（名稱、id、幾個步驟、最後更新時間）。"""
    rows = db.list_workflows()
    if not rows:
        return "目前一個工作流都還沒有。"
    out = []
    for w in rows:
        full = db.get_workflow(w["id"]) or {}
        n = len((full.get("canvas") or {}).get("nodes") or [])
        out.append(f"- {w['name']}（id={w['id']}，{n} 個節點，更新於 {w.get('updated_at','?')}）")
    return "\n".join(out)


def get_workflow_yaml(query: str) -> str:
    """讀一個工作流的完整 YAML。

    Args:
      query*: 工作流名稱或 id
    """
    wf, err = _resolve_workflow(query)
    if err:
        return err
    y = wf.get("yaml") or ""
    if not y.strip():
        return (f"「{wf['name']}」還沒有 YAML（可能只在畫布上排了節點還沒存）。"
                f"畫布上有 {len((wf.get('canvas') or {}).get('nodes') or [])} 個節點。")
    if len(y) > _MAX_YAML:
        return y[:_MAX_YAML] + f"\n\n…（YAML 太長，只顯示前 {_MAX_YAML} 字）"
    return y


def list_workflow_variables(query: str) -> str:
    """列出一個工作流裡所有「取值」動作存下的變數，以及是哪個步驟存的。

    要把值傳到下一步時先叫這個 —— 變數名猜錯就會把字面 {{名稱}} 填進欄位。

    Args:
      query*: 工作流名稱或 id
    """
    import yaml as _yaml

    wf, err = _resolve_workflow(query)
    if err:
        return err

    # ⚠ 以 YAML 為準，canvas 只是後備。YAML 是 source of truth，
    #   canvas 可能還沒重建（例如剛用 API 更新過 yaml）。讀錯來源的後果很陰險：
    #   回「沒有任何變數」而不是「讀不到」，助手就會去補一個其實已經存在的
    #   取值動作 —— 實測就這樣繞掉 3 輪。
    steps: list = []
    raw = wf.get("yaml") or ""
    if raw.strip():
        try:
            spec = _yaml.safe_load(raw) or {}
            for s in (spec.get("steps") or []):
                if isinstance(s, dict):
                    steps.append((s.get("name") or "?", s.get("actions") or []))
        except Exception as e:
            log.warning(f"[chat_tools] YAML 解析失敗、改讀 canvas:{e}")
    if not steps:
        for nd in ((wf.get("canvas") or {}).get("nodes") or []):
            d = nd.get("data") or {}
            steps.append((d.get("label") or d.get("name") or nd.get("id"),
                          d.get("actions") or []))

    found: list[str] = []
    for step, actions in steps:
        for i, a in enumerate(actions):
            if not isinstance(a, dict):
                continue
            sa = (a.get("save_as") or "").strip()
            if sa:
                found.append(f"- {{{{{sa}}}}}　由「{step}」的第 {i+1} 個動作"
                             f"（{a.get('type')}）存下"
                             + (f"，標籤「{a.get('label')}」" if a.get("label") else ""))
    if not found:
        if not steps:
            return ("讀不到這個工作流的步驟 —— YAML 是空的、canvas 也沒有節點。"
                    "先在畫布上排節點並存檔。")
        return ("這個工作流還沒有任何動作設了 save_as —— 也就是沒有任何變數。"
                "要取值請先加 ocr_get_text 或 uia_get_text 動作並填 save_as。")
    return ("\n".join(found)
            + "\n\n同一個節點內直接寫 {{變數名}}；"
              "跨節點要寫 {{ steps.<步驟名>.output.<變數名> }}。")


def get_recent_runs(query: str = "", limit: int = 5) -> str:
    """最近幾次執行的結果（成功/失敗、時間、run_id）。

    Args:
      query: 只看某個工作流（留空 = 全部）
      limit=5: 最多幾筆
    """
    wf_id = None
    if (query or "").strip():
        wf, err = _resolve_workflow(query)
        if err:
            return err
        wf_id = wf["id"]
    rows = db.list_runs(limit=max(1, min(int(limit), 20)), workflow_id=wf_id)
    if not rows:
        return "還沒有任何執行紀錄。"
    out = []
    for r in rows:
        out.append(f"- {r.get('status','?')}　{r.get('started_at','?')}　"
                   f"run_id={r.get('run_id') or r.get('id')}")
    return "\n".join(out)


def get_run_log(run_id: str, max_chars: int = _MAX_LOG) -> str:
    """讀某次執行的完整 log —— 查失敗原因用。

    Args:
      run_id*: 執行 id（從 get_recent_runs 拿）
      max_chars=12000: 最多讀多少字（從尾端讀，失敗原因通常在最後）
    """
    run = db.load_run((run_id or "").strip())
    if not run:
        return f"找不到 run_id={run_id}。用 get_recent_runs 看有哪些。"
    # ⚠ 優先讀完整 log 檔 —— 跟使用者畫面上「Log」抽屜同一個來源。
    #   舊版讀 run["steps"]（**不存在的鍵**，實際結構是 step_results/stdout_tail），
    #   永遠回「沒有步驟輸出」→ 助手看不到 log、只能長篇瞎猜可能原因
    #   （使用者實測回報的正是這個症狀）。
    text = ""
    try:
        from pathlib import Path
        p = Path(run.get("log_path") or "")
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning(f"[chat_tools] 讀 log 檔失敗:{e}")
    if not text:
        parts = []
        for s in (run.get("step_results") or []):
            parts.append(f"── {s.get('step_name','?')}　[{s.get('validation_status','?')}]"
                         + (f"　原因:{s.get('validation_reason')}" if s.get("validation_reason") else ""))
            for k in ("stdout_tail", "stderr_tail", "validation_suggestion"):
                v = (s.get(k) or "").strip()
                if v:
                    parts.append(f"  {k}: {v}")
            if s.get("step_vars"):
                parts.append(f"  變數: {s['step_vars']}")
        text = "\n".join(parts) or "（這次執行沒有留下任何步驟輸出）"
    n = max(500, int(max_chars))
    if len(text) > n:
        # 從尾端截 —— 失敗原因幾乎都在最後，截頭會把它砍掉
        text = f"…（前面 {len(text) - n} 字省略）\n" + text[-n:]
    return text


# ── 寫（一律兩步核准）─────────────────────────────────────
def _regen_canvas(yaml_content: str) -> Optional[dict]:
    from yaml_to_canvas import yaml_to_canvas
    return yaml_to_canvas(yaml_content)


def _validate_yaml(yaml_content: str) -> str:
    """回錯誤訊息，沒問題回空字串。"""
    import yaml as _yaml
    try:
        spec = _yaml.safe_load(yaml_content)
    except Exception as e:
        return f"YAML 語法錯誤：{e}"
    if not isinstance(spec, dict):
        return "YAML 最外層要是一個物件（有 name / steps）"
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        return "YAML 缺 steps，或 steps 不是清單"
    names = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            return f"第 {i+1} 個 step 不是物件"
        nm = (s.get("name") or "").strip()
        if not nm:
            return f"第 {i+1} 個 step 沒有 name"
        names.append(nm)
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        # 步驟名是跨節點引用 {{ steps.X.output.Y }} 的鍵，重複等於引用會指錯人
        return f"步驟名稱重複：{'、'.join(sorted(dup))} —— 跨節點引用會指到錯的步驟"
    return ""


_XREF_RE = re.compile(r"\{\{\s*steps\.(.+?)\.output\.(.+?)\s*\}\}")


def _check_xrefs(steps: list, target_idx: int, actions: list) -> str:
    """檢查動作裡的 {{ steps.X.output.Y }} 引用得通不通。回錯誤訊息，沒問題回空字串。

    ⚠ 為什麼要在寫入前擋：實測 gpt-oss 會把步驟名記錯
      （實際是「讀憑證金額」，它寫成「讀取金額」）。執行時 Jinja 會拋
      「'dict object' has no attribute '讀取金額'」—— 訊息是對的，但使用者要等到
      真的跑工作流才發現，而助手**當下就有辦法知道**（步驟名它剛讀過）。

    順便用機制保證「取值動作要排在填值動作之前」這條規則 ——
    光寫在提示詞裡，模型照樣會把順序弄反。
    """
    names = [(s.get("name") or "") for s in steps if isinstance(s, dict)]
    problems: list[str] = []
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            continue
        for v in a.values():
            if not isinstance(v, str):
                continue
            for step_ref, var_ref in _XREF_RE.findall(v):
                step_ref, var_ref = step_ref.strip(), var_ref.strip()
                if step_ref not in names:
                    problems.append(
                        f"第 {i+1} 個動作引用了步驟「{step_ref}」，但這個工作流沒有這個步驟。"
                        f"實際有：{'、'.join(n for n in names if n)}")
                    continue
                src_idx = names.index(step_ref)
                if src_idx >= target_idx:
                    problems.append(
                        f"第 {i+1} 個動作引用「{step_ref}」的變數，但那個步驟排在"
                        f"目前這步{'之後' if src_idx > target_idx else '（就是本身）'}"
                        f"—— 執行到這裡時變數還不存在。取值要排在填值之前。")
                    continue
                src = steps[src_idx]
                saved = {(x.get("save_as") or "").strip()
                         for x in (src.get("actions") or []) if isinstance(x, dict)}
                saved.discard("")
                if var_ref not in saved:
                    problems.append(
                        f"第 {i+1} 個動作引用 {step_ref}.output.{var_ref}，"
                        f"但那個步驟沒有存這個變數。它存的是："
                        + ("、".join(sorted(saved)) if saved else "（沒有任何 save_as）"))
    return "\n".join(problems)


def _step_names(yaml_content: str) -> list[str]:
    import yaml as _yaml
    try:
        spec = _yaml.safe_load(yaml_content) or {}
        return [(s.get("name") or "?") for s in (spec.get("steps") or [])
                if isinstance(s, dict)]
    except Exception:
        return []


def _step_diff(old_yaml: str, new_yaml: str) -> str:
    """整份覆寫時，把步驟的增 / 刪 / 改名攤在預覽裡。

    ⚠ 這是最後一道關卡。實測模型被要求「幫我加一個動作」時，會捨棄
      patch_node_actions 改用整份覆寫，順手把步驟名重寫（「讀憑證金額」→
      「讀取金額」）、甚至憑空多加步驟 —— 而步驟名是跨節點引用
      {{ steps.X.output.Y }} 的鍵，改名等於把所有引用打斷。
      使用者看不到差異就會直接說「好」。
    """
    a, b = _step_names(old_yaml), _step_names(new_yaml)
    if not a:
        return ""
    if a == b:
        return "  步驟名稱沒有變動。\n"
    sa, sb = set(a), set(b)
    lines = []
    if gone := [n for n in a if n not in sb]:
        lines.append(f"  ⚠ 消失的步驟：{'、'.join(gone)}"
                     f"（跨節點引用 {{{{ steps.<步驟名>.output.… }}}} 會斷掉）")
    if new := [n for n in b if n not in sa]:
        lines.append(f"  ⚠ 新增的步驟：{'、'.join(new)}")
    if not lines:
        lines.append(f"  ⚠ 步驟順序有變：{' → '.join(a)}　改成　{' → '.join(b)}")
    return "\n".join(lines) + "\n"


def save_workflow_yaml(query: str, yaml_content: str, confirm: bool = False) -> str:
    """覆寫一個工作流的整份 YAML。

    ⚠ 這會蓋掉整個工作流。confirm=False 只回預覽（預設）；
    **等使用者明確說「好/確定/存吧」才設 confirm=True**。不要自己決定要存。

    Args:
      query*: 工作流名稱或 id
      yaml_content*: 完整的新 YAML
      confirm=False: False 只預覽、True 真的寫入
    """
    wf, err = _resolve_workflow(query)
    if err:
        return err
    bad = _validate_yaml(yaml_content)
    if bad:
        return f"這份 YAML 有問題，沒有寫入：{bad}"
    if not confirm:
        old = len((wf.get("yaml") or "").splitlines())
        new = len(yaml_content.splitlines())
        return (f"【預覽，尚未寫入】要覆寫「{wf['name']}」的 YAML"
                f"（{old} 行 → {new} 行）。\n"
                + _step_diff(wf.get("yaml") or "", yaml_content)
                + f"\n請使用者確認後，再用 confirm=True 呼叫一次。")
    patch = {"yaml": yaml_content}
    cv = _regen_canvas(yaml_content)
    if cv:
        # canvas 一定要同步重建 —— 留著舊 canvas 的話，使用者一開畫布，
        # 前端 autosave 就會用舊 canvas 重生 YAML 蓋回去，等於白改
        patch["canvas"] = cv
    db.update_workflow(wf["id"], patch)
    return f"已寫入「{wf['name']}」（{len(yaml_content.splitlines())} 行）。"


def patch_node_actions(query: str, step_name: str, ops_json: str,
                       confirm: bool = False) -> str:
    """改某個 computer_use 步驟的動作序列 —— 助手「直接幫使用者設定」靠這個。

    比起叫使用者自己寫 YAML，這個能精準地只動一個步驟的動作，不碰其他東西。

    ops_json 是一個 JSON 陣列，每個元素一個操作：
      {"op":"append", "action":{...}}              加在最後
      {"op":"insert", "index":2, "action":{...}}   插在第 3 個之前（index 從 0 算）
      {"op":"set",    "index":2, "action":{...}}   取代第 3 個
      {"op":"delete", "index":2}                   刪掉第 3 個
      {"op":"move",   "index":2, "to":0}           把第 3 個搬到最前面

    常用的動作型別：
      {"type":"ocr_get_text","label":"總計金額","direction":"right",
       "kind":"amount","save_as":"金額"}      螢幕 OCR 讀標籤旁邊的值
      {"type":"uia_get_text","control":{...},"save_as":"單號"}   UIA 讀欄位值（更準）
      {"type":"type_text","text":"{{金額}}"}                     把變數填進去

    ⚠ 取值動作一定要排在填值動作**之前**，否則變數是空的。
    ⚠ confirm=False 只回預覽（預設）；**等使用者明確同意才 confirm=True**。

    Args:
      query*: 工作流名稱或 id
      step_name*: 要改哪一個步驟（YAML 裡的 name）
      ops_json*: 上面格式的 JSON 陣列字串
      confirm=False: False 只預覽、True 真的寫入
    """
    import yaml as _yaml

    wf, err = _resolve_workflow(query)
    if err:
        return err
    raw = wf.get("yaml") or ""
    if not raw.strip():
        return f"「{wf['name']}」還沒有 YAML —— 先在畫布上排好節點並存檔。"
    try:
        spec = _yaml.safe_load(raw)
    except Exception as e:
        return f"這個工作流現有的 YAML 解析不了：{e}"

    steps = (spec or {}).get("steps") or []
    target, target_idx = None, -1
    for i, s in enumerate(steps):
        if isinstance(s, dict) and (s.get("name") or "").strip() == step_name.strip():
            target, target_idx = s, i
            break
    if target is None:
        have = "、".join((s.get("name") or "?") for s in steps if isinstance(s, dict))
        return f"找不到步驟「{step_name}」。這個工作流有：{have}"
    # ⚠ 步驟**沒有 type 欄位**。桌面自動化節點是靠 `computer_use: true` 這個
    #   旗標認的（見 models.py 的 Step）。寫成 step.get("type") == "computer_use"
    #   會讓這個工具對所有真實工作流一律拒絕。
    if not target.get("computer_use"):
        kinds = [k for k in ("script", "condition", "human_confirm") if target.get(k)]
        return (f"步驟「{step_name}」不是桌面自動化節點"
                f"（它是 {kinds[0] if kinds else '其他型別'} 節點），沒有動作序列。"
                f"這個工具只能改 computer_use 節點。")

    if isinstance(ops_json, list):
        ops = ops_json          # 模型常直接送 JSON 陣列而不是字串 —— 收下，別浪費一輪
    else:
        try:
            ops = json.loads(ops_json)
        except Exception as e:
            # 模型很常送單引號或包 ``` 圍籬的「JSON」。回錯誤讓它重來要多花一輪
            # （每輪 5-20 秒），寬鬆挖一次划算得多。
            try:
                from engine.llm import loads_loose
                ops = loads_loose(ops_json)
            except Exception:
                return f"ops_json 不是合法的 JSON：{e}"
    if not isinstance(ops, list) or not ops:
        return "ops_json 要是一個非空的 JSON 陣列"

    actions = list(target.get("actions") or [])
    before_n = len(actions)
    log_lines = []
    for k, op in enumerate(ops):
        if not isinstance(op, dict):
            return f"第 {k+1} 個操作不是物件"
        kind = (op.get("op") or "").lower()
        idx = op.get("index")
        # ⚠ index 越界要當場擋下。放行的話 list.insert 會默默夾到頭尾，
        #   動作被塞到完全不是使用者說的位置，而且回報成功。
        if kind in ("insert", "set", "delete", "move"):
            if not isinstance(idx, int):
                return f"第 {k+1} 個操作（{kind}）缺 index"
            hi = len(actions) if kind == "insert" else len(actions) - 1
            if idx < 0 or idx > hi:
                return (f"第 {k+1} 個操作的 index={idx} 超出範圍"
                        f"（目前有 {len(actions)} 個動作，可用 0..{hi}）")
        if kind in ("append", "insert", "set"):
            act = op.get("action")
            if not isinstance(act, dict) or not (act.get("type") or "").strip():
                return f"第 {k+1} 個操作（{kind}）缺 action，或 action 沒有 type"
        if kind == "append":
            actions.append(op["action"]); log_lines.append(f"+ 加在最後：{op['action'].get('type')}")
        elif kind == "insert":
            actions.insert(idx, op["action"]); log_lines.append(f"+ 插在第 {idx+1} 個之前：{op['action'].get('type')}")
        elif kind == "set":
            log_lines.append(f"~ 取代第 {idx+1} 個：{actions[idx].get('type')} → {op['action'].get('type')}")
            actions[idx] = op["action"]
        elif kind == "delete":
            log_lines.append(f"- 刪掉第 {idx+1} 個：{actions[idx].get('type')}")
            actions.pop(idx)
        elif kind == "move":
            to = op.get("to")
            if not isinstance(to, int) or to < 0 or to > len(actions) - 1:
                return f"第 {k+1} 個操作（move）的 to 超出範圍（可用 0..{len(actions)-1}）"
            a = actions.pop(idx); actions.insert(to, a)
            log_lines.append(f"↕ 第 {idx+1} 個搬到第 {to+1} 個位置：{a.get('type')}")
        else:
            return f"不認得的 op「{kind}」—— 只能是 append/insert/set/delete/move"

    # 跨節點引用要在寫入前擋掉錯的 —— 執行時才發現代表使用者已經在等工作流跑完了
    xref_bad = _check_xrefs(steps, target_idx, actions)
    if xref_bad:
        return (f"這批修改的變數引用有問題，**沒有**執行：\n{xref_bad}\n"
                f"修好再呼叫一次。不確定有哪些變數就先用 list_workflow_variables。")

    preview = "\n".join(f"  {x}" for x in log_lines)
    summary = (f"「{wf['name']}」的步驟「{step_name}」："
               f"{before_n} 個動作 → {len(actions)} 個\n{preview}")
    if not confirm:
        return (f"【預覽，尚未寫入】{summary}\n\n"
                f"請使用者確認後，再用 confirm=True 呼叫一次。")

    target["actions"] = actions
    new_yaml = _yaml.safe_dump(spec, allow_unicode=True, sort_keys=False, width=4096)
    bad = _validate_yaml(new_yaml)
    if bad:
        return f"改完的 YAML 驗不過，沒有寫入：{bad}"
    patch = {"yaml": new_yaml}
    cv = _regen_canvas(new_yaml)
    if cv:
        patch["canvas"] = cv
    db.update_workflow(wf["id"], patch)
    return f"已寫入。{summary}"


# ── 按需知識庫 ──────────────────────────────────────────────
def read_help_doc(topic: str = "") -> str:
    """讀節點設定的教學文件。**系統提示沒寫的細節先來這裡查，不要憑印象猜欄位名。**

    可選 topic：
    - script        : script 節點（batch / working_dir / output / 背景服務）
    - project       : 接上既有 Python 專案的 SOP（venv、入口檔、bat）
    - condition     : 條件分支（expression / on_true / switch / cases）
    - human_confirm : 人工確認（message / Telegram / 截圖 / 超時）
    - computer_use  : 桌面自動化節點層設定（模式 / 視窗 pattern / 三層）
    - variables     : 變數與傳值（同節點 / 跨節點 / secrets / input）
    - patterns      : 多節點串接的完整範例 YAML

    Args:
      topic: 上述之一。留空 = 列出清單。
    """
    from help_docs import get_help_doc
    return get_help_doc(topic)


# ── 專案分析（幫使用者接既有 Python 專案）────────────────────
_SENSITIVE_GLOBS = (
    ".env", ".env.*", "*.key", "*.pem", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "*.gpg", "credentials*", "*credentials*",
    "*secret*", "*token*", ".npmrc",
)


def _is_sensitive_filename(name: str) -> bool:
    import fnmatch
    low = (name or "").lower()
    return any(fnmatch.fnmatch(low, p) for p in _SENSITIVE_GLOBS)


def _detect_proj_venv(proj) -> dict:
    """偵測 venv / .venv（Windows Scripts / Unix bin）。venv 優先。"""
    import os
    sub = "Scripts" if os.name == "nt" else "bin"
    py = "python.exe" if os.name == "nt" else "python"
    for vdir in ("venv", ".venv"):
        vpy = proj / vdir / sub / py
        if vpy.exists():
            return {"has_venv": True, "python_path": str(vpy.resolve()),
                    "venv_dir_name": vdir}
    return {"has_venv": False, "python_path": None, "venv_dir_name": None}


def inspect_project(path: str) -> str:
    """探查使用者的 Python 專案資料夾，為「啟動既有專案」的節點收集資訊。

    使用者貼專案路徑時**第一步就呼叫**，不要憑空猜入口或依賴。回 JSON：
    - venv：has_venv=true 時 python_path 就是該專案的 python，
      **組 batch 時把它加引號當前綴**（例 "C:\\proj\\venv\\Scripts\\python.exe" main.py）
    - entry_candidates：main.py / app.py / start.bat 等入口候選
    - dependency_files / readme_files / top_level_tree（敏感檔一律隱藏）

    之後用 read_project_file 讀入口檔與 README 判斷怎麼跑，再用
    patch_step_fields 組 script 節點（batch / working_dir）。

    Args:
      path*: 專案資料夾絕對路徑
    """
    from pathlib import Path
    p = (path or "").strip().strip('"').strip("'")
    if not p:
        return "請提供專案資料夾的絕對路徑。"
    proj = Path(p).expanduser()
    if not proj.exists():
        return f"路徑不存在：{proj}。請跟使用者確認正確的絕對路徑。"
    if proj.is_file():
        proj = proj.parent
    venv = _detect_proj_venv(proj)
    entry_names = ("main.py", "app.py", "run.py", "cli.py", "__main__.py",
                   "manage.py", "start.py", "server.py", "gui.py", "bot.py")
    dep_names = ("requirements.txt", "pyproject.toml", "Pipfile", "Pipfile.lock",
                 "environment.yml", "setup.py", "setup.cfg", "poetry.lock")
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv",
                 ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
                 "dist", "build", ".ruff_cache"}
    entries, deps, readmes, tree = [], [], [], []
    try:
        for child in sorted(proj.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
            nm = child.name
            if _is_sensitive_filename(nm):
                continue
            if child.is_dir():
                if nm in skip_dirs:
                    tree.append(f"{nm}/  (略)")
                    continue
                tree.append(f"{nm}/")
                try:   # 第二層只列 .py 與依賴檔
                    for g in sorted(child.iterdir()):
                        if _is_sensitive_filename(g.name):
                            continue
                        if g.is_file() and (g.suffix == ".py" or g.name in dep_names):
                            tree.append(f"  {nm}/{g.name}")
                            if g.name in entry_names:
                                entries.append(f"{nm}/{g.name}")
                except Exception:
                    pass
            else:
                tree.append(nm)
                if nm in entry_names or nm.lower().endswith(".bat"):
                    entries.append(nm)
                if nm in dep_names:
                    deps.append(nm)
                if nm.lower() in ("readme.md", "readme.txt", "readme.rst", "readme"):
                    readmes.append(nm)
    except Exception as e:
        return f"讀取資料夾失敗：{e}"
    if len(tree) > 120:
        tree = tree[:120] + [f"...(還有 {len(tree) - 120} 項略過)"]
    result = {
        "project_dir": str(proj.resolve()),
        "venv": venv,
        "entry_candidates": entries or "（頂層沒找到常見入口，請看 top_level_tree）",
        "dependency_files": deps,
        "readme_files": readmes,
        "top_level_tree": tree,
        "hint": ("有 venv → batch 用 python_path 加引號當前綴；無 venv → 用 python "
                 "並提醒使用者依賴可能缺。⚠ 組 batch 前**必須**先 read_project_file "
                 "讀入口檔 —— 參數格式（argparse 的 --flag 名 / 位置參數）猜錯，"
                 "執行就直接炸，而且是使用者跑的時候才發現。"),
    }
    return json.dumps(result, ensure_ascii=False, indent=1)


def read_project_file(path: str, max_chars: int = 8000) -> str:
    """讀專案裡的某個檔（原始碼 / README / requirements / bat），判斷怎麼跑。

    ⚠ .env / 金鑰 / credentials / token 等敏感檔一律拒讀。

    Args:
      path*: 檔案絕對路徑
      max_chars=8000: 最多回傳字元（超過截斷）
    """
    from pathlib import Path
    p = (path or "").strip().strip('"').strip("'")
    if not p:
        return "請提供檔案的絕對路徑。"
    f = Path(p).expanduser()
    if _is_sensitive_filename(f.name):
        return f"⛔ 拒讀：{f.name} 屬敏感檔（.env / 金鑰 / 憑證 / token）。"
    if not f.exists():
        return f"檔案不存在：{f}"
    if f.is_dir():
        return f"{f} 是資料夾。看目錄結構請用 inspect_project。"
    try:
        if f.stat().st_size > 2_000_000:
            return "檔案過大（>2MB），拒讀以免 token 爆。"
        raw = f.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"讀檔失敗：{e}"
    n = max(500, int(max_chars))
    if len(raw) > n:
        return raw[:n] + f"\n\n...（還有 {len(raw) - n} 字元被截斷）"
    return raw or "（檔案是空的）"


# ── 節點欄位編輯（script / condition / human_confirm 等）──────
# patch_node_actions 只管 computer_use 的動作序列；其他節點的欄位靠這個，
# 一樣兩步核准。跳轉目標（on_true 等）寫入前驗證存在，錯字當場擋。
_PATCHABLE_FIELDS = {
    # script
    "batch": str, "working_dir": str, "timeout": int, "retry": int,
    "background": bool, "ready_after_seconds": int, "background_keep": bool,
    # human_confirm
    "human_confirm": bool, "message": str, "notify_telegram": bool,
    "screenshot": bool, "send_prev_output": bool, "hc_on_timeout": str,
    # condition / 跳轉
    "condition": bool, "expression": str, "on_true": str, "on_false": str,
    "switch": str, "cases": dict, "default": str, "next": str,
    # computer_use 節點層
    "computer_use": bool, "cu_mode": str, "uia_window": str, "fail_fast": bool,
    # 輸出宣告（整個 dict：{"path": ..., "json_schema": ...}）
    "output": dict,
}
_JUMP_FIELDS = ("on_true", "on_false", "default", "next")


def patch_step_fields(query: str, step_name: str, fields_json: str,
                      confirm: bool = False) -> str:
    """改某個步驟的欄位（script 的 batch / 分支的 expression / 人工確認的 message…）。

    這是「幫使用者設定節點」的主力之一：patch_node_actions 管 computer_use 的
    動作序列，**其他所有欄位**用這個。欄位名與格式先 read_help_doc 對應主題查。

    fields_json 是 JSON 物件，例：
      {"batch": "\"C:\\proj\\venv\\Scripts\\python.exe\" main.py", "working_dir": "C:\\proj"}
      {"expression": "{{ steps.X.output.rows | int > 0 }}", "on_true": "下一步", "on_false": ""}
      {"message": "金額 {{ steps.讀值.output.金額 }} 正確嗎？", "screenshot": true}

    ⚠ confirm=False 只回預覽（預設）；**等使用者明確同意才 confirm=True**。
    跳轉欄位（on_true / on_false / default / next、cases 的值）必須是存在的步驟名。

    Args:
      query*: 工作流名稱或 id
      step_name*: 要改哪個步驟
      fields_json*: 上述格式的 JSON 物件字串
      confirm=False: False 預覽、True 寫入
    """
    import yaml as _yaml

    wf, err = _resolve_workflow(query)
    if err:
        return err
    raw = wf.get("yaml") or ""
    if not raw.strip():
        return f"「{wf['name']}」還沒有 YAML —— 先在畫布上排好節點並存檔。"
    try:
        spec = _yaml.safe_load(raw)
    except Exception as e:
        return f"現有 YAML 解析不了：{e}"
    steps = (spec or {}).get("steps") or []
    names = [(s.get("name") or "") for s in steps if isinstance(s, dict)]
    target = next((s for s in steps if isinstance(s, dict)
                   and (s.get("name") or "").strip() == step_name.strip()), None)
    if target is None:
        return f"找不到步驟「{step_name}」。這個工作流有：{'、'.join(n for n in names if n)}"

    if isinstance(fields_json, dict):
        fields = fields_json    # 模型常直接送 JSON 物件而不是字串 —— 收下，別浪費一輪
    else:
        try:
            fields = json.loads(fields_json)
        except Exception as e:
            try:
                from engine.llm import loads_loose
                fields = loads_loose(fields_json)
            except Exception:
                return f"fields_json 不是合法的 JSON：{e}"
    if not isinstance(fields, dict) or not fields:
        return "fields_json 要是一個非空的 JSON 物件"

    changes = []
    for k, v in fields.items():
        if k not in _PATCHABLE_FIELDS:
            return (f"欄位「{k}」不在可改清單。可改："
                    + "、".join(sorted(_PATCHABLE_FIELDS)))
        want = _PATCHABLE_FIELDS[k]
        if want in (int,) and isinstance(v, str) and v.strip().lstrip("-").isdigit():
            v = int(v)          # 模型常把數字送成字串，能救就救
            fields[k] = v       # ⚠ 要寫回 dict —— 只轉區域變數的話，
                                #   下面寫入迴圈用的還是原字串（實測寫出 timeout: '600'）
        if not isinstance(v, want) and not (want is str and v is None):
            return f"欄位「{k}」要是 {want.__name__}，收到 {type(v).__name__}：{v!r}"
        # 跳轉目標驗證 —— 打錯步驟名執行時才炸，這裡當場擋
        if k in _JUMP_FIELDS and isinstance(v, str) and v.strip():
            if v.strip() not in names:
                return (f"「{k}」指向的步驟「{v}」不存在。"
                        f"實際有：{'、'.join(n for n in names if n)}")
        if k == "cases" and isinstance(v, dict):
            bad_targets = [tv for tv in v.values()
                           if isinstance(tv, str) and tv.strip() and tv.strip() not in names]
            if bad_targets:
                return (f"cases 裡指向的步驟不存在：{'、'.join(bad_targets)}。"
                        f"實際有：{'、'.join(n for n in names if n)}")
        old = target.get(k)
        changes.append(f"{k}: {old!r} → {v!r}")

    summary = (f"「{wf['name']}」的步驟「{step_name}」：\n"
               + "\n".join(f"  {c}" for c in changes))
    if not confirm:
        return f"【預覽，尚未寫入】{summary}\n\n請使用者確認後，再用 confirm=True 呼叫一次。"

    for k, v in fields.items():
        target[k] = v
    new_yaml = _yaml.safe_dump(spec, allow_unicode=True, sort_keys=False, width=4096)
    bad = _validate_yaml(new_yaml)
    if bad:
        return f"改完的 YAML 驗不過，沒有寫入：{bad}"
    patch = {"yaml": new_yaml}
    cv = _regen_canvas(new_yaml)
    if cv:
        patch["canvas"] = cv
    db.update_workflow(wf["id"], patch)
    return f"已寫入。{summary}"


def add_step(query: str, step_json: str, position: str = "",
             confirm: bool = False) -> str:
    """在工作流裡**新增一個步驟**（人工確認 / 條件分支 / script / computer_use 骨架）。

    這是把節點「接上」的工具：patch_step_fields 只能改既有步驟，
    要插入新的人工確認、條件分支就用這個 —— **不要**用 save_workflow_yaml 整份重寫。
    欄位格式先 read_help_doc 對應主題查。

    step_json 是一個 JSON 物件（至少要有 name），例：
      {"name": "金額確認", "human_confirm": true,
       "message": "金額 {{ steps.讀值.output.金額 }} 正確嗎？", "screenshot": true}
      {"name": "檢查金額", "condition": true,
       "expression": "{{ steps.讀值.output.金額 | int > 0 }}",
       "on_true": "填進系統", "on_false": ""}

    Args:
      query*: 工作流名稱或 id
      step_json*: 上述格式的 JSON 物件字串
      position: 插在哪個既有步驟「之前」（步驟名）。留空 = 加在最後。
      confirm=False: False 預覽、True 寫入（等使用者同意才 True）
    """
    import yaml as _yaml

    wf, err = _resolve_workflow(query)
    if err:
        return err
    raw = wf.get("yaml") or ""
    try:
        spec = _yaml.safe_load(raw) if raw.strip() else {"name": wf["name"], "steps": []}
    except Exception as e:
        return f"現有 YAML 解析不了：{e}"
    if not isinstance(spec, dict):
        spec = {"name": wf["name"], "steps": []}
    steps = spec.setdefault("steps", [])

    if isinstance(step_json, dict):
        step = step_json
    else:
        try:
            step = json.loads(step_json)
        except Exception as e:
            try:
                from engine.llm import loads_loose
                step = loads_loose(step_json)
            except Exception:
                return f"step_json 不是合法的 JSON：{e}"
    if not isinstance(step, dict) or not (step.get("name") or "").strip():
        return "step_json 要是一個 JSON 物件，而且必須有 name"

    names = [(s.get("name") or "").strip() for s in steps if isinstance(s, dict)]
    new_name = step["name"].strip()
    if new_name in names:
        # ⚠ 訊息不能叫模型「換個名字」—— 實測模型把「重試同一次新增」當成
        #   「名字被佔用」，改名重加造成**重複步驟**（金額確認 + 金額人工確認）。
        #   先判斷是不是「剛剛已經加過同一個東西」。
        existing = next((s for s in steps if isinstance(s, dict)
                         and (s.get("name") or "").strip() == new_name), None)
        same = existing is not None and all(
            existing.get(k) == v for k, v in step.items() if k != "name")
        if same:
            return (f"步驟「{new_name}」**已經存在且內容相同** —— 這就是剛剛加好的那一個，"
                    f"任務已完成，不要換名字重加。直接告訴使用者已完成。")
        return (f"步驟名「{new_name}」已存在但內容不同。要改它的欄位用 patch_step_fields；"
                f"若是要加另一個不同用途的步驟才換名字。")

    # 跳轉目標驗證（新步驟自己可以被別人指，但它指出去的要存在或是自己）
    all_names = names + [new_name]
    for k in _JUMP_FIELDS:
        v = step.get(k)
        if isinstance(v, str) and v.strip() and v.strip() not in all_names:
            return (f"「{k}」指向的步驟「{v}」不存在。實際有：{'、'.join(all_names)}")
    if isinstance(step.get("cases"), dict):
        bad = [tv for tv in step["cases"].values()
               if isinstance(tv, str) and tv.strip() and tv.strip() not in all_names]
        if bad:
            return f"cases 指向的步驟不存在：{'、'.join(bad)}"

    idx = len(steps)
    pos_desc = "最後"
    if (position or "").strip():
        p = position.strip()
        if p not in names:
            return f"position「{p}」不是既有步驟。實際有：{'、'.join(names)}"
        idx = names.index(p)
        pos_desc = f"「{p}」之前（第 {idx + 1} 位）"

    kind = ("人工確認" if step.get("human_confirm")
            else "條件分支" if step.get("condition")
            else "桌面自動化" if step.get("computer_use")
            else "腳本")
    summary = f"在「{wf['name']}」的{pos_desc}新增步驟「{new_name}」（{kind}）"
    if not confirm:
        return (f"【預覽，尚未寫入】{summary}\n"
                f"內容：{json.dumps(step, ensure_ascii=False)[:400]}\n\n"
                f"請使用者確認後，再用 confirm=True 呼叫一次。")

    steps.insert(idx, step)
    new_yaml = _yaml.safe_dump(spec, allow_unicode=True, sort_keys=False, width=4096)
    bad = _validate_yaml(new_yaml)
    if bad:
        return f"加完的 YAML 驗不過，沒有寫入：{bad}"
    patch = {"yaml": new_yaml}
    cv = _regen_canvas(new_yaml)
    if cv:
        patch["canvas"] = cv
    db.update_workflow(wf["id"], patch)
    return f"已寫入。{summary}"


# ── 工具表 ──────────────────────────────────────────────────
# (名稱, 函式, 參數簽名, 說明)。簽名給文字協議用 —— 只給參數名的話
# 模型會漏填或填錯型別，所以標上必填 `*` 與預設值。
TOOLS: dict[str, dict[str, Any]] = {}


def _reg(fn: Callable, sig: str) -> None:
    TOOLS[fn.__name__] = {"fn": fn, "sig": sig,
                          "doc": (fn.__doc__ or "").strip()}


_reg(list_workflows, "")
_reg(get_workflow_yaml, "query*:str")
_reg(list_workflow_variables, "query*:str")
_reg(get_recent_runs, "query:str='', limit:int=5")
_reg(get_run_log, "run_id*:str, max_chars:int=12000")
_reg(save_workflow_yaml, "query*:str, yaml_content*:str, confirm:bool=False")
_reg(patch_node_actions, "query*:str, step_name*:str, ops_json*:str, confirm:bool=False")
_reg(patch_step_fields, "query*:str, step_name*:str, fields_json*:str, confirm:bool=False")
_reg(add_step, "query*:str, step_json*:str, position:str='', confirm:bool=False")
_reg(read_help_doc, "topic:str=''")
_reg(inspect_project, "path*:str")
_reg(read_project_file, "path*:str, max_chars:int=8000")

# 會改東西的工具 —— chat_agent 用這個決定要不要在 UI 上標「這步會動到資料」
MUTATING = {"save_workflow_yaml", "patch_node_actions", "patch_step_fields", "add_step"}
