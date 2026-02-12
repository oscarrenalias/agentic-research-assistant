from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

SECTION_HEADER_RE = re.compile(r"^(?P<header>[A-Za-z0-9][A-Za-z0-9 /&()'_-]*):\s*$")
INLINE_FIELD_RE = re.compile(r"^(?P<key>[A-Za-z0-9][A-Za-z0-9 /&()'_-]*):\s*(?P<value>.+)$")
URL_RE = re.compile(r"https?://[^\s)]+")
CITATION_MARKER_RE = re.compile(r"\[S\d+\]")
PUBLISHED_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T\s].*)?$")

STAGES = ["Ingest", "Research", "Outline", "Draft", "Critique", "Revise", "Final"]
REQUIRED_APPROVAL_STAGES = {"Ingest", "Final"}
MESSAGE_TYPES = {"task", "question", "result", "review", "decision", "status"}
PRIORITY_LEVELS = {"low", "normal", "high"}
TASK_RELATED_MESSAGE_TYPES = {"task", "result", "review"}
RUBRIC_DIMENSIONS = [
    "factual_accuracy",
    "evidence_quality",
    "structure_and_coherence",
    "clarity_and_readability",
    "tone_and_audience_fit",
    "originality_and_insight",
]

STAGE_OUTPUT_ARTIFACT = {
    "Ingest": "normalized_task_package",
    "Research": "evidence_pack",
    "Outline": "approved_outline",
    "Draft": "first_draft",
    "Critique": "critique_feedback",
    "Revise": "revised_draft",
    "Final": "final_post",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class NormalizedTaskPackage:
    run_id: str
    created_at: str
    input_path: str
    objective: str
    audience: str
    tone: str
    constraints: list[str] = field(default_factory=list)
    source_candidates: list[str] = field(default_factory=list)
    title: str | None = None
    key_points: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "NormalizedTaskPackage":
        return cls(
            run_id=str(payload["run_id"]),
            created_at=str(payload["created_at"]),
            input_path=str(payload["input_path"]),
            objective=str(payload["objective"]),
            audience=str(payload["audience"]),
            tone=str(payload["tone"]),
            constraints=[str(x) for x in payload.get("constraints", [])],
            source_candidates=[str(x) for x in payload.get("source_candidates", [])],
            title=(str(payload["title"]) if payload.get("title") is not None else None),
            key_points=[str(x) for x in payload.get("key_points", [])],
        )


@dataclass
class ChatMessage:
    msg_id: str
    from_agent: str
    to_agent: str
    message_type: str
    stage: str
    priority: str
    timestamp: str
    content: str
    task_id: str | None = None
    reply_to: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ChatMessage":
        return cls(
            msg_id=str(row["msg_id"]),
            from_agent=str(row["from_agent"]),
            to_agent=str(row["to_agent"]),
            message_type=str(row["message_type"]),
            stage=str(row["stage"]),
            priority=str(row["priority"]),
            timestamp=str(row["timestamp"]),
            content=str(row["content"]),
            task_id=(str(row["task_id"]) if row["task_id"] else None),
            reply_to=(str(row["reply_to"]) if row["reply_to"] else None),
        )


@dataclass
class EventEntry:
    timestamp: str
    message: str


@dataclass
class TaskRecord:
    task_id: str
    run_id: str
    stage: str
    owner: str
    status: str
    input_ref: str
    output: dict[str, object] | None = None
    error: str | None = None
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TaskRecord":
        output_payload = row["output_json"]
        output = json.loads(str(output_payload)) if output_payload else None
        return cls(
            task_id=str(row["task_id"]),
            run_id=str(row["run_id"]),
            stage=str(row["stage"]),
            owner=str(row["owner"]),
            status=str(row["status"]),
            input_ref=str(row["input_ref"]),
            output=output if isinstance(output, dict) else None,
            error=(str(row["error"]) if row["error"] else None),
            created_at=str(row["created_at"]),
            started_at=(str(row["started_at"]) if row["started_at"] else None),
            completed_at=(str(row["completed_at"]) if row["completed_at"] else None),
        )


@dataclass
class RunState:
    run_id: str
    input_path: str
    created_at: str
    updated_at: str
    stage_status: dict[str, str]
    approvals: dict[str, bool]
    artifacts: dict[str, object]
    messages: list[ChatMessage] = field(default_factory=list)
    events: list[EventEntry] = field(default_factory=list)
    tasks: list[TaskRecord] = field(default_factory=list)
