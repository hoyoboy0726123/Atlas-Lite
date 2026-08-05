"""Telegram 按鈕回呼處理。

後端啟動時掛成背景 task，持續 poll Telegram 更新。收到 pipe_* 回呼時
呼叫 engine.runner.resume_pipeline 繼續或中止工作流。

相對 Atlas 移除：AI 助手自由對話、/save YAML、遠端建立工作流。
留下的是「人在外面，工作流卡住了要能處理」需要的最小集合。

── 多實例協調 ─────────────────────────────────────────────────────
Telegram Bot API 同一個 token 同時只允許一個 getUpdates long-poll session。
兩個後端同時 poll 會收到 409 Conflict、回呼被亂搶、按鈕按了沒人回。
所以啟動前先搶一個機器級的 PID lock（Atlas / Atlas-Lite 用不同檔名，
兩邊各自用不同 bot token 時互不干擾）。
"""
import asyncio
import html
import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger("telegram_handler")

_LOCK_NAME = "atlas_lite_telegram.lock"
_task: asyncio.Task | None = None
_stop = False


# ── 單一實例鎖 ───────────────────────────────────────────────────────

def _lock_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = base / "atlas_lite"
    d.mkdir(parents=True, exist_ok=True)
    return d / _LOCK_NAME


def _pid_alive(pid: int) -> bool:
    """跨平台檢查 pid 是否真的還在跑（不靠 psutil）。

    Windows 的坑：OpenProcess 對「已結束但 handle 還沒回收」的行程也會成功，
    光靠它會把 stale PID 誤判成 alive、lock 永遠釋不掉。要用
    GetExitCodeProcess 看 exit_code 是不是 STILL_ACTIVE(259)。
    """
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259   # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False
    except Exception:
        return False


def _try_acquire_lock() -> bool:
    """True = 拿到鎖可以 poll；False = 別人還活著在 poll，本實例不 poll。"""
    path = _lock_path()
    try:
        if path.exists():
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
            holder = int(meta.get("pid", 0) or 0)
            if holder and holder != os.getpid() and _pid_alive(holder):
                logger.warning(
                    f"Telegram polling 已被另一個實例持有（pid={holder}）。"
                    f"本實例跳過 polling —— 按鈕會由那個實例處理。"
                    f"要改由本實例處理，請先關掉 pid {holder} 或刪除 {path}")
                return False
        path.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}),
                        encoding="utf-8")
        logger.info(f"Telegram polling lock 取得（pid={os.getpid()}）")
        return True
    except OSError as e:
        logger.warning(f"Telegram lock 操作失敗（忽略、照常 poll）：{e}")
        return True


def _release_lock() -> None:
    path = _lock_path()
    try:
        if path.exists():
            meta = json.loads(path.read_text(encoding="utf-8"))
            if int(meta.get("pid", 0) or 0) == os.getpid():
                path.unlink()
    except Exception:
        pass


def _i_still_hold_lock() -> bool:
    """每輪檢查一次。防的是這個實際踩過的 race：

    A 在跑，B 啟動時 _pid_alive(A) 偶發誤回 False（Windows 對長 uptime 行程
    有時這樣），B 接管 lock 也開始 poll，A 不知道自己被接管 → 兩邊一起 poll
    → 409 Conflict。解法是 A 每輪檢查 lock 還是不是自己的，不是就退出。
    """
    path = _lock_path()
    try:
        if not path.exists():
            return False
        meta = json.loads(path.read_text(encoding="utf-8"))
        return int(meta.get("pid", 0) or 0) == os.getpid()
    except Exception:
        return True   # 讀不到就當還持有，避免暫時性 I/O 錯誤讓 polling 中斷


# ── 授權 ─────────────────────────────────────────────────────────────

def _authorized_chat() -> str:
    from settings import get_telegram_credentials
    return get_telegram_credentials()[1]


def _is_authorized(chat_id) -> bool:
    """只有設定裡那個 chat 能操作。外人 DM 一律忽略。

    這不是形式檢查 —— 按鈕背後是「在使用者的電腦上執行工作流」，
    任何人都能按等於把機器交出去。
    """
    auth = _authorized_chat()
    return bool(auth) and str(chat_id) == str(auth)


