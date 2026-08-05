"""檔案系統操作：瀏覽、原生檔案對話框、venv 偵測、開啟輸出資料夾。

全部限制在使用者家目錄或資料目錄內 —— 這些端點是給本機使用的，
但「本機」不代表可以任意讀寫整台機器。
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import OUTPUT_BASE_PATH, WORKFLOW_DIR

router = APIRouter()


@router.get("/fs/browse")
async def fs_browse(path: str = ""):
    home = Path.home()
    target = Path(path).expanduser() if path else home
    try:
        target.resolve().relative_to(home.resolve())
    except ValueError:
        target = home
    if not target.exists() or not target.is_dir():
        target = home

    items = []
    try:
        for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if item.name.startswith('.'):
                continue
            items.append({"name": item.name, "path": str(item), "is_dir": item.is_dir(), "ext": item.suffix.lower() if item.is_file() else ""})
    except PermissionError:
        pass

    parent = str(target.parent) if target != home else None
    return {"path": str(target), "parent": parent, "items": items}


# ── 原生 OS 檔案對話框(本機部署用)─────────────────────────────────────
# 後端與使用者同一台(本機 app)時,開 OS 原生對話框(Windows = 檔案總管、
# Mac = Finder),使用者熟悉。用 subprocess 跑 tkinter(獨立 main thread、
# 不卡 FastAPI event loop);tkinter 不可用 / headless / 遠端 → 回 path=null,
# 前端自動 fallback 到內建瀏覽 modal。
class NativePickRequest(BaseModel):
    mode: str = "open"            # open(選檔) | save(另存新檔) | dir(選資料夾)
    initial_dir: Optional[str] = None
    default_name: Optional[str] = None
    py_only: bool = False         # open 模式預設 .py 優先


_NATIVE_PICK_SCRIPT = r'''
import sys, json
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as e:
    print(json.dumps({"path": None, "error": "tkinter unavailable: %s" % e})); sys.exit(0)
args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
mode = args.get("mode", "open")
kw = {}
if args.get("initial_dir"):
    kw["initialdir"] = args["initial_dir"]
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
    root.lift()
    root.update()
except Exception:
    pass
if mode == "dir":
    p = filedialog.askdirectory(**kw)
elif mode == "save":
    if args.get("default_name"):
        kw["initialfile"] = args["default_name"]
    p = filedialog.asksaveasfilename(**kw)
else:
    if args.get("py_only"):
        kw["filetypes"] = [("Python", "*.py"), ("All files", "*.*")]
    else:
        kw["filetypes"] = [("All files", "*.*"), ("Python", "*.py")]
    p = filedialog.askopenfilename(**kw)
try:
    root.destroy()
except Exception:
    pass
print(json.dumps({"path": p or None}))
'''


@router.post("/fs/native-pick")
async def fs_native_pick(req: NativePickRequest):
    payload = json.dumps({
        "mode": req.mode,
        "initial_dir": req.initial_dir,
        "default_name": req.default_name,
        "py_only": req.py_only,
    })
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _NATIVE_PICK_SCRIPT, payload,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        return {"path": None, "error": "timeout(使用者未在 5 分鐘內選擇)"}
    except Exception as e:
        return {"path": None, "error": f"無法開啟原生對話框:{e}"}
    txt = (out or b"").decode("utf-8", "replace").strip()
    if not txt:
        return {"path": None}
    try:
        return json.loads(txt.splitlines()[-1])
    except Exception:
        return {"path": txt or None}


@router.get("/fs/check-venv")
async def fs_check_venv(dir: str):
    """檢測腳本目錄下是否有可用的 Python 虛擬環境。
    支援兩種常見命名：`venv/`（Windows 慣例）與 `.venv/`（Unix/macOS 慣例），
    回傳第一個找到的 python 可執行檔路徑，讓使用者不用管到底叫哪個名字。"""
    target = Path(dir).expanduser().resolve()
    try:
        target.relative_to(Path.home().resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="只允許在 home 目錄下操作")
    is_win = os.name == "nt"
    venv_subdir = "Scripts" if is_win else "bin"
    py_name = "python.exe" if is_win else "python"
    # 兩種慣例都檢查一次，誰先找到用誰（venv 先，因為 Windows 使用者比較常這樣命名）
    for venv_dir_name in ("venv", ".venv"):
        venv_python = target / venv_dir_name / venv_subdir / py_name
        if venv_python.exists():
            return {
                "has_venv": True,
                "python_path": str(venv_python),
                "venv_dir_name": venv_dir_name,
            }
    return {"has_venv": False, "python_path": None, "venv_dir_name": None}


# ── 開啟輸出資料夾(本機部署用)───────────────────────────────────────
@router.get("/fs/open-output")
async def fs_open_output(name: str = ""):
    """在本機檔案總管開啟某工作流的輸出資料夾(data/workflows/<工作流名稱>/)。
    後端與使用者同機(本機 app)才有意義。找不到該資料夾 → 退回開 data/workflows/ 根。"""
    base = WORKFLOW_DIR.resolve()
    target = base
    existed = False
    if name:
        cand = (base / name).resolve()
        try:
            cand.relative_to(base)   # 防路徑穿越:必須在 data/workflows/ 底下
        except ValueError:
            raise HTTPException(status_code=400, detail="非法的工作流名稱")
        if cand.is_dir():
            target = cand
            existed = True
            # per-run 子夾:產物實際落在 <name>/run_<ts>/。若母夾底下有 run_<ts>/ 子夾,
            # 直接開「最新一次」那夾,而非停在母夾(否則使用者只看到一排 run_ 夾、還要自己點進去)。
            # 與 runner.run_output_name 的「挑最新 run」邏輯對齊。
            try:
                _run_dirs = [d for d in cand.iterdir() if d.is_dir() and d.name.startswith("run_")]
                if _run_dirs:
                    target = max(_run_dirs, key=lambda d: d.stat().st_mtime)
            except Exception:
                pass
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(target))            # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"開啟資料夾失敗:{e}")
    return {"opened": str(target), "existed": existed}


