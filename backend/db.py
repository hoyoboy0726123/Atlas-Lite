"""統一 SQLite 資料庫：workflows、runs、webhooks、folder_watches。

DB 路徑：<data 目錄>/atlas_lite.db（見 config.OUTPUT_BASE_PATH）。

相對 Atlas 移除的表：
  recipes      —— LLM 產碼的快取，沒有 LLM 就沒有東西可快取
  mcp_servers  —— MCP 節點已移除
以及 workflows.chat_messages（AI 助手對話歷史）。

排程不在這裡：APScheduler 用自己的 SQLAlchemyJobStore（見 scheduler/manager.py）。
"""
import json
import sqlite3
import threading
import time
import uuid
from typing import Optional

from config import OUTPUT_BASE_PATH

DB_PATH = str(OUTPUT_BASE_PATH / "atlas_lite.db")
_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """每個 thread 一個 connection（SQLite thread-safety）。"""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        # WAL 仍只允許單一 writer。前端高頻輪詢 + 背景執行/排程同時寫時，
        # 沒有 busy_timeout 的話 writer 拿不到鎖會「立即」拋 database is locked
        # → POST 回 500。設 5s 讓短暫的寫鎖競爭自動退讓重試。
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


def init_db():
    """建立所有表格（冪等）。"""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workflows (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL DEFAULT '新工作流',
            yaml       TEXT NOT NULL DEFAULT '',
            canvas     TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            run_id      TEXT PRIMARY KEY,
            workflow_id TEXT REFERENCES workflows(id) ON DELETE SET NULL,
            data        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS webhooks (
            token         TEXT PRIMARY KEY,
            workflow_id   TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
            enabled       INTEGER NOT NULL DEFAULT 1,
            created_at    REAL NOT NULL,
            last_fired_at REAL NOT NULL DEFAULT 0,
            fire_count    INTEGER NOT NULL DEFAULT 0,
            UNIQUE(workflow_id)
        );

        CREATE TABLE IF NOT EXISTS folder_watches (
            id              TEXT PRIMARY KEY,
            workflow_id     TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
            folder_path     TEXT NOT NULL,
            pattern         TEXT NOT NULL DEFAULT '*',
            enabled         INTEGER NOT NULL DEFAULT 1,
            created_at      REAL NOT NULL,
            last_seen_mtime REAL NOT NULL DEFAULT 0,
            trigger_count   INTEGER NOT NULL DEFAULT 0,
            UNIQUE(workflow_id)
        );
    """)
    # 欄位遷移：workflows 加 chat_messages（每個工作流一條 AI 對話歷史）。
    # CREATE TABLE IF NOT EXISTS 不會動既有表，舊 DB 要用 ALTER 補欄位。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(workflows)")}
    if "chat_messages" not in cols:
        conn.execute("ALTER TABLE workflows ADD COLUMN chat_messages TEXT NOT NULL DEFAULT '[]'")
    conn.commit()


# ── Workflow CRUD ────────────────────────────────────────────────────────────

_WF_COLS = "id, name, yaml, canvas, created_at, updated_at"


def _row_to_workflow(row) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "yaml": row[2],
        "canvas": json.loads(row[3]) if row[3] else {"nodes": [], "edges": []},
        "created_at": row[4],
        "updated_at": row[5],
    }


def create_workflow(name: str = "新工作流", canvas: dict = None) -> dict:
    conn = get_conn()
    # 自動避重名：新工作流 → 新工作流(1) → 新工作流(2) …
    existing = {row[0] for row in conn.execute("SELECT name FROM workflows").fetchall()}
    final_name, counter = name, 1
    while final_name in existing:
        final_name = f"{name}({counter})"
        counter += 1

    wf_id = f"wf-{uuid.uuid4().hex[:12]}"
    now = time.time()
    canvas = canvas or {"nodes": [], "edges": []}
    conn.execute(
        "INSERT INTO workflows (id, name, yaml, canvas, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (wf_id, final_name, "", json.dumps(canvas, ensure_ascii=False), now, now),
    )
    conn.commit()
    return {"id": wf_id, "name": final_name, "yaml": "", "canvas": canvas,
            "created_at": now, "updated_at": now}


def get_workflow(wf_id: str) -> Optional[dict]:
    row = get_conn().execute(
        f"SELECT {_WF_COLS} FROM workflows WHERE id=?", (wf_id,)).fetchone()
    return _row_to_workflow(row) if row else None


def list_workflows() -> list[dict]:
    rows = get_conn().execute(
        f"SELECT {_WF_COLS} FROM workflows ORDER BY updated_at DESC").fetchall()
    return [_row_to_workflow(r) for r in rows]


def update_workflow(wf_id: str, patch: dict) -> Optional[dict]:
    conn = get_conn()
    existing = get_workflow(wf_id)
    if not existing:
        return None
    sets, vals = [], []
    if "name" in patch:
        sets.append("name=?"); vals.append(patch["name"])
    if "yaml" in patch:
        sets.append("yaml=?"); vals.append(patch["yaml"])
    if "canvas" in patch:
        sets.append("canvas=?"); vals.append(json.dumps(patch["canvas"], ensure_ascii=False))
    if not sets:
        return existing
    sets.append("updated_at=?"); vals.append(time.time())
    vals.append(wf_id)
    conn.execute(f"UPDATE workflows SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    return get_workflow(wf_id)


def delete_workflow(wf_id: str) -> bool:
    """刪除工作流。webhooks / folder_watches 由 FK CASCADE 自動清；
    runs 保留但解除關聯（歷史紀錄不該因為刪工作流而消失）。"""
    conn = get_conn()
    conn.execute("UPDATE runs SET workflow_id=NULL WHERE workflow_id=?", (wf_id,))
    conn.execute("DELETE FROM workflows WHERE id=?", (wf_id,))
    conn.commit()
    return True


# ── Webhook 觸發器：外部 HTTP POST /webhooks/<token> 觸發工作流 ──────────────

_WEBHOOK_COLS = ("token", "workflow_id", "enabled", "created_at", "last_fired_at", "fire_count")
_WEBHOOK_SEL = ", ".join(_WEBHOOK_COLS)


def _webhook_row(r) -> Optional[dict]:
    # get_conn 沒設 row_factory → fetchone 回 tuple，用欄位名 zip 成 dict
    return dict(zip(_WEBHOOK_COLS, r)) if r else None


def create_webhook(workflow_id: str) -> dict:
    """為 workflow 建立（或重新產生）webhook token。一個 workflow 一個；重生即換 token。"""
    import secrets
    token = secrets.token_urlsafe(24)
    conn = get_conn()
    conn.execute("DELETE FROM webhooks WHERE workflow_id=?", (workflow_id,))
    conn.execute(
        "INSERT INTO webhooks(token, workflow_id, enabled, created_at) VALUES(?,?,1,?)",
        (token, workflow_id, time.time()),
    )
    conn.commit()
    return {"token": token, "workflow_id": workflow_id, "enabled": True}


def get_webhook_by_workflow(workflow_id: str) -> Optional[dict]:
    r = get_conn().execute(
        f"SELECT {_WEBHOOK_SEL} FROM webhooks WHERE workflow_id=?", (workflow_id,)).fetchone()
    return _webhook_row(r)


def get_webhook_by_token(token: str) -> Optional[dict]:
    """只回傳 enabled=1 的（停用 / 不存在 → None，觸發端一律 404）。"""
    r = get_conn().execute(
        f"SELECT {_WEBHOOK_SEL} FROM webhooks WHERE token=? AND enabled=1", (token,)).fetchone()
    return _webhook_row(r)


def mark_webhook_fired(token: str):
    conn = get_conn()
    conn.execute("UPDATE webhooks SET last_fired_at=?, fire_count=fire_count+1 WHERE token=?",
                 (time.time(), token))
    conn.commit()


def disable_webhook(workflow_id: str) -> bool:
    conn = get_conn()
    cur = conn.execute("UPDATE webhooks SET enabled=0 WHERE workflow_id=?", (workflow_id,))
    conn.commit()
    return cur.rowcount > 0


# ── 檔案夾監看觸發器：資料夾出現新檔 → 觸發工作流（輪詢式，免 watchdog）──────

_FWATCH_COLS = ("id", "workflow_id", "folder_path", "pattern", "enabled",
                "created_at", "last_seen_mtime", "trigger_count")
_FWATCH_SEL = ", ".join(_FWATCH_COLS)


def _fwatch_row(r) -> Optional[dict]:
    return dict(zip(_FWATCH_COLS, r)) if r else None


def create_folder_watch(workflow_id: str, folder_path: str, pattern: str = "*") -> dict:
    """為 workflow 建立（或取代）檔案夾監看。一個 workflow 一個。

    last_seen_mtime 初始化為現在 → 只有「建立之後」新增的檔才觸發，
    不會對資料夾裡既有的檔一次全轟。
    """
    row_id = f"fw-{uuid.uuid4().hex[:12]}"
    conn = get_conn()
    conn.execute("DELETE FROM folder_watches WHERE workflow_id=?", (workflow_id,))
    now = time.time()
    conn.execute(
        "INSERT INTO folder_watches(id, workflow_id, folder_path, pattern, enabled, "
        "created_at, last_seen_mtime) VALUES(?,?,?,?,1,?,?)",
        (row_id, workflow_id, folder_path, pattern or "*", now, now),
    )
    conn.commit()
    return get_folder_watch_by_workflow(workflow_id)


def get_folder_watch_by_workflow(workflow_id: str) -> Optional[dict]:
    r = get_conn().execute(
        f"SELECT {_FWATCH_SEL} FROM folder_watches WHERE workflow_id=?", (workflow_id,)).fetchone()
    return _fwatch_row(r)


def list_enabled_folder_watches() -> list[dict]:
    rows = get_conn().execute(
        f"SELECT {_FWATCH_SEL} FROM folder_watches WHERE enabled=1").fetchall()
    return [_fwatch_row(r) for r in rows]


def update_folder_watch_progress(watch_id: str, last_seen_mtime: float, triggered: int):
    conn = get_conn()
    conn.execute(
        "UPDATE folder_watches SET last_seen_mtime=?, trigger_count=trigger_count+? WHERE id=?",
        (last_seen_mtime, triggered, watch_id))
    conn.commit()


def disable_folder_watch(workflow_id: str) -> bool:
    conn = get_conn()
    cur = conn.execute("UPDATE folder_watches SET enabled=0 WHERE workflow_id=?", (workflow_id,))
    conn.commit()
    return cur.rowcount > 0


# ── Run CRUD（engine/store.py 走自己的 SQL，這裡是給 API 層讀原始 dict 用）────

def save_run(run_data: dict, workflow_id: Optional[str] = None):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, workflow_id, data) VALUES (?,?,?)",
        (run_data["run_id"], workflow_id, json.dumps(run_data, ensure_ascii=False)),
    )
    conn.commit()


def load_run(run_id: str) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT data, workflow_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    d = json.loads(row[0])
    d["_workflow_id"] = row[1]
    return d


def list_runs(limit: int = 20, workflow_id: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    if workflow_id:
        rows = conn.execute(
            "SELECT data, workflow_id FROM runs WHERE workflow_id=? ORDER BY rowid DESC LIMIT ?",
            (workflow_id, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT data, workflow_id FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for data, wid in rows:
        d = json.loads(data)
        d["_workflow_id"] = wid
        out.append(d)
    return out


def delete_run(run_id: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
    conn.commit()
    return cur.rowcount > 0


# ── 每工作流的 AI 對話歷史 ───────────────────────────────
# 存在 workflows.chat_messages（JSON 陣列）。跟著工作流走：切工作流就切對話、
# 刪工作流就一起消失，不需要另一張表。

def get_workflow_chat(wf_id: str) -> Optional[list]:
    """回傳對話訊息陣列；workflow 不存在回 None（跟空對話 [] 區分開）。"""
    conn = get_conn()
    row = conn.execute("SELECT chat_messages FROM workflows WHERE id=?", (wf_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0] or "[]")
    except Exception:
        return []


def set_workflow_chat(wf_id: str, messages: list) -> bool:
    """整批覆寫。workflow 不存在回 False。"""
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM workflows WHERE id=?", (wf_id,)).fetchone():
        return False
    # 只收 role + content 合法的訊息 —— 前端的 streaming/toolBlocks 等
    # ephemeral 欄位不落地
    clean = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str):
            continue
        keep = {"role": m["role"], "content": m["content"]}
        if "ts" in m:
            keep["ts"] = m["ts"]
        clean.append(keep)
    conn.execute("UPDATE workflows SET chat_messages=? WHERE id=?",
                 (json.dumps(clean, ensure_ascii=False), wf_id))
    conn.commit()
    return True


def append_workflow_chat(wf_id: str, role: str, content: str) -> Optional[list]:
    """尾端追加一則。回新的完整陣列；workflow 不存在回 None。"""
    if role not in ("user", "assistant"):
        return None
    msgs = get_workflow_chat(wf_id)
    if msgs is None:
        return None
    msgs.append({"role": role, "content": content, "ts": time.time()})
    set_workflow_chat(wf_id, msgs)
    return msgs


def clear_workflow_chat(wf_id: str) -> bool:
    """清空對話（使用者按「清除對話」）。"""
    return set_workflow_chat(wf_id, [])
