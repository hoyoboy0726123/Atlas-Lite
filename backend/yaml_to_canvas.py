"""YAML → canvas 轉換器。

使用情境：
- TG `/save` 命令把 AI 對話產生的 YAML 套到工作流時、同步重建 canvas（讓桌面開該工作流能看到節點）
- 一次性遷移 / 修復「YAML 有但 canvas 空」的工作流
- 安裝後 seed 範例工作流（seed_examples.py）

把 PipelineConfig 風格的 YAML（pyyaml 解析後的 dict）轉成 frontend stepsToFlow 期望的
{nodes: [...], edges: [...]} 格式。各種節點類型旗標的對應：
- human_confirm / computer_use / condition → 對應 React Flow node type
- 預設 → scriptStep

版面：condition（IF / Switch）分支會「扇形」攤開 —— 主線一排、分支對稱往上下展，
每條分支各自往右接成一列。讓使用者一眼看懂「這裡分流、各走各的」，不用解讀交叉的線。
"""
from __future__ import annotations

import yaml as _yaml
from typing import Optional

# 版面常數
_COL_W = 360   # 每一「欄」（深度）的水平間距
_ROW_H = 200   # 每一「列」（分支）的垂直間距
_Y0 = 160      # 主線的基準 y


def _step_to_node(step: dict, idx: int) -> dict:
    """把 YAML step dict 轉成 canvas node dict（對應 stepsToFlow 的輸出格式）。

    position 先放預設值、由 yaml_to_canvas() 算出扇形座標後覆寫。
    """
    name = step.get("name", f"步驟 {idx + 1}")
    common = {
        "id": f"step-{idx}",
        "position": {"x": idx * _COL_W, "y": _Y0},
    }
    base_data = {
        "name": name,
        "index": idx,
        "status": "idle",
        "errorMsg": "",
        "timeout": step.get("timeout", 300),
        "retry": step.get("retry", 1),
        # next:控制流跳轉(condition 分支用 next: end 防掉進對方分支)。
        # 任何節點都可能有,放 base_data 確保不會在 YAML↔canvas 來回時掉。
        "next": step.get("next", ""),
    }
    output = step.get("output") or {}
    output_path = output.get("path", "") if isinstance(output, dict) else ""
    # expect / json_schema:所有節點型別通吃,統一塞進 base_data ——
    # 只在某些分支帶的話,前端從 canvas 讀不到,autosave round-trip 就會把
    # YAML 裡寫好的驗證整批洗掉(Atlas 實測踩過)。
    if isinstance(output, dict):
        import json as _json_s
        base_data["expectText"] = output.get("expect") or output.get("description") or ""
        _js = output.get("json_schema")
        base_data["jsonSchemaText"] = (
            _json_s.dumps(_js, ensure_ascii=False) if isinstance(_js, dict) and _js else ""
        )
    else:
        base_data["expectText"] = ""
        base_data["jsonSchemaText"] = ""

    # human_confirm
    if step.get("human_confirm"):
        # message 存在 YAML 的 message: 欄,不是 batch ——
        # 讀 batch 會讓自訂訊息在 round-trip 後全變成預設的「請確認」。
        # notify/screenshot/preview/send_prev/hc_on_timeout 同理:canvas 不帶就會被 autosave 洗掉。
        return {**common, "type": "humanConfirmation", "data": {
            **base_data,
            "message": step.get("message") or step.get("batch") or "請確認",
            "outputPath": output_path,
            "sendOutput": step.get("send_output", True),
            "notifyTelegram": step.get("notify_telegram", True),
            "screenshot": bool(step.get("screenshot", False)),
            "sendPrevOutput": bool(step.get("send_prev_output", False)),
            "hcOnTimeout": step.get("hc_on_timeout", "wait"),
        }}


    # computer_use
    if step.get("computer_use"):
        return {**common, "type": "computerUse", "data": {
            **base_data,
            # ⚠ 這個 key 一定要跟 YAML/models.py 一致。寫成 computer_use_actions
            #   會讓轉出來的 canvas 動作是空的 —— 使用者一碰畫布,autosave 就用空 canvas
            #   重生 YAML 蓋回 DB,辛苦挑的 auto_id 永久消失。
            "actions": step.get("actions", []),
            # 同上：欄位名要跟 models.py 的 Step 一致（assets_dir / fail_fast），
            # 加 computer_use_ 前綴的話這兩個設定在匯入後就消失了。
            "assetsDir": step.get("assets_dir", ""),
            "failFast": step.get("fail_fast", True),
            # cu_mode / uia_window 原本整個漏掉 —— UIA 節點匯入後會退回 pixel 模式，
            # 且忘記目標視窗，回放時對著錯的視窗操作。
            "cuMode": step.get("cu_mode", "pixel"),
            "uiaWindow": step.get("uia_window", ""),
            "cvThreshold": step.get("cv_threshold", 0.5),
            "cvSearchOnlyNear": step.get("cv_search_only_near", False),
            "cvSearchRadius": step.get("cv_search_radius", 400),
            "cvTriggerHover": step.get("cv_trigger_hover", True),
            "cvHoverWaitMs": step.get("cv_hover_wait_ms", 200),
            "cvCoordFallback": step.get("cv_coord_fallback", False),
            "ocrThreshold": step.get("ocr_threshold", 0.6),
            "ocrCvFallback": step.get("ocr_cv_fallback", False),
        }}

    # condition(條件判斷節點)
    if step.get("condition"):
        has_switch = bool(step.get("switch"))
        return {**common, "type": "condition", "data": {
            **base_data,
            "mode": "switch" if has_switch else "if",
            "expression": step.get("expression", ""),
            "onTrue": step.get("on_true", ""),
            "onFalse": step.get("on_false", ""),
            "switch": step.get("switch", ""),
            "cases": step.get("cases", {}) or {},
            "default": step.get("default", ""),
        }}

    # 預設 script step
    return {**common, "type": "scriptStep", "data": {
        **base_data,
        "batch": step.get("batch", ""),
        "workingDir": step.get("working_dir", ""),
        "outputPath": output_path,
        # 前端 script 節點(StepData spread)讀的是 expect 鍵;expectedOutput 留著向後相容
        "expect": base_data.get("expectText") or step.get("expect", ""),
        "expectedOutput": base_data.get("expectText") or step.get("expect", ""),
    }}


