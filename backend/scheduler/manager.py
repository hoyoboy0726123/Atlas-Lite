"""排程管理（APScheduler）。

任務持久化在 data/scheduler.db（APScheduler 自己的 SQLAlchemyJobStore），
重啟後自動恢復觸發器。

相對 Atlas 移除了 add_task / _execute_task —— 那是給 LLM agent 任務用的，
Atlas-Lite 只排工作流。
"""
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from config import OUTPUT_BASE_PATH, SCHEDULER_DB_PATH, TIMEZONE

# 排程用的 YAML 快照存這裡。用檔案而不是把 YAML 塞進 job args 的原因：
# APScheduler 會把 args pickle 進 DB，幾 KB 的 YAML 塞進去會讓 job 表變得很難查。
_YAML_DIR = OUTPUT_BASE_PATH / "scheduled"
_YAML_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class TaskInfo:
    id: str
    name: str
    yaml_path: str
    schedule_type: str      # cron | once
    schedule_expr: str      # cron 表達式 | ISO datetime
    next_run: Optional[str]
    last_run: Optional[str]
    enabled: bool


_scheduler: Optional[AsyncIOScheduler] = None
# 任務的顯示用中繼資料（存記憶體，重啟後由 APScheduler 的 job 重建）
_task_meta: dict[str, TaskInfo] = {}


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{SCHEDULER_DB_PATH}")},
            timezone=TIMEZONE,
            job_defaults={
                "misfire_grace_time": 3600,  # 錯過 1 小時內仍補跑
                "coalesce": True,            # 多次錯過只補跑一次
            },
        )
    return _scheduler


async def _execute_pipeline_task(task_id: str, yaml_path: str, chat_id: int):
    """排程觸發的執行入口。"""
    try:
        import yaml

        from engine.models import PipelineConfig
        from engine.runner import run_pipeline

        with open(yaml_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        raw_dict = raw.get("pipeline", raw)
        workflow_id = raw_dict.get("_workflow_id")

        config_d = PipelineConfig.from_dict(raw_dict).model_dump()
        if workflow_id:
            config_d["_workflow_id"] = workflow_id
        await run_pipeline(config_dict=config_d, chat_id=chat_id)
        if task_id in _task_meta:
            _task_meta[task_id].last_run = datetime.now().isoformat()
    except Exception as e:
        print(f"[排程] 任務 {task_id} 執行失敗：{e}")


def add_pipeline_task(name: str, schedule_type: str = "cron",
                      schedule_expr: str = "0 8 * * *",
                      yaml_path: Optional[str] = None,
                      yaml_content: Optional[str] = None,
                      chat_id: int = 0) -> TaskInfo:
    """新增一個定時執行的工作流。

    schedule_type="cron" → 週期性，schedule_expr 是 cron 表達式
    schedule_type="once" → 單次，schedule_expr 是 ISO datetime 字串
    """
    task_id = str(uuid.uuid4())[:8]
    if yaml_content and not yaml_path:
        safe = re.sub(r"[^\w\-]", "_", name)[:40]
        yaml_path = str(_YAML_DIR / f"{safe}_{task_id}.yaml")
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(yaml_content)
    elif not yaml_path:
        raise ValueError("yaml_path 或 yaml_content 至少要給一個")

    if schedule_type == "once":
        trigger = DateTrigger(run_date=datetime.fromisoformat(schedule_expr),
                              timezone=TIMEZONE)
    else:
        trigger = CronTrigger.from_crontab(schedule_expr, timezone=TIMEZONE)

    scheduler = get_scheduler()
    scheduler.add_job(_execute_pipeline_task, trigger=trigger,
                      args=[task_id, yaml_path, chat_id],
                      id=task_id, name=name, replace_existing=True)
    job = scheduler.get_job(task_id)

    info = TaskInfo(
        id=task_id, name=name, yaml_path=yaml_path,
        schedule_type=schedule_type, schedule_expr=schedule_expr,
        next_run=job.next_run_time.isoformat() if job and job.next_run_time else None,
        last_run=None, enabled=True,
    )
    _task_meta[task_id] = info
    return info


def remove_task(task_id: str) -> bool:
    scheduler = get_scheduler()
    try:
        scheduler.remove_job(task_id)
    except Exception:
        return False
    _task_meta.pop(task_id, None)
    return True


def remove_task_by_name(name: str) -> bool:
    for tid, meta in list(_task_meta.items()):
        if meta.name == name:
            return remove_task(tid)
    return False


def list_tasks() -> list[dict]:
    scheduler = get_scheduler()
    out = []
    for tid, meta in _task_meta.items():
        job = scheduler.get_job(tid)
        meta.next_run = (job.next_run_time.isoformat()
                         if job and job.next_run_time else None)
        out.append(asdict(meta))
    return out


async def start():
    """啟動排程，並從持久化的 job 重建 _task_meta（否則重啟後前端看不到排程）。"""
    sched = get_scheduler()
    if not sched.running:
        sched.start()
    for job in sched.get_jobs():
        if job.id in _task_meta:
            continue
        args = job.args or []
        _task_meta[job.id] = TaskInfo(
            id=job.id, name=job.name,
            yaml_path=args[1] if len(args) > 1 else "",
            schedule_type="cron", schedule_expr=str(job.trigger),
            next_run=job.next_run_time.isoformat() if job.next_run_time else None,
            last_run=None, enabled=True,
        )


async def shutdown():
    sched = get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