def _remote_control_on() -> bool:
    from settings import get_settings
    return bool(get_settings().get("telegram_remote_control", False))


# ── 回呼處理 ─────────────────────────────────────────────────────────

# 按鈕 callback_data 前綴 → resume_pipeline 的 decision
_DECISION_MAP = {
    "pipe_retry": "retry",
    "pipe_skip": "skip",
    "pipe_abort": "abort",
    "pipe_continue": "continue",
    "pipe_redo_prev": "redo_prev",
}


async def _handle_callback(bot, cb) -> None:
    data = cb.data or ""
    chat_id = cb.message.chat.id if cb.message else None
    if not _is_authorized(chat_id):
        await bot.answer_callback_query(cb.id, text="未授權")
        return

    action, _, rest = data.partition(":")
    run_id, _, extra = rest.partition(":")
    if not run_id:
        await bot.answer_callback_query(cb.id, text="按鈕資料不完整")
        return

    from engine.runner import resume_pipeline

    if action in _DECISION_MAP:
        await bot.answer_callback_query(cb.id, text="處理中…")
        msg = await resume_pipeline(run_id, _DECISION_MAP[action])
        await bot.send_message(chat_id=chat_id, text=msg)
        return

    if action == "pipe_install_dep":
        if not extra:
            await bot.answer_callback_query(cb.id, text="缺套件名")
            return
        await bot.answer_callback_query(cb.id, text=f"安裝 {extra}…")
        msg = await resume_pipeline(run_id, "install_dep", hint=extra)
        await bot.send_message(chat_id=chat_id, text=msg)
        return

    if action == "pipe_install_all":
        from engine.store import get_store
        run = get_store().load(run_id)
        pkgs = []
        if run:
            try:
                pkgs = json.loads(run.awaiting_suggestion or "{}").get("packages", [])
            except json.JSONDecodeError:
                pkgs = []
        if not pkgs:
            await bot.answer_callback_query(cb.id, text="找不到待安裝的套件")
            return
        await bot.answer_callback_query(cb.id, text=f"安裝 {len(pkgs)} 個套件…")
        msg = await resume_pipeline(run_id, "install_dep", hint=",".join(pkgs))
        await bot.send_message(chat_id=chat_id, text=msg)
        return

    if action == "pipe_log":
        await bot.answer_callback_query(cb.id)
        await _send_log_tail(bot, chat_id, run_id)
        return

    if action == "pipe_screenshot":
        await bot.answer_callback_query(cb.id, text="截圖中…")
        from engine import notify
        from engine.store import get_store
        run = get_store().load(run_id)
        paths = await asyncio.get_event_loop().run_in_executor(
            None, notify.take_screenshots,
            (run.pipeline_name if run else "screenshot"), "手動截圖")
        if paths:
            await notify.send_photos(chat_id, paths, caption_prefix="📸 目前畫面")
        else:
            await bot.send_message(chat_id=chat_id, text="截圖失敗（看後端 log）")
        return

    if action == "pipe_prev_output":
        await bot.answer_callback_query(cb.id, text="找檔案中…")
        await _send_prev_output(bot, chat_id, run_id)
        return

    if action == "pipe_select_step":
        await bot.answer_callback_query(cb.id)
        await _send_step_picker(bot, chat_id, run_id)
        return

    if action == "pipe_step_output":
        await bot.answer_callback_query(cb.id, text="傳送中…")
        await _send_step_output(bot, chat_id, run_id, extra)
        return

    await bot.answer_callback_query(cb.id, text="未知的按鈕")


async def _send_log_tail(bot, chat_id, run_id: str, lines: int = 60) -> None:
    from engine.logger import find_run_log
    from engine.store import get_store

    run = get_store().load(run_id)
    p = Path(run.log_path) if (run and run.log_path) else None
    if not (p and p.is_file()):
        p = find_run_log(run_id)
    if not p:
        await bot.send_message(chat_id=chat_id, text="找不到 log 檔")
        return
    tail = p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    await bot.send_message(chat_id=chat_id,
                           text=f"<pre>{html.escape(chr(10).join(tail))[-3500:]}</pre>",
                           parse_mode="HTML")


