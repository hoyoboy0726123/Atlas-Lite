"""錄製、錨點資產、UIA 選取器、螢幕擷取、地端定位模型狀態。

這一整組是「在畫布上把桌面自動化步驟編出來」會用到的 API，跟執行流程無關。
"""
import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import OUTPUT_BASE_PATH, WORKFLOW_DIR

router = APIRouter()


def _validate_assets_path(path_str: str) -> Path:
    """把 assets 路徑解析成絕對 Path，並強制限制在資料目錄內。

    這是路徑遍歷的防線：前端傳什麼字串進來都不能讓它讀寫到 data/ 以外
    （`../../../Windows/System32` 這種）。相對路徑一律以資料目錄為基準。
    """
    target = Path(path_str).expanduser()
    if not target.is_absolute():
        # 相容 Atlas 存的 `ai_output/<工作流>/<步驟>_assets`
        parts = target.parts
        if parts and parts[0] in ("ai_output", "workflows"):
            target = WORKFLOW_DIR.joinpath(*parts[1:])
        else:
            target = WORKFLOW_DIR / target
    try:
        resolved = target.resolve()
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"路徑解析失敗：{e}")
    root = OUTPUT_BASE_PATH.resolve()
    if not (str(resolved) == str(root) or str(resolved).startswith(str(root) + os.sep)):
        raise HTTPException(
            status_code=403,
            detail="拒絕存取：路徑不在允許範圍內（只能動 data/ 底下的檔案）。")
    return resolved


def _resolve_output_dir(path_str: str) -> Path:
    """錄製輸出目錄。跟 _validate_assets_path 同樣的限制。"""
    return _validate_assets_path(path_str)


class RecordingStartRequest(BaseModel):
    session_id: str
    # 相對路徑 → 解析到專案根；絕對路徑直接用
    output_dir: str


@router.post("/computer-use/recording/start")
async def start_computer_use_recording(req: RecordingStartRequest):
    """開始錄製一個 computer_use session（鎖定單一進程）。"""
    from engine.recorder import start_recording
    out_path = _resolve_output_dir(req.output_dir)
    try:
        return start_recording(session_id=req.session_id, output_dir=str(out_path))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/computer-use/recording/stop")
async def stop_computer_use_recording():
    """停止目前錄製中的 session，flush actions.json + meta.json。"""
    from engine.recorder import stop_recording
    return stop_recording()


@router.post("/computer-use/recording/arm-hotkey")
async def arm_recording_hotkey(req: RecordingStartRequest):
    """註冊全域熱鍵(預設 F7)按下後自動 start_recording。
    用途:讓使用者最小化瀏覽器、把焦點留在要錄製的 app、用熱鍵啟動錄製。
    """
    from engine.recorder import arm_start_hotkey
    out_path = _resolve_output_dir(req.output_dir)
    try:
        return arm_start_hotkey(session_id=req.session_id, output_dir=str(out_path), key="f7")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/computer-use/recording/disarm-hotkey")
async def disarm_recording_hotkey():
    """取消已註冊的全域熱鍵(panel 關閉或開始錄製時呼叫)。"""
    from engine.recorder import disarm_start_hotkey
    return disarm_start_hotkey()


class DuplicateAssetsRequest(BaseModel):
    src: str   # 原始 assetsDir(相對 or 絕對)
    dest: str  # 新 assetsDir


@router.post("/canvas/duplicate-assets")
async def duplicate_canvas_assets(req: DuplicateAssetsRequest):
    """節點複製貼上時、把 computer_use 的 assets 資料夾整份複製到新路徑。
    防止兩節點共用同一個資料夾(會互覆寫)。"""
    import shutil

    src = _validate_assets_path(req.src)
    dest = _validate_assets_path(req.dest)
    if not src.exists() or not src.is_dir():
        return {"ok": False, "error": f"原始資料夾不存在:{src}", "copied_files": 0}
    if dest.exists():
        return {"ok": False, "error": f"目標已存在(避免覆寫):{dest}", "copied_files": 0}
    try:
        shutil.copytree(src, dest)
        # 算 copied file 數
        n = sum(1 for _ in dest.rglob("*") if _.is_file())
        return {"ok": True, "src": str(src), "dest": str(dest), "copied_files": n}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "copied_files": 0}


