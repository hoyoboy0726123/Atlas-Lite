"""
桌面自動化引擎（computer_use 節點專用）。

核心能力：
- L1 basic template matching（cv2.matchTemplate + TM_CCOEFF_NORMED）
- L2 multi-scale matching（對 template 做 ±15% 縮放，解決 DPI/視窗大小差異）
- 動作執行：click_image / click_at / type_text / hotkey / wait / wait_image / screenshot
- Emergency abort：pyautogui.FAILSAFE（滑鼠移到左上角 0,0 立即觸發）+ run_id 中止訊號

不與 skill / recipe 系統共用 — 純 pyautogui + opencv 執行，無 LLM 參與。
"""
from __future__ import annotations
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np


# ── Emergency abort signal（執行中可從外部 set，立即中斷）────────
_abort_flags: dict[str, bool] = {}

# vlm_mode='grounding' 的錨點局部驗證。
# 模型不會承認找不到目標（實測問它畫面上沒有的元素，它會指一個「最像的」位置，
# 提示詞只擋得住最離譜的那種），所以拿錄製時的錨點圖跟它指的位置比對來擋幻覺。
#
# 門檻 0.55 是量出來的（2026-08-06，記事本 + 視窗搬移的真實漂移情境）：
#   正確位置（視窗搬到別處後重新定位）  1.000 × 6/6
#   錯誤位置（同一錨點比到畫面別處）    0.156 ~ 0.262
#   實測攔到的一個真幻覺              0.447   ← 舊門檻 0.30 放行了它
#
# ⚠ Atlas 原本用 0.30，理由寫著「正確結果量到 0.171~0.308」——
#   那組數字其實是**錯位置**的分數（上面第二列），不是正確位置的。
#   等於把門檻訂在雜訊的上緣，幾乎等於沒設防。0.55 夾在幻覺 0.447 與
#   正確 1.000 中間，兩邊都留了餘裕。
#
# 誠實的但書：上面的真陽性是「視窗平移、內容沒重繪」。換主題色、改 DPI
# 縮放、應用程式改版會讓分數往下掉多少，沒有數據。真的被誤擋時，
# 錯誤訊息會寫出實際分數，照著調就好。
VLM_GROUNDING_VERIFY_MIN = 0.55
VLM_GROUNDING_VERIFY_MARGIN = 40   # 局部搜尋窗半徑（px）

# 🪄 產生描述後的自我驗證門檻：拿產生的描述回去定位，離錄製點擊位置多遠算失敗。
# 40px 是量出來的 —— 描述正確時誤差都在 2~11px，講錯時是 70~259px，中間空得很開。
GROUNDING_DESC_VERIFY_PX = 40


# ── 模板圖 LRU 快取 ───────────────────────────────────────────────
# 對同一張錨點圖反覆 read_bytes + imdecode + cvtColor + Canny 是浪費；
# 典型一個 step 會對同一圖做 2~14 次（multi-scale × edge fallback × retry）。
# 以 (abs_path, mtime) 當 key，mtime 變動（使用者重錄）會自動失效。
# 記憶體成本：每個 ~5-50KB，上限 64 張 → < 4MB
_TPL_CACHE_MAX = 64
_tpl_cache: "OrderedDict[tuple[str, float], tuple[np.ndarray, np.ndarray]]" = OrderedDict()


def _best_match_score(win, tpl) -> tuple[float, str]:
    """錨點局部驗證用：彩色 / 灰階 / 邊緣三種比對取最高分。回 (分數, 用了哪種)。

    為什麼不只用彩色（2026-08-03 實測）：
      錄製時滑鼠正停在按鈕上，錨點拍到的是 **hover 高亮**狀態；
      回放驗證時滑鼠還沒移過去，畫面是原始狀態。
      實測檔案總管的關閉鈕：錨點 BGR(40,54,199) 鮮紅、現況 BGR(224,203,209) 淺白，
      顏色完全相反 → 彩色比對只剩 0.389，門檻 0.3 差一點就誤殺。
      同一組圖：灰階 0.505、邊緣 0.777（形狀不受 hover 影響）。
      而幻覺那側三種模式幾乎一樣（0.219 / 0.219 / 0.234），
      所以取最高分只救正確案例、不會讓幻覺變好混。

    這個問題對**每個 click_image 都存在** —— 錄製時滑鼠必然停在目標上。
    多數按鈕 hover 效果較弱（淺灰）所以還沒被發現，關閉鈕最明顯。
    """
    import cv2
    if win.shape[0] < tpl.shape[0] or win.shape[1] < tpl.shape[1]:
        return (-1.0, "尺寸不足")
    best, best_mode = -1.0, ""
    wg = cv2.cvtColor(win, cv2.COLOR_BGR2GRAY)
    tg = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
    for mode, a, b in (("彩色", win, tpl),
                       ("灰階", wg, tg),
                       ("邊緣", cv2.Canny(wg, 50, 150), cv2.Canny(tg, 50, 150))):
        try:
            s = float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED).max())
        except cv2.error:
            continue
        if s > best:
            best, best_mode = s, mode
    return (best, best_mode)


def _imread_unicode(path: Path):
    """讀圖但吃得下非 ASCII 路徑。讀不到回 None。

    cv2.imread 在 Windows 無法開啟含中文的路徑（直接回 None，不報錯）。
    工作流名稱幾乎都是中文（例：ai_output\\新工作流\\桌面自動化_1_assets），
    所以整份程式碼一律走 read_bytes + imdecode —— 2026-08-03 我在新功能裡
    圖省事用了 cv2.imread，使用者一測就撞「錨點圖讀取失敗」。
    """
    import cv2
    try:
        buf = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _load_template(tpl_path: Path):
    """解碼錨點圖 → 回傳 (gray, edge) 灰階/Canny 邊緣陣列，兩者皆用於 find_template 的 mode 切換。
    命中快取直接回；未命中解碼一次存入。失敗回 (None, None, 錯誤訊息)。"""
    import cv2
    try:
        mtime = tpl_path.stat().st_mtime
    except OSError as e:
        return None, None, f"模板 stat 失敗：{e}"
    key = (str(tpl_path), mtime)
    cached = _tpl_cache.get(key)
    if cached is not None:
        _tpl_cache.move_to_end(key)
        return cached[0], cached[1], ""
    try:
        buf = np.frombuffer(tpl_path.read_bytes(), dtype=np.uint8)
        tpl_color = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception as e:
        return None, None, f"模板讀取例外：{e}"
    if tpl_color is None:
        return None, None, f"模板解碼失敗（格式錯誤？）：{tpl_path}"
    tpl_gray = cv2.cvtColor(tpl_color, cv2.COLOR_BGR2GRAY)
    tpl_edge = cv2.Canny(tpl_gray, 50, 150)
    _tpl_cache[key] = (tpl_gray, tpl_edge)
    while len(_tpl_cache) > _TPL_CACHE_MAX:
        _tpl_cache.popitem(last=False)
    return tpl_gray, tpl_edge, ""


def clear_template_cache() -> None:
    """測試或使用者重錄大量錨點後手動清快取用"""
    _tpl_cache.clear()


def request_abort(run_id: str) -> None:
    """標記此 run 需立即中止;computer_use 引擎會在每個動作間檢查"""
    _abort_flags[run_id] = True


def clear_abort(run_id: str) -> None:
    _abort_flags.pop(run_id, None)


def _should_abort(run_id: Optional[str]) -> bool:
    return bool(run_id) and _abort_flags.get(run_id, False)


# ── ESC × 2 緊急停止 watcher(Windows only)────────────────────────────
# 比 pyautogui FAILSAFE (滑鼠甩到 0,0) 更直覺。
# 偵測邏輯:GetAsyncKeyState 輪詢 VK_ESCAPE,看到 rising edge(從沒按 → 按下)
# 兩次間隔 < 500ms 就觸發 abort。
#
# 設計:模組級單例 watcher、新 step 啟動時切換 run_id;不需要 try/finally 包整個
# step 函式 — 即使 step 函式有很多 return 路徑、watcher 也會 follow 最新 run_id。
_esc_watcher_state: dict = {"running": False, "run_id": "", "logger": None, "thread": None}


def _esc_watcher_loop():
    """單例 watcher loop:看 _esc_watcher_state['run_id'] 動態 follow 當前 run。"""
    import threading
    try:
        from ctypes import windll, c_short, c_long
        VK_ESCAPE = 0x1B
        get_key = windll.user32.GetAsyncKeyState
        get_key.restype = c_short
        get_key.argtypes = [c_long]
    except Exception:
        return  # 非 Windows、FAILSAFE 仍為備援

    was_down = False
    last_press_time = 0.0
    DOUBLE_PRESS_WINDOW_SEC = 0.5

    while _esc_watcher_state["running"]:
        try:
            raw = get_key(VK_ESCAPE)
            is_down = bool(raw & 0x8000)
            pressed_since = bool(raw & 1)
            if (is_down and not was_down) or pressed_since:
                now = time.time()
                if now - last_press_time < DOUBLE_PRESS_WINDOW_SEC:
                    rid = _esc_watcher_state.get("run_id") or ""
                    lg = _esc_watcher_state.get("logger")
                    if rid and lg:
                        lg.warning(f"[esc-watcher] ⚠ 偵測到 ESC × 2、觸發 abort run_id={rid}")
                        request_abort(rid)
                    # 不停 loop、繼續看下個 step 的 ESC × 2
                    last_press_time = 0.0
                else:
                    last_press_time = now
            was_down = is_down
            time.sleep(0.05)
        except Exception:
            time.sleep(0.1)


def _ensure_esc_watcher(run_id: str, logger: logging.Logger) -> None:
    """確保 watcher 已啟動;每次 step 開始時呼叫、會 update 當前 run_id"""
    import threading
    _esc_watcher_state["run_id"] = run_id
    _esc_watcher_state["logger"] = logger
    if _esc_watcher_state["running"]:
        return  # 已有 watcher 在跑、只 update run_id 即可
    _esc_watcher_state["running"] = True
    t = threading.Thread(target=_esc_watcher_loop, daemon=True, name="esc-watcher")
    _esc_watcher_state["thread"] = t
    t.start()


# ── 螢幕擷取與圖像比對 ──────────────────────────────────────────

def _capture_screen() -> tuple[np.ndarray, int, int]:
    """抓所有螢幕聯集的完整截圖，回傳 (BGR ndarray, 原點 x, 原點 y)。

    關鍵：用 monitors[0]（虛擬桌面聯集）而非 monitors[1]（主螢幕），
    讓 cv2 template matching 能在多螢幕環境下找到任意螢幕上的目標；
    多螢幕時主螢幕左上不一定是 (0,0)，回傳的 origin 用來把比對到的
    相對座標轉回絕對桌面座標（pyautogui.click 接受的就是絕對座標）。
    """
    import mss
    import cv2
    with mss.mss() as sct:
        mon = sct.monitors[0]      # 所有螢幕聯集
        img = np.array(sct.grab(mon))
    bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return bgr, mon["left"], mon["top"]


def _point_in_any_screen(x: int, y: int) -> tuple[bool, str]:
    """檢查 (x, y) 是否落在目前任一螢幕可見範圍內（支援多螢幕負座標）。
    用途：scroll / click 前避免把滑鼠拉到超出桌面範圍的座標。
    回傳 (是否在範圍內, 目前螢幕配置描述)。"""
    import mss
    try:
        with mss.mss() as sct:
            for mon in sct.monitors[1:]:
                left = mon["left"]
                top = mon["top"]
                if left <= x < left + mon["width"] and top <= y < top + mon["height"]:
                    return True, ""
            layout = "; ".join(
                f"{m['width']}×{m['height']} @ ({m['left']},{m['top']})"
                for m in sct.monitors[1:]
            )
            return False, f"目前螢幕：{layout}"
    except Exception:
        return True, ""  # 抓不到資訊就寬容處理


@dataclass
class MatchResult:
    found: bool
    center: tuple[int, int] = (0, 0)   # (x, y) 螢幕座標
    confidence: float = 0.0
    scale: float = 1.0                  # 命中的縮放比例
    reason: str = ""
    mode: str = "gray"                  # "gray" = 灰階匹配，"edge" = Canny 邊緣匹配