def _is_end(nxt) -> bool:
    """next 是否為「分支終點、不再往下」標記。"""
    return isinstance(nxt, str) and nxt.strip().lower() == "end"


def _build_graph(steps: list[dict]) -> tuple[list[list[int]], list[dict]]:
    """從 steps 算出每個節點的出邊。

    回傳 (out, edges):
    - out[i] = 該節點往哪些節點(target index 清單)
    - edges  = [{"i", "j", "handle"}, ...]，handle 是 condition 分支標籤(顯示用)

    連線規則:
    - condition 節點 → 依 cases(Switch) 或 on_true/on_false(IF) 連到具名目標
    - 其他節點有 next: end → 不連(分支終點)
    - 其他節點有 next: <名稱> → 連到該節點
    - 其他節點無 next → 線性連到下一步(i → i+1)
    """
    n = len(steps)
    name_to_idx: dict[str, int] = {}
    for i, s in enumerate(steps):
        nm = s.get("name")
        if nm and str(nm) not in name_to_idx:
            name_to_idx[str(nm)] = i

    out: list[list[int]] = [[] for _ in range(n)]
    edges: list[dict] = []

    for i, s in enumerate(steps):
        targets: list[tuple[int, Optional[str]]] = []
        if s.get("condition"):
            if s.get("switch"):
                for label, tgt in (s.get("cases") or {}).items():
                    j = name_to_idx.get(str(tgt))
                    if j is not None:
                        targets.append((j, str(label)))
                dflt = s.get("default")
                if dflt and str(dflt) in name_to_idx:
                    targets.append((name_to_idx[str(dflt)], "預設"))
            else:
                ot = s.get("on_true")
                if ot and str(ot) in name_to_idx:
                    targets.append((name_to_idx[str(ot)], "成立"))
                of = s.get("on_false")
                if of and str(of) in name_to_idx:
                    targets.append((name_to_idx[str(of)], "不成立"))
        else:
            nxt = s.get("next")
            if _is_end(nxt):
                pass
            elif nxt:
                j = name_to_idx.get(str(nxt))
                if j is not None:
                    targets.append((j, None))
            elif i + 1 < n:
                targets.append((i + 1, None))

        # 同一目標去重:default 與某個 case 指向同一節點時(如 default 與「評價分歧」
        # 都指「產生分歧分析」)只畫一條邊、避免畫面上重複箭頭。先處理的 case 標籤較有意義、保留它。
        seen_j: set[int] = set()
        for j, handle in targets:
            if j in seen_j:
                continue
            seen_j.add(j)
            out[i].append(j)
            edges.append({"i": i, "j": j, "handle": handle})

    return out, edges