@router.get("/computer-use/recording/status")
async def get_computer_use_recording_status():
    """查詢目前錄製中 session 的即時狀態（前端 polling 用）。"""
    from engine.recorder import get_recording_status
    return get_recording_status()


@router.get("/computer-use/recording/load")
async def load_computer_use_recording(output_dir: str):
    """讀回已錄好的 session（actions + meta），供前端編輯器載入。"""
    from engine.recorder import load_recording
    out_path = _resolve_output_dir(output_dir)
    result = load_recording(str(out_path))
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/computer-use/grounding/status")
async def grounding_status(force: bool = False):
    """這台機器能不能用 vlm_mode='grounding'，不能的話缺什麼。有 60s 快取。

    前端拿它決定「直接定位」按鈕是亮的還是停用 —— 停用時要說得出原因，
    不能讓使用者選了才發現不能用。
    """
    from engine.vlm_grounding import capability
    return await asyncio.get_event_loop().run_in_executor(None, capability, force)


class AnchorAnalyzeRequest(BaseModel):
    assets_dir: str
    actions: list[dict]   # 整個步驟的動作序列（只有 click_image 之類的會被分析）
    # 步驟層級的 CV 設定。分析必須用「執行時真的會用的那組值」，否則會出現
    # 「警告說有風險、實際上根本搆不到」的假警報。
    cv_search_radius: int = 400
    cv_threshold: float = 0.5
    cv_search_only_near: bool = False


@router.post("/computer-use/assets/analyze-anchors")
async def analyze_anchors(req: AnchorAnalyzeRequest):
    """算每張錨點在錄製畫面上有幾個替身，而且**執行時真的搆得到**。

    錄製完自動跑一次。只有真的有風險才回報 —— 早期版本掃整張圖就報警，
    結果報一堆執行時根本碰不到的位置，反而害使用者去改不該改的設定。
    """
    from engine.computer_use import analyze_anchor_uniqueness

    assets = _validate_assets_path(req.assets_dir)
    if not assets.is_dir():
        raise HTTPException(status_code=404, detail=f"找不到 assets 目錄：{assets}")

    loop = asyncio.get_event_loop()

    def _run() -> list[dict]:
        out = []
        for i, a in enumerate(req.actions or []):
            if not isinstance(a, dict) or not (a.get("image") or "").strip():
                out.append({"index": i, "checked": False, "reason": "非錨點動作"})
                continue
            r = analyze_anchor_uniqueness(
                assets, a,
                cv_search_radius=req.cv_search_radius,
                cv_threshold=float(a.get("confidence") or req.cv_threshold),
                cv_search_only_near=bool(a.get("cv_search_only_near",
                                               req.cv_search_only_near)),
            )
            r["index"] = i
            out.append(r)
        return out

    # 每張圖一次全螢幕 matchTemplate，20 個動作約 1~2 秒 —— 丟到 executor 別卡 event loop
    return {"results": await loop.run_in_executor(None, _run)}


class GroundingVerifyRequest(BaseModel):
    assets_dir: str
    action: dict      # 該步驟的 action dict（要有 image，最好也有 full_image）
    description: str  # 使用者自己寫的定位描述


@router.post("/computer-use/grounding/verify")
async def grounding_verify(req: GroundingVerifyRequest):
    """把描述餵給地端定位模型，看它能不能點回錄製時的那個位置。

    Atlas 另有一個「🪄 自動產生描述」端點，那個要打雲端視覺 API —— Atlas-Lite
    不帶雲端 LLM，所以描述由使用者自己寫，但**驗證這一步一定要留**：
    講錯的描述讀起來一樣通順，使用者不會逐字比對畫面，只能由系統當場抓出來。
    """
    from engine.computer_use import verify_grounding_desc
    assets = _validate_assets_path(req.assets_dir)
    if not assets.is_dir():
        raise HTTPException(status_code=404, detail=f"找不到 assets 目錄：{assets}")
    ok, dist, msg = await asyncio.get_event_loop().run_in_executor(
        None, verify_grounding_desc, assets, req.action, req.description)
    return {"verified": ok, "verify_px": None if dist < 0 else round(dist, 1),
            "verify_msg": msg}