def find_template(
    template_path: str,
    threshold: float = 0.5,
    multi_scale: bool = True,
    near_xy: Optional[tuple[int, int]] = None,
    search_radius: int = 400,
    region: Optional[tuple[int, int, int, int]] = None,
    mode: str = "gray",
) -> MatchResult:
    """在當前螢幕找指定模板圖，回傳中心座標與相似度。

    L1: 單一尺度 matchTemplate（快，~5ms）
    L2: multi_scale=True 時額外跑 0.85/0.9/0.95/1.05/1.1/1.15 倍縮放，
        取最高相似度（~30ms，吸收 DPI 125%/150% 縮放差異）

    mode:
      - "gray"（預設）：灰階像素比對
      - "edge"：Canny 邊緣偵測後再比對 — 兩張圖都先跑 Canny 只留輪廓，
               對色彩/光線/hover 動畫等差異更容忍（conf 通常略低但更穩）

    搜尋範圍優先序（三選一）：
      region 給定 > near_xy 給定 > 全螢幕
      - region: (left, top, width, height) 虛擬桌面絕對座標，使用者明確指定的紅框
      - near_xy: 錄製座標附近 ±search_radius px 的方形範圍（自動退回舊行為）
      - 皆未給：整個虛擬桌面都找（速度最慢、誤匹配風險最高）
    """
    import cv2

    tpl_path = Path(template_path)
    if not tpl_path.is_file():
        return MatchResult(False, reason=f"模板不存在：{template_path}")

    # 從 LRU 快取拿灰階 + Canny 邊緣，避免每次呼叫都重做 decode+cvtColor+Canny
    tpl_gray, tpl_edge, err = _load_template(tpl_path)
    if err:
        return MatchResult(False, reason=err)

    screen_color, origin_x, origin_y = _capture_screen()
    screen_gray_full = cv2.cvtColor(screen_color, cv2.COLOR_BGR2GRAY)

    # Edge 模式：template 在 _load_template 已預算好 Canny；螢幕每次都要重算（畫面會變）。
    # 閾值 50/150 是常用的 hysteresis 組合，對 UI 元素邊緣偵測穩定
    if mode == "edge":
        tpl_proc_full = tpl_edge
        screen_proc_full = cv2.Canny(screen_gray_full, 50, 150)
    else:
        tpl_proc_full = tpl_gray
        screen_proc_full = screen_gray_full

    # 三選一裁切策略：region > near_xy > 全螢幕
    clip_offset_x, clip_offset_y = origin_x, origin_y
    if region is not None:
        # 使用者明確指定的搜尋矩形（絕對桌面座標）
        rl, rt, rw, rh = region
        rel_x = rl - origin_x
        rel_y = rt - origin_y
        H, W = screen_proc_full.shape
        left = max(0, rel_x)
        top = max(0, rel_y)
        right = min(W, rel_x + rw)
        bottom = min(H, rel_y + rh)
        if right - left < 20 or bottom - top < 20:
            return MatchResult(False, reason=f"search_region ({rl},{rt},{rw},{rh}) 與目前桌面範圍重疊不足")
        screen_proc = screen_proc_full[top:bottom, left:right]
        clip_offset_x = origin_x + left
        clip_offset_y = origin_y + top
    elif near_xy is not None:
        nx, ny = near_xy
        # 絕對座標 → 相對截圖的座標
        rel_x = nx - origin_x
        rel_y = ny - origin_y
        H, W = screen_proc_full.shape
        left = max(0, rel_x - search_radius)
        top = max(0, rel_y - search_radius)
        right = min(W, rel_x + search_radius)
        bottom = min(H, rel_y + search_radius)
        if right - left < 20 or bottom - top < 20:
            # 範圍超出螢幕太多（錄製座標根本不在目前桌面範圍內）
            return MatchResult(False, reason=f"錄製座標 ({nx},{ny}) 超出目前桌面範圍")
        screen_proc = screen_proc_full[top:bottom, left:right]
        clip_offset_x = origin_x + left
        clip_offset_y = origin_y + top
    else:
        screen_proc = screen_proc_full

    scales = [1.0]
    if multi_scale:
        # L2：涵蓋常見 DPI 差（100%/125%/150%）
        scales = [0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15]

    best = MatchResult(False, mode=mode)
    for s in scales:
        if abs(s - 1.0) < 1e-6:
            tpl_scaled = tpl_proc_full
        else:
            new_w = max(1, int(tpl_proc_full.shape[1] * s))
            new_h = max(1, int(tpl_proc_full.shape[0] * s))
            if new_w >= screen_proc.shape[1] or new_h >= screen_proc.shape[0]:
                continue
            tpl_scaled = cv2.resize(tpl_proc_full, (new_w, new_h), interpolation=cv2.INTER_AREA)
        try:
            res = cv2.matchTemplate(screen_proc, tpl_scaled, cv2.TM_CCOEFF_NORMED)
        except cv2.error:
            continue
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > best.confidence:
            h, w = tpl_scaled.shape
            # 比對結果是相對於裁切區域的座標；加上裁切原點換算成桌面絕對座標
            cx = max_loc[0] + w // 2 + clip_offset_x
            cy = max_loc[1] + h // 2 + clip_offset_y
            best = MatchResult(
                found=max_val >= threshold,
                center=(cx, cy),
                confidence=float(max_val),
                scale=s,
                mode=mode,
            )
    if not best.found:
        area = "附近範圍" if near_xy else "整個桌面"
        best.reason = f"最佳相似度 {best.confidence:.3f} 低於門檻 {threshold}（搜尋{area}，{mode} 模式）"
    return best


# ── 動作執行 ────────────────────────────────────────────────────

@dataclass
class ActionResult:
    ok: bool
    action_index: int
    action_type: str
    message: str = ""
    duration_ms: int = 0


def _check_abort(run_id: Optional[str]) -> None:
    if _should_abort(run_id):
        raise RuntimeError("使用者中止（emergency abort）")


def _parse_search_region(action: dict) -> Optional[tuple[int, int, int, int]]:
    """解析 action['search_region'] = [left, top, width, height]（虛擬桌面絕對座標）。
    格式不對或尺寸 <= 0 回 None（代表不限制，走 near_xy / 全螢幕邏輯）。"""
    sr = action.get("search_region") or []
    if not isinstance(sr, (list, tuple)) or len(sr) != 4:
        return None
    try:
        l, t, w, h = int(sr[0]), int(sr[1]), int(sr[2]), int(sr[3])
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (l, t, w, h)


# 錨點獨特性分析：把「這張圖在畫面上能對上幾個地方」算出來。
# 門檻用 0.5（跟 CV 預設一致）—— 分數在門檻以上的都是回放時可能被挑中的候選。
ANCHOR_RIVAL_THRESHOLD = 0.5

# 替身要「搶得走」才算數：CV 取的是搜尋範圍內**分數最高**的那一個，
# 所以替身必須先贏過真目標。分數差得遠 = 只有真目標整個消失時才會被誤點，
# 那不是「錨點不夠獨特」，報出來只會讓使用者學會忽略警告。
#
# 2026-08-06 實測（使用者實際錄的 8 個動作）：真目標一律 1.000，
# 範圍內最強替身 0.593~0.699，差距 0.30~0.41 —— 全部搶不走，
# 但舊版把它們全報成「長得幾乎一樣」。
# 反例（會搶走的長相）：視窗標題列那三顆按鈕彼此 ~1.000，差距接近 0。
# 取 0.15 當界線：回放時真目標本身也會掉分（主題色、DPI 縮放、反鋸齒），
# 留這個緩衝才不會漏掉「目標稍微掉分就被搶走」的真風險。
ANCHOR_RIVAL_GAP = 0.15

# 錨點「有沒有特徵」的下限（灰階變異數）。
# 2026-08-06 實測：TM_CCOEFF_NORMED 對零變異的模板是數學上退化的（除以 0），
# 純色錨點跟**任何東西**比都拿 1.000 —— 連純雜訊都 1.000。
# 也就是說純色錨點會讓 CV 隨便命中一塊平坦區域、讓幻覺守門形同虛設。
# 判別力實測：變異數 20 以下完全分不出來，100 以上正常（0.000~0.062）。
# 實際錄製的錨點是 888~5295，門檻取 100 有將近 9 倍餘裕。
ANCHOR_MIN_VARIANCE = 100.0


def _anchor_variance(img) -> float:
    """錨點的灰階變異數。太低 = 這張圖沒有特徵，比對結果不可信。"""
    import cv2
    if img is None:
        return 0.0
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return float(np.var(g))

# 全桌面 fallback 的最低 CV 門檻（避免大螢幕假匹配點錯位置）。
# 放模組層級是因為 analyze_anchor_uniqueness 也要用同一個值 —— 兩邊不一致
# 就會出現「警告說有風險、執行時其實根本搆不到」的假警報。
_FULLSCREEN_MIN_THRESHOLD = 0.80


