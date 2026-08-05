"""UIA(Microsoft UI Automation)action 分派 + element tree 檢視。

跟 pixel-based(computer_use.py)互補:
- pixel 走錄製座標 + CV/OCR/VLM 找位置;對 canvas / 自繪 UI / Citrix 才需要
- UIA 走 Windows 原生 GUI 結構樹;對 WPF/WinForms/Office/.NET app 直接讀寫、
  不靠像素、座標漂移免疫

兩者**共用 ComputerUseAction.type 命名空間**(actions[] 同一個 list、依 type 分派):
  uia_click           — 找控制項 click 中心
  uia_send_keys       — 對控制項打字 / 按鍵
  uia_get_text        — 讀控制項文字、save_as 變數
  uia_get_table_rowcount — 讀 DataGrid / ListView 列數、save_as 變數
  uia_click_cell      — 點 DataGrid 第 N 列第 M 欄 cell
  uia_wait_enabled    — 等控制項 enabled / 出現
  uia_assert_state    — 驗控制項狀態(enabled / checked / focused / exists)

詳見 docs/uia-feature-evaluation.md。
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from typing import Optional, Any


# ── 共用:lazy import uiautomation(避免非 Windows / 沒裝時 import error 影響其他功能)──
def _get_auto():
    """lazy import、回 uiautomation module(失敗 raise 給上層處理)。"""
    import uiautomation
    return uiautomation


# ── 跨 step 變數儲存(uia_get_text / uia_get_table_rowcount 用 save_as 存的值)──
# scoped 到單個 step 的執行 session、執行完不保留。
# 變數替換語法:`{{var_name}}` 或 `{{var_name + 1}}`(支援簡單算術)
_VAR_PATTERN = None  # lazy compile


def _substitute_vars(text: str, variables: dict[str, Any]) -> str:
    """把 text 內的 {{var_name}} / {{var_name + N}} 替換為 variables 內的值。

    支援:
      {{x}}       → str(variables['x'])
      {{x + 1}}   → str(int(variables['x']) + 1)  (整數運算、便於 row_count + 1)
      {{x - 1}}   → str(int(variables['x']) - 1)

    找不到變數 → 原樣保留(不丟錯、避免炸掉)。
    """
    global _VAR_PATTERN
    if _VAR_PATTERN is None:
        import re
        _VAR_PATTERN = re.compile(r"\{\{\s*(\w+)\s*(?:([+-])\s*(\d+))?\s*\}\}")
    if not isinstance(text, str):
        return text

    def _repl(m):
        name = m.group(1)
        op = m.group(2)
        delta = m.group(3)
        if name not in variables:
            return m.group(0)  # 原樣
        v = variables[name]
        if op:
            try:
                vi = int(v)
                d = int(delta)
                v = vi + d if op == "+" else vi - d
            except (ValueError, TypeError):
                return str(v)  # 算術失敗就純字串替代
        return str(v)

    return _VAR_PATTERN.sub(_repl, text)


# ── window 定位 ─────────────────────────────────────────────────────────
def _resolve_window(auto, action: dict, step_window: str = ""):
    """把 action 或 step 層級的 window 設定解析成 uiautomation Window control。

    優先級:action.window > step_window > foreground 視窗。
    支援 wildcard(* 開頭 / 結尾 / 兩端)、底層走 RegexName。
    """
    win_pattern = (action.get("window") or step_window or "").strip()
    if not win_pattern:
        # 沒指定就用 foreground
        return auto.GetForegroundControl()

    # wildcard 處理
    has_star = "*" in win_pattern
    if has_star:
        # 轉 regex:* → .*
        import re
        regex = "^" + re.escape(win_pattern).replace(r"\*", ".*") + "$"
        return auto.WindowControl(searchDepth=1, RegexName=regex)
    else:
        return auto.WindowControl(searchDepth=1, Name=win_pattern)


def _find_control(auto, parent, control_def: dict, fallback_rect: Optional[list] = None):
    """在 parent 控制項下找符合 control_def 的子控制項。

    control_def 例:
      {"type": "Button", "name": "儲存"}
      {"type": "DataGrid", "auto_id": "main-grid"}
      {"name": "OK"}    # 沒指定 type、找任何 control

    fallback_rect: 當 control_def 沒 Name 也沒 AutomationId(純 type 搜尋
    uiautomation 會拒、丟 LookupError "searchProperties must not be empty")、
    用 ControlFromPoint(rect 中心)當 fallback。是 picker 抓到的當下 rect、
    對 PaneControl / GroupControl 等沒 name 的 generic 容器特別有用。
    """
    if not isinstance(control_def, dict):
        return None
    ctype = (control_def.get("type") or "").strip()
    name = (control_def.get("name") or "").strip()
    auto_id = (control_def.get("auto_id") or control_def.get("automation_id") or "").strip()

    # 沒任何識別資訊 → 走 rect fallback
    if not name and not auto_id:
        if fallback_rect and len(fallback_rect) == 4 and fallback_rect[2] > 0 and fallback_rect[3] > 0:
            cx = int(fallback_rect[0] + fallback_rect[2] // 2)
            cy = int(fallback_rect[1] + fallback_rect[3] // 2)
            try:
                return auto.ControlFromPoint(cx, cy)
            except Exception:
                return None
        return None

    # 組 kwargs
    kwargs = {"searchDepth": control_def.get("depth", 10)}
    if name:
        kwargs["Name"] = name
    if auto_id:
        kwargs["AutomationId"] = auto_id

    # 依 type 走對應 method;沒指定就走通用 Control
    method_name = f"{ctype}Control" if ctype else "Control"
    if not hasattr(parent, method_name):
        # 不存在的 type、退回通用 Control
        method_name = "Control"
    return getattr(parent, method_name)(**kwargs)


# ── inspect:回傳 element tree(給 frontend tree picker 用)──────────────
def inspect_window(window_pattern: str = "", max_depth: int = 6,
                   max_children_per_node: int = 50,
                   logger: Optional[logging.Logger] = None) -> dict:
    """檢視指定視窗的 UIA element tree、回 JSON 結構。

    Args:
        window_pattern: 視窗 title(支援 wildcard *)、空字串 = foreground
        max_depth: tree 深度上限(避免某些 app 有上千層)
        max_children_per_node: 每節點子元素上限(避免大表格 1 萬列展開)

    Returns 結構:
        {
            "ok": bool,
            "window": {"name": "...", "class": "...", "rect": [l,t,r,b]} 或 None,
            "tree": {
                "type": "Button",
                "name": "儲存",
                "auto_id": "save-btn",
                "rect": [x, y, w, h],
                "enabled": true,
                "children": [...],
            },
            "error": str (失敗時),
        }
    """
    log = logger or logging.getLogger(__name__)
    try:
        auto = _get_auto()
    except ImportError as e:
        return {"ok": False, "error": f"uiautomation 套件未安裝:{e}"}

    try:
        import re
        # 取得 window control
        if not window_pattern.strip():
            win = auto.GetForegroundControl()
        else:
            pat = window_pattern.strip()
            # 第一線:RegexName 比對(支援 * wildcard、預設視為 substring)
            if "*" in pat:
                regex = "^" + re.escape(pat).replace(r"\*", ".*") + "$"
            else:
                regex = "^.*" + re.escape(pat) + ".*$"
            win = auto.WindowControl(searchDepth=1, RegexName=regex)

            # 第二線:RegexName 萬一沒命中(uiautomation 對某些 shell window 如
            # Program Manager 的 RegexName 比對不穩)、改用 root.GetChildren() 在 Python 層手動比對
            if not win.Exists(2, 0.5):
                pat_lower = pat.lower().replace("*", "")
                root = auto.GetRootControl()
                matched = None
                for child in root.GetChildren():
                    try:
                        cname = (child.Name or "").strip()
                        if not cname:
                            continue
                        if pat_lower in cname.lower():
                            matched = child
                            break
                    except Exception:
                        continue
                if matched is not None:
                    win = matched
                else:
                    return {"ok": False, "error": f"找不到視窗:{window_pattern or '(foreground)'}"}

        # 元數據
        rect = win.BoundingRectangle
        win_meta = {
            "name": str(win.Name or ""),
            "class": str(getattr(win, "ClassName", "") or ""),
            "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
            "process_id": int(getattr(win, "ProcessId", 0) or 0),
        }

        # 遞迴抓 tree
        def _walk(ctrl, depth: int) -> dict:
            try:
                rect = ctrl.BoundingRectangle
                node = {
                    "type": str(ctrl.ControlTypeName or ""),
                    "name": str(ctrl.Name or "")[:200],   # 防超長 description
                    "auto_id": str(getattr(ctrl, "AutomationId", "") or ""),
                    "rect": [int(rect.left), int(rect.top),
                             int(rect.right - rect.left), int(rect.bottom - rect.top)],
                    "enabled": bool(getattr(ctrl, "IsEnabled", True)),
                    "offscreen": bool(getattr(ctrl, "IsOffscreen", False)),
                    "children": [],
                }
            except Exception as e:
                return {"type": "?", "name": f"(讀取失敗:{e!s:.80})", "children": []}

            if depth >= max_depth:
                return node

            try:
                kids = ctrl.GetChildren()[:max_children_per_node]
                for k in kids:
                    node["children"].append(_walk(k, depth + 1))
            except Exception:
                pass
            return node

        tree = _walk(win, depth=0)
        return {"ok": True, "window": win_meta, "tree": tree}

    except Exception as e:
        log.exception("inspect_window 失敗")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── action 分派 ──────────────────────────────────────────────────────────
@dataclass
class UiaActionResult:
    ok: bool
    message: str = ""
    saved_var: Optional[tuple[str, Any]] = None   # (var_name, value);step 層級的 variables 會收集


def execute_uia_action(action: dict, step_window: str,
                       variables: dict[str, Any],
                       logger: logging.Logger) -> UiaActionResult:
    """執行單個 uia_* action。

    variables 是 step 層級累積的(uia_get_text 等 save_as 進去的、
    後續 uia_send_keys.text 內 {{var}} 替換靠這個)。
    """
    try:
        auto = _get_auto()
    except ImportError as e:
        return UiaActionResult(False, f"uiautomation 未安裝:{e}")

    atype = action.get("type", "")
    try:
        win = _resolve_window(auto, action, step_window)
        if not win.Exists(2, 0.5):
            return UiaActionResult(False, f"找不到視窗(action={atype})")

        if atype == "uia_click":
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"))
            if not ctrl or not ctrl.Exists(2, 0.5):
                return UiaActionResult(False, f"找不到控制項:{action.get('control')}")
            # 優先用 UIA pattern 互動(不必把視窗拉到前景、真正背景操作);
            # pattern 都不支援才退回 ctrl.Click()(那個會 SetActive 把視窗叫到前景再用滑鼠點)
            method_used = ""
            try:
                ip = ctrl.GetInvokePattern()
                if ip:
                    ip.Invoke()
                    method_used = "InvokePattern"
            except Exception:
                pass
            if not method_used:
                try:
                    tp = ctrl.GetTogglePattern()
                    if tp:
                        tp.Toggle()
                        method_used = "TogglePattern"
                except Exception:
                    pass
            if not method_used:
                try:
                    sp = ctrl.GetSelectionItemPattern()
                    if sp:
                        sp.Select()
                        method_used = "SelectionItemPattern"
                except Exception:
                    pass
            if not method_used:
                try:
                    ep = ctrl.GetExpandCollapsePattern()
                    if ep:
                        # 展開 / 收合切換(menu / tree node 用)
                        if ep.ExpandCollapseState in (0, 2):  # collapsed / partial
                            ep.Expand()
                        else:
                            ep.Collapse()
                        method_used = "ExpandCollapsePattern"
                except Exception:
                    pass
            if not method_used:
                # fallback:退回滑鼠點(會叫前景、影響其他工作流)
                ctrl.Click()
                method_used = "Click(mouse, 視窗會被拉到前景)"
            return UiaActionResult(True, f"已點擊 {action.get('control', {}).get('name', '?')} via {method_used}")

        elif atype == "uia_send_keys":
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"))
            if not ctrl or not ctrl.Exists(2, 0.5):
                return UiaActionResult(False, f"找不到控制項:{action.get('control')}")
            text = action.get("text", "")
            keys = action.get("keys", [])
            if text:
                # variable 替換
                text = _substitute_vars(text, variables)
                # 優先 ValuePattern.SetValue(背景 work、瞬時、不用敲鍵盤)
                via_pattern = False
                try:
                    vp = ctrl.GetValuePattern()
                    if vp and not vp.IsReadOnly:
                        vp.SetValue(text)
                        via_pattern = True
                except Exception:
                    pass
                if not via_pattern:
                    # fallback:模擬鍵盤打字、需要 focus(可能拉前景)
                    ctrl.SendKeys(text)
                return UiaActionResult(True, f"已輸入 {text[:60]!r} via {'ValuePattern' if via_pattern else 'SendKeys(keyboard sim)'}")
            elif keys:
                # 把 ["ctrl","s"] 之類轉 uiautomation 格式 "{Ctrl}s"
                # 簡化版:單一鍵(含特殊)直接 SendKeys、組合用 {Ctrl} 之類
                if len(keys) == 1:
                    ctrl.SendKeys("{" + keys[0].capitalize() + "}" if len(keys[0]) > 1 else keys[0])
                else:
                    # 修飾鍵 + 一般鍵組合
                    mod_map = {"ctrl": "{Ctrl}", "shift": "{Shift}", "alt": "{Alt}", "win": "{Win}"}
                    parts = []
                    for k in keys[:-1]:
                        parts.append(mod_map.get(k.lower(), "{" + k.capitalize() + "}"))
                    last = keys[-1]
                    parts.append("{" + last.capitalize() + "}" if len(last) > 1 else last)
                    ctrl.SendKeys("".join(parts))
                return UiaActionResult(True, f"已按鍵 {'+'.join(keys)}")
            else:
                return UiaActionResult(False, "uia_send_keys 缺 text 或 keys")

        elif atype == "uia_get_text":
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"))
            if not ctrl or not ctrl.Exists(2, 0.5):
                return UiaActionResult(False, f"找不到控制項:{action.get('control')}")
            # 優先取 Value pattern、退到 Name
            text = ""
            try:
                vp = ctrl.GetValuePattern()
                text = vp.Value if vp else ""
            except Exception:
                pass
            if not text:
                text = ctrl.Name or ""
            save_as = (action.get("save_as") or "").strip()
            if save_as:
                return UiaActionResult(True, f"讀到 {text[:60]!r}、存到 {save_as}",
                                       saved_var=(save_as, text))
            return UiaActionResult(True, f"讀到 {text[:60]!r}(沒設 save_as)")

        elif atype == "uia_get_table_rowcount":
            grid = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"))
            if not grid or not grid.Exists(2, 0.5):
                return UiaActionResult(False, f"找不到表格:{action.get('control')}")
            # GridPattern 是標準介面;沒實作就退到子元素數
            n = 0
            try:
                gp = grid.GetGridPattern()
                if gp:
                    n = int(gp.RowCount)
            except Exception:
                pass
            if n == 0:
                # fallback:數 DataItem 子元素
                try:
                    n = sum(1 for c in grid.GetChildren()
                            if str(c.ControlTypeName or "") in ("DataItem", "ListItem", "TreeItem"))
                except Exception:
                    pass
            save_as = (action.get("save_as") or "").strip()
            if save_as:
                return UiaActionResult(True, f"表格列數 {n}、存到 {save_as}",
                                       saved_var=(save_as, n))
            return UiaActionResult(True, f"表格列數 {n}(沒設 save_as)")

        elif atype == "uia_click_cell":
            grid = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"))
            if not grid or not grid.Exists(2, 0.5):
                return UiaActionResult(False, f"找不到表格:{action.get('control')}")
            row = action.get("row", 0)
            col = action.get("column", 0)
            # variable 替換(row 可能是 "{{row_count + 1}}")
            if isinstance(row, str):
                row = int(_substitute_vars(row, variables))
            if isinstance(col, str):
                col = int(_substitute_vars(col, variables))
            row, col = int(row), int(col)

            cell = None
            try:
                gp = grid.GetGridPattern()
                if gp:
                    cell = gp.GetItem(row, col)
            except Exception:
                pass
            if cell is None:
                # fallback:用 DataItem 子層、第 row 個的第 col 個 child
                try:
                    items = [c for c in grid.GetChildren()
                             if str(c.ControlTypeName or "") in ("DataItem", "ListItem", "TreeItem")]
                    if 0 <= row < len(items):
                        cells = items[row].GetChildren()
                        if 0 <= col < len(cells):
                            cell = cells[col]
                except Exception:
                    pass
            if cell is None:
                return UiaActionResult(False, f"找不到 cell ({row}, {col})")
            # 優先 SelectionItemPattern.Select(cell 通常是可選元素、不必滑鼠點)
            method_used = ""
            try:
                sp = cell.GetSelectionItemPattern()
                if sp:
                    sp.Select()
                    method_used = "SelectionItemPattern"
            except Exception:
                pass
            if not method_used:
                try:
                    ip = cell.GetInvokePattern()
                    if ip:
                        ip.Invoke()
                        method_used = "InvokePattern"
                except Exception:
                    pass
            if not method_used:
                cell.Click()
                method_used = "Click(mouse、視窗會拉前景)"
            return UiaActionResult(True, f"已點 cell ({row}, {col}) via {method_used}")

        elif atype == "uia_wait_enabled":
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"))
            timeout = float(action.get("timeout_sec", 10))
            t_start = time.time()
            while time.time() - t_start < timeout:
                if ctrl and ctrl.Exists(0, 0) and ctrl.IsEnabled:
                    return UiaActionResult(True, f"控制項已 enabled、耗時 {time.time()-t_start:.1f}s")
                time.sleep(0.3)
            return UiaActionResult(False, f"等 {timeout}s 控制項仍未 enabled")

        elif atype == "uia_set_clipboard":
            # 把文字塞進 Windows 剪貼簿、給後續 Ctrl+V 用、跨步驟 / 跨節點傳值用
            # 文字支援 {{變數}} 替換(get_text / get_table_rowcount 存的變數都能用)
            raw = action.get("text", "")
            text = _substitute_vars(raw, variables) if isinstance(raw, str) else str(raw)
            try:
                import win32clipboard  # type: ignore
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
            except ImportError:
                return UiaActionResult(False, "pywin32 (win32clipboard) 未安裝、無法寫剪貼簿")
            except Exception as e:
                return UiaActionResult(False, f"寫剪貼簿失敗:{type(e).__name__}: {e}")
            return UiaActionResult(True, f"已寫剪貼簿 {text[:60]!r}({len(text)} 字)")

        elif atype == "uia_close_window":
            # 關閉視窗的「正確」方式:走 WindowPattern.Close()、不靠 title/X 點擊、
            # 不必把視窗拉前景。控制項可以是視窗本身或視窗內任何元素(會往上找 Window)
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"))
            if not ctrl:
                # 沒指定 control 時用 step 的 window
                ctrl = win
            if not ctrl.Exists(2, 0.5):
                return UiaActionResult(False, "找不到要關的視窗")
            # 往上找 Window control(WindowPattern 在 WindowControl 才有)
            target = ctrl
            for _ in range(20):  # 最多往上 20 層、防 infinite loop
                try:
                    if str(target.ControlTypeName or "") == "WindowControl":
                        break
                    parent = target.GetParentControl()
                    if not parent:
                        break
                    target = parent
                except Exception:
                    break
            try:
                wp = target.GetWindowPattern()
                if wp:
                    wp.Close()
                    return UiaActionResult(True, f"已關閉視窗 {target.Name!r:.60} via WindowPattern")
            except Exception as e:
                return UiaActionResult(False, f"WindowPattern.Close() 失敗:{e}")
            return UiaActionResult(False, "目標控制項或父鏈沒 WindowPattern、不能關")

        elif atype == "uia_assert_state":
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"))
            check = (action.get("check") or "exists").strip()
            if check == "exists":
                ok = bool(ctrl and ctrl.Exists(2, 0.5))
            elif check == "enabled":
                ok = bool(ctrl and ctrl.Exists(2, 0.5) and ctrl.IsEnabled)
            elif check == "focused":
                ok = bool(ctrl and ctrl.Exists(2, 0.5) and getattr(ctrl, "HasKeyboardFocus", False))
            elif check == "checked":
                ok = False
                try:
                    tp = ctrl.GetTogglePattern()
                    ok = bool(tp and tp.ToggleState == 1)   # ToggleState_On
                except Exception:
                    pass
            else:
                return UiaActionResult(False, f"未知 check 類型:{check}")
            return UiaActionResult(ok, f"assert {check} → {ok}")

        else:
            return UiaActionResult(False, f"未知 uia action type:{atype}")

    except Exception as e:
        logger.exception(f"uia action {atype} 失敗")
        return UiaActionResult(False, f"{type(e).__name__}: {e}")