@router.get("/computer-use/assets/list")
async def list_assets(dir: str):
    """列出 assets_dir 內的 PNG 錨點檔。給「VLM 挑錨點」的檔案選擇器用 —
    使用者錄完動作後，這個目錄會有 img_NNN.png（自動截）跟 img_NNN_manual.png
    （手動圈），這兩種都是合法錨點；full_NNN.png 是全螢幕截圖（給編輯器顯示
    用），不是錨點，過濾掉。"""
    target_dir = _validate_assets_path(dir)
    if not target_dir.is_dir():
        return {"dir": str(target_dir), "files": []}
    files = []
    for p in sorted(target_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue
        if p.name.startswith("full_"):
            continue   # 全螢幕截圖不是錨點
        try:
            stat = p.stat()
            files.append({
                "name": p.name,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            })
        except OSError:
            continue
    return {"dir": str(target_dir), "files": files}


@router.get("/computer-use/assets/image")
async def get_assets_image(dir: str, name: str):
    """提供單一錨點/全螢幕 PNG 檔供前端顯示（Modal 編輯錨點時用）。
    Query：dir=assets 資料夾（相對或絕對）、name=檔名"""
    from fastapi.responses import FileResponse
    target_dir = _validate_assets_path(dir)
    target_file = target_dir / name
    # 二次防呆：確保 file 也在 target_dir 內（防 name 含 ..）
    try:
        rf = target_file.resolve()
        if not str(rf).startswith(str(target_dir) + os.sep):
            raise HTTPException(status_code=403, detail="檔名不合法")
    except Exception:
        raise HTTPException(status_code=403, detail="檔名不合法")
    if not target_file.is_file():
        raise HTTPException(status_code=404, detail=f"檔案不存在：{name}")
    return FileResponse(str(target_file), media_type="image/png")


class CropRequest(BaseModel):
    dir: str                # assets 資料夾
    full_image: str         # 來源全螢幕截圖檔名（full_NNN.png）
    click_x: int            # 點擊的虛擬桌面絕對座標 X
    click_y: int            # 點擊的虛擬桌面絕對座標 Y
    full_left: int = 0      # 全螢幕截圖對應的虛擬桌面原點 X（可能是負值）
    full_top: int = 0       # 全螢幕截圖對應的虛擬桌面原點 Y
    # 使用者選的裁切區域（虛擬桌面絕對座標系）
    crop_left: int
    crop_top: int
    crop_width: int
    crop_height: int
    save_as: str            # 輸出檔名（例如 img_003_manual.png）


@router.get("/screen/snapshot")
async def get_screen_snapshot():
    """即時抓「整個虛擬桌面」一張 PNG，回 base64。視覺驗證節點的「螢幕區域拉選器」用。

    回傳：
      origin_x / origin_y：虛擬桌面左上角的絕對座標（多螢幕配置可能是負值）
      width / height：截圖像素尺寸
      image_b64：PNG base64（前端直接塞進 <img src="data:image/png;base64,..."/>）

    座標系跟 computer_use 一致：使用者拉出的矩形 [l, t, w, h] 都用「虛擬桌面絕對座標」。"""
    try:
        import base64
        import mss as _mss
        from mss.tools import to_png as _to_png
        with _mss.mss() as sct:
            mon = sct.monitors[0]   # 虛擬桌面全景（含所有實體螢幕聯集）
            shot = sct.grab(mon)
            # to_png(data, size, output=None) → 直接回 PNG bytes（output=path 才寫檔）
            png_bytes = _to_png(shot.rgb, shot.size)
        return {
            "origin_x": int(mon["left"]),
            "origin_y": int(mon["top"]),
            "width": int(mon["width"]),
            "height": int(mon["height"]),
            "image_b64": base64.b64encode(png_bytes).decode(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"螢幕擷取失敗：{e}")


@router.get("/computer-use/monitors")
async def get_computer_use_monitors():
    """列出實體螢幕的幾何（虛擬桌面絕對座標）。
    前端錨點編輯器用這個做「只看單螢幕」的切換 — 多螢幕時整張 full_*.png 被 fit 到
    viewport 會變很小，切單螢幕後畫面可以放大到看清楚。
    回傳 monitors[0] 為虛擬桌面全景、monitors[1..N] 為每台實體螢幕。"""
    try:
        import mss as _mss
        with _mss.mss() as sct:
            monitors = [
                {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}
                for m in sct.monitors
            ]
        return {"monitors": monitors}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取 monitor 清單失敗：{e}")


@router.post("/computer-use/assets/crop")
async def crop_anchor_from_full(req: CropRequest):
    """從全螢幕截圖裁出新錨點。
    - 回傳新錨點檔名 + anchor_off_x/y（點擊相對新錨點中心的偏移）+ variance
    - 支援多螢幕負座標（full_left/top 可以是負的）"""
    import cv2
    import numpy as np
    target_dir = _validate_assets_path(req.dir)
    full_path = target_dir / req.full_image
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail=f"全螢幕截圖不存在：{req.full_image}")

    # 讀 full 圖（支援中文路徑 → 走 read_bytes + imdecode）
    try:
        buf = np.frombuffer(full_path.read_bytes(), dtype=np.uint8)
        full_img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取全螢幕截圖失敗：{e}")
    if full_img is None:
        raise HTTPException(status_code=500, detail=f"全螢幕截圖解碼失敗：{req.full_image}")

    H, W = full_img.shape[:2]
    # 絕對座標 → full 圖的相對座標
    rel_left = req.crop_left - req.full_left
    rel_top = req.crop_top - req.full_top
    rel_right = rel_left + req.crop_width
    rel_bottom = rel_top + req.crop_height
    # 邊界 clamp
    rel_left = max(0, min(rel_left, W))
    rel_top = max(0, min(rel_top, H))
    rel_right = max(0, min(rel_right, W))
    rel_bottom = max(0, min(rel_bottom, H))
    if rel_right - rel_left < 20 or rel_bottom - rel_top < 20:
        raise HTTPException(status_code=400,
            detail=f"裁切範圍太小（{rel_right-rel_left}×{rel_bottom-rel_top}，最小 20×20）")

    cropped = full_img[rel_top:rel_bottom, rel_left:rel_right]
    # 點擊位置相對裁切圖的偏移（依絕對座標計算）
    actual_crop_abs_left = rel_left + req.full_left
    actual_crop_abs_top = rel_top + req.full_top
    actual_w = rel_right - rel_left
    actual_h = rel_bottom - rel_top
    click_dx = req.click_x - actual_crop_abs_left
    click_dy = req.click_y - actual_crop_abs_top
    anchor_off_x = click_dx - actual_w // 2
    anchor_off_y = click_dy - actual_h // 2

    # 特徵豐富度（variance）
    try:
        gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
        variance = float(np.var(gray))
    except Exception:
        variance = 0.0

    # 存檔
    save_name = req.save_as
    if not save_name.endswith(".png"):
        save_name += ".png"
    out_path = target_dir / save_name
    try:
        ok, enc = cv2.imencode(".png", cropped)
        if not ok:
            raise HTTPException(status_code=500, detail="imencode 失敗")
        out_path.write_bytes(enc.tobytes())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"寫檔失敗：{e}")

    return {
        "image": save_name,
        "anchor_off_x": anchor_off_x,
        "anchor_off_y": anchor_off_y,
        "width": actual_w,
        "height": actual_h,
        "variance": round(variance, 1),
    }


