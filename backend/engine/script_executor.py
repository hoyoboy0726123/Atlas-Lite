"""Python 腳本步驟的執行器。

從 Atlas 的 pipeline/executor.py 抽出。原檔 5878 行，裡面 90% 是 LLM 的
skill agent loop（在模組層 `from langchain_groq import ChatGroq`）——
Atlas-Lite 只需要「跑一支腳本」這條路，所以整支抽出來，不 import 任何 LLM。

保留的設計決策（來自原檔註解，都是踩過坑的）：
  - script 節點刻意與後端 venv 脫鉤：主動把 VIRTUAL_ENV/Scripts 移出 PATH，
    讓裸 `python` 落到系統全域 Python。使用者要用特定 venv 請在 batch 寫絕對路徑。
  - kill 走 process tree（psutil），因為 Windows 上 create_subprocess_shell
    會包一層 cmd.exe，只殺父行程會留下孤兒。
  - 多行 `python -c` 改寫成暫存 .py，避免 Windows 命令列跳脫地獄。
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── 子行程登記表 ──────────────────────────────────────────────
# run_id → 該次執行開出去的所有子行程，中止時要能整批 kill。
_proc_lock = threading.Lock()
_running_procs: dict[str, list] = {}
# background_keep=True 的行程：workflow 正常結束時**不**殺（例如刻意留在桌面的 GUI），
# 只有使用者手動中止（force=True）才殺。
_keepalive_procs: dict[str, set] = {}


# ── 命令改寫用的樣式 ───────────────────────────────────────────
# 整條命令 = `<python> -c "<code>"`（允許前後空白、code 可跨行）
_PY_DASH_C_RE = re.compile(r'^\s*(\S.*?)\s+-c\s+(["\'])(.*)\2\s*$', re.DOTALL)
_PY_INTERP_RE = re.compile(r'(?:^|[\\/ "])(?:python3?|py)(?:\.exe)?"?$', re.IGNORECASE)


def _quote_path(path: str) -> str:
    """跨平台為含空格的路徑加引號。"""
    if os.name == "nt":
        return f'"{path}"' if (" " in path or "\t" in path) else path
    import shlex
    return shlex.quote(path)


def _detect_system_python() -> str:
    """找一支**系統全域** Python 給腳本節點用。

    刻意不優先用 sys.executable（那是後端自己的 venv）—— 見檔頭的設計決策：
    腳本節點的依賴不該污染編排器的執行環境，反之亦然。
    要指定特定 interpreter 請設環境變數 ATLASLITE_PYTHON。

    （Atlas 版在這裡還會逐一 probe「有沒有裝 skill agent 需要的套件」，
      Atlas-Lite 沒有 skill agent，所以只挑第一支找得到的。）
    """
    override = os.getenv("ATLASLITE_PYTHON")
    if override and Path(override).exists():
        return override

    is_windows = os.name == "nt"
    names = ("python", "py", "python.exe", "py.exe") if is_windows else ("python3", "python")
    for name in names:
        p = shutil.which(name)
        if p:
            return p
    if not is_windows:
        for p in ("/usr/bin/python3", "/usr/local/bin/python3", "/opt/homebrew/bin/python3"):
            if os.path.exists(p):
                return p
    # 真的找不到才退回 sys.executable（至少跑得起來，只是會用到後端 venv）
    return sys.executable or ("python" if is_windows else "python3")


_SYSTEM_PYTHON = _detect_system_python()


def register_proc(run_id: str, proc, keep: bool = False):
    """註冊一個正在執行的子進程，供 abort 時立即 kill。
    keep=True:background_keep 進程,workflow 正常結束時不 kill(僅 abort/force 才殺)。"""
    with _proc_lock:
        _running_procs.setdefault(run_id, []).append(proc)
        if keep:
            _keepalive_procs.setdefault(run_id, set()).add(proc)


def unregister_proc(run_id: str, proc):
    """反註冊子進程"""
    with _proc_lock:
        if run_id in _running_procs:
            try:
                _running_procs[run_id].remove(proc)
            except ValueError:
                pass
            if not _running_procs[run_id]:
                del _running_procs[run_id]


def kill_run_processes(run_id: str, force: bool = False):
    """立即終止指定 run 的所有子進程,連同 process tree(防 cmd.exe wrapper 殺掉、Python 變孤兒)。
    Windows 經典問題:create_subprocess_shell 透過 cmd.exe 開 Python,proc.kill() 只殺 cmd.exe,
    Python 子進程繼續活著(尤其有 GUI 的時候 GUI window 留著)。用 psutil 走完整 process tree。
    force=False(workflow 正常結束):跳過 background_keep 進程、讓它們留在桌面。
    force=True(手動 abort):全部殺掉、不保留。
    """
    with _proc_lock:
        procs = _running_procs.pop(run_id, [])
        keep = _keepalive_procs.pop(run_id, set())
    if not force and keep:
        survivors = [p for p in procs if p in keep]
        procs = [p for p in procs if p not in keep]
        if survivors:
            try:
                import logging as _logging
                _logging.getLogger("pipeline").info(f"🛡️ kill_run_processes({run_id}):保留 {len(survivors)} 個 background_keep 進程(留在桌面)")
            except Exception:
                pass
    if not procs:
        return
    try:
        import psutil as _psutil
    except ImportError:
        _psutil = None
    killed = 0
    for proc in procs:
        pid = getattr(proc, "pid", None)
        if pid is None:
            continue
        # 用 psutil 找 children + kill 整棵樹
        if _psutil is not None:
            try:
                parent = _psutil.Process(pid)
                children = parent.children(recursive=True)
                for child in children:
                    try:
                        child.kill()
                        killed += 1
                    except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                        pass
                try:
                    parent.kill()
                    killed += 1
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
                continue
            except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                pass
            except Exception:
                pass
        # psutil 不在 / 失敗時的 fallback:只 kill 直接 proc
        try:
            proc.kill()
            killed += 1
        except (ProcessLookupError, OSError):
            pass
    if killed > 0:
        try:
            import logging as _logging
            _logging.getLogger("pipeline").info(f"🧹 kill_run_processes({run_id}):終結 {killed} 個進程(含子樹)")
        except Exception:
            pass


def _script_env() -> dict:
    """腳本節點專用環境（刻意與後端 venv 脫鉤）。

    設計決策（Atlas 2026-06-01 沿用）：腳本節點未指定虛擬環境時，裸 `python`
    要走**系統全域** Python，而非 Atlas-Lite 後端自己的 venv —— 避免使用者腳本
    的依賴污染編排器的執行環境。缺依賴就 loud fail，由使用者改用專案自帶 venv 解決。

    做法：把 active VIRTUAL_ENV 與後端 venv 的 bin 目錄從 PATH 移除，
    再把 `_SYSTEM_PYTHON` 所在目錄插到最前面。
    （後端是用 backend/.venv 的 python 啟動的，所以 sys.executable 的目錄
      就是要排除的那個。）
    """
    env = os.environ.copy()
    venv = env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    paths = env.get("PATH", "").split(os.pathsep)

    drop = []
    if venv:
        drop.append(os.path.join(venv, "Scripts" if os.name == "nt" else "bin"))
    if sys.executable:
        drop.append(os.path.dirname(sys.executable))
    dropn = {os.path.normcase(os.path.normpath(p)) for p in drop if p}
    paths = [p for p in paths if os.path.normcase(os.path.normpath(p)) not in dropn]

    sys_dir = os.path.dirname(_SYSTEM_PYTHON)
    if sys_dir and os.path.isdir(sys_dir):
        norm = os.path.normcase(os.path.normpath(sys_dir))
        paths = [p for p in paths if os.path.normcase(os.path.normpath(p)) != norm]
        paths.insert(0, sys_dir)

    env["PATH"] = os.pathsep.join(paths)
    # 強制 UTF-8，否則 Windows cp950/cp1252 遇到中文 print() 會炸 UnicodeEncodeError
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _maybe_extract_multiline_python_c(command: str):
    """多行 `python -c "..."` 在 Windows cmd.exe 會被換行切斷 → 只剩第一行送進
    cmd、其餘靜默丟掉,結果是 exit 0 卻什麼都沒做(過夜測試 BUG 5)。

    偵測到「整條命令就是一個跨行的 python -c」就把程式碼抽出寫進暫存 .py、
    改成 `<python> "<暫存檔>"`。單行 -c 不受影響、照舊。

    回傳 (改寫後命令, 暫存檔路徑 or None)。暫存檔由 caller 跑完負責刪。
    """
    m = _PY_DASH_C_RE.match(command)
    if not m:
        return command, None
    interpreter, _q, code = m.group(1), m.group(2), m.group(3)
    if "\n" not in code:
        return command, None  # 單行 -c shell 接得住、不動
    if not _PY_INTERP_RE.search(interpreter.strip()):
        return command, None  # 開頭不是 python、避免誤判其他含 -c 的命令
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".py", prefix="step_inline_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(code)
    return f'{interpreter} {_quote_path(path)}', path


@dataclass
class ExecResult:
    """一個步驟執行完的結果。

    （相對 Atlas 版精簡：拿掉 pending_recipe / missing_packages /
      agent_concluded_fail / token_usage / tool_calls —— 那五個欄位都是
      LLM skill agent 專用，純腳本路徑永遠是預設值。）
    """
    exit_code: int
    stdout: str
    stderr: str


async def execute_step(
    command: str,
    timeout: int,
    logger: logging.Logger,
    step_name: str,
    run_id: str = "",
    working_dir: Optional[str] = None,
    background: bool = False,
    ready_after_seconds: int = 0,
    background_keep: bool = False,
) -> ExecResult:
    """
    執行 shell 命令，串流輸出到 logger，回傳完整結果。

    Args:
        command:     shell 命令字串
        timeout:     最大執行秒數
        logger:      file logger（記錄完整輸出）
        step_name:   用於 log 標籤
        run_id:      pipeline run id（用於立即中止追蹤）
        working_dir: 當前工作目錄（會注入 PIPELINE_OUTPUT_DIR）

    Returns:
        ExecResult(exit_code, stdout, stderr)
    """
    # 設計決策(2026-06-01):script 節點**不**把裸 `python` 改寫成 V5 venv interpreter。
    # 裸 `python` 走系統全域(見 _script_env);要用特定 venv 請在 batch 用該 venv 的
    # 絕對路徑(UI「使用虛擬環境」勾選會自動填)。避免污染編排器自己的 venv。
    # （skill / AI 生成 code 仍走 _SKILL_PYTHON,不受此影響。）

    # 多行 `python -c "..."` 在 Windows cmd 會被換行切斷 → 改寫成暫存 .py 執行
    command, _inline_tmp = _maybe_extract_multiline_python_c(command)
    if _inline_tmp:
        logger.info(f"[{step_name}] 偵測到多行 python -c、已改寫成暫存腳本 {_inline_tmp}")

    logger.info(f"[{step_name}] ▶ 開始執行：{command}")

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    # 準備環境變數(script 節點專用:與 V5 venv 脫鉤、裸 python 走系統全域)
    env = _script_env()
    cwd_arg: Optional[str] = None
    if working_dir:
        # 強制將工作目錄注入環境變數,供 stage 系列腳本主動讀取
        env["PIPELINE_OUTPUT_DIR"] = str(Path(working_dir).absolute())
        # 把 subprocess CWD 設成 workflow dir
        # → 一般使用者寫的 Python 工具就算用 open("x.csv") / Path("x.csv").write_text(...)
        # 也會落在 workflow 資料夾、snapshot diff 抓得到、下游 {{ steps.X.output.path }} 自動代入
        cwd_arg = str(Path(working_dir).absolute())

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd_arg,
        )
        if run_id:
            register_proc(run_id, proc, keep=(background and background_keep))

        # ── 背景模式:不等 exit、給 daemon 一段時間 boot up 後直接回 success ──
        if background:
            if ready_after_seconds > 0:
                logger.info(f"[{step_name}] 🚀 背景模式:等 {ready_after_seconds}s 讓 daemon 啟動完成…")
                await asyncio.sleep(ready_after_seconds)
                # 啟動期間若 proc 已 exit 了、應該當作失敗
                if proc.returncode is not None:
                    rc = proc.returncode
                    logger.warning(f"[{step_name}] ⚠ 背景進程在 {ready_after_seconds}s 內已退出(exit={rc}),非預期 daemon 行為")
                    return ExecResult(
                        exit_code=rc if rc is not None else -1,
                        stdout="(背景進程提早退出)",
                        stderr=f"進程在 ready_after_seconds={ready_after_seconds} 內就 exit、應該設成非背景或檢查啟動失敗",
                    )
            else:
                logger.info(f"[{step_name}] 🚀 背景模式啟動、不等 exit、立即下一步")
            # 子程序留著、由 run 結束時統一 kill(register_proc 註冊過了)
            return ExecResult(
                exit_code=0,
                stdout=f"(背景啟動 OK pid={proc.pid})",
                stderr="",
            )

        async def _drain(stream: asyncio.StreamReader, buf: list[str], tag: str):
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                buf.append(line)
                logger.debug(f"[{step_name}][{tag}] {line}")

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _drain(proc.stdout, stdout_lines, "out"),
                    _drain(proc.stderr, stderr_lines, "err"),
                    proc.wait(),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            logger.error(f"[{step_name}] ⏱ 執行超時（>{timeout}s），已強制終止")
            if run_id:
                unregister_proc(run_id, proc)
            return ExecResult(
                exit_code=-1,
                stdout="\n".join(stdout_lines),
                stderr=f"執行超時（>{timeout}s）",
            )

        if run_id:
            unregister_proc(run_id, proc)

        exit_code = proc.returncode if proc.returncode is not None else -99
        level = logging.INFO if exit_code == 0 else logging.WARNING
        logger.log(level, f"[{step_name}] ■ 結束，exit code: {exit_code}")

        return ExecResult(
            exit_code=exit_code,
            stdout="\n".join(stdout_lines),
            stderr="\n".join(stderr_lines),
        )

    except FileNotFoundError as e:
        logger.error(f"[{step_name}] 命令找不到：{e}")
        return ExecResult(exit_code=-2, stdout="", stderr=f"命令找不到：{e}")

    except Exception as e:
        logger.error(f"[{step_name}] 執行異常：{e}")
        return ExecResult(exit_code=-3, stdout="", stderr=str(e))

    finally:
        if _inline_tmp:
            try:
                os.unlink(_inline_tmp)
            except OSError:
                pass


