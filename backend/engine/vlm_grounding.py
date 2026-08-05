"""地端 GUI 定位模型客戶端（host 直跑版）。

用途：`vlm_mode='grounding'` — 給一句自然語言描述，回螢幕上的精確座標。

## 與 Atlas 版的差異
Atlas 把模型跑在 WSL2 + Docker 沙盒容器裡（那邊本來就有沙盒供 LLM 用）。
Atlas-Lite 不帶 Docker，改成**獨立外掛 + host 直跑**：
  plugins/vlm_grounding/.venv    自己的 Python 環境（torch ~3GB）
  plugins/vlm_grounding/models/  模型權重（8.3GB）
主程式（backend/.venv）完全不需要 torch。

2026-08-06 於 RTX 5090 Laptop / Windows 11 / Python 3.13 實測，
同一套 11 個定位目標，對照容器版：
  fp16  容器 峰值9.24G 誤差4.0px 2.5s → host 峰值9.65G 誤差3.0px 5.1s
  int4  容器 峰值3.31G 誤差4.2px 4.7s → host 峰值3.68G 誤差3.6px 6.0s
精度 host 反而更好；推論慢一倍（成因未查證，可能是 torch 版本或
Windows CUDA 開銷）。對「CV 點不準時的備援」這個定位可以接受。

## 為什麼是獨立行程而不是直接 import
torch + 模型要吃 3-10GB 記憶體，載入 3-8 秒。做成常駐子行程可以：
  - 主程式啟動時完全不碰 torch，沒裝外掛的人零成本
  - 模型只載入一次，之後每次推論 5-6 秒
  - 外掛掛掉不會拖垮主程式（失敗自動退回 CV）
兩邊用檔案交換，不開網路埠。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# 專案根目錄：backend/engine/vlm_grounding.py → 上三層
_ROOT = Path(__file__).resolve().parent.parent.parent
_PLUGIN = _ROOT / "plugins" / "vlm_grounding"
_PLUGIN_PY = _PLUGIN / ".venv" / "Scripts" / "python.exe"   # Windows
if not _PLUGIN_PY.exists():                                  # 非 Windows 備援
    _PLUGIN_PY = _PLUGIN / ".venv" / "bin" / "python"
_SERVER_PY = _PLUGIN / "server.py"
_MODELS = _PLUGIN / "models"

# 交換目錄。host 直跑沒有掛載限制，放 data/ 底下即可。
_IO = Path(os.environ.get("ATLASLITE_VLM_IO") or (_ROOT / "data" / "_vlm_io"))
_REQ, _RESP, _READY = _IO / "_req.json", _IO / "_resp.json", _IO / "_ready"

_LOAD_TIMEOUT = 180.0     # 模型載入上限（實測 3-8s）
_INFER_TIMEOUT = 90.0     # 單次推論上限（實測 5-6s）
# 啟動失敗後的冷卻。沒有的話每一步都要重試一次啟動，20 步的工作流白等 10 秒。
_FAIL_COOLDOWN = 300.0

_proc: Optional[subprocess.Popen] = None
_fail_until: float = 0.0
_fail_why: str = ""


def shared_dir() -> Path:
    _IO.mkdir(parents=True, exist_ok=True)
    return _IO


def _server_alive() -> bool:
    """服務是否真的活著。

    不能只看 _ready 檔 —— 主程式重啟、或上一輪關閉沒清乾淨時，殘留的
    _ready 會讓這裡誤判成「還在跑」，接著每個請求都逾時（Atlas 實測踩過）。
    _proc is None 代表這個行程沒有啟動過它，一律當成沒在跑。
    """
    return _READY.exists() and _proc is not None and _proc.poll() is None


def reset_failure() -> None:
    """手動清冷卻（使用者裝好外掛後不用重啟主程式）。"""
    global _fail_until, _fail_why
    _fail_until, _fail_why = 0.0, ""


def _mark_fail(lg: logging.Logger, why: str) -> tuple[bool, str]:
    global _fail_until, _fail_why
    _fail_until, _fail_why = time.time() + _FAIL_COOLDOWN, why
    lg.warning(f"[vlm_grounding] {why} → {_FAIL_COOLDOWN:.0f}s 內不再重試，"
               f"這段期間所有 grounding 步驟直接走 CV")
    return False, why


def ensure_server(logger: logging.Logger | None = None) -> tuple[bool, str]:
    """確保推論服務在跑。已在跑就直接回 True。"""
    global _proc
    lg = logger or log
    if _server_alive():
        return True, "already running"
    if time.time() < _fail_until:
        return False, f"（冷卻中，前次失敗：{_fail_why}）"

    if not _PLUGIN_PY.exists():
        return _mark_fail(lg, f"GUI 定位外掛未安裝（找不到 {_PLUGIN_PY.name}）")
    if not _SERVER_PY.exists():
        return _mark_fail(lg, f"找不到外掛的 server.py")
    if not _MODELS.is_dir() or not any(_MODELS.rglob("*.safetensors")):
        return _mark_fail(lg, "模型權重未下載（plugins/vlm_grounding/models/ 找不到 .safetensors）")

    shared_dir()
    for f in (_REQ, _RESP, _READY):
        try:
            f.unlink()
        except (FileNotFoundError, OSError):
            pass

    lg.info(f"[vlm_grounding] 啟動推論服務（host，{_PLUGIN_PY.parent.parent.name}）")
    try:
        _proc = subprocess.Popen(
            [str(_PLUGIN_PY), "-u", str(_SERVER_PY), str(_IO), str(_MODELS)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(_PLUGIN),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return _mark_fail(lg, f"啟動失敗：{e.__class__.__name__}: {e}")

    t0 = time.time()
    while time.time() - t0 < _LOAD_TIMEOUT:
        if _READY.exists():
            lg.info(f"[vlm_grounding] 模型就緒（{time.time() - t0:.0f}s）")
            reset_failure()
            return True, "ok"
        if _proc.poll() is not None:
            return _mark_fail(lg, "服務程序意外結束（顯卡記憶體不足？模型損毀？）")
        time.sleep(0.3)
    return _mark_fail(lg, f"模型載入逾時（>{_LOAD_TIMEOUT:.0f}s）")


def shutdown() -> None:
    global _proc
    try:
        shared_dir()
        _REQ.write_text(json.dumps({"cmd": "quit"}), encoding="utf-8")
        time.sleep(0.8)
    except Exception:
        pass
    if _proc is not None and _proc.poll() is None:
        try:
            _proc.terminate()
        except Exception:
            pass
    _proc = None
    # 一定要清 _ready —— 留著會讓下次 _server_alive() 誤判成還在跑
    for f in (_READY, _REQ, _RESP):
        try:
            f.unlink()
        except (FileNotFoundError, OSError):
            pass


def locate(prompt: str, screenshot_path: Path | str, img_w: int, img_h: int,
           logger: logging.Logger | None = None) -> tuple[bool, int, int, str]:
    """問模型「描述的東西在哪」，回 (ok, x, y, reason)。

    x/y 是相對 screenshot 左上角的像素；呼叫端自行加上截圖原點位移。
    """
    lg = logger or log
    ok, why = ensure_server(lg)
    if not ok:
        return False, 0, 0, f"推論服務不可用：{why}"

    try:
        _RESP.unlink()
    except (FileNotFoundError, OSError):
        pass
    try:
        _REQ.write_text(json.dumps(
            {"image": str(screenshot_path), "prompt": prompt},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return False, 0, 0, f"寫入請求失敗：{e}"

    t0 = time.time()
    while time.time() - t0 < _INFER_TIMEOUT:
        if _RESP.exists():
            time.sleep(0.1)
            try:
                r = json.loads(_RESP.read_text(encoding="utf-8"))
            except Exception:
                time.sleep(0.2)
                continue
            if not r.get("ok"):
                return False, 0, 0, r.get("reason") or "模型未回座標"
            # 模型輸出正規化到 [0,1000]，換回像素
            x = int(int(r["nx"]) / 1000 * img_w)
            y = int(int(r["ny"]) / 1000 * img_h)
            if not (0 <= x < img_w and 0 <= y < img_h):
                return False, 0, 0, f"座標 ({x},{y}) 超出截圖範圍 {img_w}x{img_h}"
            lg.info(f"[vlm_grounding] ({r['nx']},{r['ny']})/1000 → ({x},{y}) px"
                    f"（{r.get('elapsed', 0):.1f}s）{r.get('desp', '')[:60]}")
            return True, x, y, r.get("desp") or ""
        if _proc is not None and _proc.poll() is not None:
            return False, 0, 0, "推論服務中途死亡"
        time.sleep(0.12)
    return False, 0, 0, f"推論逾時（>{_INFER_TIMEOUT:.0f}s）"


_STATUS_CACHE: dict = {"ts": 0.0, "data": None}
_STATUS_TTL = 60.0


def capability(force: bool = False) -> dict:
    """回報這台機器能不能用 grounding，不能的話缺什麼。有 60s 快取。

    前端拿它決定「直接定位」按鈕是亮的還是停用 —— 停用時要說得出原因，
    不能讓使用者選了才發現不能用。
    """
    if not force and _STATUS_CACHE["data"] and (time.time() - _STATUS_CACHE["ts"]) < _STATUS_TTL:
        return _STATUS_CACHE["data"]

    venv_ok = _PLUGIN_PY.exists() and _SERVER_PY.exists()
    # 遞迴找 —— 權重可能直接放 models/，也可能包一層資料夾
    # （例：models/Mano-CUA-4B-Thinking-1.1/*.safetensors）。
    # server.py 的 _resolve_model() 兩種都吃，這裡的檢查也要一致。
    model_ok = _MODELS.is_dir() and any(_MODELS.rglob("*.safetensors"))
    gpu_ok, vram = False, 0.0
    if venv_ok:
        try:
            r = subprocess.run(
                [str(_PLUGIN_PY), "-c",
                 "import torch,sys;"
                 "a=torch.cuda.is_available();"
                 "print(a, torch.cuda.get_device_properties(0).total_memory/1e9 if a else 0)"],
                capture_output=True, text=True, timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0 and r.stdout.strip():
                parts = r.stdout.strip().split()
                gpu_ok = parts[0] == "True"
                try:
                    vram = float(parts[1])
                except (IndexError, ValueError):
                    vram = 0.0
        except Exception:
            pass

    # 門檻與 server.py 的自動選精度一致
    enough = vram >= 6.0
    available = venv_ok and model_ok and gpu_ok and enough
    if available:
        reason = ""
    elif not venv_ok:
        reason = "GUI 定位外掛未安裝"
    elif not gpu_ok:
        reason = "偵測不到可用的 NVIDIA GPU（僅支援 NVIDIA / CUDA）"
    elif not enough:
        reason = f"顯卡記憶體不足（{vram:.1f}GB，至少需要 6GB）"
    else:
        reason = "模型權重未下載（8.9GB）"

    data = {
        "available": available,
        "plugin_installed": venv_ok,
        "model_present": model_ok,
        "gpu_ok": gpu_ok,
        "vram_gb": round(vram, 1),
        "precision": ("fp16" if vram >= 12.0 else "int4") if available else "",
        "reason": reason,
        "install_hint": r"執行 plugins\vlm_grounding\setup.bat",
    }
    _STATUS_CACHE["ts"] = time.time()
    _STATUS_CACHE["data"] = data
    return data