class SavePngRequest(BaseModel):
    """前端裁切完直接送 base64 PNG 存到 assets_dir、給 VLM 錨點立即截圖用。
    跟 crop_anchor_from_full 不同:那個要先有 full_image 在磁碟、這個直接收 base64。"""
    dir: str                 # assets 資料夾
    name: str                # 檔名(可不含 .png、會自動補)
    png_b64: str             # 純 PNG base64(不含 data: prefix)


@router.post("/computer-use/assets/save-png")
async def save_png_to_assets(req: SavePngRequest):
    """把前端 canvas.toBlob() 出來的 PNG bytes 存進 assets_dir。
    用途:VLM 錨點立即截圖功能 — 使用者按下截圖、瀏覽器內裁切、再回傳裁好的圖。
    跟 crop_anchor_from_full 互補(那個吃磁碟上的 full_image、這個吃 base64)。
    """
    import base64
    target_dir = _validate_assets_path(req.dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    save_name = req.name.strip()
    if not save_name:
        raise HTTPException(status_code=400, detail="name 為空")
    # 過濾路徑符號(防 .. / 等跳出資料夾)
    if "/" in save_name or "\\" in save_name or save_name.startswith("."):
        raise HTTPException(status_code=400, detail=f"name 含非法字元:{save_name!r}")
    if not save_name.lower().endswith(".png"):
        save_name += ".png"

    try:
        # 容忍前端有沒帶 data:image/png;base64, prefix
        b64 = req.png_b64.split(",", 1)[-1] if "," in req.png_b64 else req.png_b64
        data = base64.b64decode(b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"base64 解碼失敗:{e}")

    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise HTTPException(status_code=400, detail="不是有效 PNG bytes")
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"PNG 太大({len(data)} bytes、上限 5MB)")

    out_path = target_dir / save_name
    try:
        out_path.write_bytes(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"寫檔失敗:{e}")

    # 跟 crop 同步回 metadata、讓前端 UI 顯示一致
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            h, w = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            variance = float(np.var(gray))
        else:
            h, w, variance = 0, 0, 0.0
    except Exception:
        h, w, variance = 0, 0, 0.0

    return {
        "image": save_name,
        "width": w,
        "height": h,
        "variance": round(variance, 1),
        "size_bytes": len(data),
    }