async def _send_prev_output(bot, chat_id, run_id: str) -> None:
    from engine import notify
    from engine.models import PipelineConfig
    from engine.store import get_store

    run = get_store().load(run_id)
    if not run:
        await bot.send_message(chat_id=chat_id, text="找不到這次執行")
        return
    config = PipelineConfig.from_dict(run.config_dict)
    path = notify.find_prev_output(run, config)
    if not path:
        await bot.send_message(chat_id=chat_id, text="上一步沒有可傳的輸出檔")
        return
    ok, msg = await notify.send_file(chat_id, path, caption="📎 上一步輸出")
    if not ok:
        await bot.send_message(chat_id=chat_id, text=f"傳送失敗：{msg}")


async def _send_step_picker(bot, chat_id, run_id: str) -> None:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    from engine.store import get_store

    run = get_store().load(run_id)
    if not run or not run.step_results:
        await bot.send_message(chat_id=chat_id, text="這次執行還沒有任何步驟結果")
        return
    rows = [[InlineKeyboardButton(f"{sr.step_index + 1}. {sr.step_name[:24]}",
                                  callback_data=f"pipe_step_output:{run_id}:{sr.step_index}")]
            for sr in sorted(run.step_results, key=lambda s: s.step_index)
            if sr.actual_output_path]
    if not rows:
        await bot.send_message(chat_id=chat_id, text="沒有任何步驟產出檔案")
        return
    await bot.send_message(chat_id=chat_id, text="要哪一步的輸出？",
                           reply_markup=InlineKeyboardMarkup(rows[:20]))


async def _send_step_output(bot, chat_id, run_id: str, step_index: str) -> None:
    from engine import notify
    from engine.store import get_store

    run = get_store().load(run_id)
    try:
        idx = int(step_index)
    except ValueError:
        await bot.send_message(chat_id=chat_id, text="步驟編號不合法")
        return
    sr = next((s for s in (run.step_results if run else []) if s.step_index == idx), None)
    if not (sr and sr.actual_output_path):
        await bot.send_message(chat_id=chat_id, text="這一步沒有輸出檔")
        return
    ok, msg = await notify.send_file(chat_id, sr.actual_output_path,
                                     caption=f"📂 步驟 {idx + 1}：{sr.step_name}")
    if not ok:
        await bot.send_message(chat_id=chat_id, text=f"傳送失敗：{msg}")


# ── 指令 ─────────────────────────────────────────────────────────────

_HELP = (
    "<b>Atlas-Lite</b>\n\n"
    "/status　最近幾次執行的狀態\n"
    "/menu　　列出工作流（可直接啟動）\n"
    "/help　　這則說明\n\n"
    "工作流暫停時的決策按鈕會自動推播過來，直接點就好。"
)


async def _handle_command(bot, msg) -> None:
    chat_id = msg.chat.id
    if not _is_authorized(chat_id):
        return   # 外人一律靜默忽略，不要回「未授權」洩漏這個 bot 在做什麼
    text = (msg.text or "").strip()
    cmd = text.split()[0].lower().split("@")[0] if text else ""

    if cmd == "/help":
        await bot.send_message(chat_id=chat_id, text=_HELP, parse_mode="HTML")
        return

    if cmd == "/status":
        from api.runs import run_to_dict  # noqa: F401  （確保 store 已初始化）
        from engine.store import get_store
        runs = get_store().list_recent(5)
        if not runs:
            await bot.send_message(chat_id=chat_id, text="還沒有任何執行紀錄")
            return
        icon = {"completed": "✅", "failed": "❌", "aborted": "🛑",
                "awaiting_human": "✋", "running": "▶️"}
        lines = [f"{icon.get(r.status, '❓')} <b>{html.escape(r.pipeline_name)}</b>"
                 f"　{r.current_step + 1}/{len(r.config_dict.get('steps', []))}"
                 f"　<code>{r.run_id}</code>" for r in runs]
        await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
        return

    if cmd == "/menu":
        if not _remote_control_on():
            await bot.send_message(
                chat_id=chat_id,
                text="遠端遙控預設是關的。要用請到設定頁開啟「Telegram 遠端遙控」。\n"
                     "（開了之後，這個對話就能直接在你的電腦上啟動工作流。）")
            return
        await _send_workflow_menu(bot, chat_id)
        return

    if cmd == "/run":
        if not _remote_control_on():
            await bot.send_message(chat_id=chat_id, text="遠端遙控未開啟")
            return
        await _send_workflow_menu(bot, chat_id)
        return


