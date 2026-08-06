"""工作流執行引擎。

從 Atlas 的 pipeline/runner.py（3853 行）改寫。移除的：skill / subagent /
outlook / web_crawler / mcp / visual_validation 六種節點分支、recipe 快取、
LLM 驗證、AI 自我修復。剩下四種節點：

  script         → engine.script_executor.execute_step
  computer_use   → engine.computer_use.execute_computer_use_step
  condition      → engine.expression（Jinja2，不是 eval、不是 LLM）
  human_confirm  → 暫停 + Telegram 推播，等 resume_pipeline

**四樣看起來像 LLM 週邊、實際是純腳本工作流命脈的東西，一個都不能拆**：
  1. engine.expression —— {{ }} 變數插值與 condition 求值
  2. _step_export.json 協議 —— 腳本把具名變數傳給下游
  3. .json 輸出自動攤平 —— 腳本正常寫 JSON 就能被下游引用
  4. output.json_schema + schema_gate —— 唯一的輸出結構驗證
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import WORKFLOW_DIR
from engine import notify
from engine.checks import ValidationResult, deterministic_validate
from engine.logger import create_run_logger, resume_run_logger
from engine.models import PipelineConfig
from engine.script_executor import execute_step, kill_run_processes
from engine.store import PipelineRun, StepResult, get_store

logger = logging.getLogger("atlas_lite")

# condition 節點的訪問次數上限，防 on_true/on_false 互指造成無限迴圈
MAX_VISITS_PER_STEP = 1000


# ── 中止旗標與執行中的 task（皆為 in-memory）────────────────────────────

_abort_flags: set[str] = set()
_running_tasks: dict[str, asyncio.Task] = {}


def register_task(run_id: str, task: asyncio.Task):
    _running_tasks[run_id] = task


def unregister_task(run_id: str):
    _running_tasks.pop(run_id, None)


def request_abort(run_id: str):
    """標記此次執行需要中止（下一個步驟邊界會生效）。"""
    _abort_flags.add(run_id)


def is_abort_requested(run_id: str) -> bool:
    return run_id in _abort_flags


def clear_abort(run_id: str):
    _abort_flags.discard(run_id)


async def force_abort(run_id: str):
    """立即中止：kill 子行程 → 通知 computer_use → cancel task → 更新狀態。"""
    from engine.computer_use import request_abort as cu_abort

    _abort_flags.add(run_id)
    # 手動中止 = 全部殺掉，包含 background_keep 的
    kill_run_processes(run_id, force=True)
    # computer_use 跑在 executor thread 裡，kill 不到它，要另外通知
    cu_abort(run_id)

    task = _running_tasks.pop(run_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    store = get_store()
    run = store.load(run_id)
    if run and run.status in ("running", "awaiting_human"):
        run.status = "aborted"
        run.ended_at = datetime.now().isoformat()
        store.save(run)
        logger.info(f"⛔ 執行 {run_id} 被立即中止")
        try:
            await notify.notify_final(run, PipelineConfig.from_dict(run.config_dict))
        except Exception:
            pass
    clear_abort(run_id)


# ── 輸出目錄 ─────────────────────────────────────────────────────────

def workflow_output_dir(workflow_name: str) -> Optional[Path]:
    return (WORKFLOW_DIR / workflow_name) if workflow_name else None


def run_output_name(run) -> str:
    """本次執行的實體輸出目錄名 = <工作流名>/run_<時間戳>_<run_id 前綴>。

    每次執行的產物落進各自的子夾，彼此隔離：重跑不覆蓋上一輪、也不會去動
    使用者正開著的舊檔。顯示名（run.pipeline_name）保持乾淨，只有實體目錄帶 run_。

    時間戳從 run.started_at 衍生（建立時就定、存進 DB），所以 resume 同一次執行
    會落回同一個資料夾。加 run_id 尾綴是因為 started_at 只到秒 —— 同一個工作流
    在同一秒被觸發兩次（folder_watch 一次進多檔、webhook 連打）會撞夾、輸出互蓋。
    """
    base = (getattr(run, "pipeline_name", "") or "").strip()
    rid = (getattr(run, "run_id", "") or "").replace("-", "")[:6]
    try:
        ts = datetime.fromisoformat(run.started_at).strftime("%Y%m%d_%H%M%S")
    except (ValueError, TypeError):
        ts = (getattr(run, "run_id", "") or "run")[:8]
    sub = f"run_{ts}_{rid}" if rid else f"run_{ts}"
    return f"{base}/{sub}" if base else sub


# 判斷哪些檔是「真的步驟產出」、哪些是雜訊
_SKIP_PREFIXES = ("screenshot_",)
_SKIP_SUFFIXES = ("_preview.png", "_compressed.jpg")
_SKIP_EXTS = {".log"}
_SKIP_NAMES = {"_step_export.json", "settings.json",
               "atlas_lite.db", "atlas_lite.db-shm", "atlas_lite.db-wal"}


def _is_output_candidate(path: Path) -> bool:
    n = path.name
    return not (n.startswith(_SKIP_PREFIXES)
                or any(n.endswith(s) for s in _SKIP_SUFFIXES)
                or path.suffix.lower() in _SKIP_EXTS
                or n in _SKIP_NAMES)


def _snapshot_dir(workflow_name: str) -> dict:
    """掃輸出資料夾取每個檔的 mtime（給步驟前後比對用）。失敗回空 dict。"""
    out: dict = {}
    wf = workflow_output_dir(workflow_name)
    if not wf or not wf.is_dir():
        return out
    try:
        for f in wf.rglob("*"):
            if f.is_file() and _is_output_candidate(f):
                try:
                    out[str(f.absolute())] = f.stat().st_mtime
                except OSError:
                    pass
    except OSError:
        pass
    return out


# 「像最終產出」的副檔名，多個候選時排前面
_REPORT_EXTS = {".docx", ".pdf", ".xlsx", ".xls", ".pptx", ".md", ".csv",
                ".html", ".htm", ".json", ".txt", ".png", ".jpg", ".jpeg"}


def _diff_pick_main(before: dict, workflow_name: str) -> Optional[str]:
    """比對前後快照，挑出這步的「主要產出」絕對路徑。沒變化回 None。

    1. 先看新增的檔（比修改既有檔更可能是最終產出）
    2. 都沒新增才看被改過的（mtime 變新）
    3. 多個候選時，報告類副檔名優先，再看誰最新
    """
    after = _snapshot_dir(workflow_name)
    new_files = [p for p in after if p not in before]
    candidates = new_files or [p for p in after if p in before and after[p] > before[p]]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (0 if Path(p).suffix.lower() in _REPORT_EXTS else 1,
                                   -after.get(p, 0)))
    return candidates[0]


# ── 對外入口 ─────────────────────────────────────────────────────────

async def run_pipeline(config_dict: dict, chat_id: int = 0,
                       run_id: Optional[str] = None, start_from_step: int = 0) -> str:
    """執行（或恢復）一個工作流。

    這層是薄殼：處理「含 computer_use 節點時自動縮小前景視窗」的 setup/teardown，
    以及未預期例外的兜底。實際邏輯在 _run_inner。

    自動縮小用 reference counting 處理並發：多個工作流同時跑時，第一個縮小、
    最後一個還原，中間的呼叫只是 +1/-1。
    """
    from engine import window_helper

    try:
        from settings import get_settings
        auto_min = bool(get_settings().get("auto_minimize_for_computer_use", False))
    except Exception:
        auto_min = False
    do_minimize = auto_min and window_helper.config_has_computer_use(config_dict)
    if do_minimize:
        window_helper.request_minimize()

    try:
        return await _run_inner(config_dict, chat_id, run_id, start_from_step)
    except Exception as fatal:
        # 兜底：resume 的 run_pipeline 都是 fire-and-forget task，內部的未預期例外
        # 會被 asyncio 吞掉 → 執行永遠卡在 running、不通知也不能決策。
        # （CancelledError 屬 BaseException，abort 仍正常傳播，不會進到這裡。）
        logger.error(f"[run_pipeline] 未預期例外，run_id={run_id}：{fatal}", exc_info=True)
        try:
            store = get_store()
            r = store.load(run_id) if run_id else None
            if r is not None and r.status not in ("completed", "failed", "aborted"):
                r.status = "failed"
                r.ended_at = datetime.now().isoformat()
                store.save(r)
                total = len(PipelineConfig.from_dict(r.config_dict).steps)
                await notify.notify_failure(r, ValidationResult(
                    status="failed", reason=f"未預期錯誤：{fatal}",
                    suggestion="請看後端 log 追根因"), "(工作流)", total)
        except Exception:
            logger.error("[run_pipeline] 兜底標記 failed 也失敗", exc_info=True)
        return run_id or ""
    finally:
        if do_minimize:
            window_helper.request_restore()


async def _run_inner(config_dict: dict, chat_id: int,
                     run_id: Optional[str], start_from_step: int) -> str:
    store = get_store()

    if run_id:
        run = store.load(run_id)
        if not run:
            raise ValueError(f"找不到執行紀錄：{run_id}")
        run.config_dict = config_dict
        run.status = "running"
        run.current_step = start_from_step
        log = resume_run_logger(run.run_id, run.log_path)
        log.info(f"恢復執行，從步驟 {start_from_step + 1} 繼續")
    else:
        config = PipelineConfig.from_dict(config_dict)
        run_id = str(uuid.uuid4())[:12]
        log, log_path = create_run_logger(run_id, config.name)
        run = PipelineRun(
            run_id=run_id, pipeline_name=config.name, config_dict=config_dict,
            telegram_chat_id=chat_id, log_path=log_path,
            workflow_id=config_dict.get("_workflow_id"),
            input_params=config_dict.get("_input_params") or {},
        )
        log.info(f"工作流開始：{config.name}，共 {len(config.steps)} 個步驟")

    config = PipelineConfig.from_dict(run.config_dict)
    # config.name 從這裡開始是「本次執行的實體目錄」，不是顯示名
    config.name = run_output_name(run)
    log.info(f"本次輸出目錄：data/workflows/{config.name}/")
    store.save(run)

    name_to_index = {s.name: i for i, s in enumerate(config.steps)}
    visit_count: dict[int, int] = {}
    prev_step_wd: Optional[str] = None

    proj_out = WORKFLOW_DIR

    def resolve_path(p: str) -> Path:
        """把設定裡的相對路徑解析成絕對路徑。

        ~/xxx 展開家目錄；絕對路徑直接用；`workflows/` 開頭視為相對資料根
        （這條讓錨點圖這種「屬於工作流、不屬於單次執行」的資源指得到穩定位置）；
        其他相對路徑以**本次執行的**輸出資料夾為基準（含 run_<ts>/ 子夾）。

        `ai_output/` 開頭是 Atlas 的寫法，對應到這裡的 `workflows/`。
        """
        pp = Path(p).expanduser()
        if pp.is_absolute():
            return pp
        parts = pp.parts
        if parts and parts[0] == "workflows":
            return proj_out.parent / pp
        if parts and parts[0] == "ai_output":
            return proj_out.joinpath(*parts[1:])
        return proj_out / config.name / pp

    default_wd = str(proj_out / config.name)

    while run.current_step < len(config.steps):
        if is_abort_requested(run.run_id):
            clear_abort(run.run_id)
            unregister_task(run.run_id)
            run.status = "aborted"
            run.ended_at = datetime.now().isoformat()
            store.save(run)
            log.info("使用者中止工作流")
            await notify.notify_final(run, config)
            return run.run_id

        step = config.steps[run.current_step]
        step_num = run.current_step + 1
        total = len(config.steps)
        log.info(f"══ 步驟 {step_num}/{total}：{step.name} ══")

        step_vars: dict = {}
        step_started_at = datetime.now().isoformat()

        def write_result(sr: StepResult):
            """寫入或覆蓋本步的結果。"""
            if len(run.step_results) > run.current_step:
                run.step_results[run.current_step] = sr
            else:
                run.step_results.append(sr)

        async def fail_and_pause(reason: str, suggestion: str, stderr: str = "") -> str:
            """把本步標成失敗、轉人工決策，並回傳 run_id（呼叫端直接 return）。"""
            write_result(StepResult(
                step_index=run.current_step, step_name=step.name,
                exit_code=1, stdout_tail="", stderr_tail=stderr or reason,
                validation_status="failed", validation_reason=reason,
                validation_suggestion=suggestion, retries_used=0,
                started_at=step_started_at, ended_at=datetime.now().isoformat(),
            ))
            run.status = "awaiting_human"
            run.awaiting_type = "failure"
            run.awaiting_message = reason
            run.awaiting_suggestion = suggestion
            store.save(run)
            await notify.notify_failure(
                run, ValidationResult("failed", reason, suggestion), step.name, total)
            unregister_task(run.run_id)
            return run.run_id

        # ── 變數展開：render 本步所有 {{ }} 欄位 ──────────────────────
        # 沒寫 {{ }} 的欄位完全不動（零成本，舊工作流行為不變）。
        # context = 已完成步驟的結果 + run.input_params + os.environ
        try:
            from engine.expression import build_context, render_step
            var_ctx = build_context(step_results=run.step_results,
                                    input_params=run.input_params or {})
            render_step(step, var_ctx)
        except Exception as exc:
            # 放寬到 Exception：Jinja2 執行期的 TypeError / ValueError 也要接住，
            # 不然這個協程會靜默崩掉、執行永遠卡在 running。
            log.error(f"[{step.name}] 變數展開失敗：{exc}")
            return await fail_and_pause(
                f"變數展開失敗：{exc}",
                "檢查 {{ }} 內引用的變數是否存在（上游步驟有沒有 export 出來），以及語法是否正確。",
                str(exc))

        # ── condition 節點：純求值 + 跳轉，不執行任何命令 ────────────
        if step.condition:
            from engine.expression import eval_condition, eval_value, ExpressionError

            visit_count[run.current_step] = visit_count.get(run.current_step, 0) + 1
            if visit_count[run.current_step] > MAX_VISITS_PER_STEP:
                log.error(f"[{step.name}] 訪問超過 {MAX_VISITS_PER_STEP} 次，判定無限迴圈，中止")
                write_result(StepResult(
                    step_index=run.current_step, step_name=step.name, exit_code=1,
                    stdout_tail="", stderr_tail=f"訪問超過 {MAX_VISITS_PER_STEP} 次",
                    validation_status="failed",
                    validation_reason=f"condition 節點被訪問超過 {MAX_VISITS_PER_STEP} 次",
                    validation_suggestion="檢查 on_true / on_false / cases 是否互指造成循環",
                    retries_used=0, started_at=step_started_at,
                    ended_at=datetime.now().isoformat(),
                ))
                run.status = "failed"
                run.ended_at = datetime.now().isoformat()
                store.save(run)
                await notify.notify_final(run, config)
                unregister_task(run.run_id)
                return run.run_id

            try:
                if step.switch:
                    value = eval_value(step.switch, var_ctx)
                    # cases 的 key 統一轉字串：YAML 裸數字（cases: {200: ok}）會被
                    # parse 成 int，跟字串化的求值結果對不上 → 永遠走 default。
                    cases = {str(k): v for k, v in (step.cases or {}).items()}
                    target = cases.get(str(value), "") or step.default
                    decision = f"switch 求值 = {value!r} → 跳到 {target or '(結束)'}"
                elif step.expression:
                    cond = eval_condition(step.expression, var_ctx)
                    target = step.on_true if cond else step.on_false
                    decision = f"IF 求值 = {cond} → 跳到 {target or '(結束)'}"
                else:
                    raise ExpressionError("condition 節點要填 expression（IF）或 switch（Switch）")
            except Exception as exc:
                log.error(f"[{step.name}] condition 求值失敗：{exc}")
                return await fail_and_pause(
                    f"condition 求值失敗：{exc}",
                    "檢查 expression / switch 的語法與引用的變數是否存在。"
                    "Jinja2 判斷「包含」用 \"'關鍵字' in 變數\"（不是 .contains()）；"
                    "字串相等用 ==；list / dict 取值用 []。",
                    str(exc))

            log.info(f"[{step.name}] 🔀 {decision}")
            sr = StepResult(
                step_index=run.current_step, step_name=step.name, exit_code=0,
                stdout_tail=decision, stderr_tail="", validation_status="ok",
                validation_reason="condition 求值成功", validation_suggestion="",
                retries_used=0, started_at=step_started_at,
                ended_at=datetime.now().isoformat(),
            )
            write_result(sr)

            if not target or target in ("end", "__end__"):
                log.info(f"[{step.name}] 跳轉目標為「{target or '空'}」→ 結束流程")
                run.current_step = len(config.steps)
            elif target not in name_to_index:
                sr.validation_status = "failed"
                sr.validation_reason = f"跳轉目標「{target}」不存在於這個工作流"
                sr.stderr_tail = sr.validation_reason
                log.error(f"[{step.name}] {sr.validation_reason}")
                return await fail_and_pause(
                    sr.validation_reason,
                    "把 on_true / on_false / cases / default 的目標改成實際存在的步驟名稱，"
                    "或補上缺的那個步驟。",
                    sr.validation_reason)
            else:
                run.current_step = name_to_index[target]
            store.save(run)
            continue

        # 步驟開始前先掃一次輸出資料夾，結束後比 mtime 找出這步寫了什麼
        snapshot_before = _snapshot_dir(config.name)

        # ── human_confirm 節點：暫停等人 ────────────────────────────
        if step.human_confirm:
            log.info(f"[{step.name}] ✋ 人工確認節點，暫停等待確認")

            prev_summary = ""
            if run.step_results:
                prev = run.step_results[-1]
                icon = {"ok": "✅", "failed": "❌"}.get(prev.validation_status, "⚠️")
                prev_summary = (f"前一步驟：{prev.step_name}\n"
                                f"狀態：{icon} {prev.validation_status}\n"
                                f"原因：{prev.validation_reason or '（無）'}\n")
                if prev.stdout_tail:
                    prev_summary += f"輸出摘要：{prev.stdout_tail[-300:]}\n"

            confirm_msg = step.message or "請確認上一步結果是否正確，再繼續執行"

            run.status = "awaiting_human"
            run.awaiting_type = "human_confirm"
            run.awaiting_message = confirm_msg
            # 狀態與 step_result 一次寫完，後面 await Telegram 期間就不再 save。
            # 否則使用者按「繼續」時 resume_pipeline 把狀態改成 running，本協程手上
            # 的 stale run 物件又 save 回 awaiting_human → 同一步被啟動兩次。
            write_result(StepResult(
                step_index=run.current_step, step_name=step.name, exit_code=0,
                stdout_tail="等待人工確認", stderr_tail="", validation_status="ok",
                validation_reason="人工確認節點 — 等待中", validation_suggestion="",
                retries_used=0, started_at=step_started_at,
            ))
            store.save(run)

            if step.notify_telegram:
                text = (f"✋ <b>等待確認</b>\n\n"
                        f"📋 {run.pipeline_name}\n"
                        f"📍 步驟 {step_num}/{total}：<b>{step.name}</b>\n\n")
                if prev_summary:
                    text += f"{prev_summary}\n"
                text += f"💬 {confirm_msg}\n\n請選擇："
                await notify.send(run.telegram_chat_id, text,
                                  notify.confirm_keyboard(run.run_id, screenshot=step.screenshot))

                if step.send_prev_output:
                    prev_file = notify.find_prev_output(run, config)
                    if prev_file:
                        ok, msg = await notify.send_file(
                            run.telegram_chat_id, prev_file, caption="📎 上一步輸出")
                        log.info(f"[{step.name}] 自動傳上一步輸出："
                                 f"{'✓ ' + msg if ok else '未成功（不廣播到 TG）：' + msg}")
                    else:
                        log.info(f"[{step.name}] send_prev_output 開啟，但找不到上一步的輸出檔")

                if step.screenshot:
                    try:
                        paths = notify.take_screenshots(config.name, step.name)
                        if paths:
                            await notify.send_photos(run.telegram_chat_id, paths,
                                                     caption_prefix=f"📸 {step.name}")
                    except Exception as e:
                        log.warning(f"[{step.name}] 自動截圖傳送失敗：{e}")

            # 超時自動行動。預設 wait = 永遠等（忽略 step.timeout，這是刻意的）
            hc_timeout = int(getattr(step, "timeout", 0) or 0)
            hc_action = (step.hc_on_timeout or "wait").lower()
            if hc_timeout > 0 and hc_action in ("pass", "reject", "abort"):
                _spawn_hc_timeout_watcher(run.run_id, hc_timeout, hc_action, step.name, config, log)
                log.info(f"[{step.name}] ⏰ 已設定 {hc_timeout}s 超時 → {hc_action}")

            unregister_task(run.run_id)
            return run.run_id  # 暫停，等 resume_pipeline

        # ── script / computer_use：決定工作目錄 ──────────────────────
        wd = step.working_dir
        if not wd and step.output and step.output.path:
            wd = str(resolve_path(step.output.path).parent)
        if not wd and prev_step_wd:
            wd = prev_step_wd
            log.info(f"[{step.name}] working_dir 沿用前一步：{wd}")
        if not wd:
            wd = default_wd
        prev_step_wd = wd
        Path(wd).mkdir(parents=True, exist_ok=True)

        retries_used = 0
        while True:
            if step.computer_use:
                exec_result = await _run_computer_use(step, run, resolve_path, log)
                # UIA / computer_use 透過 save_as 累積的變數，下游可用
                # {{ steps.<名>.output.<key> }} 引用
                step_vars.update(getattr(exec_result, "step_variables", None) or {})
            else:
                log.debug(f"[{step.name}] 命令（{len(step.batch)} 字元）：{step.batch[:500]}")
                exec_result = await execute_step(
                    command=step.batch, timeout=step.timeout, logger=log,
                    step_name=step.name, run_id=run.run_id, working_dir=wd,
                    background=step.background,
                    ready_after_seconds=step.ready_after_seconds,
                    background_keep=step.background_keep,
                )

            # ── 算這步實際寫了什麼檔 ────────────────────────────────
            # 就算使用者沒設 output.path，也能靠 snapshot diff 知道產出在哪 ——
            # 人工確認節點要傳「上一步輸出」時全靠這個。
            eff_output: Optional[str] = None
            try:
                if step.output and step.output.path:
                    p = resolve_path(step.output.path)
                    if p.is_file():
                        eff_output = str(p.absolute())
                if not eff_output:
                    eff_output = _diff_pick_main(snapshot_before, config.name)
            except OSError as e:
                log.debug(f"[{step.name}] 算 snapshot diff 失敗（略過）：{e}")

            # ── 驗證 ────────────────────────────────────────────────
            val = _validate(step, exec_result, eff_output, resolve_path, config.name, log)

            # ── 收集這步匯出的變數 ─────────────────────────────────
            _collect_step_vars(step, wd, eff_output, step_vars, log)

            write_result(StepResult(
                step_index=run.current_step, step_name=step.name,
                exit_code=exec_result.exit_code,
                stdout_tail=exec_result.stdout[-500:],
                stderr_tail=exec_result.stderr[-200:],
                validation_status=val.status, validation_reason=val.reason,
                validation_suggestion=val.suggestion, retries_used=retries_used,
                actual_output_path=eff_output or "",
                started_at=step_started_at, ended_at=datetime.now().isoformat(),
                step_vars=dict(step_vars),
            ))
            store.save(run)

            if val.status == "ok":
                log.info(f"步驟 {step_num} ✅ 通過")
                # 決定下一步：預設線性前進，step.next 有設就跳
                nxt = (step.next or "").strip()
                if nxt in ("end", "__end__"):
                    run.current_step = len(config.steps)
                elif nxt and nxt in name_to_index:
                    run.current_step = name_to_index[nxt]
                else:
                    if nxt:
                        log.warning(f"[{step.name}] next='{nxt}' 不存在，改線性前進")
                    run.current_step += 1
                store.save(run)
                break

            if retries_used < step.retry:
                retries_used += 1
                log.warning(f"步驟 {step_num} 失敗，自動重試 {retries_used}/{step.retry}：{val.reason}")
                continue

            # 重試耗盡 → 轉人工決策
            missing = _detect_missing_packages(exec_result.stderr)
            run.status = "awaiting_human"
            if missing:
                import json
                run.awaiting_type = "missing_dependency"
                run.awaiting_message = f"缺少套件：{', '.join(missing)}"
                run.awaiting_suggestion = json.dumps({
                    "packages": missing,
                    "stderr_tail": (exec_result.stderr or "")[-500:],
                    "step_name": step.name,
                }, ensure_ascii=False)
                log.warning(f"步驟 {step_num} 缺套件 {missing} → 等待使用者授權安裝")
            else:
                run.awaiting_type = "failure"
                run.awaiting_message = val.reason or ""
                run.awaiting_suggestion = val.suggestion or ""
                log.warning(f"步驟 {step_num} 失敗且重試次數耗盡，等待人為決策")
            store.save(run)
            await notify.notify_failure(run, val, step.name, total)
            unregister_task(run.run_id)
            return run.run_id

    # ── 全部完成 ─────────────────────────────────────────────────────
    clear_abort(run.run_id)
    unregister_task(run.run_id)
    # 清掉殘留的背景行程（background=true 留下的 GUI / daemon）。
    # force=False → background_keep 的留在桌面，這是刻意的。
    try:
        kill_run_processes(run.run_id)
    except Exception as e:
        log.warning(f"清理背景行程失敗（忽略）：{e}")
    run.status = "completed"
    run.ended_at = datetime.now().isoformat()
    store.save(run)
    log.info(f"工作流 {run.pipeline_name} 全部完成！")
    await notify.notify_final(run, config)
    return run.run_id


# ── 主迴圈用到的小工具 ───────────────────────────────────────────────

async def _run_computer_use(step, run, resolve_path, log):
    """跑一個桌面自動化節點。回傳帶 step_variables 的結果物件。

    ⚠ 這支曾經多收 config / wd 兩個參數，但函式體從來沒用到，跟呼叫端對不上，
      一執行就 TypeError 把整個 run 打掉 —— 桌面自動化節點等於完全不能用。
      要加參數請先確認函式體真的用得到。
    """
    from engine.computer_use import execute_computer_use_step

    # assets_dir 沒設時預設 <本次輸出夾>/<步驟名>_assets
    assets_abs = str(resolve_path(step.assets_dir) if step.assets_dir
                     else resolve_path(f"{step.name}_assets"))
    # by_alias=True：把 else_ 這種為了閃 Python 保留字取的別名還原成 YAML 的 "else"，
    # 讓 execute_action 用 .get("else") 讀得到。
    actions = [a.model_dump(by_alias=True) if hasattr(a, "model_dump") else dict(a)
               for a in (step.actions or [])]
    return await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: execute_computer_use_step(
            actions=actions, assets_dir=assets_abs, logger=log, run_id=run.run_id,
            fail_fast=step.fail_fast,
            cv_threshold=step.cv_threshold,
            cv_search_only_near=step.cv_search_only_near,
            cv_search_radius=step.cv_search_radius,
            cv_trigger_hover=step.cv_trigger_hover,
            cv_hover_wait_ms=step.cv_hover_wait_ms,
            cv_coord_fallback=step.cv_coord_fallback,
            ocr_threshold=step.ocr_threshold,
            ocr_cv_fallback=step.ocr_cv_fallback,
            uia_window=step.uia_window,
        ),
    )


def _validate(step, exec_result, eff_output, resolve_path, workflow_name, log) -> ValidationResult:
    """決定這步成功還是失敗。全部確定性，沒有任何 LLM 參與。"""
    # computer_use：成敗已由動作執行結果決定
    if step.computer_use:
        if exec_result.exit_code == 0:
            return ValidationResult("ok", f"桌面自動化 {exec_result.stdout.count('OK')} 個動作成功")
        return ValidationResult("failed",
                                f"桌面自動化有 {exec_result.exit_code} 個動作失敗",
                                exec_result.stderr)

    # 完成守門：宣告了 output.path、exit_code=0，卻沒產出任何檔 → 判失敗。
    # 抓「腳本以為自己寫了檔但沒寫」「路徑寫錯」這種「狀態完成但沒交付」。
    if (exec_result.exit_code == 0 and step.output and step.output.path and not eff_output):
        try:
            decl = resolve_path(step.output.path)
            decl_ok = decl.exists() and (
                (decl.is_file() and decl.stat().st_size > 0)
                or (decl.is_dir() and any(decl.iterdir())))
        except OSError:
            decl_ok = False
        if not decl_ok:
            log.warning(f"[{step.name}] ⚠ 完成守門：宣告 output.path={step.output.path} 但沒產出任何檔")
            return ValidationResult(
                "failed",
                f"步驟結束時 exit code 是 0，但宣告的產出檔不存在：{step.output.path}",
                "確認腳本真的有寫檔（例外被 try/except 吞掉？路徑打錯？），"
                "或把 output.path 拿掉不要宣告。")

    # JSON Schema 合約：唯一的輸出結構驗證，0 成本、錯誤訊息含具體欄位
    if (exec_result.exit_code == 0 and step.output
            and getattr(step.output, "json_schema", None)
            and (eff_output or step.output.path)):
        try:
            from engine.schema_gate import validate_output_schema
            gate_path = eff_output or str(resolve_path(step.output.path))
            ok, err = validate_output_schema(gate_path, step.output.json_schema)
            if not ok:
                log.warning(f"[{step.name}] ❌ schema 合約未通過：{err.splitlines()[0]}")
                return ValidationResult(
                    "failed", err,
                    "輸出結構不符合 json_schema 合約，請依錯誤逐欄修正"
                    "（欄位名 / 型別要完全一致，且只輸出純 JSON）。")
            log.info(f"[{step.name}] ✅ schema 合約通過")
        except Exception as e:
            log.warning(f"[{step.name}] schema 閘門異常（略過、不擋）：{e}")

    return deterministic_validate(step, exec_result, log, workflow_name=workflow_name)


def _collect_step_vars(step, wd, eff_output, step_vars: dict, log):
    """把這步匯出的具名變數收進 step_vars，供 `{{ steps.<名>.output.<key> }}` 引用。

    兩個來源，都不需要腳本學任何 API：
      1. _step_export.json —— 腳本在自己的 cwd 寫一個扁平 dict，讀完就刪
         （不刪會洩漏到下一步）。這是純腳本工作流傳遞具名值的唯一管道，
         尤其是餵給 condition 節點 —— condition 只吃乾淨的值，stdout 太雜。
      2. .json 輸出檔的純量欄位 —— 腳本正常寫它的 JSON 輸出就好。
    """
    import json

    # 讀「這步的 working_dir」下的檔案 —— 必須跟腳本寫入的位置一致。
    # wd 在有 output.path 時是 output.path 的母夾，不是輸出根目錄。
    export_f = Path(wd) / "_step_export.json"
    try:
        if export_f.is_file():
            exported = json.loads(export_f.read_text(encoding="utf-8"))
            if isinstance(exported, dict):
                for k, v in exported.items():
                    step_vars[str(k)] = v
                log.info(f"[{step.name}] 收到節點 export 變數：{list(exported.keys())}")
            export_f.unlink()
    except Exception as e:
        log.warning(f"[{step.name}] 讀 _step_export.json 失敗（忽略）：{e}")

    try:
        if eff_output and str(eff_output).lower().endswith(".json"):
            oj = Path(eff_output)
            if oj.is_file():
                data = json.loads(oj.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    promoted = []
                    for k, v in data.items():
                        if isinstance(v, (str, int, float, bool)) and str(k) not in step_vars:
                            step_vars[str(k)] = v
                            promoted.append(str(k))
                    if promoted:
                        log.info(f"[{step.name}] 自動開放 JSON 輸出欄位：{promoted}")
    except Exception as e:
        log.warning(f"[{step.name}] 讀 JSON 輸出欄位失敗（忽略）：{e}")


def _detect_missing_packages(stderr: str) -> list[str]:
    """從 stderr 抓 ModuleNotFoundError 的套件名（去重保序）。"""
    if not stderr:
        return []
    import re
    found = re.findall(r"ModuleNotFoundError: No module named '([^']+)'", stderr)
    return list(dict.fromkeys(p.split(".")[0] for p in found))


def _spawn_hc_timeout_watcher(run_id: str, secs: int, action: str,
                              step_name: str, config, log):
    """人工確認超時的看門狗。"""
    async def watcher():
        try:
            await asyncio.sleep(secs)
            store = get_store()
            cur = store.load(run_id)
            # 已被使用者處理、或變成別種等待（例如缺套件）→ 不要打架
            if not cur or cur.status != "awaiting_human" or cur.awaiting_type != "human_confirm":
                return
            log.warning(f"[{step_name}] ⏰ 超時 {secs}s 沒回應 → 自動執行「{action}」")
            if action == "pass":
                await resume_pipeline(run_id, decision="continue", hint="(自動超時通過)")
            elif action == "reject":
                # 用 redo_prev 而不是 retry —— retry 會重跑人工確認節點本身，
                # 又彈同一個確認、超時再彈…無限迴圈。reject 的語意是「駁回、重做被審的那步」。
                await resume_pipeline(run_id, decision="redo_prev", hint="(自動超時駁回)")
            elif action == "abort":
                cur.status = "aborted"
                cur.ended_at = datetime.now().isoformat()
                store.save(cur)
                try:
                    await notify.notify_final(cur, config)
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"[{step_name}] 超時看門狗例外：{e}")

    asyncio.create_task(watcher())


# ── 決策後恢復執行 ───────────────────────────────────────────────────

def _resolve_script_interpreter(step) -> Optional[str]:
    """從腳本節點的命令解析出它實際用的 python 直譯器。

    要裝套件時得裝進「這一步真正執行的直譯器」，而不是後端 venv —— 腳本節點
    刻意與後端 venv 脫鉤（見 script_executor._script_env），裝錯地方等於沒裝。
    非 python 指令（node 等）或判不出來 → None。
    """
    import re
    import shutil

    batch = (step.batch or "").strip()
    if not batch:
        return None
    m = re.match(r'\s*"([^"]+)"|\s*(\S+)', batch)   # 第一個 token（支援引號路徑）
    tok = ((m.group(1) or m.group(2)) if m else "").strip()
    if not tok or not os.path.basename(tok).lower().startswith("python"):
        return None
    if ("/" in tok) or ("\\" in tok):               # 帶路徑（venv 等）→ 直接用
        p = tok if os.path.isabs(tok) else os.path.join(step.working_dir or os.getcwd(), tok)
        return os.path.normpath(p)
    # 裸 python → 用腳本執行環境的 PATH 解析（= 它實際吃到的那支）
    from engine.script_executor import _script_env
    return shutil.which(tok, path=_script_env().get("PATH"))


def _pip_install_into(python_exe: str, pkg: str) -> tuple[bool, str]:
    """用指定直譯器安裝單一套件。只有在使用者按下授權按鈕後才會被呼叫。"""
    import subprocess

    from engine.script_executor import _script_env
    try:
        r = subprocess.run([python_exe, "-m", "pip", "install", pkg],
                           capture_output=True, text=True, timeout=600,
                           env=_script_env())
        if r.returncode == 0:
            return True, (r.stdout or "")[-300:]
        return False, (r.stderr or r.stdout or "")[-500:]
    except subprocess.TimeoutExpired:
        return False, "安裝逾時（超過 10 分鐘）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _restart_from(run, start_from: int, config_dict: Optional[dict] = None):
    """排一個延遲 0.2s 的重啟 task。

    延遲是為了讓 Windows 釋放 SQLite 的寫鎖。wrapper 本身也要 register_task，
    這樣中止落在延遲窗內也取消得到。
    """
    async def delayed():
        await asyncio.sleep(0.2)
        t = asyncio.create_task(run_pipeline(
            config_dict=config_dict if config_dict is not None else run.config_dict,
            chat_id=run.telegram_chat_id, run_id=run.run_id, start_from_step=start_from))
        register_task(run.run_id, t)

    w = asyncio.create_task(delayed())
    register_task(run.run_id, w)


async def resume_pipeline(run_id: str, decision: str, hint: str = "") -> str:
    """使用者做出決策後繼續執行。回傳給使用者看的回應訊息。

    decision: abort | skip | retry | redo_prev | continue | install_dep
    hint:     install_dep 時帶套件名（逗號分隔）
    """
    store = get_store()
    run = store.load(run_id)
    if not run:
        return f"❌ 找不到執行紀錄：{run_id}"
    if run.status != "awaiting_human":
        return f"⚠️ 這次執行目前狀態是 {run.status}，不需要決策"

    config = PipelineConfig.from_dict(run.config_dict)
    step_num = run.current_step + 1
    total = len(config.steps)
    # 附加到原本的 log 檔，讓前端讀到的 log_path 始終指向同一個檔
    log = resume_run_logger(run.run_id, run.log_path)

    def clear_awaiting():
        run.awaiting_type = ""
        run.awaiting_message = ""
        run.awaiting_suggestion = ""
        run.status = "running"

    if decision == "abort":
        run.status = "aborted"
        run.ended_at = datetime.now().isoformat()
        store.save(run)
        log.info("使用者選擇中止")
        await notify.notify_final(run, config)
        return f"🛑 已中止（步驟 {step_num}/{total}）"

    if decision == "skip":
        log.info(f"使用者選擇跳過步驟 {step_num}")
        next_step = run.current_step + 1
        if next_step >= total:
            run.status = "completed"
            run.ended_at = datetime.now().isoformat()
            store.save(run)
            await notify.notify_final(run, config)
            return "⏩ 跳過最後一步，工作流完成"
        clear_awaiting()
        store.save(run)
        _restart_from(run, next_step)
        return f"⏩ 跳過步驟 {step_num}，繼續執行步驟 {step_num + 1}/{total}"

    if decision == "retry":
        log.info(f"使用者選擇重試步驟 {step_num}")
        clear_awaiting()
        store.save(run)
        _restart_from(run, run.current_step)
        return f"🔄 重試步驟 {step_num}/{total}"

    if decision == "redo_prev":
        # 使用者看到當前步驟失敗、判斷是上一步沒做好想回頭重來。
        # 例：第 5 步驗證 PPT 排版失敗 → 重做第 4 步（產 PPT）→ 再推進到第 5 步。
        if run.current_step <= 0:
            return "⚠️ 已經是第一步，沒有上一步可以重做"
        prev_step = run.current_step - 1
        log.info(f"使用者選擇重做上一步 {prev_step + 1}（原本失敗在 {step_num}）")
        # 清掉這兩步的結果，讓它們都重跑
        run.step_results = [sr for sr in run.step_results if sr.step_index < prev_step]
        run.current_step = prev_step
        clear_awaiting()
        store.save(run)
        _restart_from(run, prev_step)
        return f"↩ 重做上一步（{prev_step + 1}/{total}），完成後會再推進到原步驟"

    if decision == "continue":
        log.info(f"使用者確認通過步驟 {step_num}")
        # 按 step_index 找，不按 list 位置 —— condition 跳轉會讓兩者不一致
        cur_sr = next((sr for sr in run.step_results if sr.step_index == run.current_step), None)
        if cur_sr is not None:
            cur_sr.validation_reason = "人工確認 — 已通過"
            cur_sr.stdout_tail = "已確認通過"
        run.awaiting_type = ""
        run.awaiting_message = ""
        next_step = run.current_step + 1
        if next_step >= total:
            try:
                kill_run_processes(run.run_id)
            except Exception as e:
                log.warning(f"清理背景行程失敗（忽略）：{e}")
            run.status = "completed"
            run.ended_at = datetime.now().isoformat()
            store.save(run)
            log.info(f"工作流 {run.pipeline_name} 全部完成！")
            await notify.notify_final(run, config)
            return "✅ 確認通過，工作流全部完成"
        run.status = "running"
        store.save(run)
        _restart_from(run, next_step)
        return f"✅ 確認通過，繼續執行步驟 {next_step + 1}/{total}"

    if decision == "install_dep":
        # 使用者從 missing_dependency 按「允許安裝」。hint 帶套件名（可逗號分隔多個）。
        # ⚠ 這是整個系統唯一會安裝套件的地方，而且一定經過使用者按鈕授權。
        pkgs = [p.strip() for p in (hint or "").split(",") if p.strip()]
        if not pkgs:
            return "⚠️ install_dep 需要在 hint 帶套件名"
        log.info(f"使用者授權安裝套件：{pkgs}")

        failed_step = config.steps[run.current_step] if run.current_step < total else None
        python_exe = _resolve_script_interpreter(failed_step) if failed_step else None
        if not python_exe:
            return ("⚠️ 判斷不出這一步用的是哪個 Python，無法代為安裝。\n"
                    f"請自己在終端機執行：pip install {' '.join(pkgs)}")

        results = [(pkg, *_pip_install_into(python_exe, pkg)) for pkg in pkgs]
        for pkg, ok, msg in results:
            log.info(f"[install_dep] {python_exe} pip install {pkg}：ok={ok}，{str(msg)[:200]}")

        failed = [(p, m) for p, ok, m in results if not ok]
        if failed:
            import json
            names = [p for p, _ in failed]
            manual = (f"請在終端機自己執行：\n"
                      f'  "{python_exe}" -m pip install {" ".join(names)}\n\n'
                      f"（app 內安裝的原始錯誤，供參考）\n"
                      + "\n".join(f"• {p}: {m[:300]}" for p, m in failed))
            run.awaiting_message = f"安裝失敗，請改在終端機安裝：{', '.join(names)}"
            run.awaiting_suggestion = json.dumps({
                "packages": names, "manual_hint": manual, "install_failed": True,
            }, ensure_ascii=False)
            log.warning(f"[install_dep] 安裝未成功 {names} → 手動安裝指引：\n{manual}")
            store.save(run)  # awaiting_type 維持 missing_dependency，使用者還能重試 / 中止
            return f"❌ 安裝失敗（已附終端機安裝指引）：{', '.join(names)}"

        clear_awaiting()
        store.save(run)
        _restart_from(run, run.current_step)
        return f"✅ 已安裝：{', '.join(p for p, _, _ in results)}\n🔄 重試步驟 {step_num}/{total}"

    return f"❓ 未知的決策：{decision}"