class UiaInspectRequest(BaseModel):
    """檢視指定視窗(或 foreground)的 UIA element tree。"""
    window: str = ""             # 視窗 title pattern(支援 wildcard *)、空字串 = 當前 foreground
    max_depth: int = 6           # tree 深度上限(避免某些 app 上千層)
    max_children_per_node: int = 50  # 每節點子元素上限(避免大表格 1 萬列展開)


@router.post("/computer-use/uia/inspect")
async def uia_inspect(req: UiaInspectRequest):
    """檢視 UIA element tree、給 frontend tree picker 用。
    詳見 docs/uia-feature-evaluation.md。
    """
    from engine.uia_executor import inspect_window
    import logging as _log
    result = inspect_window(
        window_pattern=req.window,
        max_depth=req.max_depth,
        max_children_per_node=req.max_children_per_node,
        logger=_log.getLogger("uia_inspect"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "inspect 失敗"))
    return result


class UiaHighlightRequest(BaseModel):
    """在桌面對應位置畫紅框 outline、給 inspector hover 看清楚對應實體 control 用。
    清除用 ttl_ms=0 或呼 /computer-use/uia/highlight/clear。"""
    x: int                       # 螢幕絕對 X(虛擬桌面座標、可負)
    y: int                       # 螢幕絕對 Y
    width: int                   # 邊框寬
    height: int                  # 邊框高
    ttl_ms: int = 1500           # 自動消失時間;0 = 立即清掉


@router.post("/computer-use/uia/highlight")
async def uia_highlight(req: UiaHighlightRequest):
    """在桌面 (x, y, w, h) 位置畫紅色 outline。
    透明 topmost、click 穿透不擋滑鼠。"""
    from engine.cu_highlight_overlay import highlight, clear_highlight
    if req.ttl_ms <= 0:
        clear_highlight()
    else:
        highlight(req.x, req.y, req.width, req.height, req.ttl_ms)
    return {"ok": True}


@router.post("/computer-use/uia/highlight/clear")
async def uia_highlight_clear():
    from engine.cu_highlight_overlay import clear_highlight
    clear_highlight()
    return {"ok": True}


@router.post("/computer-use/uia/picker/start")
async def uia_picker_start():
    """啟動 Live Picker:滑鼠 hover 桌面 → UIA 元素跟隨紅框 + F8 確認 / F9 取消。"""
    from engine.uia_picker import get_picker
    p = get_picker()
    started = p.start()
    return {"ok": True, "started": started, "running": p.is_running}


@router.get("/computer-use/uia/picker/poll")
async def uia_picker_poll():
    """frontend 輪詢:當下 hover element + 是否 confirmed。"""
    from engine.uia_picker import get_picker
    return get_picker().poll()


@router.post("/computer-use/uia/picker/consume")
async def uia_picker_consume():
    """拿完 confirmed 後 reset、避免重複處理。"""
    from engine.uia_picker import get_picker
    el = get_picker().consume_confirmed()
    return {"ok": True, "element": el}


@router.post("/computer-use/uia/picker/stop")
async def uia_picker_stop():
    from engine.uia_picker import get_picker
    was_running = get_picker().stop()
    return {"ok": True, "was_running": was_running}


