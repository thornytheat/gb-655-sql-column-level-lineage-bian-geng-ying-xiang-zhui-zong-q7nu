"""SQLite 读写：批次快照、运行清单、隔离行、键值状态。

只放数据访问，不放判定；调用方拿到的是普通字典。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from app import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    schema_id      TEXT PRIMARY KEY,
    version_label  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    encoding       TEXT NOT NULL,
    bom            INTEGER NOT NULL,
    field_count    INTEGER NOT NULL,
    blocked_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    batch_key      TEXT NOT NULL UNIQUE,
    old_schema_id  TEXT NOT NULL,
    new_schema_id  TEXT NOT NULL,
    version_seq    INTEGER NOT NULL,
    input_sha256   TEXT NOT NULL,
    ruleset        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary_json   TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    schema_id     TEXT NOT NULL,
    line          INTEGER,
    table_name    TEXT,
    column_name   TEXT,
    reason        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id    TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    payload_json  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoff_journal (
    journal_id    TEXT PRIMARY KEY,
    handoff_id    TEXT NOT NULL,
    op            TEXT NOT NULL,
    status        TEXT NOT NULL,
    before_json   TEXT NOT NULL,
    after_json    TEXT,
    created_at    TEXT NOT NULL,
    finished_at   TEXT,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_quarantine_schema ON quarantine(schema_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_run ON handoffs(run_id);
CREATE INDEX IF NOT EXISTS idx_handoff_journal_open ON handoff_journal(handoff_id, status);
"""


def db_path() -> Path:
    raw = os.environ.get("LINEAGEGUARD_DB", "").strip()
    path = Path(raw) if raw else config.DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)


def reset() -> None:
    """测试与重建用：清空全部业务表。"""
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        for table in ("snapshots", "runs", "quarantine", "kv",
                      "handoffs", "handoff_journal"):
            conn.execute(f"DELETE FROM {table}")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    if not value:
        return None
    return json.loads(value)


# ---------- 快照 ----------

def save_snapshot(snapshot: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
                   encoding, bom, field_count, blocked_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(schema_id) DO UPDATE SET
                   version_label=excluded.version_label,
                   source_path=excluded.source_path,
                   source_sha256=excluded.source_sha256,
                   encoding=excluded.encoding,
                   bom=excluded.bom,
                   field_count=excluded.field_count,
                   blocked_count=excluded.blocked_count,
                   created_at=excluded.created_at,
                   payload_json=excluded.payload_json""",
            (
                snapshot["schema_id"], snapshot["version_label"], snapshot["source_path"],
                snapshot["source_sha256"], snapshot["encoding"], int(bool(snapshot["bom"])),
                int(snapshot["field_count"]), int(snapshot["blocked_count"]),
                snapshot["created_at"], _dumps(snapshot),
            ),
        )


def load_snapshot(schema_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE schema_id=?", (schema_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_snapshots(limit: int = 20) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, version_label, source_path, source_sha256, encoding, bom,
                      field_count, blocked_count, created_at
               FROM snapshots ORDER BY created_at DESC, schema_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_quarantine(schema_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (schema_id,))
        conn.executemany(
            """INSERT INTO quarantine (schema_id, line, table_name, column_name, reason,
                   blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    schema_id, int(row.get("line") or 0), row.get("table") or "",
                    row.get("column") or "", row["reason"], int(bool(row.get("blocking"))),
                    row.get("detail") or "", row.get("raw") or "",
                )
                for row in rows
            ],
        )