def analyze_anchor_uniqueness(assets_dir: Path, action: dict,
                              threshold: float = ANCHOR_RIVAL_THRESHOLD,
                              max_peaks: int = 8,
                              cv_search_radius: int = 400,
                              cv_threshold: float = 0.5,
                              cv_search_only_near: bool = False) -> dict:
    """算這張錨點在錄製畫面上有幾個替身，**而且執行時真的搆得到**。

    為什麼要算：CV 找的是「最像的」。如果畫面上有好幾個長得一樣的東西
    （最典型的就是視窗標題列那三顆按鈕、表格裡重複的圖示），回放時只要
    真目標稍微跑掉，它就會挑中替身點下去 —— 而且分數可以高到 0.95 以上，
    調門檻擋不住。與其執行時猜，不如錄製時就把這件事量出來。

    ⚠ 2026-08-06 修正：這支原本掃「整張截圖、門檻 0.5」就報警，但執行時
      根本不是那樣搜的，導致大量假警報 —— 實測一個動作報 7 個替身，
      逐一比對後**沒有任何一個搆得到**（全在 ±400px 外，分數也全部低於
      全螢幕階段的 0.80 門檻），警告卻叫使用者去改設定。
      現在改成照 find_template 的三階段逐一判定：

        Phase 1  橘框內       門檻 cv_threshold   —— 沒拉橘框就不執行
        Phase 2  錄製座標 ±cv_search_radius       門檻 cv_threshold
        Phase 3  整個桌面     門檻 max(cv_threshold, 0.80)
                              —— 勾了「只搜附近」或嚴格鎖橘框就不執行

      只有某個階段真的會執行、而且那個替身在該階段的範圍內又過得了該階段
      門檻，才算數。

    用錄製時存的 full_NNN.png（那一刻的整個畫面），不是「現在」的畫面 ——
    現在的畫面跟錄製時可能完全不同，量出來的沒有意義。

    回傳：
      checked            有沒有真的算（沒有全螢幕圖就算不了）
      rivals             **執行時搆得到**的替身數（前端拿它決定要不要警告）
      nearest_rival_px   這些替身裡最近的那個離真目標多遠
      scanned            整張畫面上總共找到幾個相似處（含搆不到的）
      phases             {"box": n, "near": n, "fullscreen": n} 各階段搆得到幾個
      reason             人看的結論

    ⚠ 這個函式只負責「量」，不決定執行行為。試過用它自動鎖搜尋半徑，
      2026-08-06 實測不可行 —— 錄製當下的替身分佈預測不了回放當下的。
    """
    import cv2

    def _skip(why: str) -> dict:
        return {"checked": False, "rivals": 0, "nearest_rival_px": 0,
                "scanned": 0, "phases": {"box": 0, "near": 0, "fullscreen": 0},
                "flat": False, "variance": 0.0,
                "target_score": 0.0, "best_rival_score": 0.0, "reason": why}

    img_name = (action.get("image") or "").strip()
    full_name = (action.get("full_image") or "").strip()
    if not img_name:
        return _skip("這個動作沒有錨點圖")
    if not full_name:
        return _skip("沒有錄製當下的全螢幕截圖，無法判斷")

    tpl = _imread_unicode(assets_dir / img_name)
    full = _imread_unicode(assets_dir / full_name)
    if tpl is None or full is None:
        return _skip("錨點圖或全螢幕截圖讀取失敗")

    # 點擊點換算到全螢幕圖的座標系
    cx = int(action.get("x", 0) or 0) - int(action.get("full_left", 0) or 0)
    cy = int(action.get("y", 0) or 0) - int(action.get("full_top", 0) or 0)
    H, W = full.shape[:2]
    th, tw = tpl.shape[:2]
    if not (0 <= cx < W and 0 <= cy < H):
        return _skip("點擊座標不在全螢幕截圖範圍內")
    if th >= H or tw >= W:
        return _skip("錨點圖比全螢幕截圖還大")

    # 純色錨點是比「有替身」更嚴重的問題：它跟畫面上任何一塊平坦區域都是滿分，
    # CV 會隨便命中、幻覺守門也失效。直接回報，不用再算替身（算了也沒意義）。
    variance = _anchor_variance(tpl)
    if variance < ANCHOR_MIN_VARIANCE:
        return {
            "checked": True, "rivals": 0, "nearest_rival_px": 0, "scanned": 0,
            "phases": {"box": 0, "near": 0, "fullscreen": 0},
            "flat": True, "variance": round(variance, 1),
            "target_score": 0.0, "best_rival_score": 0.0,
            "reason": (f"這張錨點幾乎沒有特徵（灰階變異數 {variance:.1f}）——"
                       f"它跟畫面上任何一塊平坦區域都會是滿分，"
                       f"CV 可能命中完全無關的位置，幻覺守門也擋不住。"
                       f"請重圈一個含文字或邊框的範圍。"),
        }

    g_full = cv2.cvtColor(full, cv2.COLOR_BGR2GRAY)
    g_tpl = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
    try:
        res = cv2.matchTemplate(g_full, g_tpl, cv2.TM_CCOEFF_NORMED)
    except cv2.error as e:
        return _skip(f"比對失敗：{e}")

    # 逐一取峰值，取完就把周圍遮掉，避免同一個峰被算成好幾個
    peaks: list[tuple[float, int, int]] = []
    work = res.copy()
    for _ in range(max_peaks):
        _, val, _, loc = cv2.minMaxLoc(work)
        if val < threshold:
            break
        peaks.append((float(val), loc[0] + tw // 2, loc[1] + th // 2))
        work[max(0, loc[1] - th):loc[1] + th, max(0, loc[0] - tw):loc[0] + tw] = -1.0

    if not peaks:
        return _skip("錨點在自己的錄製截圖上都對不到，可能是錄壞了")

    # 離點擊點最近的那個峰值視為「真目標」，其餘都是替身
    peaks.sort(key=lambda p: (p[1] - cx) ** 2 + (p[2] - cy) ** 2)
    target_score = peaks[0][0]
    all_rivals = peaks[1:]
    scanned = len(all_rivals)

    # 只留「搶得走」的：CV 取範圍內最高分，替身得先贏過真目標才可能被點到。
    # 差距超過 ANCHOR_RIVAL_GAP 的替身，要等真目標整個從畫面消失才有機會 ——
    # 那是「目標不見了」的問題，不是「錨點不夠獨特」，混在一起報只會製造雜訊。
    _win_floor = target_score - ANCHOR_RIVAL_GAP
    rivals = [r for r in all_rivals if r[0] >= _win_floor]
    weak = scanned - len(rivals)

    # ── 逐一判定：執行時哪個階段真的搆得到這個替身 ──────────────────
    # 座標換算：peak 是全螢幕圖的座標，橘框是虛擬桌面的絕對座標
    off_l = int(action.get("full_left", 0) or 0)
    off_t = int(action.get("full_top", 0) or 0)
    box = action.get("search_region") or []
    has_box = isinstance(box, (list, tuple)) and len(box) == 4 and box[2] > 0 and box[3] > 0
    cv_strict = bool(action.get("cv_strict_region", False))
    # 全螢幕階段被關掉的兩種情況（跟 execute_action 的 Phase 3 條件一致）
    fullscreen_on = not cv_search_only_near and not (cv_strict and has_box)
    full_min = max(cv_threshold, _FULLSCREEN_MIN_THRESHOLD)

    counts = {"box": 0, "near": 0, "fullscreen": 0}
    reachable: list[int] = []
    best_rival = 0.0
    for val, px, py in rivals:
        d = int(((px - cx) ** 2 + (py - cy) ** 2) ** 0.5)
        hit = False
        if has_box and val >= cv_threshold:
            l, t, w, h = box[0], box[1], box[2], box[3]
            if l <= px + off_l <= l + w and t <= py + off_t <= t + h:
                counts["box"] += 1
                hit = True
        # 嚴格鎖橘框時 Phase 2 不會執行。
        # ⚠ near_xy 是**方形**範圍（x、y 各 ±search_radius），不是圓形 ——
        #   這裡若用歐氏距離判斷，方形四個角落的替身會被誤判成「搆不到」，
        #   那是假陰性（該警告卻沒警告），比假警報危險得多。
        in_near = (abs(px - cx) <= cv_search_radius
                   and abs(py - cy) <= cv_search_radius)
        if not (cv_strict and has_box) and val >= cv_threshold and in_near:
            counts["near"] += 1
            hit = True
        if fullscreen_on and val >= full_min:
            counts["fullscreen"] += 1
            hit = True
        if hit:
            reachable.append(d)
            best_rival = max(best_rival, val)

    if not reachable:
        if weak and not rivals:
            why = (f"畫面上有 {weak} 個較像的地方，但分數都低於目標 "
                   f"{ANCHOR_RIVAL_GAP:.2f} 以上（目標 {target_score:.2f}），"
                   f"搶不走 —— 只有真目標整個從畫面消失時才可能被誤點")
        elif scanned:
            why = (f"畫面上有 {scanned} 個相似處，但執行時都搆不到"
                   f"（不在搜尋範圍內、或分數低於該階段門檻）")
        else:
            why = "錨點在錄製畫面上是獨一無二的"
        return {"checked": True, "rivals": 0, "nearest_rival_px": 0,
                "scanned": scanned, "phases": counts,
                "flat": False, "variance": round(variance, 1),
                "target_score": round(target_score, 3), "best_rival_score": 0.0,
                "reason": why}

    nearest = min(reachable)
    where = "、".join(
        n for n, ok in (("橘框內", counts["box"]), ("錄製座標附近", counts["near"]),
                        ("退回全螢幕時", counts["fullscreen"])) if ok)
    return {
        "checked": True,
        "rivals": len(reachable),
        "nearest_rival_px": nearest,
        "scanned": scanned,
        "phases": counts,
        "flat": False,
        "variance": round(variance, 1),
        "target_score": round(target_score, 3),
        "best_rival_score": round(best_rival, 3),
        "reason": (f"有 {len(reachable)} 個地方分數逼近真目標（{where}）："
                   f"目標 {target_score:.2f} vs 替身 {best_rival:.2f}，"
                   f"最近的在 {nearest}px 外"
                   + (f"；另有 {scanned - len(reachable)} 個差太多或搆不到，不列入"
                      if scanned > len(reachable) else "")),
    }


def verify_grounding_desc(assets_dir: Path, action: dict, desc: str) -> tuple[bool, float, str]:
    """把描述餵回地端定位模型，看它能不能回到錄製時的點擊位置。回 (通過, 誤差px, 說明)。

    為什麼要做這件事：
      產生描述的是雲端模型，2026-08-03 實測 5 題會講錯 1 題，
      而且錯的描述讀起來一樣通順（把右上角搜尋框說成「綠色的『好』儲存格樣式按鈕」）。
      使用者看到一句漂亮的中文不會逐字比對畫面，直接採用就點錯了。
      → 產生完立刻自己驗一次：描述若定位不回原點，就當場警告。
      成本是一次地端推論（約 2 秒、不花錢），很划算。
    """
    import cv2
    full_name = (action.get("full_image") or "").strip()
    if not full_name or not (assets_dir / full_name).exists():
        return (True, -1.0, "沒有全螢幕截圖可驗證（略過）")
    full_p = assets_dir / full_name
    img = _imread_unicode(full_p)
    if img is None:
        return (True, -1.0, "全螢幕截圖讀取失敗（略過）")
    cx = int(action.get("x", 0) or 0) - int(action.get("full_left", 0) or 0)
    cy = int(action.get("y", 0) or 0) - int(action.get("full_top", 0) or 0)
    H, W = img.shape[:2]
    if not (0 <= cx < W and 0 <= cy < H):
        return (True, -1.0, "點擊座標不在截圖範圍內（略過）")

    _tmp = None
    try:
        from . import vlm_grounding as _vg
        # 錄製產物在 ai_output/<中文工作流名>/... 底下，路徑含中文。
        # 容器讀得到那個掛載，但檔名編碼在 docker exec 這段容易出事 ——
        # 複製一份到共用目錄、用純 ASCII 檔名，最保險。
        _tmp = _vg.shared_dir() / f"_verify_{os.getpid()}.png"
        _ok_w, _enc = cv2.imencode(".png", img)
        if not _ok_w:
            return (True, -1.0, "驗證跳過（截圖編碼失敗）")
        _tmp.write_bytes(_enc.tobytes())
        ok, gx, gy, why = _vg.locate(desc, str(_tmp), W, H,
                                     logging.getLogger("pipeline.computer_use"))
    except Exception as e:
        return (True, -1.0, f"驗證跳過（{e.__class__.__name__}）")
    finally:
        if _tmp is not None:
            try:
                _tmp.unlink()
            except OSError:
                pass
    if not ok:
        # 一定要分清楚「模型說找不到」和「驗證根本跑不起來」。
        # 兩者都回 False 的話，外掛沒裝就會跳紅字說「這段描述會點錯」——
        # 那是冤枉描述、也會讓使用者對警告麻痺（2026-08-03 自己踩到）。
        #
        # ⚠ 只有 stop(reason=...) 才算「模型主張畫面上沒有這東西」。
        #   「模型未給座標」是它吐了看不懂的東西（實測會回 finish()），
        #   那是驗證跑不起來、不是否定答案 —— 2026-08-06 實測一句完全正確的
        #   描述就因為模型回 finish() 被判成「會點錯」，正是這段註解要防的事。
        _model_said_no = "模型回報找不到目標" in why
        if _model_said_no:
            return (False, -1.0, f"定位模型在畫面上找不到這段描述講的東西：{why[:60]}")
        return (True, -1.0, f"驗證未執行（{why[:60]}）")
    d = ((gx - cx) ** 2 + (gy - cy) ** 2) ** 0.5
    if d <= GROUNDING_DESC_VERIFY_PX:
        return (True, d, f"驗證通過（定位回錄製位置，誤差 {d:.0f}px）")
    return (False, d, f"這段描述會定位到別的地方（離錄製位置 {d:.0f}px）")


def _strip_json_fence(s: str) -> str:
    """模型愛把 JSON 包在 ```json ... ``` 裡，剝掉。"""
    t = s.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _vlm_describe_to_text(prompt: str, region: Optional[tuple[int, int, int, int]],
                          logger: logging.Logger) -> tuple[bool, str, str]:
    """vlm_mode='description' 用：把螢幕送視覺模型，請它回目標的「實際文字」。

    座標仍由後續 OCR 決定 —— 模型只負責「要找什麼字」，不負責「在哪裡」。
    這正是這個模式存在的理由：通用視覺模型給座標不準（實測會指到隔壁），
    但「讀出畫面上這行字是什麼」它很行。

    回 (found, target_text, reason)。
    """
    from . import vlm_cloud

    screen, ox, oy = _capture_screen()
    if region is not None:
        l, t, w, h = region
        x, y = max(0, l - ox), max(0, t - oy)
        x_end = min(screen.shape[1], x + w)
        y_end = min(screen.shape[0], y + h)
        if x_end <= x or y_end <= y:
            return (False, "", f"裁切區域 {(l, t, w, h)} 與螢幕無交集")
        screen = screen[y:y_end, x:x_end]

    sys_msg = ("你是 UI 視覺助手。看到的圖是螢幕當下狀態。回 JSON："
               '{"found": true/false, "text": "目標的實際文字", "reason": "簡短說明"}。'
               'text 必須是螢幕上「實際看得到」的字串（含大小寫和標點），不是描述、不是同義詞。'
               "只回 JSON。")
    user_text = (f"使用者描述要點擊的目標：「{prompt}」\n"
                 "請看圖，找出符合此描述的 UI 元素，告訴我它身上實際顯示的文字"
                 "（等一下會用這段文字做 OCR 定位）。畫面上找不到就回 found=false。")

    ok, raw, err = vlm_cloud.ask_with_image(user_text, sys_msg, screen, logger)
    if not ok:
        return (False, "", err)
    try:
        data = json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError:
        return (False, "", f"視覺模型的回應不是 JSON：{raw[:200]}")
    if not isinstance(data, dict):
        return (False, "", f"視覺模型回了 JSON 但不是物件：{raw[:150]}")
    found = bool(data.get("found", False))
    text = str(data.get("text") or "").strip()
    reason = str(data.get("reason") or "").strip() or "(無說明)"
    if found and not text:
        return (False, "", f"模型說找到了但沒給文字：{reason}")
    logger.info(f"[vlm_describe] found={found} text={text!r} reason={reason[:120]}")
    return (found, text, reason)


def _pyautogui_with_failsafe():
    """lazy import pyautogui 並設好 failsafe / 節流"""
    import pyautogui
    pyautogui.FAILSAFE = True  # 滑鼠甩到左上角 (0,0) 立即 FailSafeException
    pyautogui.PAUSE = 0.15     # 每個 pyautogui 呼叫後自動等 150ms，防過快
    return pyautogui


def _do_click(pg, x: int, y: int, button: str, clicks: int, hold_sec: float, modifiers: list) -> None:
    """統一的點擊執行器：處理長按 + 修飾鍵。
    modifiers: ["ctrl"], ["ctrl","shift"] 等 — 按下→click→放開。

    跨螢幕瞬移防護：pyautogui.click(x=,y=) 預設是「瞬間 moveTo + click」，
    在多螢幕大跨度移動下（如從主螢幕跳到副螢幕）Windows 事件處理會 race —
    click 事件可能被路由到 cursor 飛過的中間位置或視窗動畫剛好的位置，造成
    使用者看到的「附近又多點一下」幽靈點擊。修法：先做帶 duration 的平滑
    moveTo、短暫等游標到位、再 click（不帶 x/y，用當下位置）。"""
    # 按下修飾鍵
    for mod in (modifiers or []):
        pg.keyDown(mod)
    try:
        # 先平滑移動到目標位置；duration=0.1 對單螢幕無感，跨螢幕避開瞬移 race
        # 拋例外（座標超出螢幕等）就略過 moveTo，讓 click 自己處理
        try:
            pg.moveTo(x, y, duration=0.1)
            time.sleep(0.05)   # 讓 OS 確認游標到位
        except Exception:
            pass
        if hold_sec > 0.1:
            pg.mouseDown(button=button)
            time.sleep(hold_sec)
            pg.mouseUp(button=button)
        else:
            # 不帶 x/y → click 在當下游標位置（就是上面 moveTo 過去的點）
            pg.click(button=button, clicks=clicks)
    finally:
        # 反序放開修飾鍵，即使 click 拋例外也確保按鍵會放
        for mod in reversed(modifiers or []):
            pg.keyUp(mod)


def execute_action(
    action: dict,
    assets_dir: Path,
    index: int,
    logger: logging.Logger,
    run_id: Optional[str] = None,
    allow_coord_fallback: bool = True,
    cv_threshold: float = 0.5,
    cv_search_only_near: bool = False,
    cv_search_radius: int = 400,
    cv_trigger_hover: bool = True,
    cv_hover_wait_ms: int = 200,
    cv_coord_fallback: bool = False,
    ocr_threshold: float = 0.6,
    ocr_cv_fallback: bool = False,
    _depth: int = 0,
) -> ActionResult:
    """執行單一 action。action 是 ComputerUseAction.model_dump() 結果的 dict。
    _depth: 遞迴深度（if_image_found / retry_until 巢狀時累加），防寫爛的 YAML 無限遞迴。"""
    t0 = time.time()
    atype = action.get("type", "")
    desc = action.get("description") or atype
    indent = "  " * _depth if _depth > 0 else ""
    logger.info(f"[computer_use] {indent}動作 #{index + 1} ({atype})：{desc}")

    # 遞迴深度守衛：正常使用者寫不到 10 層，超過 10 層一定是 YAML 爛掉或 copy-paste 出錯
    if _depth > 10:
        return ActionResult(False, index, atype,
            f"動作巢狀超過深度 10（if_image_found / retry_until 遞迴過深），拒絕執行")

    _check_abort(run_id)

    # Bundle all the execution kwargs so nested dispatch (if_image_found / retry_until)
    # 不用手動 re-list 每個參數
    _exec_ctx = {
        "allow_coord_fallback": allow_coord_fallback,
        "cv_threshold": cv_threshold,
        "cv_search_only_near": cv_search_only_near,
        "cv_search_radius": cv_search_radius,
        "cv_trigger_hover": cv_trigger_hover,
        "cv_hover_wait_ms": cv_hover_wait_ms,
        "cv_coord_fallback": cv_coord_fallback,
        "ocr_threshold": ocr_threshold,
        "ocr_cv_fallback": ocr_cv_fallback,
    }

    try:
        pg = _pyautogui_with_failsafe()

        if atype == "click_image":
            img_name = action.get("image", "")
            if not img_name:
                return ActionResult(False, index, atype, "click_image 缺 image 欄位")
            tpl_path = assets_dir / img_name
            # 多形態錨點：同一顆按鈕的不同樣子（最大化↔還原、亮↔暗主題）。
            # 每張都比一次取最高分 —— 取代 Atlas 靠雲端模型挑圖的 anchor_pick。
            _variants = [v for v in (action.get("image_variants") or [])
                         if isinstance(v, str) and v.strip()]
            tpl_candidates: list[tuple[str, Path]] = [(img_name, tpl_path)]
            for _v in _variants:
                _vp = assets_dir / _v
                if _vp.is_file():
                    tpl_candidates.append((_v, _vp))
                else:
                    logger.warning(f"[computer_use]   多形態錨點 {_v} 檔案不存在，略過")
            if len(tpl_candidates) > 1:
                logger.info(f"[computer_use]   多形態錨點：共 {len(tpl_candidates)} 張候選"
                            f"（{', '.join(n for n, _ in tpl_candidates)}）")
            # 門檻：action-level confidence 覆蓋 step 層級 cv_threshold，皆缺就用 0.5
            threshold = float(action.get("confidence") or cv_threshold)
            button = action.get("button", "left")
            clicks = int(action.get("clicks", 1))
            fx = action.get("x")
            fy = action.get("y")
            has_coord = isinstance(fx, (int, float)) and isinstance(fy, (int, float))

            hold_sec = float(action.get("hold_sec", 0) or 0)
            modifiers = list(action.get("modifiers", []) or [])
            mods_tag = f"[{'+'.join(modifiers)}]" if modifiers else ""

            # 四種 primary mode（user-feedback 排序，VLM 永遠最高優先因為使用者明確開了它）：
            #   vlm_mode=description → VLM 看圖回「目標的實際文字」→ OCR 找文字 → 點中心
            #   vlm_mode=anchor_pick → VLM 從多張候選錨點挑一張最像螢幕當下的 → 用該張走 CV 比對
            #   use_ocr 勾起 + 有 ocr_text → 走 OCR
            #   use_ocr 沒勾 + use_coord=true → 走絕對座標
            #   use_ocr 沒勾 + use_coord=false → 走 CV 圖像比對
            # VLM 永遠不直接給座標 — 它只負責「決定要找的東西」，避開 VLM 點錯位置的問題
            vlm_mode = (action.get("vlm_mode") or "off").lower()
            use_ocr = bool(action.get("use_ocr", False))
            ocr_text = (action.get("ocr_text") or "").strip()
            ocr_will_run = use_ocr and bool(ocr_text)

            # ── 三層 fallback toggle (UIA / CV / 強制座標) ────────────────────
            # 預設三個 toggle 全 True、順序 UIA → CV → 強制座標。
            # 使用者可在 panel 取消某層、組合自定義(細節見 ComputerUseAction schema 註解)。
            # OCR / VLM 啟用時自帶 primary 邏輯、這三層不適用、UIA-first 跳過。
            use_uia_layer = bool(action.get("use_uia", True))
            use_cv_layer = bool(action.get("use_cv", True))
            use_coord_layer = bool(action.get("use_coord", True))
            user_chose_explicit = vlm_mode != "off" or use_ocr

            # 全關 + 沒 OCR/VLM = 無方法可用、直接 fail
            if (not use_uia_layer and not use_cv_layer and not use_coord_layer
                    and not user_chose_explicit):
                fail_msg = "UIA / CV / 強制座標 三個 toggle 全關、又沒啟用 OCR/VLM、無方法可用"
                logger.error(f"[computer_use]   ✗ {fail_msg}")
                return ActionResult(False, index, atype, fail_msg)

            # ── Phase 0:UIA-first ────────────────────────────────────────────
            ui_info = action.get("ui") if isinstance(action.get("ui"), dict) else None
            uia_first_attempted = False
            if ui_info and use_uia_layer and not user_chose_explicit:
                logger.info(f"[computer_use]   [UIA-first] 嘗試定位: name='{ui_info.get('name', '')[:40]}' type='{ui_info.get('control_type', '')}' auto_id='{ui_info.get('automation_id', '')[:30]}'")
                uia_first_attempted = True
                find_click_point = None
                try:
                    from .uia_lookup import find_click_point as _fcp
                    find_click_point = _fcp
                except Exception as _e1:
                    try:
                        from .uia_lookup import find_click_point as _fcp  # type: ignore
                        find_click_point = _fcp
                    except Exception as _e2:
                        logger.warning(f"[computer_use]   [UIA-first] 載入 uia_lookup 模組失敗:{type(_e2).__name__}: {_e2} (上游 {type(_e1).__name__}: {_e1})")
                if find_click_point is None:
                    logger.info(f"[computer_use]   [UIA-first] uia_lookup 不可用、退下層")
                else:
                    try:
                        point = find_click_point(ui_info, timeout=2.0)
                    except Exception as _e3:
                        logger.warning(f"[computer_use]   [UIA-first] find_click_point 例外:{type(_e3).__name__}: {_e3}")
                        point = None
                    if point:
                        cx, cy = point
                        _do_click(pg, cx, cy, button, clicks, hold_sec, modifiers)
                        hold_tag_u = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                        msg = (f"{mods_tag} UIA 命中 '{ui_info.get('name', '')[:40]}' "
                               f"({ui_info.get('control_type', '')}) @ ({cx},{cy}){hold_tag_u}")
                        duration = int((time.time() - t0) * 1000)
                        logger.info(f"[computer_use]   ✓ {msg}(UIA-first、{duration}ms)")
                        return ActionResult(True, index, atype, msg, duration)
                    # UIA 沒中 — 看下層 toggle 決定 fall through 還是 strict fail
                    if not use_cv_layer and not use_coord_layer:
                        # 嚴格 UIA 模式(只勾 UIA, CV/座標都關)→ 找不到就死
                        fail_msg = (f"嚴格 UIA 模式: 元素 '{ui_info.get('name', '')[:40]}' 找不到 "
                                    f"(CV 與 強制座標 toggle 都關)")
                        logger.error(f"[computer_use]   ✗ {fail_msg}")
                        return ActionResult(False, index, atype, fail_msg)
                    _next = ("CV 模板比對" if use_cv_layer else "強制座標")
                    logger.info(f"[computer_use]   [UIA-first] 沒命中 '{ui_info.get('name', '')[:40]}' → 退 {_next}")
            elif ui_info and not use_uia_layer:
                logger.info(f"[computer_use]   [UIA-first] 跳過 — 使用者關了 UIA toggle")
            elif ui_info and user_chose_explicit:
                _chosen = "VLM" if vlm_mode != "off" else "OCR"
                logger.info(f"[computer_use]   [UIA-first] 跳過 — 使用者顯式選了 {_chosen} primary, 尊重使用者")

            # ── VLM 模式 1：description → OCR ──
            # 適用場景：畫面上的文字是動態的（訂單編號、當日日期、使用者名稱），
            # 錄製當下不知道回放時會是什麼字，所以 ocr_text 沒得填。
            # 做法是模型看圖回「目標實際顯示的文字」，座標仍交給 OCR 決定 ——
            # 模型不碰座標，就不會出現「指到隔壁那顆」的問題。
            #
            # 區域讀使用者拉的「藍框」（ocr_box_*）而不是紅框（search_region）：
            # 這個模式最終走 OCR，範圍語意要跟純 OCR 路徑一致。
            if vlm_mode == "description":
                vlm_prompt_click = (action.get("vlm_prompt") or action.get("description") or "").strip()
                if not vlm_prompt_click:
                    return ActionResult(False, index, atype,
                        "vlm_mode=description 但 vlm_prompt 為空（必填，描述要點什麼）")
                # 沒設定視覺模型就明確講清楚，不要靜默退回別的定位方式去點錯東西
                from . import vlm_cloud
                _cap = vlm_cloud.capability()
                if not _cap["available"]:
                    return ActionResult(False, index, atype,
                        f"vlm_mode=description 需要視覺模型，但{_cap['reason']}。{_cap['hint']}")

                _vlm_box_w = int(action.get("ocr_box_width", 0) or 0)
                _vlm_box_h = int(action.get("ocr_box_height", 0) or 0)
                vlm_region: Optional[tuple[int, int, int, int]] = None
                if _vlm_box_w > 0 and _vlm_box_h > 0:
                    vlm_region = (
                        int(action.get("ocr_box_left", 0) or 0),
                        int(action.get("ocr_box_top", 0) or 0),
                        _vlm_box_w,
                        _vlm_box_h,
                    )
                vlm_found, vlm_text, vlm_reason = _vlm_describe_to_text(
                    vlm_prompt_click, vlm_region, logger)
                if not vlm_found:
                    return ActionResult(False, index, atype,
                        f"視覺模型在螢幕上找不到目標：{vlm_reason}")

                find_text_on_screen = None
                try:
                    from .ocr import find_text_on_screen
                except Exception as _e:
                    return ActionResult(False, index, atype,
                        f"視覺模型給了文字 '{vlm_text}' 但 OCR 模組載不進來：{_e}")
                screen_bgr_v, sxv, syv = _capture_screen()
                near_v = (int(fx), int(fy)) if has_coord else None
                ocr_res_v = find_text_on_screen(
                    screen_bgr_v, vlm_text, origin_x=sxv, origin_y=syv,
                    lang_tag="zh-Hant-TW", near_xy=near_v,
                    search_radius=cv_search_radius, threshold=ocr_threshold,
                    region=vlm_region,
                    strict_region=bool(action.get("ocr_strict_region", False)),
                )
                if not ocr_res_v.found:
                    return ActionResult(False, index, atype,
                        f"視覺模型看到的目標文字是 '{vlm_text}'（{vlm_reason[:60]}），"
                        f"但 OCR 在螢幕上找不到這段文字：{ocr_res_v.reason}")
                _do_click(pg, ocr_res_v.center[0], ocr_res_v.center[1],
                          button, clicks, hold_sec, modifiers)
                hold_tag_v = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                msg = (f"{mods_tag} 描述→OCR 點擊 '{vlm_text}' @ {ocr_res_v.center} "
                       f"(模型: {vlm_reason[:60]}, OCR conf={ocr_res_v.confidence:.2f}){hold_tag_v}")
                duration = int((time.time() - t0) * 1000)
                logger.info(f"[computer_use]   ✓ {msg}（{duration}ms）")
                return ActionResult(True, index, atype, msg, duration)

            # ── VLM 模式 3：grounding → 地端 GUI 定位模型直接給座標 ──
            # 這是唯一一個「VLM 真的決定座標」的模式，前提是用**專門訓練過 GUI 定位**
            # 的地端模型（Mano-CUA 系）。2026-08-03 實測 14/14 命中、誤差中位數 4.5px。
            # 通用雲端模型不適用這條路（給座標不準，那正是 description/anchor_pick
            # 刻意繞開座標的原因）。
            # 失敗一律不讓整步掛掉 —— 往下掉回原本的 CV → 座標流程。
            if vlm_mode == "grounding":
                _g_prompt = (action.get("vlm_prompt") or action.get("description") or "").strip()
                if not _g_prompt:
                    return ActionResult(False, index, atype,
                        "vlm_mode=grounding 但 vlm_prompt 為空（必填，描述要點什麼）")
                _g_ok = False
                try:
                    from . import vlm_grounding as _vg
                    _full, _fox, _foy = _capture_screen()

                    # ── 決定要給模型看多大範圍 ────────────────────────────
                    # 這個模型的已知弱點是「一定會指一個最像的東西」，畫面上候選
                    # 越多越容易指錯。所以先只給它一小塊看，不行才放大到整個螢幕。
                    #
                    # 範圍怎麼來：
                    #   拖過橘框 → 用橘框
                    #   沒拖過   → 用「錄製座標 ±cv_search_radius」
                    #             這正是編輯器裡那個自動產生的橘框所畫的範圍，
                    #             也是 CV 的預設搜尋範圍。
                    # ⚠ 2026-08-06 修正：這裡原本只讀 search_region，沒拖過就把
                    #   整個螢幕丟給模型 —— 同一個橘框 CV 遵守、直接定位卻無視，
                    #   使用者看到框卻不知道模型其實在看整片桌面。
                    _reg = _parse_search_region(action)
                    if _reg is None and has_coord:
                        _r = max(1, int(cv_search_radius))
                        _reg = (int(fx) - _r, int(fy) - _r, _r * 2, _r * 2)

                    def _crop(reg):
                        """把整螢幕裁成 reg。回 (影像, 原點x, 原點y, 標籤)。"""
                        if reg is None:
                            return _full, _fox, _foy, "整個螢幕"
                        _l, _t, _w, _h = reg
                        _x0, _y0 = max(0, _l - _fox), max(0, _t - _foy)
                        _x1 = min(_full.shape[1], _x0 + _w)
                        _y1 = min(_full.shape[0], _y0 + _h)
                        if _x1 <= _x0 or _y1 <= _y0:
                            return _full, _fox, _foy, "整個螢幕（指定範圍與螢幕無交集）"
                        return (_full[_y0:_y1, _x0:_x1], _fox + _x0, _foy + _y0,
                                f"限定範圍 {_x1 - _x0}x{_y1 - _y0}")

                    # 兩段嘗試：先小範圍（準），失敗才整個螢幕（不失去現有能力）
                    _attempts = [_crop(_reg)]
                    if _reg is not None:
                        _attempts.append(_crop(None))

                    def _verify(scr_, gx_, gy_) -> bool:
                        """錨點局部驗證：模型指的那一塊，長得像不像錄製時的錨點。

                        2026-08-03 實測：模型**不會承認找不到東西**。問它畫面上
                        沒有的元素（例：在 Excel 裡問「開始播放投影片」按鈕），
                        它會自信地指一個最像的位置。提示詞只擋得住最離譜的那種。
                        → 步驟通常還留著錄製時的錨點圖，拿它跟「模型指的位置」做
                          局部比對：像 = 採用；完全不像 = 判定幻覺。
                          這裡是**局部**比對（只看那一小塊），跟 CV 全域搜尋不同 ——
                          CV 全域找不到正是使用者選這個模式的原因。
                        """
                        if tpl_path is None or not Path(tpl_path).exists():
                            return True          # 沒錨點圖可比，只能相信模型
                        try:
                            _tpl = _imread_unicode(Path(tpl_path))
                            if _tpl is None:
                                return True
                            # 純色錨點跟任何東西比都 1.000（實測連雜訊都 1.000），
                            # 拿它「驗證」等於蓋橡皮圖章。守不住就要講出來，
                            # 不能讓使用者以為有守門。
                            _v = _anchor_variance(_tpl)
                            if _v < ANCHOR_MIN_VARIANCE:
                                logger.warning(
                                    f"[computer_use]   ⚠ 錨點 {Path(tpl_path).name} 幾乎沒有特徵"
                                    f"（灰階變異數 {_v:.1f} < {ANCHOR_MIN_VARIANCE}）——"
                                    f"幻覺守門對這張圖無效，這次定位沒有經過驗證。"
                                    f"建議重圈一個含文字或邊框的範圍。")
                                return True
                            _th, _tw = _tpl.shape[:2]
                            # 在座標周圍開一個小窗「搜尋」，而不是在座標上「對齊」。
                            # 實測固定位置比對太脆：模型誤差 3px 相似度就從 1.00 掉到 0.31，
                            # 誤差 11px 掉到 0.17 —— 跟幻覺的 0.02~0.07 只差一點點。
                            # 開窗搜尋容許幾像素位移，正確結果的分數才拉得開。
                            _mg = VLM_GROUNDING_VERIFY_MARGIN
                            _sh, _sw = scr_.shape[:2]
                            # ⚠ 目標在畫面邊緣時，直接依座標往兩邊截會讓窗比錨點
                            #   還小，然後「比不了」就靜默放行 —— 等於邊緣位置永遠
                            #   免驗證。2026-08-06 實測：模型指到 (5,5) 就這樣被放過。
                            #   改成把窗整個推回畫面內，維持足夠大小再比。
                            _ww, _wh = _tw + 2 * _mg, _th + 2 * _mg
                            _wx0 = min(max(0, gx_ - _ww // 2), max(0, _sw - _ww))
                            _wy0 = min(max(0, gy_ - _wh // 2), max(0, _sh - _wh))
                            _win = scr_[_wy0:_wy0 + _wh, _wx0:_wx0 + _ww]
                            if _win.shape[0] < _th or _win.shape[1] < _tw:
                                # 整張畫面都比錨點小才會走到這 —— 真的比不了，
                                # 但要講出來，不能讓人以為驗過了
                                logger.warning(
                                    f"[computer_use]   ⚠ 畫面({_sw}x{_sh})比錨點"
                                    f"({_tw}x{_th})還小，這次定位沒有經過幻覺驗證")
                                return True
                            _score, _mode = _best_match_score(_win, _tpl)
                            _v = _score >= VLM_GROUNDING_VERIFY_MIN
                            logger.info(
                                f"[computer_use]   錨點局部驗證 相似度 {_score:.3f}"
                                f"（{_mode}、門檻 {VLM_GROUNDING_VERIFY_MIN}、"
                                f"搜尋窗 ±{_mg}px）{'通過' if _v else ' ✗ 疑似幻覺'}")
                            return _v
                        except Exception as _ve:
                            logger.debug(f"[computer_use]   局部驗證跳過：{_ve}")
                            return True

                    # 驗證也要放進迴圈 —— 小範圍指錯（驗證沒過）跟「沒定位到」一樣，
                    # 都該讓它用整個螢幕再試一次，而不是直接放棄退回 CV。
                    _hit = None
                    _last_why = ""
                    for _ai, (_scr, _ox, _oy, _tag) in enumerate(_attempts):
                        logger.info(f"[computer_use]   VLM 定位範圍：{_tag}"
                                    + ("（第 2 次嘗試）" if _ai else ""))
                        import cv2 as _cv2
                        # 一定要寫在共用交換目錄 —— Windows 的 %TEMP% 沒掛進容器，
                        # 容器會回「No such file or directory」（2026-08-03 實測）。
                        # 檔名也只用 ASCII，避免路徑編碼問題。
                        _png = str(_vg.shared_dir() / f"_shot_{os.getpid()}_{index}_{_ai}.png")
                        _ok_w, _enc = _cv2.imencode(".png", _scr)
                        if not _ok_w:
                            raise RuntimeError("截圖編碼失敗")
                        Path(_png).write_bytes(_enc.tobytes())
                        try:
                            _ok, _gx, _gy, _why = _vg.locate(
                                _g_prompt, _png, _scr.shape[1], _scr.shape[0], logger)
                        finally:
                            try:
                                os.unlink(_png)
                            except OSError:
                                pass
                        _last_why = _why
                        if not _ok:
                            logger.info(f"[computer_use]   {_tag}內沒定位到（{_why[:60]}）")
                            continue
                        if not _verify(_scr, _gx, _gy):
                            logger.info(f"[computer_use]   {_tag}內指的位置與錨點不符")
                            _last_why = "指的位置與錨點不符"
                            continue
                        _hit = (_ox + _gx, _oy + _gy, _why)
                        break

                    if _hit is not None:
                        _sx, _sy, _why = _hit
                        logger.info(f"[computer_use]   VLM 定位 → 螢幕 ({_sx},{_sy})：{_why[:70]}")
                        _do_click(pg, _sx, _sy, button, clicks, hold_sec, modifiers)
                        return ActionResult(True, index, atype,
                            f"{mods_tag}VLM 定位點擊 ({_sx},{_sy})：{_g_prompt[:40]}")
                    logger.warning(f"[computer_use]   VLM 定位失敗（{_last_why[:80]}）→ 退回 CV")
                except Exception as _e:
                    logger.warning(
                        f"[computer_use]   VLM 定位例外（{_e.__class__.__name__}: {_e}）→ 退回 CV")
                # 沒 return = 往下走既有的 CV / 座標 fallback
                ocr_will_run = False

            # ── VLM 模式 anchor_pick：Atlas-Lite 不支援 ──
            # 原本是「雲端 VLM 從多張變體錨點挑一張最像的 → 用該張走 CV」。
            # 替代做法：把變體錨點各自建一個 if_image_found 分支，
            # 或直接用 grounding 描述目標。
            if vlm_mode == "anchor_pick":
                return ActionResult(False, index, atype,
                    "vlm_mode='anchor_pick' 需要雲端 VLM，Atlas-Lite 不支援。"
                    "請改用 vlm_mode='grounding'，或用 if_image_found 逐張試錨點。")

            # 短路到「強制座標」: 使用者顯式關了 CV toggle 但保留座標 toggle = 直接點記錄座標
            # use_cv=False + use_coord=True 才走這裡, 預設(use_cv=True)永遠跑 CV 模板比對
            # OCR / vlm_mode=anchor_pick / vlm_mode=description 各有自己路徑、不走這裡
            if (vlm_mode != "anchor_pick"
                and not use_cv_layer
                and use_coord_layer
                and has_coord
                and not ocr_will_run):
                _do_click(pg, int(fx), int(fy), button, clicks, hold_sec, modifiers)
                hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                _src = "UIA 沒中" if uia_first_attempted else "CV toggle 關"
                msg = f"[強制座標 ({_src})]{mods_tag} 點擊 ({fx},{fy}) button={button} clicks={clicks}{hold_tag}"
                duration = int((time.time() - t0) * 1000)
                logger.info(f"[computer_use]   ✓ {msg}（{duration}ms）")
                return ActionResult(True, index, atype, msg, duration)

            # Hover 預熱：錄製當下游標停在按鈕上、錨點擷取到 Windows hover highlight
            # 狀態；回放用 pyautogui 瞬移沒觸發 hover → 螢幕與錨點不一樣 conf 掉
            # 把游標移到錄製座標附近、等指定 ms 讓 hover 效果渲染後再比對
            # OCR 模式跳過 hover（純文字偵測不受 hover 影響，而且可能反而干擾游標位置）
            if cv_trigger_hover and has_coord and not ocr_will_run:
                try:
                    pg.moveTo(int(fx), int(fy))
                    time.sleep(max(50, int(cv_hover_wait_ms)) / 1000.0)
                except Exception:
                    pass  # 移動失敗就略過（例如座標超出螢幕），後面搜尋仍然照跑

            # ── OCR 模式分支 ──
            # 只有 use_ocr=True 且 ocr_text 有值才跑。OCR 失敗時的後續行為由 ocr_cv_fallback 控制：
            #   ocr_cv_fallback=False（預設）→ 失敗立即 FAIL（符合「選 OCR 就代表 CV 不適用」的直覺）
            #   ocr_cv_fallback=True         → 失敗繼續走下面的 CV 比對鏈（再受 cv_coord_fallback 接棒）
            if ocr_will_run:
                find_text_on_screen = None
                try:
                    from .ocr import find_text_on_screen
                except Exception:
                    try:
                        from .ocr import find_text_on_screen  # type: ignore
                    except Exception as _e:
                        logger.error(f"[computer_use]   ✗ 無法載入 OCR 模組：{_e}")
                if find_text_on_screen is not None:
                    screen_bgr, sx, sy = _capture_screen()
                    near = (int(fx), int(fy)) if has_coord else None
                    # 藍框：per-action 顯式 OCR 搜尋範圍（絕對桌面座標）
                    ocr_region = None
                    _box_w = int(action.get("ocr_box_width", 0) or 0)
                    _box_h = int(action.get("ocr_box_height", 0) or 0)
                    if _box_w > 0 and _box_h > 0:
                        ocr_region = (
                            int(action.get("ocr_box_left", 0) or 0),
                            int(action.get("ocr_box_top", 0) or 0),
                            _box_w,
                            _box_h,
                        )
                    ocr_res = find_text_on_screen(
                        screen_bgr, ocr_text, origin_x=sx, origin_y=sy,
                        lang_tag="zh-Hant-TW",
                        near_xy=near, search_radius=cv_search_radius,
                        threshold=ocr_threshold,
                        region=ocr_region,
                        strict_region=bool(action.get("ocr_strict_region", False)),
                    )
                    if ocr_res.found:
                        _do_click(pg, ocr_res.center[0], ocr_res.center[1],
                                  button, clicks, hold_sec, modifiers)
                        hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                        msg = (f"{mods_tag} 點擊 OCR 文字 '{ocr_text}' @ {ocr_res.center} "
                               f"(matched='{ocr_res.text[:30]}', conf={ocr_res.confidence:.2f}){hold_tag}")
                        duration = int((time.time() - t0) * 1000)
                        logger.info(f"[computer_use]   ✓ {msg}（{duration}ms）")
                        return ActionResult(True, index, atype, msg, duration)
                    # OCR 失敗
                    if not ocr_cv_fallback:
                        fail_msg = f"{ocr_res.reason}（ocr_cv_fallback=off → 失敗直接 FAIL 不退回 CV/座標）"
                        logger.error(f"[computer_use]   ✗ {fail_msg}")
                        return ActionResult(False, index, atype, fail_msg)
                    logger.info(f"[computer_use]   {ocr_res.reason[:120]}，ocr_cv_fallback=on → 改試 CV 比對")
                elif not ocr_cv_fallback:
                    # OCR 模組載不進來且使用者沒開 fallback → 直接 FAIL（不偷偷走 CV）
                    return ActionResult(False, index, atype, "OCR 模組無法載入且 ocr_cv_fallback=off")

            # 搜尋策略：
            # 1. 有錄製座標 → 先在附近 ±cv_search_radius 範圍搜尋（防跨螢幕假陽性）
            #    首次 match 若 conf 低於門檻，等 150ms 再 retry 一次（最多 2 次）
            #    吸收 hover fade-in / transition 動畫未穩定造成的瞬時誤判
            #    典型 case：Windows 關閉鈕第一次 match 得 0.56、再等 150ms 變 0.97
            # 2. 仍找不到：若 cv_search_only_near=True → 直接 FAIL
            #              否則擴大到整個桌面
            # 3. 全螢幕也找不到 → 退回絕對座標 fallback（下方 else 分支）
            _SETTLE_RETRIES = 2          # 第一次 + 最多 1 次 retry
            _SETTLE_WAIT_MS = 150        # retry 前 sleep

            # 使用者明確指定的搜尋紅框（優先於錄製座標附近搜尋）
            region_rect = _parse_search_region(action)
            cv_strict = bool(action.get("cv_strict_region", False))

            # ⚠ 這裡曾經試過「錄製時算出安全半徑、執行時自動鎖範圍」，
            #   2026-08-06 實測證明不可行 —— 錄製當下的替身分佈預測不了回放當下的
            #   （桌面上開了什麼視窗會變，新的替身會冒出來）。實驗中自動半徑
            #   開與關都一樣誤點 764px。已移除，不要再照那個方向做。
            #
            #   目前唯一實測有效的是「限制搜尋範圍 + 找不到就停」，
            #   由使用者自己決定：cv_search_only_near / search_region + cv_strict_region。
            #   錄製後的錨點獨特性分析（analyze_anchor_uniqueness）只負責提醒，
            #   不自動改行為。

            def _search(nx_: Optional[int], ny_: Optional[int],
                        force_region: Optional[tuple[int, int, int, int]] = "use_outer",
                        threshold_override: Optional[float] = None) -> MatchResult:
                """先跑 gray 模式，若 conf < threshold 再跑 edge 模式，取較高 conf。
                edge 對 hover fade / 主題色差異更容忍，代價 +20ms。

                force_region:
                  "use_outer"（預設）→ 用外層 region_rect（紅框）
                  None              → 強制忽略 region_rect，用 nx_/ny_ 或全螢幕
                  tuple             → 強制用這個 region
                threshold_override: 全桌面搜尋時傳較高門檻,避免大螢幕假匹配點錯位置。
                """
                if force_region == "use_outer":
                    use_region = region_rect
                else:
                    use_region = force_region
                eff_threshold = threshold if threshold_override is None else threshold_override

                def _find_one(p: str, m: str) -> MatchResult:
                    if use_region is not None:
                        return find_template(p, threshold=eff_threshold, multi_scale=True,
                                             region=use_region, mode=m)
                    if nx_ is not None and ny_ is not None:
                        return find_template(p, threshold=eff_threshold, multi_scale=True,
                                             near_xy=(nx_, ny_), search_radius=cv_search_radius, mode=m)
                    return find_template(p, threshold=eff_threshold, multi_scale=True, mode=m)

                def _find(m: str) -> MatchResult:
                    """單張錨點時就是一次比對；多形態時每張都比、取分數最高的。

                    取最高分而不是「第一張過門檻就用」—— 兩張候選都可能勉強過門檻，
                    但只有一張是畫面當下真正的樣子，分數差距才分得出來。
                    """
                    best = _find_one(str(tpl_candidates[0][1]), m)
                    if len(tpl_candidates) == 1:
                        return best
                    best_name = tpl_candidates[0][0]
                    for _nm, _p in tpl_candidates[1:]:
                        r = _find_one(str(_p), m)
                        if r.confidence > best.confidence:
                            best, best_name = r, _nm
                    if best_name != img_name:
                        logger.info(f"[computer_use]   多形態錨點[{m}]：採用 {best_name}"
                                    f"（conf={best.confidence:.2f}）")
                    return best
                gray = _find("gray")
                if gray.found:
                    return gray
                # Gray 沒過門檻 → 試 edge 救一下
                edge = _find("edge")
                # 以 conf 做仲裁，但考量 edge 先天分數偏低，edge 要多給 0.05 才可以勝出
                # 避免 gray 比較接近但仍低、edge 亂抓到邊緣多的位置
                if edge.found or edge.confidence >= gray.confidence + 0.05:
                    logger.info(f"[computer_use]   edge fallback: gray={gray.confidence:.2f}, edge={edge.confidence:.2f} → 採用 edge")
                    return edge
                return gray

            if has_coord:
                # Phase 1：紅框內找（如果 region_rect 設了），或近錄製座標找
                m = MatchResult(False)
                for _attempt in range(_SETTLE_RETRIES):
                    m = _search(int(fx), int(fy))
                    if m.found:
                        break
                    if _attempt + 1 < _SETTLE_RETRIES:
                        logger.info(f"[computer_use]   首次比對 conf={m.confidence:.2f} < {threshold}，等 {_SETTLE_WAIT_MS}ms 讓動畫穩定後 retry")
                        time.sleep(_SETTLE_WAIT_MS / 1000.0)
                # Phase 2：紅框 miss、不嚴格 → 退回錄製座標附近 ±cv_search_radius 找
                if not m.found and region_rect is not None and not cv_strict:
                    logger.info(f"[computer_use]   紅框內找不到 → 退回錄製座標附近 ±{cv_search_radius}px 重試")
                    m = _search(int(fx), int(fy), force_region=None)
                # Phase 3：附近 miss、不嚴格、未鎖定附近 → 擴大全螢幕
                if not m.found and not cv_search_only_near and not cv_strict:
                    logger.info(f"[computer_use]   附近 ±{cv_search_radius}px 找不到（best={m.confidence:.2f}），擴大到整個桌面（門檻提高避免誤點）")
                    # 全桌面搜尋強制較高門檻:0.5 在 4K/多螢幕幾乎必有假陽性 → 點到無關位置卻當成功
                    m = _search(None, None, force_region=None,
                                threshold_override=max(threshold, _FULLSCREEN_MIN_THRESHOLD))
                # 嚴格模式 + 紅框 miss：明確標示原因
                if not m.found and cv_strict and region_rect is not None:
                    m.reason = f"嚴格鎖定範圍：紅框內找不到 {img_name}（{m.reason}）"
            else:
                # 無錄製座標 → 純全桌面模板搜尋,同樣用較高門檻避免誤點
                m = _search(None, None, threshold_override=max(threshold, _FULLSCREEN_MIN_THRESHOLD))

            if m.found:
                # 螢幕邊緣擷取時，點擊位置不在錨點影像中心，加上偏移校正
                off_x = int(action.get("anchor_off_x", 0) or 0)
                off_y = int(action.get("anchor_off_y", 0) or 0)
                click_x = m.center[0] + int(off_x * m.scale)
                click_y = m.center[1] + int(off_y * m.scale)
                _do_click(pg, click_x, click_y, button, clicks, hold_sec, modifiers)
                hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                off_tag = f" off=({off_x},{off_y})" if (off_x or off_y) else ""
                msg = f"{mods_tag} 點擊 {img_name} @ ({click_x},{click_y}) (conf={m.confidence:.2f} [{m.mode}], scale={m.scale}){off_tag}{hold_tag}"
            else:
                # 嚴格鎖定範圍：紅框 miss 時連座標 fallback 都禁掉
                if cv_strict and region_rect is not None:
                    fail_msg = m.reason
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)
                # Fallback 判斷(分三層 gate):
                #   1. has_coord:有錄製座標(沒有就根本退不了)
                #   2. allow_coord_fallback:系統層級信心(螢幕解析度跟錄製時相同)
                #   3. use_coord_layer:action 層級「📍 強制座標」 toggle(三層 fallback 的最後一層)
                #
                # step-level cv_coord_fallback 只在「使用者顯式進入進階模式」時生效:
                #   - OCR primary (use_ocr=True)
                #   - CV-only 自定義 (use_uia=False)
                # 預設三層 fallback (UIA ☑ + CV ☑ + 座標 ☑) 完全忽略 cv_coord_fallback、
                # 避免 step-level 跟 action-level 兩個 toggle 互相干擾、使用者全勾預設卻退不到座標的 bug。
                is_explicit_advanced = use_ocr or (not use_uia_layer)
                step_level_blocks_fallback = is_explicit_advanced and not cv_coord_fallback

                if (has_coord and allow_coord_fallback and use_coord_layer
                        and not step_level_blocks_fallback):
                    logger.warning(f"[computer_use]   ⚠ 圖像比對失敗({m.reason}),退回錄製座標 ({fx},{fy})")
                    _do_click(pg, int(fx), int(fy), button, clicks, hold_sec, modifiers)
                    hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                    msg = f"[fallback]{mods_tag} 點擊絕對座標 ({fx},{fy}){hold_tag}(原圖 {img_name} 找不到)"
                elif has_coord and not allow_coord_fallback:
                    fail_msg = (f"找不到錨點圖 {img_name}({m.reason}),且目前螢幕解析度與錄製時不同,"
                        f"絕對座標 ({fx},{fy}) 不可信、請重錄或調整到原螢幕布局")
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)
                elif has_coord and not use_coord_layer:
                    fail_msg = (f"找不到錨點圖 {img_name}({m.reason}),且使用者關閉了 action 的「📍 強制座標」 toggle。"
                        f"若要容錯請到 panel 打開該 toggle。")
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)
                elif has_coord and step_level_blocks_fallback:
                    _mode = "OCR 進階模式" if use_ocr else "CV-only 模式"
                    fail_msg = (f"找不到錨點圖 {img_name}({m.reason}),且在 {_mode} 下使用者關閉了步驟層級「CV 失敗退回座標」。"
                        f"若要容錯請到 panel 的 CV 設定打開該 toggle。")
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)
                else:
                    fail_msg = f"找不到錨點圖 {img_name}（{m.reason}），且無 fallback 座標可用"
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)

        elif atype == "click_at":
            x, y = int(action.get("x", 0)), int(action.get("y", 0))
            in_range, layout_info = _point_in_any_screen(x, y)
            if not in_range:
                return ActionResult(False, index, atype,
                    f"座標 ({x},{y}) 超出目前螢幕範圍（{layout_info}）")
            button = action.get("button", "left")
            clicks = int(action.get("clicks", 1))
            hold_sec = float(action.get("hold_sec", 0) or 0)
            modifiers = list(action.get("modifiers", []) or [])
            mods_tag = f"[{'+'.join(modifiers)}]" if modifiers else ""

            # 三層 fallback toggle — click_at 沒有 CV 模板 (它就是純座標 action),
            # 所以只有 UIA + 強制座標 兩層可用。CV toggle 在 click_at 沒效。
            use_uia_layer = bool(action.get("use_uia", True))
            use_coord_layer = bool(action.get("use_coord", True))
            if not use_uia_layer and not use_coord_layer:
                fail_msg = "click_at: UIA / 強制座標 兩個 toggle 都關、無方法可用"
                logger.error(f"[computer_use]   ✗ {fail_msg}")
                return ActionResult(False, index, atype, fail_msg)

            # Phase 0: UIA-first — 有錄到 ui + use_uia=True 才跑
            ui_info = action.get("ui") if isinstance(action.get("ui"), dict) else None
            if ui_info and use_uia_layer:
                try:
                    from .uia_lookup import find_click_point as _uia_find
                except Exception:
                    try:
                        from .uia_lookup import find_click_point as _uia_find  # type: ignore
                    except Exception:
                        _uia_find = None  # type: ignore
                if _uia_find is not None:
                    point = _uia_find(ui_info, timeout=2.0)
                    if point:
                        cx, cy = point
                        _do_click(pg, cx, cy, button, clicks, hold_sec, modifiers)
                        hold_tag_u = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                        msg = (f"{mods_tag} UIA 命中 '{ui_info.get('name', '')[:40]}' "
                               f"@ ({cx},{cy}){hold_tag_u}")
                        duration = int((time.time() - t0) * 1000)
                        logger.info(f"[computer_use]   ✓ {msg}(UIA-first、{duration}ms)")
                        return ActionResult(True, index, atype, msg, duration)
                    if not use_coord_layer:
                        # 嚴格 UIA: 沒命中 + 座標 toggle 關 = fail
                        fail_msg = f"嚴格 UIA 模式: 元素 '{ui_info.get('name', '')[:40]}' 找不到 (強制座標 toggle 關)"
                        logger.error(f"[computer_use]   ✗ {fail_msg}")
                        return ActionResult(False, index, atype, fail_msg)
                    logger.info(f"[computer_use]   UIA 沒命中 → 退到強制座標")
            elif ui_info and not use_uia_layer:
                logger.info(f"[computer_use]   UIA 跳過 — 使用者關了 UIA toggle, 直接走強制座標")
            _do_click(pg, x, y, button, clicks, hold_sec, modifiers)
            hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
            msg = f"{mods_tag} 點擊絕對座標 ({x}, {y}){hold_tag}"

        elif atype == "type_text":
            text = action.get("text", "")
            if not text:
                return ActionResult(False, index, atype, "type_text 缺 text 欄位")
            # interval 控制打字節奏（每個字之間的間隔秒數）；中文用 write 可能失效，改 copy-paste
            if any(ord(c) > 127 for c in text):
                import pyperclip
                try:
                    pyperclip.copy(text)
                    pg.hotkey("ctrl", "v")
                    msg = f"輸入非 ASCII 文字（clipboard）：{text[:30]}"
                except Exception:
                    # 沒 pyperclip 就 fallback
                    pg.write(text, interval=0.03)
                    msg = f"輸入文字（逐字）：{text[:30]}"
            else:
                pg.write(text, interval=0.03)
                msg = f"輸入文字：{text[:30]}"

        elif atype == "hotkey":
            keys = action.get("keys", [])
            if not keys:
                return ActionResult(False, index, atype, "hotkey 缺 keys 欄位")
            # 單獨按修飾鍵（Shift / Ctrl / Alt / Win）要特別處理：
            # pyautogui.hotkey("shift") 底層用老 API keybd_event，Windows IME 的
            # 中英切換 hotkey 常常觸發不到。改用 pynput（SendInput）並明確拉長
            # press→release 間隔，讓 IME 有時間辨識為「獨立按 tap」。
            _MOD_TO_PYNPUT = {"shift": "shift", "ctrl": "ctrl", "alt": "alt",
                              "win": "cmd", "cmd": "cmd"}
            if len(keys) == 1 and keys[0].lower() in _MOD_TO_PYNPUT:
                from pynput.keyboard import Controller as _KC, Key as _K
                _kc = _KC()
                _pk = getattr(_K, _MOD_TO_PYNPUT[keys[0].lower()])
                _kc.press(_pk)
                time.sleep(0.12)
                _kc.release(_pk)
                msg = f"單按 {keys[0]}（pynput tap，IME-safe）"
            else:
                pg.hotkey(*keys)
                msg = f"熱鍵：{'+'.join(keys)}"

        elif atype == "wait":
            sec = float(action.get("seconds", 0.0))
            # 分段 sleep，中間可以 abort
            total, step = sec, 0.2
            while total > 0:
                _check_abort(run_id)
                time.sleep(min(step, total))
                total -= step
            msg = f"等待 {sec}s"

        elif atype == "wait_image":
            img_name = action.get("image", "")
            if not img_name:
                return ActionResult(False, index, atype, "wait_image 缺 image 欄位")
            tpl_path = assets_dir / img_name
            timeout = float(action.get("timeout_sec", 10.0))
            # action.confidence 沒設或為 0 → 退步驟層級 cv_threshold（跟 click_image 一致）
            threshold = float(action.get("confidence") or cv_threshold)
            region_rect = _parse_search_region(action)
            deadline = time.time() + timeout
            last_conf = 0.0
            while time.time() < deadline:
                _check_abort(run_id)
                m = find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                  region=region_rect)
                if m.found:
                    msg = f"{img_name} 出現（conf={m.confidence:.2f}）"
                    break
                last_conf = max(last_conf, m.confidence)
                time.sleep(0.3)
            else:
                return ActionResult(False, index, atype,
                    f"等待 {timeout}s 仍未出現 {img_name}（最佳 {last_conf:.2f} < {threshold}）")

        elif atype == "drag":
            x1 = int(action.get("x", 0))
            y1 = int(action.get("y", 0))
            x2 = int(action.get("x2", 0))
            y2 = int(action.get("y2", 0))
            button = action.get("button", "left")
            # 起點：預設使用絕對座標；只有使用者切到圖像模式（use_coord=False）才嘗試圖像定位校正
            img_name = action.get("image", "")
            if img_name and action.get("use_coord", True) is False:
                tpl_path = assets_dir / img_name
                # drag 也吃 step 層級 cv_threshold / cv_search_radius
                threshold = float(action.get("confidence") or cv_threshold)
                m = find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                  near_xy=(x1, y1), search_radius=cv_search_radius)
                if m.found:
                    dx = m.center[0] - x1
                    dy_shift = m.center[1] - y1
                    x1, y1 = m.center[0], m.center[1]
                    # 終點同步偏移，保持相對位移
                    x2 += dx
                    y2 += dy_shift
                elif cv_search_only_near:
                    return ActionResult(False, index, atype,
                        f"【只搜附近模式】drag 起點在 ({x1},{y1}) ±{cv_search_radius}px 內找不到錨點 {img_name}")
            # 座標防護：超出螢幕就拒絕執行
            for cx, cy, label in [(x1, y1, "起點"), (x2, y2, "終點")]:
                in_range, layout_info = _point_in_any_screen(cx, cy)
                if not in_range:
                    return ActionResult(False, index, atype,
                        f"拖曳{label}座標 ({cx},{cy}) 超出目前螢幕（{layout_info}）")
            # Windows 的 DragDetect 要求 mouseDown 後第一個 move 必須**嚴格超過 SM_CXDRAG (~4px)**
            # 才觸發真正的 OLE Drag-Drop。pyautogui.dragTo + 平順 lerp 常常第一步 < 4px 就被當
            # 普通點擊。解法：press 前從偏移位置抵達產生「pre-move delta」，press 後立刻做一個
            # 6px 的明顯跳躍突破閾值，再開始平滑 lerp。
            # 參考：https://devblogs.microsoft.com/oldnewthing/20100304-00/?p=14733
            from pynput.mouse import Controller as _MC, Button as _Btn
            _mc = _MC()
            _btn_map = {"left": _Btn.left, "right": _Btn.right, "middle": _Btn.middle}
            _btn = _btn_map.get(button, _Btn.left)
            drag_mods = list(action.get("modifiers", []) or [])
            # 修飾鍵在整個拖曳期間都要按著（Shift+drag=移動、Ctrl+drag=複製）
            for mod in drag_mods:
                pg.keyDown(mod)
            try:
                # 計算單位方向（用來做 6px 初始跨閾值跳躍；若起終點距離 < 6px 就固定往右跳）
                dx = x2 - x1
                dy = y2 - y1
                dist = max(1, (dx * dx + dy * dy) ** 0.5)
                nx, ny = dx / dist, dy / dist

                # 1. 先從偏移位置抵達起點，產生真實的 pre-move event
                _mc.position = (int(x1 - nx * 3), int(y1 - ny * 3))
                time.sleep(0.05)
                _mc.position = (x1, y1)
                time.sleep(0.08)
                # 2. 按下
                _mc.press(_btn)
                time.sleep(0.10)
                # 3. 關鍵：press 後第一個 move 必須 > 4px 突破 SM_CXDRAG
                _mc.position = (int(x1 + nx * 6), int(y1 + ny * 6))
                time.sleep(0.06)
                # 4. 剩餘距離分段平滑移動到終點
                steps = 25
                total_move_sec = 0.6
                for i in range(1, steps + 1):
                    t = i / steps
                    mx = int(x1 + nx * 6 + (x2 - (x1 + nx * 6)) * t)
                    my = int(y1 + ny * 6 + (y2 - (y1 + ny * 6)) * t)
                    _mc.position = (mx, my)
                    time.sleep(total_move_sec / steps)
                # 5. 在終點停頓，讓 drop target highlight 起來再放手
                time.sleep(0.25)
                _mc.release(_btn)
            finally:
                # 即使過程拋例外也要放開修飾鍵，避免使用者鍵盤卡在按下狀態
                for mod in reversed(drag_mods):
                    pg.keyUp(mod)
            mods_tag = f"[{'+'.join(drag_mods)}] " if drag_mods else ""
            msg = f"{mods_tag}拖曳 ({x1},{y1}) → ({x2},{y2}) button={button}"

        elif atype == "scroll":
            x = int(action.get("x", 0))
            y = int(action.get("y", 0))
            dy = int(action.get("dy", 0))
            if dy == 0:
                logger.warning(f"[computer_use]   ⚠ scroll action dy=0，略過（action={action}）")
                return ActionResult(False, index, atype, "scroll 缺 dy 欄位或為 0")
            modifiers = list(action.get("modifiers", []) or [])
            # 座標防護：超出螢幕時不移動滑鼠直接在當前位置捲
            in_range, _ = _point_in_any_screen(x, y)
            if in_range:
                pg.moveTo(x, y)
                # Windows 上滑鼠移入新視窗需要短時間觸發 hover，否則後續 scroll 會被吞掉
                time.sleep(0.15)
            # 用 pynput 取代 pyautogui.scroll（pyautogui 在 Windows 有 known bug）
            from pynput.mouse import Controller as _MC
            _mc = _MC()
            # 按下修飾鍵（Ctrl+滾輪 = 縮放）→ scroll → 放開
            for mod in modifiers:
                pg.keyDown(mod)
            try:
                _mc.scroll(0, dy)
            finally:
                for mod in reversed(modifiers):
                    pg.keyUp(mod)
            mods_tag = f"[{'+'.join(modifiers)}] " if modifiers else ""
            msg = f"{mods_tag}在 ({x},{y}) 捲動 dy={dy}"

        elif atype == "activate_window":
            # 將指定標題的視窗帶到前景。解決錄製回放最常見的失敗原因：
            # 目標視窗在背景 → 點擊被其他視窗截去 or hover 作用在錯的視窗。
            # Linux 下 pygetwindow 支援很薄，用 try/except 吞例外並回 FAIL 讓使用者知情。
            title = (action.get("title") or "").strip()
            title_contains = (action.get("title_contains") or "").strip()
            if not title and not title_contains:
                return ActionResult(False, index, atype,
                    "activate_window 缺 title 或 title_contains 欄位")
            timeout = float(action.get("timeout_sec", 3.0))
            try:
                import pygetwindow as gw
            except Exception as e:
                return ActionResult(False, index, atype,
                    f"pygetwindow 無法載入（此平台可能不支援）：{e}")

            def _find_win():
                try:
                    all_wins = gw.getAllWindows()
                except Exception:
                    return []
                if title:
                    wins = [w for w in all_wins if (w.title or "") == title]
                else:
                    needle = title_contains.lower()
                    wins = [w for w in all_wins if needle in (w.title or "").lower()]
                return [w for w in wins if (w.title or "").strip()]

            deadline = time.time() + timeout
            target = None
            while True:
                _check_abort(run_id)
                matched = _find_win()
                if matched:
                    target = matched[0]
                    break
                if time.time() >= deadline:
                    break
                time.sleep(0.2)

            if target is None:
                needle = title or title_contains
                return ActionResult(False, index, atype,
                    f"{timeout}s 內找不到視窗標題 ~= '{needle}'")

            activated = False
            try:
                # 最小化的視窗必須先 restore 才能被 activate（pygetwindow 已實作這邏輯但不保證）
                if getattr(target, "isMinimized", False):
                    try:
                        target.restore()
                    except Exception:
                        pass
                target.activate()
                activated = True
            except Exception as _gw_err:
                # pygetwindow 在 foreground lock 等情境會拋 PyGetWindowException；改用 Win32 直接搶焦點
                try:
                    import ctypes  # type: ignore
                    hwnd = getattr(target, "_hWnd", None)
                    if hwnd:
                        ctypes.windll.user32.SetForegroundWindow(hwnd)
                        activated = True
                except Exception:
                    pass
                if not activated:
                    return ActionResult(False, index, atype,
                        f"找到視窗 '{target.title[:60]}' 但無法 activate：{_gw_err}")
            # 給 Window Manager 時間切換焦點，避免下個動作時視窗還沒完全在前
            time.sleep(0.25)
            msg = f"已將視窗 '{(target.title or '')[:60]}' 切到前景"

        elif atype == "assert_image":
            # 驗證某張錨點圖「當下」必須可見（和 wait_image 相似但語意不同：
            # wait_image 等畫面載入、timeout 較長；assert_image 檢查當前狀態、timeout 較短）。
            # 失敗訊息也更精確，方便排查為什麼流程走到這一步畫面長得不對。
            img_name = action.get("image", "")
            if not img_name:
                return ActionResult(False, index, atype, "assert_image 缺 image 欄位")
            tpl_path = assets_dir / img_name
            timeout = float(action.get("timeout_sec", 2.0))
            threshold = float(action.get("confidence") or cv_threshold)
            region_rect = _parse_search_region(action)
            deadline = time.time() + timeout
            last_conf = 0.0
            found_m: Optional[MatchResult] = None
            while True:
                _check_abort(run_id)
                m = find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                  region=region_rect)
                if m.found:
                    found_m = m
                    break
                last_conf = max(last_conf, m.confidence)
                if time.time() >= deadline:
                    break
                time.sleep(0.2)
            if found_m is None:
                return ActionResult(False, index, atype,
                    f"assert 失敗：{timeout}s 內 {img_name} 未出現（最佳 {last_conf:.2f} < {threshold}）")
            msg = f"assert 通過：{img_name} 可見（conf={found_m.confidence:.2f}）"

        elif atype == "assert_text":
            # OCR 版本的 assert：驗證螢幕上應該有某段文字。
            # 常見用途：登入成功後檢查「歡迎回來」、錯誤訊息檢查、狀態列文字等。
            text = (action.get("text") or action.get("ocr_text") or "").strip()
            if not text:
                return ActionResult(False, index, atype, "assert_text 缺 text 欄位")
            find_text_on_screen = None
            try:
                from .ocr import find_text_on_screen
            except Exception:
                try:
                    from .ocr import find_text_on_screen  # type: ignore
                except Exception as _e:
                    return ActionResult(False, index, atype, f"無法載入 OCR 模組：{_e}")
            timeout = float(action.get("timeout_sec", 2.0))
            threshold = float(action.get("ocr_threshold") or ocr_threshold)
            # 沿用既有 ocr_box_* 欄位做 OCR 搜尋範圍（藍框）
            _box_w = int(action.get("ocr_box_width", 0) or 0)
            _box_h = int(action.get("ocr_box_height", 0) or 0)
            region = None
            if _box_w > 0 and _box_h > 0:
                region = (
                    int(action.get("ocr_box_left", 0) or 0),
                    int(action.get("ocr_box_top", 0) or 0),
                    _box_w, _box_h,
                )
            deadline = time.time() + timeout
            last_reason = ""
            found_ocr = None
            while True:
                _check_abort(run_id)
                screen_bgr, sx, sy = _capture_screen()
                ocr_res = find_text_on_screen(
                    screen_bgr, text, origin_x=sx, origin_y=sy,
                    lang_tag="zh-Hant-TW",
                    threshold=threshold, region=region,
                    strict_region=bool(action.get("ocr_strict_region", False)),
                )
                if ocr_res.found:
                    found_ocr = ocr_res
                    break
                last_reason = ocr_res.reason
                if time.time() >= deadline:
                    break
                time.sleep(0.3)
            if found_ocr is None:
                return ActionResult(False, index, atype,
                    f"assert 失敗：{timeout}s 內未偵測到文字 '{text}'（{last_reason}）")
            msg = (f"assert 通過：文字 '{text}' 可見 @ {found_ocr.center} "
                   f"(matched='{found_ocr.text[:30]}', conf={found_ocr.confidence:.2f})")

        elif atype == "if_image_found":
            # 條件分支：根據錨點圖是否可見，選擇執行 then[] 或 else[]
            # 不叫 LLM、不燒 token — 純 CV template matching（跟 click_image 同一套）
            # 常見用途：
            #   1. 處理偶爾跳出的對話框（密碼過期、更新提示、網路錯誤）
            #   2. 登入狀態判斷（session 在 / 過期 兩種畫面）
            img_name = action.get("image", "")
            if not img_name:
                return ActionResult(False, index, atype, "if_image_found 缺 image 欄位")
            tpl_path = assets_dir / img_name
            timeout = float(action.get("timeout_sec", 2.0))
            threshold = float(action.get("confidence") or cv_threshold)
            region_rect = _parse_search_region(action)

            # 在 timeout 內等錨點出現；可能 0.3s 就找到、也可能等到 deadline
            deadline = time.time() + timeout
            found = False
            best_conf = 0.0
            while True:
                _check_abort(run_id)
                m = find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                  region=region_rect)
                if m.found:
                    found = True
                    best_conf = m.confidence
                    break
                best_conf = max(best_conf, m.confidence)
                if time.time() >= deadline:
                    break
                time.sleep(0.2)

            branch = action.get("then", []) if found else action.get("else", [])
            branch = branch or []
            branch_label = "then" if found else "else"
            logger.info(f"[computer_use] {indent}  → {img_name} "
                        f"{'found' if found else 'not found'} (conf={best_conf:.2f}) "
                        f"→ 走 {branch_label} 分支（{len(branch)} 個子動作）")

            for sub_i, sub_action in enumerate(branch):
                if not isinstance(sub_action, dict):
                    return ActionResult(False, index, atype,
                        f"{branch_label}[{sub_i}] 不是 dict，YAML 格式錯誤")
                sub_res = execute_action(
                    sub_action, assets_dir, sub_i, logger, run_id,
                    _depth=_depth + 1, **_exec_ctx,
                )
                if not sub_res.ok:
                    return ActionResult(False, index, atype,
                        f"if_image_found/{branch_label}[{sub_i+1}] "
                        f"({sub_res.action_type}) 失敗：{sub_res.message}")
            msg = (f"if {img_name}: {'match' if found else 'no-match'} "
                   f"→ 執行 {branch_label}（{len(branch)} 個子動作皆 OK）")

        elif atype == "retry_until":
            # 重複動作直到條件滿足：按鈕沒反應再按一次、網路抖動後重試
            # do[]  = 每輪要執行的動作清單
            # until = 檢查是否完成的單一動作（建議 wait_image / assert_image / assert_text）
            do_list = action.get("do", []) or []
            until_action = action.get("until", None)
            if not do_list:
                return ActionResult(False, index, atype, "retry_until 缺 do: 動作清單")
            if until_action is None or not isinstance(until_action, dict):
                return ActionResult(False, index, atype,
                    "retry_until 缺 until: 檢查條件（必須是單一動作 dict）")
            max_attempts = int(action.get("max_attempts", 3) or 3)
            wait_between = float(action.get("wait_between_sec", 1.0) or 1.0)
            if max_attempts < 1:
                max_attempts = 1

            last_fail_reason = ""
            success = False
            for attempt in range(1, max_attempts + 1):
                _check_abort(run_id)
                logger.info(f"[computer_use] {indent}  retry_until 第 {attempt}/{max_attempts} 輪")
                # 1. 跑 do[] 裡所有動作
                attempt_do_ok = True
                for sub_i, sub_a in enumerate(do_list):
                    if not isinstance(sub_a, dict):
                        return ActionResult(False, index, atype,
                            f"do[{sub_i}] 不是 dict，YAML 格式錯誤")
                    sub_res = execute_action(
                        sub_a, assets_dir, sub_i, logger, run_id,
                        _depth=_depth + 1, **_exec_ctx,
                    )
                    if not sub_res.ok:
                        attempt_do_ok = False
                        last_fail_reason = (f"第 {attempt} 輪 do[{sub_i+1}] "
                                            f"({sub_res.action_type}) 失敗：{sub_res.message}")
                        logger.info(f"[computer_use] {indent}    {last_fail_reason[:160]}")
                        break
                # 2. 跑 until 檢查
                if attempt_do_ok:
                    until_res = execute_action(
                        until_action, assets_dir, 0, logger, run_id,
                        _depth=_depth + 1, **_exec_ctx,
                    )
                    if until_res.ok:
                        success = True
                        msg = f"retry_until 成功於第 {attempt}/{max_attempts} 輪（{until_res.message[:80]}）"
                        break
                    last_fail_reason = f"第 {attempt} 輪 until 未通過：{until_res.message}"
                    logger.info(f"[computer_use] {indent}    {last_fail_reason[:160]}")
                # 3. 還有輪次就等一下再重試
                if attempt < max_attempts:
                    # sleep 分段好讓 abort 能及時生效
                    remaining = wait_between
                    while remaining > 0:
                        _check_abort(run_id)
                        chunk = min(0.3, remaining)
                        time.sleep(chunk)
                        remaining -= chunk

            if not success:
                return ActionResult(False, index, atype,
                    f"retry_until {max_attempts} 輪仍未成功：{last_fail_reason}")

        elif atype == "vlm_check":
            # 純判斷不點擊：把當下畫面送 Settings 主模型（必須支援視覺）
            # 用途：登入後確認成功訊息、確認對話框出現、檢查表單填好等
            # pass=false 步驟即失敗，VLM 寫的 reason 會出現在錯誤訊息中
            prompt = (action.get("vlm_prompt") or action.get("description") or "").strip()
            if not prompt:
                return ActionResult(False, index, atype,
                    "vlm_check 缺 vlm_prompt（判斷條件必填）")
            region_rect = _parse_search_region(action)
            # Atlas-Lite 無雲端 VLM —— vlm_check 沒有實作。
            # 替代:用 assert_image(錨點比對) 或 assert_text(OCR) 做確定性驗證。
            return ActionResult(False, index, atype,
                "vlm_check 需要雲端 VLM，Atlas-Lite 不支援。"
                "請改用 assert_image（錨點比對）或 assert_text（OCR 文字驗證）。")
            if not passed:
                return ActionResult(False, index, atype,
                    f"VLM 判斷未通過：{reason}")
            msg = f"VLM 判斷通過：{reason[:120]}"

        elif atype == "screenshot":
            import cv2
            img, _ox, _oy = _capture_screen()
            ts = int(time.time())
            out = assets_dir / f"debug_screenshot_{ts}.png"
            # 用 imencode + write_bytes 避免中文路徑問題
            ok, buf = cv2.imencode(".png", img)
            if ok:
                out.write_bytes(buf.tobytes())
                msg = f"已存 screenshot：{out.name}"
            else:
                msg = "screenshot imencode 失敗"

        else:
            return ActionResult(False, index, atype, f"未知動作類型：{atype}")

        duration = int((time.time() - t0) * 1000)
        logger.info(f"[computer_use]   ✓ {msg}（{duration}ms）")
        return ActionResult(True, index, atype, msg, duration)

    except RuntimeError as e:
        # abort signal
        raise
    except Exception as e:
        # pyautogui.FailSafeException / 其他意外
        import traceback
        logger.error(f"[computer_use]   ✗ {atype} 失敗：{e}")
        logger.debug(traceback.format_exc())
        return ActionResult(False, index, atype, f"{type(e).__name__}: {e}",
                            int((time.time() - t0) * 1000))