@router.post("/computer-use/uia/picker/confirm")
async def uia_picker_confirm():
    """frontend 按鈕「確認當前 hover」走這個、不靠 F8 hotkey。"""
    from engine.uia_picker import get_picker
    p = get_picker()
    el = p.confirm_current()
    if not el:
        return {"ok": False, "error": "目前沒 hover 任何元素、移動滑鼠到目標再確認"}
    return {"ok": True, "element": el}


@router.get("/computer-use/uia/windows")
async def uia_list_windows():
    """列當下所有可見的 top-level 視窗、給 frontend 「📋 列出視窗」選單用。

    用 uiautomation 為主(File Explorer / TeamsWebView 等 shell-hosted window
    EnumWindows 抓不到)、win32 EnumWindows 為輔(catch cloaked / 邊角 cases)。
    去重 by name+class、合併兩路結果。
    """
    try:
        import uiautomation as auto
    except ImportError:
        raise HTTPException(status_code=500, detail="uiautomation 未安裝")

    seen: set[tuple[str, str]] = set()
    windows: list[dict] = []

    # Pass 1: uiautomation(看 shell-hosted / 看標準 GUI app)
    try:
        root = auto.GetRootControl()
        for w in root.GetChildren():
            try:
                name = str(w.Name or "").strip()
                cls = str(getattr(w, "ClassName", "") or "")
                rect = w.BoundingRectangle
                rw = int(rect.right - rect.left)
                rh = int(rect.bottom - rect.top)
                if not name and (rw == 0 or rh == 0):
                    continue
                if not name:
                    name = f"(無標題 {cls})"
                key = (name, cls)
                if key in seen:
                    continue
                seen.add(key)
                windows.append({
                    "name": name,
                    "class": cls,
                    "rect": [int(rect.left), int(rect.top), rw, rh],
                    "is_offscreen": bool(getattr(w, "IsOffscreen", False)),
                })
            except Exception:
                continue
    except Exception:
        pass

    # Pass 2: win32 EnumWindows(補 cloaked / hidden ApplicationFrameWindow)
    try:
        import win32gui  # type: ignore
        import ctypes

        def _enum_cb(hwnd, _ignored):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd) or ""
                cls = win32gui.GetClassName(hwnd) or ""
                rect = win32gui.GetWindowRect(hwnd)
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]
                if w < 50 or h < 50:
                    return True
                # 系統殼有名字才補(避免一堆 noise)
                if not title:
                    return True
                key = (title, cls)
                if key in seen:
                    return True
                seen.add(key)
                # 偵測 cloaked
                is_cloaked = False
                try:
                    DWMWA_CLOAKED = 14
                    cloaked = ctypes.c_int(0)
                    res = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                        hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
                    )
                    is_cloaked = (res == 0 and cloaked.value != 0)
                except Exception:
                    pass
                windows.append({
                    "name": title, "class": cls,
                    "rect": [rect[0], rect[1], w, h],
                    "is_offscreen": is_cloaked,
                })
            except Exception:
                pass
            return True

        win32gui.EnumWindows(_enum_cb, None)
    except Exception:
        pass

    # 排序:非 cloaked + 有真正 title 優先
    windows.sort(key=lambda x: (
        x["is_offscreen"],
        x["name"].startswith("(無標題"),
        -len(x["name"]),
    ))
    return {"ok": True, "windows": windows[:80]}


@router.delete("/computer-use/assets")
async def delete_computer_use_assets(dir: str):
    """刪除指定的錨點資料夾（含 PNG、actions.json、meta.json）。

    用於：面板的「清除全部」、刪除節點時的清理。
    路徑限制走 _validate_assets_path —— 這是遞迴刪除，防線不能少。
    """
    import shutil

    target = _validate_assets_path(dir)
    # 多一道：不准刪資料根目錄本身
    if target == OUTPUT_BASE_PATH.resolve() or target == WORKFLOW_DIR.resolve():
        raise HTTPException(status_code=403, detail="拒絕刪除：這是資料根目錄")
    if not target.exists():
        return {"deleted": False, "reason": "資料夾不存在", "path": str(target)}
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"路徑不是資料夾：{target}")
    try:
        shutil.rmtree(target)
        return {"deleted": True, "path": str(target)}
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"刪除失敗：{e}")