async def _send_workflow_menu(bot, chat_id) -> None:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    import db
    wfs = db.list_workflows()
    if not wfs:
        await bot.send_message(chat_id=chat_id, text="還沒有任何工作流")
        return
    rows = [[InlineKeyboardButton(w["name"][:32], callback_data=f"wf_run:{w['id']}")]
            for w in wfs[:20]]
    await bot.send_message(chat_id=chat_id, text="要跑哪一個？",
                           reply_markup=InlineKeyboardMarkup(rows))


async def _handle_workflow_start(bot, cb) -> None:
    chat_id = cb.message.chat.id if cb.message else None
    if not (_is_authorized(chat_id) and _remote_control_on()):
        await bot.answer_callback_query(cb.id, text="未授權")
        return
    wf_id = (cb.data or "").partition(":")[2]

    import db
    from api.runs import PipelineRunRequest, start_pipeline
    wf = db.get_workflow(wf_id)
    if not wf or not (wf.get("yaml") or "").strip():
        await bot.answer_callback_query(cb.id, text="這個工作流還沒有內容")
        return
    await bot.answer_callback_query(cb.id, text="啟動中…")
    try:
        result = await start_pipeline(PipelineRunRequest(
            yaml_content=wf["yaml"], workflow_id=wf_id))
        await bot.send_message(chat_id=chat_id,
                               text=f"▶️ 已啟動「{wf['name']}」\n<code>{result['run_id']}</code>",
                               parse_mode="HTML")
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"啟動失敗：{e}")


# ── 主迴圈 ───────────────────────────────────────────────────────────

async def _poll_loop() -> None:
    from telegram import Bot
    from telegram.error import Conflict, NetworkError, TimedOut

    from settings import get_telegram_credentials

    token, _ = get_telegram_credentials()
    if not token:
        logger.info("未設定 Telegram bot token，不啟動 polling")
        return
    if not _try_acquire_lock():
        return

    offset = None
    backoff = 1.0
    try:
        async with Bot(token=token) as bot:
            logger.info("Telegram polling 已啟動")
            while not _stop:
                if not _i_still_hold_lock():
                    logger.warning("Telegram lock 已被別的實例接管，本實例停止 polling")
                    return
                try:
                    updates = await bot.get_updates(offset=offset, timeout=25,
                                                    allowed_updates=["callback_query", "message"])
                    backoff = 1.0
                except (TimedOut, NetworkError):
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                except Conflict:
                    logger.error("Telegram 409 Conflict —— 同一個 token 有別的程式在 poll，停止")
                    return
                except Exception as e:
                    logger.error(f"getUpdates 失敗：{e}")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue

                for u in updates:
                    offset = u.update_id + 1
                    try:
                        if u.callback_query:
                            data = u.callback_query.data or ""
                            if data.startswith("wf_run:"):
                                await _handle_workflow_start(bot, u.callback_query)
                            else:
                                await _handle_callback(bot, u.callback_query)
                        elif u.message and (u.message.text or "").startswith("/"):
                            await _handle_command(bot, u.message)
                    except Exception as e:
                        logger.error(f"處理更新 {u.update_id} 失敗：{e}", exc_info=True)
    finally:
        _release_lock()


async def start_polling() -> None:
    global _task, _stop
    _stop = False
    if _task is None or _task.done():
        _task = asyncio.create_task(_poll_loop())


async def stop_polling() -> None:
    global _stop
    _stop = True
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _release_lock()