# ── 對外入口：執行一整個 computer_use 步驟 ─────────────────────────

@dataclass
class StepResult:
    success: bool
    total_actions: int
    succeeded: int
    failed_at: int = -1        # 首次失敗的 index；-1 = 全部成功
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    # UIA / Computer Use 透過 save_as 累積的步驟變數(uia_get_text 等存入)。
    # runner 拿到後寫進 PipelineRun.step_results[i].step_vars,後續 step 可用
    # `{{ steps.<name>.output.<key> }}` 引用。
    step_variables: dict = field(default_factory=dict)


MAX_ACTIONS_PER_STEP = 500  # 單步動作數上限，防止失控腳本無限循環


def validate_action_assets(actions: list[dict], assets_dir: Path) -> list[str]:
    """Preflight：掃一遍 actions 裡引用到的所有錨點圖是否存在（含巢狀 then/else/do/until）。
    提早 FAIL 比回放跑到一半才發現圖不見好太多，也讓使用者錯誤訊息更集中。
    回傳缺失檔名 list（保留順序、去重）。"""
    missing: list[str] = []
    seen: set[str] = set()

    def _scan(acts: list) -> None:
        for a in acts:
            if not isinstance(a, dict):
                continue
            for key in ("image", "image2"):
                name = a.get(key) or ""
                if not name or name in seen:
                    continue
                seen.add(name)
                if not (assets_dir / name).is_file():
                    missing.append(name)
            # 多形態錨點 + 相容用的 anchor_pick 候選圖，一樣要檢查
            for name in list(a.get("image_variants") or []) + list(a.get("vlm_anchors") or []):
                if not name or name in seen:
                    continue
                seen.add(name)
                if not (assets_dir / name).is_file():
                    missing.append(name)
            # 遞迴掃 if_image_found / retry_until 的巢狀動作
            for sub_key in ("then", "else", "do"):
                sub = a.get(sub_key)
                if isinstance(sub, list):
                    _scan(sub)
            until_a = a.get("until")
            if isinstance(until_a, dict):
                _scan([until_a])

    _scan(actions)
    return missing