def list_quarantine(schema_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT line, table_name, column_name, reason, blocking, detail, raw
               FROM quarantine WHERE schema_id=? ORDER BY line, reason""",
            (schema_id,),
        ).fetchall()
    return [
        {
            "line": row["line"],
            "table": row["table_name"],
            "column": row["column_name"],
            "reason": row["reason"],
            "blocking": bool(row["blocking"]),
            "detail": row["detail"],
            "raw": row["raw"],
        }
        for row in rows
    ]


# ---------- 运行清单 ----------

def save_run(run: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO runs (run_id, batch_key, old_schema_id, new_schema_id, version_seq,
                   input_sha256, ruleset, created_at, effective_from, effective_to,
                   summary_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run["run_id"], run["batch_key"], run["old_schema_id"], run["new_schema_id"],
                int(run["version_seq"]), run["input_sha256"], run["ruleset"],
                run["created_at"], run["effective_from"], run["effective_to"],
                _dumps(run["summary"]), _dumps(run),
            ),
        )


def find_run_by_key(batch_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (batch_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def run_by_id(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_run() -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq DESC LIMIT 1"
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT summary_json FROM runs ORDER BY version_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["summary_json"]) for row in rows]


def count_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def next_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"]) + 1


def current_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"])


def list_windows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, effective_from, effective_to FROM runs ORDER BY version_seq"
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 键值 ----------

def set_kv(key: str, value) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, _dumps(value)),
        )


def get_kv(key: str, default=None):
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(row["v"]) if row else default


# ---------- 执行交接 ----------
#
# revision 是乐观锁：读到的版本与写入时的版本不一致就拒绝，保证并发撤回 / 领取
# 只有一方生效。journal 是写前日志：先记意图，再改交接单，最后登记完成；
# 进程在任一步中断，恢复时按 revision 判断意图是否已经落地。

def insert_handoff(payload: dict) -> bool:
    """一次性插入；同一 run 只允许一张交接单，重复创建返回 False。"""
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO handoffs
                   (handoff_id, run_id, revision, status, created_at, updated_at,
                    payload_json)
               VALUES (?,?,?,?,?,?,?)""",
            (
                payload["handoff_id"], payload["run_id"], int(payload["revision"]),
                payload["status"], payload["created_at"], payload["updated_at"],
                _dumps(payload),
            ),
        )
        return cursor.rowcount == 1


def get_handoff(handoff_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM handoffs WHERE handoff_id=?", (handoff_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def find_handoff_by_run(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM handoffs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_handoffs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT payload_json FROM handoffs
               ORDER BY created_at DESC, handoff_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["payload_json"]) for row in rows]


def cas_handoff(handoff_id: str, expected_revision: int, payload: dict) -> bool:
    """比较并交换：只有库里的 revision 仍等于 expected 时才写入。"""
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """UPDATE handoffs
                  SET revision=?, status=?, updated_at=?, payload_json=?
                WHERE handoff_id=? AND revision=?""",
            (
                int(payload["revision"]), payload["status"], payload["updated_at"],
                _dumps(payload), handoff_id, int(expected_revision),
            ),
        )
        return cursor.rowcount == 1


def journal_begin(journal_id: str, handoff_id: str, op: str,
                  before: dict, after: dict, created_at: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO handoff_journal
                   (journal_id, handoff_id, op, status, before_json, after_json,
                    created_at, finished_at, note)
               VALUES (?,?,?,?,?,?,?,NULL,NULL)
               ON CONFLICT(journal_id) DO UPDATE SET
                   handoff_id=excluded.handoff_id,
                   op=excluded.op,
                   status=excluded.status,
                   before_json=excluded.before_json,
                   after_json=excluded.after_json,
                   created_at=excluded.created_at,
                   finished_at=NULL,
                   note=NULL""",
            (journal_id, handoff_id, op, "pending", _dumps(before), _dumps(after),
             created_at),
        )


def journal_finish(journal_id: str, status: str, finished_at: str,
                   note: str | None = None) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """UPDATE handoff_journal
                  SET status=?, finished_at=?, note=?
                WHERE journal_id=?""",
            (status, finished_at, note, journal_id),
        )


def get_journal(journal_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM handoff_journal WHERE journal_id=?", (journal_id,)
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["before"] = _loads(item.pop("before_json"))
    item["after"] = _loads(item.pop("after_json"))
    return item


def open_journals(handoff_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM handoff_journal WHERE status='pending'"
    args: tuple = ()
    if handoff_id:
        query += " AND handoff_id=?"
        args = (handoff_id,)
    query += " ORDER BY created_at, journal_id"
    with _LOCK, _connect() as conn:
        rows = conn.execute(query, args).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["before"] = _loads(item.pop("before_json"))
        item["after"] = _loads(item.pop("after_json"))
        out.append(item)
    return out