def _layout(n: int, out: list[list[int]]) -> list[dict]:
    """算出每個節點的扇形座標。

    - 欄(x):從起點算最長路徑深度
    - 列(y):沿邊 DFS;單一出邊 → 同列;多出邊(condition)→ 對稱往上下攤開
    """
    if n == 0:
        return []

    # 欄:最長路徑(放寬鬆弛,DAG 收斂)
    col = [0] * n
    for _ in range(n):
        changed = False
        for i in range(n):
            for j in out[i]:
                if col[j] < col[i] + 1:
                    col[j] = col[i] + 1
                    changed = True
        if not changed:
            break

    # 列:DFS,condition 多出邊對稱攤開
    lane: dict[int, float] = {}

    def dfs(i: int, l: float) -> None:
        if i in lane:
            return
        lane[i] = l
        ch = out[i]
        if len(ch) <= 1:
            for c in ch:
                dfs(c, l)
        else:
            k = len(ch)
            for idx, c in enumerate(ch):
                dfs(c, l + (idx - (k - 1) / 2.0))

    dfs(0, 0.0)
    for i in range(n):
        lane.setdefault(i, 0.0)

    # 匯流置中:多條分支收斂回同一節點時(如 IF 兩條路最後都接同一份報告),
    # 把該節點擺在各前驅的垂直中點、其後的單線子節點一起跟著移,線才不會歪。
    preds: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        for j in out[i]:
            preds[j].append(i)
    for j in sorted(range(n), key=lambda x: col[x]):
        if len(preds[j]) >= 2:
            lane[j] = sum(lane[p] for p in preds[j]) / len(preds[j])
            cur = j
            while len(out[cur]) == 1 and len(preds[out[cur][0]]) < 2:
                nxt = out[cur][0]
                lane[nxt] = lane[cur]
                cur = nxt

    return [
        {"x": col[i] * _COL_W, "y": _Y0 + lane[i] * _ROW_H}
        for i in range(n)
    ]


def yaml_to_canvas(yaml_str: str) -> Optional[dict]:
    """解析 YAML 字串、產出 canvas dict（{nodes, edges}）。

    回傳 None：YAML 為空、無 steps、解析失敗。
    回傳 dict：{nodes: [...], edges: [...]}
    - 線性工作流 → 一排;有 condition 分支 → 扇形攤開
    - edges 依 condition 的 cases/on_true/on_false 與各步 next 連接
    """
    if not yaml_str or not yaml_str.strip():
        return None
    try:
        parsed = _yaml.safe_load(yaml_str)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    parsed = parsed.get("pipeline", parsed)   # 支援頂層包一層 pipeline:
    if not isinstance(parsed, dict):
        return None
    raw_steps = parsed.get("steps") or []
    if not isinstance(raw_steps, list) or len(raw_steps) == 0:
        return None
    steps = [s for s in raw_steps if isinstance(s, dict)]
    if not steps:
        return None

    nodes = [_step_to_node(s, i) for i, s in enumerate(steps)]
    out, raw_edges = _build_graph(steps)
    positions = _layout(len(nodes), out)
    for node, pos in zip(nodes, positions):
        node["position"] = pos

    edges = [
        {
            "id": f"edge-{e['i']}-{e['j']}",
            "source": f"step-{e['i']}",
            "target": f"step-{e['j']}",
        }
        for e in raw_edges
    ]

    return {"nodes": nodes, "edges": edges}