def _screen_layout_match(meta_path: Path, logger: logging.Logger) -> bool:
    """比對錄製時與回放時的螢幕解析度。
    True = 一致（絕對座標 fallback 仍可靠）；False = 已改變（座標 fallback 不可信，應禁用）"""
    if not meta_path.is_file():
        return True  # 沒 meta 就寬容處理
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        rec_w, rec_h = meta.get("screen_width"), meta.get("screen_height")
        if not rec_w or not rec_h:
            return True
        import mss
        with mss.mss() as sct:
            cur = sct.monitors[1]
        if cur["width"] == rec_w and cur["height"] == rec_h:
            return True
        logger.warning(
            f"[computer_use] ⚠ 螢幕解析度變了："
            f"錄製 {rec_w}×{rec_h} → 目前 {cur['width']}×{cur['height']}；"
            f"將禁用絕對座標 fallback，強制圖像比對（常見於接/拔外接螢幕後）"
        )
        return False
    except Exception as e:
        logger.warning(f"[computer_use] 讀 meta.json 失敗：{e}")
        return True


def execute_computer_use_step(
    actions: list[dict],
    assets_dir: str,
    logger: logging.Logger,
    run_id: Optional[str] = None,
    fail_fast: bool = True,
    cv_threshold: float = 0.5,
    cv_search_only_near: bool = False,
    cv_search_radius: int = 400,
    cv_trigger_hover: bool = True,
    cv_hover_wait_ms: int = 200,
    cv_coord_fallback: bool = False,
    ocr_threshold: float = 0.6,
    ocr_cv_fallback: bool = False,
    # UIA 模式相關(action.type 是 uia_* 時用)
    uia_window: str = "",                   # 視窗 title pattern(可含 *)、空字串 = foreground
) -> StepResult:
    """執行一整個 computer_use 步驟。

    - actions: ComputerUseAction 物件的 list of dict
    - assets_dir: 錨點圖片資料夾（絕對路徑，通常是 ai_output/<name>/ 下的子資料夾）
    - fail_fast: True 則遇到失敗立刻中止；False 則繼續但記錄失敗數
    - cv_threshold: CV 比對門檻（0.50 寬鬆 / 0.80 標準 / 0.90 嚴格）
    - cv_search_only_near: True = 只搜錄製座標附近、找不到直接 FAIL（不退回全螢幕也不退回座標）
    - cv_search_radius: 附近搜尋半徑（像素）；實際搜尋範圍 (2r × 2r)
    - cv_trigger_hover: True = 比對前先 moveTo(錄製座標) + 200ms 讓 Windows hover 效果出現
    """
    import json  # 供 _screen_layout_match 讀 meta.json
    clear_abort(run_id or "")
    # ── ESC × 2 緊急中止 watcher(Windows only)──
    # 比 FAILSAFE 滑鼠甩到 (0,0) 更直覺;非 Windows 自動 no-op、FAILSAFE 仍生效當備援
    if run_id:
        _ensure_esc_watcher(run_id, logger)
    if len(actions) > MAX_ACTIONS_PER_STEP:
        return StepResult(
            success=False,
            total_actions=len(actions),
            succeeded=0,
            failed_at=-1,
            stdout="",
            stderr=f"動作數 {len(actions)} 超過安全上限 {MAX_ACTIONS_PER_STEP}，拒絕執行",
            exit_code=2,
        )
    assets = Path(assets_dir)
    if not assets.is_dir():
        # 沒有 assets 目錄也可能 OK（例如只有 type_text / wait），不直接失敗
        logger.warning(f"[computer_use] assets 目錄不存在：{assets_dir}")
    else:
        # 錨點圖 preflight：避免跑到一半才發現圖不見
        missing_imgs = validate_action_assets(actions, assets)
        if missing_imgs:
            preview = ", ".join(missing_imgs[:5])
            more = f"...（共 {len(missing_imgs)} 張）" if len(missing_imgs) > 5 else ""
            return StepResult(
                success=False,
                total_actions=len(actions),
                succeeded=0,
                failed_at=0,
                stdout="",
                stderr=f"preflight 失敗：assets_dir 缺少錨點圖：{preview}{more}",
                exit_code=2,
            )

    # 螢幕解析度比對：若改變（接/拔外接螢幕）就禁用座標 fallback
    layout_ok = _screen_layout_match(assets / "meta.json", logger) if assets.is_dir() else True

    logger.info(f"[computer_use] ▶ 開始執行 {len(actions)} 個動作 "
                f"（assets: {assets_dir}, fail_fast={fail_fast}）")
    logger.info(f"[computer_use] 🛡 Safety: 滑鼠移到螢幕左上角 (0,0) 可立即中止")

    succeeded = 0
    failed_at = -1
    messages: list[str] = []
    # 跨 action 變數儲存(uia_get_text / uia_get_table_rowcount 用 save_as 存的)
    # 後續 action 內 {{var_name}} 替換靠這個
    step_variables: dict[str, Any] = {}

    # （Atlas 的「動作後 VLM 把關」在此已移除 —— 它要把前後截圖送雲端 VLM 比對
    #   action.expected。預設 strategy 本來就是 off，移除不改變任何既有行為。
    #   要驗證動作結果請用 assert_image / assert_text 這類確定性檢查。）

    for i, action in enumerate(actions):
        # 路由:uia_* type 走 uia_executor、其他 type 走原 pixel-based execute_action
        atype = action.get("type", "")
        if atype.startswith("uia_"):
            try:
                from .uia_executor import execute_uia_action
                uia_res = execute_uia_action(action, uia_window, step_variables, logger)
                # 把結果包成 ActionResult 兼容後續流程
                res = ActionResult(
                    ok=uia_res.ok,
                    action_index=i,
                    action_type=atype,
                    message=uia_res.message,
                    duration_ms=0,
                )
                # 收集 save_as 變數
                if uia_res.saved_var:
                    var_name, var_value = uia_res.saved_var
                    step_variables[var_name] = var_value
                    logger.info(f"[computer_use] 變數 {var_name} = {var_value!r:.80}")
            except Exception as e:
                logger.exception(f"[computer_use] uia action {atype} 例外")
                res = ActionResult(ok=False, action_index=i, action_type=atype,
                                   message=f"{type(e).__name__}: {e}")
            messages.append(f"#{i+1} [{res.action_type}] {'OK' if res.ok else 'FAIL'}: {res.message}")
            if res.ok:
                succeeded += 1
            else:
                if failed_at < 0:
                    failed_at = i
                if fail_fast:
                    return StepResult(
                        success=False, total_actions=len(actions),
                        succeeded=succeeded, failed_at=i,
                        stdout="\n".join(messages),
                        stderr=f"動作 #{i+1} ({atype}) 失敗:{res.message}",
                        exit_code=1,
                        step_variables=dict(step_variables),
                    )
            continue  # uia 動作不走 VLM 把關(它本來就讀結構、漂移免疫)

        try:
            res = execute_action(action, assets, i, logger, run_id,
                                 allow_coord_fallback=layout_ok,
                                 cv_threshold=cv_threshold,
                                 cv_search_only_near=cv_search_only_near,
                                 cv_search_radius=cv_search_radius,
                                 cv_trigger_hover=cv_trigger_hover,
                                 cv_hover_wait_ms=cv_hover_wait_ms,
                                 cv_coord_fallback=cv_coord_fallback,
                                 ocr_threshold=ocr_threshold,
                                 ocr_cv_fallback=ocr_cv_fallback)
        except RuntimeError as abort_err:
            logger.warning(f"[computer_use] {abort_err}")
            return StepResult(
                success=False,
                total_actions=len(actions),
                succeeded=succeeded,
                failed_at=i,
                stdout="\n".join(messages),
                stderr=str(abort_err),
                exit_code=130,  # SIGINT-ish
                step_variables=dict(step_variables),
            )
        messages.append(f"#{i+1} [{res.action_type}] {'OK' if res.ok else 'FAIL'}: {res.message}")

        # （Atlas-Lite 移除「動作後 VLM 把關」）
        # 原本會截前後圖送雲端 VLM 比對 action.expected，不符就重試/中止/推 TG。
        # 預設 strategy 就是 off，移除不影響既有行為。
        # 要驗證動作結果請改用 assert_image / assert_text 這類確定性檢查。
        if res.ok:
            succeeded += 1
        else:
            if failed_at < 0:
                failed_at = i
            if fail_fast:
                return StepResult(
                    success=False,
                    total_actions=len(actions),
                    succeeded=succeeded,
                    failed_at=i,
                    stdout="\n".join(messages),
                    stderr=f"動作 #{i + 1} ({res.action_type}) 失敗：{res.message}",
                    exit_code=1,
                    step_variables=dict(step_variables),
                )

    all_ok = (failed_at < 0)
    logger.info(f"[computer_use] ■ 結束：{succeeded}/{len(actions)} 成功")
    return StepResult(
        success=all_ok,
        total_actions=len(actions),
        succeeded=succeeded,
        failed_at=failed_at,
        stdout="\n".join(messages),
        stderr="" if all_ok else f"失敗動作數：{len(actions) - succeeded}",
        exit_code=0 if all_ok else 1,
        step_variables=dict(step_variables),
    )
