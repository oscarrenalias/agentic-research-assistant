from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.domain.models import ChatMessage, EventEntry, RunState, TaskRecord, now_iso

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    input_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stage_status_json TEXT NOT NULL,
    approvals_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, name),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    msg_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    message_type TEXT NOT NULL,
    stage TEXT NOT NULL,
    priority TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    content TEXT NOT NULL,
    task_id TEXT,
    reply_to TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    message TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_run_ts ON messages(run_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_run_ts ON events(run_id, timestamp);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    owner TEXT NOT NULL,
    status TEXT NOT NULL,
    input_ref TEXT NOT NULL,
    output_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tasks_run_stage ON tasks(run_id, stage);
"""


class RunRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "reply_to" not in columns:
            self.conn.execute("ALTER TABLE messages ADD COLUMN reply_to TEXT")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def create_run(self, state: RunState) -> None:
        self.conn.execute(
            """
            INSERT INTO runs (run_id, input_path, created_at, updated_at, stage_status_json, approvals_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                state.run_id,
                state.input_path,
                state.created_at,
                state.updated_at,
                json.dumps(state.stage_status, ensure_ascii=True),
                json.dumps(state.approvals, ensure_ascii=True),
            ),
        )
        self.conn.commit()

    def save_run_status(self, state: RunState) -> None:
        state.updated_at = now_iso()
        self.conn.execute(
            """
            UPDATE runs
            SET updated_at = ?, stage_status_json = ?, approvals_json = ?
            WHERE run_id = ?
            """,
            (
                state.updated_at,
                json.dumps(state.stage_status, ensure_ascii=True),
                json.dumps(state.approvals, ensure_ascii=True),
                state.run_id,
            ),
        )
        self.conn.commit()

    def upsert_artifact(self, run_id: str, name: str, value: object) -> None:
        self.conn.execute(
            """
            INSERT INTO artifacts (run_id, name, value_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id, name)
            DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (run_id, name, json.dumps(value, ensure_ascii=True), now_iso()),
        )
        self.conn.commit()

    def add_event(self, run_id: str, message: str, timestamp: str) -> None:
        self.conn.execute(
            "INSERT INTO events (run_id, timestamp, message) VALUES (?, ?, ?)",
            (run_id, timestamp, message),
        )
        self.conn.commit()

    def add_message(self, run_id: str, message: ChatMessage) -> None:
        self.conn.execute(
            """
            INSERT INTO messages (msg_id, run_id, from_agent, to_agent, message_type, stage, priority, timestamp, content, task_id, reply_to)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.msg_id,
                run_id,
                message.from_agent,
                message.to_agent,
                message.message_type,
                message.stage,
                message.priority,
                message.timestamp,
                message.content,
                message.task_id,
                message.reply_to,
            ),
        )
        self.conn.commit()

    def load_run(self, run_id: str) -> RunState | None:
        run = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            return None

        artifacts_rows = self.conn.execute(
            "SELECT name, value_json FROM artifacts WHERE run_id = ?", (run_id,)
        ).fetchall()
        artifacts: dict[str, object] = {}
        for row in artifacts_rows:
            artifacts[str(row["name"])] = json.loads(str(row["value_json"]))

        msg_rows = self.conn.execute(
            "SELECT * FROM messages WHERE run_id = ? ORDER BY timestamp ASC", (run_id,)
        ).fetchall()
        messages = [ChatMessage.from_row(row) for row in msg_rows]

        event_rows = self.conn.execute(
            "SELECT timestamp, message FROM events WHERE run_id = ? ORDER BY timestamp ASC", (run_id,)
        ).fetchall()
        events = [EventEntry(timestamp=str(row["timestamp"]), message=str(row["message"])) for row in event_rows]

        task_rows = self.conn.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
        ).fetchall()
        tasks = [TaskRecord.from_row(row) for row in task_rows]

        return RunState(
            run_id=str(run["run_id"]),
            input_path=str(run["input_path"]),
            created_at=str(run["created_at"]),
            updated_at=str(run["updated_at"]),
            stage_status={str(k): str(v) for k, v in json.loads(str(run["stage_status_json"])).items()},
            approvals={str(k): bool(v) for k, v in json.loads(str(run["approvals_json"])).items()},
            artifacts=artifacts,
            messages=messages,
            events=events,
            tasks=tasks,
        )

    def upsert_task(self, task: TaskRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO tasks (
                task_id, run_id, stage, owner, status, input_ref, output_json, error,
                created_at, started_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                status = excluded.status,
                output_json = excluded.output_json,
                error = excluded.error,
                started_at = excluded.started_at,
                completed_at = excluded.completed_at
            """,
            (
                task.task_id,
                task.run_id,
                task.stage,
                task.owner,
                task.status,
                task.input_ref,
                (json.dumps(task.output, ensure_ascii=True) if task.output is not None else None),
                task.error,
                task.created_at,
                task.started_at,
                task.completed_at,
            ),
        )
        self.conn.commit()
