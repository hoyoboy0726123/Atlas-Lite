"""執行狀態持久化（SQLite）。

每次執行建立一個 PipelineRun 記錄，包含每步的結果與驗證結論，支援暫停後恢復。

相對 Atlas 移除的欄位：token_usage / tool_calls（LLM 用量統計）、
pending_recipes（recipe 快取）、self_heal_count / self_heal_history（AI 自我修復）。
反序列化時會濾掉不認識的鍵，所以 Atlas 寫的舊 run 記錄仍讀得進來。
"""
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime
from typing import Optional

from db import get_conn


@dataclass
class StepResult:
    step_index: int
    step_name: str
    exit_code: int
    stdout_tail: str        # 最後 ~500 字（完整輸出在 log 檔）
    stderr_tail: str        # 最後 ~200 字
    validation_status: str  # "ok" | "warning" | "failed"
    validation_reason: str
    validation_suggestion: str
    retries_used: int = 0
    # 這步在輸出資料夾實際產生 / 修改的主要檔案（絕對路徑）。
    # 用 dir-snapshot 比對 mtime 算出來 —— 沒設 output.path 的節點也能對應到
    # 自己真正寫的那個檔，不會跟別的步驟搶到「資料夾裡最新的檔」。
    actual_output_path: str = ""
    started_at: str = ""
    ended_at: str = ""
    # 這步匯出的變數（UIA / computer_use 的 save_as、腳本寫的 _step_export.json、
    # 以及 .json 輸出被自動攤平的純量欄位）。
    # 後續步驟用 `{{ steps.<name>.output.<key> }}` 引用。
    step_vars: dict = field(default_factory=dict)


@dataclass
class PipelineRun:
    run_id: str
    pipeline_name: str
    config_dict: dict
    current_step: int = 0
    step_results: list = field(default_factory=list)  # list[StepResult]
    status: str = "running"   # running | awaiting_human | completed | failed | aborted
    telegram_chat_id: Optional[int] = None
    log_path: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    ended_at: Optional[str] = None
    workflow_id: Optional[str] = None
    awaiting_type: str = ""       # "" | "failure" | "human_confirm" | "missing_dependency"
    awaiting_message: str = ""    # 人工確認節點的自訂訊息 / 失敗原因
    awaiting_suggestion: str = ""  # 失敗時的解決建議
    # 啟動時傳入的參數（POST /pipeline/run 的 input_params）。
    # render 階段以 `{{ input.<key> }}` 引用。
    input_params: dict = field(default_factory=dict)


_STEP_KEYS = {f.name for f in fields(StepResult)}
_RUN_KEYS = {f.name for f in fields(PipelineRun)}


def _row_to_run(data: str, workflow_id) -> PipelineRun:
    """把 DB 的一列還原成 PipelineRun。

    刻意濾掉不認識的鍵 —— Atlas 寫的 run 記錄含 token_usage、self_heal_count 等
    Atlas-Lite 沒有的欄位，不濾的話 dataclass 建構會 TypeError，整個歷史清單掛掉。
    """
    d = json.loads(data)
    d["step_results"] = [
        StepResult(**{k: v for k, v in s.items() if k in _STEP_KEYS})
        for s in d.get("step_results", [])
    ]
    d["workflow_id"] = workflow_id
    return PipelineRun(**{k: v for k, v in d.items() if k in _RUN_KEYS})


class RunStore:
    def save(self, run: PipelineRun):
        conn = get_conn()
        raw = asdict(run)
        raw["step_results"] = [
            asdict(s) if isinstance(s, StepResult) else s
            for s in run.step_results
        ]
        workflow_id = raw.pop("workflow_id", None)
        conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, workflow_id, data) VALUES (?, ?, ?)",
            (run.run_id, workflow_id, json.dumps(raw, ensure_ascii=False)),
        )
        conn.commit()

    def load(self, run_id: str) -> Optional[PipelineRun]:
        conn = get_conn()
        row = conn.execute(
            "SELECT data, workflow_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return _row_to_run(row[0], row[1]) if row else None

    def list_recent(self, limit: int = 10) -> list[PipelineRun]:
        conn = get_conn()
        rows = conn.execute(
            "SELECT data, workflow_id FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_run(d, w) for d, w in rows]

    def list_by_workflow(self, wf_id: str, limit: int = 10) -> list[PipelineRun]:
        """只取某 workflow 的執行紀錄（新→舊）。給「可用變數」查詢用 ——
        不受全域 list_recent 視窗影響，跨多次執行都抓得到。"""
        conn = get_conn()
        rows = conn.execute(
            "SELECT data, workflow_id FROM runs WHERE workflow_id=? ORDER BY rowid DESC LIMIT ?",
            (wf_id, limit),
        ).fetchall()
        return [_row_to_run(d, w) for d, w in rows]

    def delete(self, run_id: str) -> bool:
        conn = get_conn()
        cursor = conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        conn.commit()
        return cursor.rowcount > 0

    def list_awaiting(self) -> list[PipelineRun]:
        """所有正在等待人為決策的執行。"""
        conn = get_conn()
        rows = conn.execute("SELECT data, workflow_id FROM runs").fetchall()
        out = []
        for data, wid in rows:
            if json.loads(data).get("status") == "awaiting_human":
                out.append(_row_to_run(data, wid))
        return out


_store: Optional[RunStore] = None


def get_store() -> RunStore:
    global _store
    if _store is None:
        _store = RunStore()
    return _store
