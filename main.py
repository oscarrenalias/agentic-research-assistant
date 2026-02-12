from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, RichLog, Static

SECTION_HEADER_RE = re.compile(r"^(?P<header>[A-Za-z0-9][A-Za-z0-9 /&()'_-]*):\s*$")
INLINE_FIELD_RE = re.compile(r"^(?P<key>[A-Za-z0-9][A-Za-z0-9 /&()'_-]*):\s*(?P<value>.+)$")
URL_RE = re.compile(r"https?://[^\s)]+")

# Load environment variables from .env automatically if present.
load_dotenv()

STAGES = ["Ingest", "Research", "Outline", "Draft", "Critique", "Revise", "Final"]
REQUIRED_APPROVAL_STAGES = {"Ingest", "Final"}

STAGE_OUTPUT_ARTIFACT = {
    "Ingest": "normalized_task_package",
    "Research": "evidence_pack",
    "Outline": "approved_outline",
    "Draft": "first_draft",
    "Critique": "critique_feedback",
    "Revise": "revised_draft",
    "Final": "final_post",
}


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def maybe_url(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value.strip()
    return None


def infer_source_tier(source_ref: str) -> int:
    value = source_ref.lower()
    if any(token in value for token in ["iea", "iaea", "doe", "energy.gov", "oecd", "nea"]):
        return 1
    return 2


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


class ResearchEngine:
    """LangChain-backed source analyzer with deterministic fallback behavior."""

    def __init__(self) -> None:
        self.enabled = False
        self._chain = None
        self._review_chain = None
        self._init_error: str | None = None

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("RESEARCH_MODEL", "gpt-4o-mini").strip()
        if not api_key:
            self._init_error = "OPENAI_API_KEY not set"
            return

        try:
            from langchain_core.output_parsers import StrOutputParser
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_openai import ChatOpenAI

            prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are a research analyst. Return ONLY compact JSON with keys: "
                            "claim (string), evidence_note (string), confidence (float 0-1), "
                            "risk_flags (array of strings). No markdown."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Objective: {objective}\n"
                            "Audience: {audience}\n"
                            "Tone: {tone}\n"
                            "Constraints: {constraints}\n"
                            "Task objective: {task_objective}\n"
                            "Task instructions: {task_instructions}\n"
                            "Source candidate: {source}\n"
                            "Task: extract one evidence-backed claim from this source candidate."
                        ),
                    ),
                ]
            )
            review_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are a research analyst reviewing task instructions. "
                            "Return ONLY compact JSON with keys: decision, message. "
                            "decision must be one of: clear, question."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Task objective: {task_objective}\n"
                            "Task instructions: {task_instructions}\n"
                            "Source candidate: {source}\n"
                            "Decide whether instructions are clear enough to execute."
                        ),
                    ),
                ]
            )
            llm = ChatOpenAI(model=model, temperature=0)
            self._chain = prompt | llm | StrOutputParser()
            self._review_chain = review_prompt | llm | StrOutputParser()
            self.enabled = True
        except Exception as exc:  # noqa: BLE001
            self._init_error = str(exc)

    @property
    def init_error(self) -> str | None:
        return self._init_error

    @staticmethod
    def _parse_llm_json(raw: str) -> dict[str, Any]:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise

    def analyze_source(
        self,
        *,
        source: str,
        objective: str,
        audience: str,
        tone: str,
        constraints: list[str],
        task_objective: str,
        task_instructions: str,
    ) -> dict[str, Any]:
        if not self.enabled or self._chain is None:
            return {
                "claim": f"Potentially relevant source candidate: {source[:140]}",
                "evidence_note": "Fallback mode (no model configured).",
                "confidence": 0.35,
                "risk_flags": ["model_unavailable"],
            }

        try:
            raw = self._chain.invoke(
                {
                    "objective": objective,
                    "audience": audience,
                    "tone": tone,
                    "constraints": "; ".join(constraints),
                    "task_objective": task_objective,
                    "task_instructions": task_instructions,
                    "source": source,
                }
            )
            parsed = self._parse_llm_json(raw)
            return {
                "claim": str(parsed.get("claim", ""))[:500],
                "evidence_note": str(parsed.get("evidence_note", ""))[:600],
                "confidence": float(parsed.get("confidence", 0.5)),
                "risk_flags": [str(x) for x in parsed.get("risk_flags", [])],
            }
        except Exception:  # noqa: BLE001
            return {
                "claim": f"Potentially relevant source candidate: {source[:140]}",
                "evidence_note": "Fallback mode (research inference call failed).",
                "confidence": 0.3,
                "risk_flags": ["model_call_failed"],
            }

    def review_task_instruction(
        self,
        *,
        task_objective: str,
        task_instructions: str,
        source: str,
    ) -> dict[str, str]:
        if len(task_instructions.strip()) < 24:
            return {
                "decision": "question",
                "message": "Can you clarify success criteria and expected output format?",
            }

        if not self.enabled or self._review_chain is None:
            return {"decision": "clear", "message": "Instructions look clear; I can proceed."}

        try:
            raw = self._review_chain.invoke(
                {
                    "task_objective": task_objective,
                    "task_instructions": task_instructions,
                    "source": source,
                }
            )
            parsed = self._parse_llm_json(raw)
            decision = str(parsed.get("decision", "clear")).strip().lower()
            if decision not in {"clear", "question"}:
                decision = "clear"
            message = str(parsed.get("message", "Instructions look clear; I can proceed.")).strip()
            return {"decision": decision, "message": message}
        except Exception:
            return {"decision": "clear", "message": "Instructions look clear; I can proceed."}


class CoordinatorEngine:
    """Inference-first coordinator planner for ingest and research orchestration."""

    def __init__(self) -> None:
        self.enabled = False
        self._chain = None
        self._feedback_chain = None
        self._intent_chain = None
        self._runtime_chain = None
        self._init_error: str | None = None

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("COORDINATOR_MODEL", os.getenv("RESEARCH_MODEL", "gpt-4o-mini")).strip()
        if not api_key:
            self._init_error = "OPENAI_API_KEY not set"
            return

        try:
            from langchain_core.output_parsers import StrOutputParser
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_openai import ChatOpenAI

            prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are the coordinator in a multi-agent research workflow. "
                            "Return ONLY JSON with keys: "
                            "summary_for_user (string), "
                            "execution_plan_for_user (string), "
                            "approval_question (string), "
                            "key_topics (array of strings), "
                            "research_focus (array of strings), "
                            "priority_rationale (array of strings), "
                            "analyst_tasks (array of objects with keys: agent_id, objective, source_hint, instructions, priority), "
                            "notes (array of strings). "
                            "Do not return markdown."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Title: {title}\n"
                            "Objective: {objective}\n"
                            "Audience: {audience}\n"
                            "Tone: {tone}\n"
                            "Constraints: {constraints}\n"
                            "Key points: {key_points}\n"
                            "Source candidates: {source_candidates}\n"
                            "Create an ingest understanding and research fan-out plan."
                        ),
                    ),
                ]
            )
            feedback_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are the coordinator in a multi-agent research workflow. "
                            "Given the current plan and user feedback, produce an updated plan. "
                            "Return ONLY JSON with keys: "
                            "response_to_user (string), "
                            "updated_plan (object with keys: summary_for_user, execution_plan_for_user, approval_question, key_topics, "
                            "research_focus, priority_rationale, analyst_tasks, notes). "
                            "analyst_tasks items must contain: agent_id, objective, source_hint, instructions, priority."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Title: {title}\n"
                            "Objective: {objective}\n"
                            "Audience: {audience}\n"
                            "Tone: {tone}\n"
                            "Constraints: {constraints}\n"
                            "Current plan JSON: {current_plan_json}\n"
                            "User feedback: {feedback}\n"
                            "Revise the plan while preserving parts not contradicted by feedback."
                        ),
                    ),
                ]
            )
            intent_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "Classify user intent in a planning conversation. "
                            "Return ONLY JSON with keys: intent, reason. "
                            "intent must be one of: approve, iterate, question, hold."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Current plan summary: {plan_summary}\n"
                            "Current approval question: {approval_question}\n"
                            "User message: {user_message}\n"
                            "Decide if user is approving the plan to proceed or asking for iteration."
                        ),
                    ),
                ]
            )
            runtime_prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        (
                            "You are the coordinator in a multi-agent workflow. "
                            "Answer user runtime questions about progress/next steps using the provided context. "
                            "Return ONLY compact JSON with keys: action, reply_for_user. "
                            "action must be one of: none, advance_stage."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Run context JSON: {run_context_json}\n"
                            "User message: {user_message}\n"
                            "Decide if the user is asking to advance to the next step or just asking for status/clarification."
                        ),
                    ),
                ]
            )
            llm = ChatOpenAI(model=model, temperature=0)
            self._chain = prompt | llm | StrOutputParser()
            self._feedback_chain = feedback_prompt | llm | StrOutputParser()
            self._intent_chain = intent_prompt | llm | StrOutputParser()
            self._runtime_chain = runtime_prompt | llm | StrOutputParser()
            self.enabled = True
        except Exception as exc:  # noqa: BLE001
            self._init_error = str(exc)

    @property
    def init_error(self) -> str | None:
        return self._init_error

    @staticmethod
    def _parse_llm_json(raw: str) -> dict[str, Any]:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise

    @staticmethod
    def _fallback_plan(package: NormalizedTaskPackage, note: str) -> dict[str, Any]:
        key_topics = package.key_points[:5] if package.key_points else [package.objective[:100]]
        task_count = max(2, min(4, len(package.source_candidates) or 2))
        analyst_tasks: list[dict[str, str]] = []
        for i in range(task_count):
            source_hint = package.source_candidates[i] if i < len(package.source_candidates) else package.objective
            analyst_tasks.append(
                {
                    "agent_id": f"research_agent_{i+1}",
                    "objective": "Extract one verifiable claim relevant to the user objective.",
                    "source_hint": source_hint,
                    "instructions": "Find one concrete claim, include caveats, and estimate confidence.",
                    "priority": "normal",
                }
            )
        return {
            "summary_for_user": (
                "Fallback coordinator plan active because inference is unavailable. "
                f"Objective interpreted as: {package.objective[:180]}"
            ),
            "execution_plan_for_user": (
                "I will assign multiple research agents to cover the main topics, then aggregate findings "
                "into a single evidence pack before drafting."
            ),
            "approval_question": "Approve this fallback plan so Research can start?",
            "key_topics": key_topics,
            "research_focus": key_topics,
            "priority_rationale": [
                "Cover the core claim first (cost competitiveness and deployment feasibility).",
                "Add risk-heavy topics early to avoid one-sided conclusions.",
            ],
            "analyst_tasks": analyst_tasks,
            "notes": [note],
        }

    @staticmethod
    def _normalize_plan_dict(parsed: dict[str, Any]) -> dict[str, Any]:
        tasks = parsed.get("analyst_tasks", [])
        normalized_tasks: list[dict[str, str]] = []
        for i, task in enumerate(tasks):
            if not isinstance(task, dict):
                continue
            normalized_tasks.append(
                {
                    "agent_id": str(task.get("agent_id", f"research_agent_{i+1}")),
                    "objective": str(task.get("objective", "Extract one relevant claim.")),
                    "source_hint": str(task.get("source_hint", "")),
                    "instructions": str(task.get("instructions", "Provide one evidence-backed claim.")),
                    "priority": str(task.get("priority", "normal")),
                }
            )
        return {
            "summary_for_user": str(parsed.get("summary_for_user", "")),
            "execution_plan_for_user": str(parsed.get("execution_plan_for_user", "")),
            "approval_question": str(parsed.get("approval_question", "Approve ingest plan?")),
            "key_topics": [str(x) for x in parsed.get("key_topics", [])],
            "research_focus": [str(x) for x in parsed.get("research_focus", [])],
            "priority_rationale": [str(x) for x in parsed.get("priority_rationale", [])],
            "analyst_tasks": normalized_tasks,
            "notes": [str(x) for x in parsed.get("notes", [])],
        }

    def plan(self, package: NormalizedTaskPackage) -> dict[str, Any]:
        if not self.enabled or self._chain is None:
            return self._fallback_plan(package, "coordinator_fallback_mode")

        try:
            raw = self._chain.invoke(
                {
                    "title": package.title or "",
                    "objective": package.objective,
                    "audience": package.audience,
                    "tone": package.tone,
                    "constraints": "; ".join(package.constraints),
                    "key_points": "; ".join(package.key_points),
                    "source_candidates": "; ".join(package.source_candidates),
                }
            )
            parsed = self._parse_llm_json(raw)
            return self._normalize_plan_dict(parsed)
        except Exception:  # noqa: BLE001
            return self._fallback_plan(package, "coordinator_call_failed_fallback")

    def revise_plan(
        self,
        *,
        package: NormalizedTaskPackage,
        current_plan: dict[str, Any],
        feedback: str,
    ) -> tuple[dict[str, Any], str]:
        if not self.enabled or self._feedback_chain is None:
            revised = dict(current_plan)
            notes = [str(x) for x in revised.get("notes", [])]
            notes.append("feedback_received_fallback")
            revised["notes"] = notes
            response = (
                "I captured your feedback and adjusted the plan context, but inference is unavailable. "
                "I can keep iterating in fallback mode."
            )
            return revised, response

        try:
            raw = self._feedback_chain.invoke(
                {
                    "title": package.title or "",
                    "objective": package.objective,
                    "audience": package.audience,
                    "tone": package.tone,
                    "constraints": "; ".join(package.constraints),
                    "current_plan_json": json.dumps(current_plan, ensure_ascii=True),
                    "feedback": feedback,
                }
            )
            parsed = self._parse_llm_json(raw)
            response = str(parsed.get("response_to_user", "Thanks, I revised the plan."))
            updated = parsed.get("updated_plan", {})
            if not isinstance(updated, dict):
                return current_plan, "I couldn't parse a revised plan; keeping the current plan."
            normalized = self._normalize_plan_dict(updated)
            return normalized, response
        except Exception:
            revised = dict(current_plan)
            notes = [str(x) for x in revised.get("notes", [])]
            notes.append("feedback_call_failed_fallback")
            revised["notes"] = notes
            response = (
                "I received your feedback, but plan revision inference failed. "
                "I kept the current plan and can try again with more specific guidance."
            )
            return revised, response

    def classify_intent(self, *, current_plan: dict[str, Any], user_message: str) -> tuple[str, str]:
        if not self.enabled or self._intent_chain is None:
            return "iterate", "inference_unavailable"

        try:
            raw = self._intent_chain.invoke(
                {
                    "plan_summary": str(current_plan.get("summary_for_user", "")),
                    "approval_question": str(current_plan.get("approval_question", "")),
                    "user_message": user_message,
                }
            )
            parsed = self._parse_llm_json(raw)
            intent = str(parsed.get("intent", "iterate")).strip().lower()
            if intent not in {"approve", "iterate", "question", "hold"}:
                intent = "iterate"
            reason = str(parsed.get("reason", ""))
            return intent, reason
        except Exception:
            return "iterate", "intent_classification_failed"

    def runtime_response(
        self,
        *,
        run_context: dict[str, Any],
        user_message: str,
    ) -> tuple[str, str]:
        if not self.enabled or self._runtime_chain is None:
            next_stage = str(run_context.get("next_stage", "unknown"))
            return "none", f"I can help with process tracking. Next stage appears to be `{next_stage}`."

        try:
            raw = self._runtime_chain.invoke(
                {
                    "run_context_json": json.dumps(run_context, ensure_ascii=True),
                    "user_message": user_message,
                }
            )
            parsed = self._parse_llm_json(raw)
            action = str(parsed.get("action", "none")).strip().lower()
            if action not in {"none", "advance_stage"}:
                action = "none"
            reply = str(parsed.get("reply_for_user", ""))
            if not reply:
                reply = "Here is the current status and next step based on the run context."
            return action, reply
        except Exception:
            next_stage = str(run_context.get("next_stage", "unknown"))
            return "none", f"I couldn't classify that reliably. Next stage appears to be `{next_stage}`."


class RunRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
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
            INSERT INTO messages (msg_id, run_id, from_agent, to_agent, message_type, stage, priority, timestamp, content, task_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            stage_status={str(k): str(v) for k, v in json.loads(str(run["stage_status_json"])) .items()},
            approvals={str(k): bool(v) for k, v in json.loads(str(run["approvals_json"])) .items()},
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


def normalize_label(value: str) -> str:
    return value.strip().lower()


def parse_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current_header: str | None = None

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if current_header:
                sections.setdefault(current_header, []).append("")
            continue

        header_match = SECTION_HEADER_RE.match(stripped)
        if header_match:
            current_header = normalize_label(header_match.group("header"))
            sections.setdefault(current_header, [])
            continue

        inline_match = INLINE_FIELD_RE.match(stripped)
        if inline_match and normalize_label(inline_match.group("key")) in {"title", "objective", "audience"}:
            key = normalize_label(inline_match.group("key"))
            sections.setdefault(key, []).append(inline_match.group("value").strip())
            current_header = key
            continue

        if current_header:
            sections.setdefault(current_header, []).append(stripped)

    return sections


def normalize_bullets(lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in lines:
        item = line.strip()
        if not item:
            continue
        item = re.sub(r"^[-*]\s*", "", item)
        items.append(item)
    return items


def first_nonempty_line(lines: list[str]) -> str:
    for line in lines:
        if line.strip():
            return line.strip()
    return ""


def build_normalized_task(input_path: Path) -> NormalizedTaskPackage:
    text = input_path.read_text(encoding="utf-8")
    section_map = parse_sections(text)

    objective = first_nonempty_line(section_map.get("objective", []))
    audience = first_nonempty_line(section_map.get("audience", []))
    tone = "; ".join(normalize_bullets(section_map.get("tone and style constraints", [])))

    constraints: list[str] = []
    constraints.extend(normalize_bullets(section_map.get("draft output preference", [])))
    constraints.extend(normalize_bullets(section_map.get("questions to answer explicitly", [])))

    missing = []
    if not objective:
        missing.append("objective")
    if not audience:
        missing.append("audience")
    if not tone:
        missing.append("tone")
    if not constraints:
        missing.append("constraints")
    if missing:
        raise ValueError(f"Input brief missing required fields: {', '.join(missing)}")

    source_candidates: list[str] = normalize_bullets(section_map.get("potential sources to investigate", []))
    for match in URL_RE.findall(text):
        if match not in source_candidates:
            source_candidates.append(match)

    run_id = str(uuid.uuid4())
    return NormalizedTaskPackage(
        run_id=run_id,
        created_at=now_iso(),
        input_path=str(input_path),
        objective=objective,
        audience=audience,
        tone=tone,
        constraints=constraints,
        source_candidates=source_candidates,
        title=first_nonempty_line(section_map.get("title", [])) or None,
        key_points=normalize_bullets(section_map.get("core points to explore", [])),
    )


class AgenticTUI(App[None]):
    CSS = """
    Screen { layout: vertical; }
    #top { height: 1fr; }
    #left-pane, #right-pane {
        width: 1fr;
        border: round $panel;
        padding: 0 1;
    }
    #chat-pane {
        height: 1fr;
        border: round $panel;
        padding: 0 1;
    }
    #chat-log {
        height: 1fr;
    }
    #input-bar {
        height: auto;
    }
    #run-summary { height: 8; }
    #stages { height: 12; }
    #tasks { height: 12; }
    #event-log { height: 1fr; }
    """

    BINDINGS = [
        ("n", "advance_stage", "Advance Stage"),
        ("a", "approve_gate", "Approve Gate"),
        ("d", "demo_message", "Demo Msg"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, input_path: Path, db_path: Path, resume_run_id: str | None = None):
        super().__init__()
        self.input_path = input_path
        self.db_path = db_path
        self.resume_run_id = resume_run_id
        self.coordinator_engine = CoordinatorEngine()
        self.research_engine = ResearchEngine()
        self.coordinator_plan: dict[str, Any] = {}
        self.task_briefs: dict[str, dict[str, str]] = {}
        self._spinner_idx = 0
        self.chat_view_mode = "compact"
        self.chat_scope_mode = "focus"
        self.show_internal_messages = False
        self.show_progress_updates = True
        self.repo: RunRepository | None = None
        self.package: NormalizedTaskPackage | None = None
        self.state: RunState | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top"):
            with Vertical(id="left-pane"):
                yield Static("Run not initialized", id="run-summary")
                yield DataTable(id="stages")
            with Vertical(id="right-pane"):
                yield Static("Task Ledger")
                yield DataTable(id="tasks")
                yield Static("Events")
                yield RichLog(id="event-log", wrap=True, highlight=True)
        with Vertical(id="chat-pane"):
            yield Static("Shared Chat")
            yield RichLog(id="chat-log", wrap=True, highlight=True)
            yield Input(placeholder="Type a message to coordinator and press Enter", id="input-bar")
        yield Footer()

    def on_mount(self) -> None:
        stages = self.query_one("#stages", DataTable)
        stages.add_columns("Stage", "Status", "Approval")
        tasks = self.query_one("#tasks", DataTable)
        tasks.add_columns("Task ID", "Owner", "Stage", "Status")
        self.repo = RunRepository(self.db_path)
        self.repo.init_schema()
        if not self.coordinator_engine.enabled and self.coordinator_engine.init_error:
            self.query_one("#event-log", RichLog).write(
                f"Coordinator engine fallback mode: {self.coordinator_engine.init_error}"
            )
        if not self.research_engine.enabled and self.research_engine.init_error:
            self.query_one("#event-log", RichLog).write(
                f"Research engine fallback mode: {self.research_engine.init_error}"
            )
        self.set_focus(self.query_one("#input-bar", Input))
        self._set_status("Initializing run and coordinator plan...", level="in_progress")
        self.run_worker(self._initialize_run_async(), exclusive=True, group="startup")

    def on_unmount(self) -> None:
        if self.repo is not None:
            self.repo.close()

    def _restore_logs(self) -> None:
        if not self.state:
            return
        chat = self.query_one("#chat-log", RichLog)
        events = self.query_one("#event-log", RichLog)

        for entry in self.state.events:
            events.write(f"[{entry.timestamp}] {entry.message}")

        for message in self.state.messages:
            self._write_chat_renderable(message)

        self._render_tasks()

    async def _initialize_run_async(self) -> None:
        event_log = self.query_one("#event-log", RichLog)
        if self.repo is None:
            event_log.write("[error]Repository is not initialized[/error]")
            self._set_status("Initialization failed.", level="error")
            return

        if self.resume_run_id:
            loaded = self.repo.load_run(self.resume_run_id)
            if loaded is None:
                event_log.write(f"[error]Run not found in DB: {self.resume_run_id}[/error]")
                self._render_summary(error=f"Run not found: {self.resume_run_id}")
                self._set_status("Run not found.", level="error")
                return
            self.state = loaded
            package_payload = loaded.artifacts.get("normalized_task_package")
            if isinstance(package_payload, dict):
                self.package = NormalizedTaskPackage.from_dict(package_payload)
            plan_payload = loaded.artifacts.get("coordinator_plan")
            if isinstance(plan_payload, dict):
                self.coordinator_plan = plan_payload
            self._restore_logs()
            self._log_event("Run resumed from SQLite state.")
            self._render_all()
            self._set_status("Run resumed. You can chat with coordinator or press 'n' to continue.", level="done")
            return

        try:
            self.package = build_normalized_task(self.input_path)
        except Exception as exc:  # noqa: BLE001
            event_log.write(f"[error]Failed to initialize run:[/error] {exc}")
            self._render_summary(error=str(exc))
            self._set_status("Initialization failed.", level="error")
            return

        created = now_iso()
        self.state = RunState(
            run_id=self.package.run_id,
            input_path=self.package.input_path,
            created_at=created,
            updated_at=created,
            stage_status={stage: "not_started" for stage in STAGES},
            approvals={stage: False for stage in REQUIRED_APPROVAL_STAGES},
            artifacts={"user_brief": {"input_path": self.package.input_path}},
            tasks=[],
        )

        self.repo.create_run(self.state)
        self.repo.upsert_artifact(self.state.run_id, "user_brief", self.state.artifacts["user_brief"])

        self._start_stage("Ingest")
        self._complete_stage("Ingest", self.package.to_dict())
        await self._generate_coordinator_plan_async()
        self._post_ingest_summary_and_approval_request()
        self._log_event("Run initialized. Ingest completed. Waiting for approval to proceed.")
        self._render_all()
        self._set_status("Waiting for your feedback or approval.", level="done")

    async def _generate_coordinator_plan_async(self) -> None:
        if not self.package:
            return
        self._set_status("Coordinator is analyzing your request and drafting a plan...", level="in_progress")
        self.coordinator_plan = await asyncio.to_thread(self.coordinator_engine.plan, self.package)
        self._persist_artifact("coordinator_plan", self.coordinator_plan)
        if self.coordinator_engine.enabled:
            self._log_event("Coordinator plan generated via inference.")
        else:
            self._log_event("Coordinator fallback planning used (no inference).")
        self._set_status("Coordinator plan ready. Review and provide feedback or approval.", level="done")

    def _post_ingest_summary_and_approval_request(self) -> None:
        if not self.package:
            return
        summary = str(self.coordinator_plan.get("summary_for_user", "")).strip()
        execution_plan = str(self.coordinator_plan.get("execution_plan_for_user", "")).strip()
        if not summary:
            constraints_preview = "; ".join(self.package.constraints[:2]) if self.package.constraints else "n/a"
            summary = (
                "Ingest summary: "
                f"Objective='{self.package.objective[:160]}', "
                f"Audience='{self.package.audience[:120]}', "
                f"Tone='{self.package.tone[:120]}', "
                f"Source candidates={len(self.package.source_candidates)}, "
                f"Constraints(sample)='{constraints_preview[:180]}'."
            )
        approval_question = str(
            self.coordinator_plan.get("approval_question", "Please approve this ingest plan so I can start Research.")
        )
        if not execution_plan:
            execution_plan = (
                "Execution plan: run parallel research agents across key topics, prioritize disputed/controversial "
                "areas for deeper evidence checks, then synthesize findings into one evidence pack."
            )
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="status",
                stage="Ingest",
                priority="normal",
                timestamp=now_iso(),
                content=summary,
            )
        )
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="status",
                stage="Ingest",
                priority="normal",
                timestamp=now_iso(),
                content=execution_plan,
            )
        )
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="question",
                stage="Ingest",
                priority="high",
                timestamp=now_iso(),
                content=approval_question,
            )
        )

    def _set_status(self, text: str, *, level: str = "info") -> None:
        if level == "in_progress":
            spinner_frames = ["⏳", "🔄", "⌛", "🔃"]
            prefix = spinner_frames[self._spinner_idx % len(spinner_frames)]
            self._spinner_idx += 1
        elif level == "done":
            prefix = "✅"
        elif level == "error":
            prefix = "❌"
        else:
            prefix = "ℹ️"
        content = f"{prefix} {text}"
        stage = "Ingest"
        if self.state:
            stage = self._next_stage() or "Final"
        self._write_chat_renderable(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="broadcast",
                message_type="status",
                stage=stage if stage in STAGES else "Ingest",
                priority="normal",
                timestamp=now_iso(),
                content=content,
            )
        )

    def _approve_ingest(self, decision_text: str, *, auto_advance: bool = False) -> None:
        if not self.state:
            return
        self.state.approvals["Ingest"] = True
        self._persist_run_status()
        self._log_event("Approval granted: Ingest checkpoint.")
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="user",
                to_agent="coordinator",
                message_type="decision",
                stage="Ingest",
                priority="normal",
                timestamp=now_iso(),
                content=decision_text,
            )
        )
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="broadcast",
                message_type="status",
                stage="Ingest",
                priority="normal",
                timestamp=now_iso(),
                content="Ingest plan approved by user. Proceeding when commanded.",
            )
        )
        self._render_all()
        if auto_advance:
            self._set_status("Ingest approved. Starting Research now.", level="done")
        else:
            self._set_status("Ingest approved. Press 'n' to start Research.", level="done")

    async def _iterate_ingest_with_feedback(self, feedback_text: str) -> None:
        if not self.state or not self.package:
            return
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="user",
                to_agent="coordinator",
                message_type="question",
                stage="Ingest",
                priority="normal",
                timestamp=now_iso(),
                content=feedback_text,
            )
        )
        self._set_status("Coordinator is revising the plan based on your feedback...", level="in_progress")
        updated_plan, response_text = await asyncio.to_thread(
            self.coordinator_engine.revise_plan,
            package=self.package,
            current_plan=self.coordinator_plan,
            feedback=feedback_text,
        )
        self.coordinator_plan = updated_plan
        self._persist_artifact("coordinator_plan", self.coordinator_plan)
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="status",
                stage="Ingest",
                priority="normal",
                timestamp=now_iso(),
                content=response_text,
            )
        )
        self._post_ingest_summary_and_approval_request()
        self._log_event("Coordinator revised ingest plan based on user feedback.")
        self._render_all()
        self._set_status("Plan updated. Continue feedback, or approve when satisfied.", level="done")

    def _next_stage(self) -> str | None:
        if not self.state:
            return None
        for stage in STAGES:
            if self.state.stage_status.get(stage) != "completed":
                return stage
        return None

    def _persist_run_status(self) -> None:
        if self.repo is None or self.state is None:
            return
        self.repo.save_run_status(self.state)

    def _log_event(self, message: str) -> None:
        timestamp = now_iso()
        if self.state:
            self.state.events.append(EventEntry(timestamp=timestamp, message=message))
            if self.repo is not None:
                self.repo.add_event(self.state.run_id, message=message, timestamp=timestamp)
        self.query_one("#event-log", RichLog).write(message)

    def _persist_artifact(self, name: str, value: object) -> None:
        if self.state is None or self.repo is None:
            return
        self.state.artifacts[name] = value
        self.repo.upsert_artifact(self.state.run_id, name, value)

    def _render_summary(self, error: str | None = None) -> None:
        summary = self.query_one("#run-summary", Static)
        if error:
            summary.update(f"[b red]Initialization error[/b red]\n{error}")
            return
        if not self.state:
            summary.update("Run not initialized")
            return

        next_stage = self._next_stage() or "Done"
        ingest_approval = "approved" if self.state.approvals.get("Ingest") else "pending"
        final_approval = "approved" if self.state.approvals.get("Final") else "pending"
        title = self.package.title if self.package else "n/a"

        summary.update(
            "\n".join(
                [
                    f"[b]Run ID:[/b] {self.state.run_id}",
                    f"[b]Input:[/b] {self.state.input_path}",
                    f"[b]Title:[/b] {title}",
                    f"[b]Next Stage:[/b] {next_stage}",
                    f"[b]Ingest Approval:[/b] {ingest_approval}",
                    f"[b]Final Approval:[/b] {final_approval}",
                ]
            )
        )

    def _render_stages(self) -> None:
        stages_table = self.query_one("#stages", DataTable)
        stages_table.clear(columns=False)
        if not self.state:
            return

        for stage in STAGES:
            approval = "required" if stage in REQUIRED_APPROVAL_STAGES else "-"
            if stage in self.state.approvals:
                approval = "approved" if self.state.approvals[stage] else "pending"
            stages_table.add_row(stage, self.state.stage_status.get(stage, "not_started"), approval)

    def _render_all(self) -> None:
        self._render_summary()
        self._render_stages()
        self._render_tasks()

    def _render_tasks(self) -> None:
        tasks_table = self.query_one("#tasks", DataTable)
        tasks_table.clear(columns=False)
        if not self.state:
            return
        for task in self.state.tasks[-30:]:
            tasks_table.add_row(task.task_id[:8], task.owner, task.stage, task.status)

    def _start_stage(self, stage: str) -> bool:
        if not self.state:
            return False

        index = STAGES.index(stage)
        for prev in STAGES[:index]:
            if self.state.stage_status.get(prev) != "completed":
                self._log_event(f"Cannot start {stage}: previous stage {prev} is not completed.")
                return False

        if stage == "Research" and not self.state.approvals.get("Ingest", False):
            self._log_event("Cannot start Research: Ingest approval is pending.")
            return False

        if stage == "Final" and not self.state.approvals.get("Final", False):
            self._log_event("Cannot start Final: Final approval is pending.")
            return False

        self.state.stage_status[stage] = "in_progress"
        self._persist_run_status()
        self._log_event(f"Stage started: {stage}")
        return True

    def _complete_stage(self, stage: str, output: object) -> None:
        if not self.state:
            return

        artifact_key = STAGE_OUTPUT_ARTIFACT[stage]
        self._persist_artifact(artifact_key, output)
        self.state.stage_status[stage] = "completed"
        self._persist_run_status()
        self._log_event(f"Stage completed: {stage}")

    def _post_chat_message(self, message: ChatMessage) -> bool:
        if not self.state:
            return False

        if message.to_agent == "broadcast" and message.message_type not in {"status", "decision"}:
            self._log_event("Broadcast rejected: only status/decision allowed.")
            return False

        if message.message_type == "task":
            if not message.task_id:
                self._log_event("Task rejected: task_id is required.")
                return False
            if message.to_agent == "broadcast":
                self._log_event("Task rejected: tasks cannot target broadcast.")
                return False

        self.state.messages.append(message)
        if self.repo is not None:
            self.repo.add_message(self.state.run_id, message)
        self._write_chat_renderable(message)
        return True

    @staticmethod
    def _format_coordinator_plan_markdown(plan: dict[str, Any]) -> str:
        if not plan:
            return "No coordinator plan is available yet."

        summary = str(plan.get("summary_for_user", "")).strip() or "n/a"
        execution_plan = str(plan.get("execution_plan_for_user", "")).strip() or "n/a"
        approval = str(plan.get("approval_question", "")).strip() or "n/a"
        topics = [str(x) for x in plan.get("key_topics", [])] if isinstance(plan.get("key_topics"), list) else []
        rationale = [str(x) for x in plan.get("priority_rationale", [])] if isinstance(plan.get("priority_rationale"), list) else []
        tasks = plan.get("analyst_tasks", [])

        topic_lines = "\n".join([f"- {item}" for item in topics[:8]]) or "- n/a"
        rationale_lines = "\n".join([f"- {item}" for item in rationale[:8]]) or "- n/a"

        task_lines_list: list[str] = []
        if isinstance(tasks, list):
            for item in tasks[:12]:
                if not isinstance(item, dict):
                    continue
                agent = str(item.get("agent_id", "research_agent"))
                objective = str(item.get("objective", ""))
                source = str(item.get("source_hint", ""))
                task_lines_list.append(f"- **{agent}**: {objective} _(source: {source})_")
        task_lines = "\n".join(task_lines_list) or "- n/a"

        return (
            "## Coordinator Plan\n"
            f"**Summary**: {summary}\n\n"
            f"**Execution Plan**: {execution_plan}\n\n"
            f"**Approval Question**: {approval}\n\n"
            "**Key Topics**\n"
            f"{topic_lines}\n\n"
            "**Priority Rationale**\n"
            f"{rationale_lines}\n\n"
            "**Analyst Assignments**\n"
            f"{task_lines}"
        )

    @staticmethod
    def _format_json_block(value: object) -> str:
        return f"```json\n{json.dumps(value, ensure_ascii=True, indent=2)}\n```"

    def _format_help_markdown(self) -> str:
        return (
            "## Chat Commands\n"
            "- `/help`: Show available commands.\n"
            "- `/plan`: Show the latest coordinator plan.\n"
            "- `/run`: Show run summary and approval/checkpoint state.\n"
            "- `/agents`: List active agents and aggregate task status counts.\n"
            "- `/agent <agent_id>`: Show tasks and outputs for one agent.\n"
            "- `/task <task_id_or_prefix>`: Inspect one task output/error details.\n"
            "- `/view compact|detailed`: Switch chat density.\n"
            "- `/scope focus|all`: Show only user/coordinator chat or all traffic.\n"
            "- `/internal on|off`: Show/hide internal status chatter.\n"
            "- `/progress on|off`: Show/hide long-running progress updates."
        )

    def _post_coordinator_markdown(self, content: str, *, stage: str | None = None) -> None:
        if not self.state:
            return
        effective_stage = stage or (self._next_stage() or "Final")
        if effective_stage not in STAGES:
            effective_stage = "Ingest"
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="status",
                stage=effective_stage,
                priority="normal",
                timestamp=now_iso(),
                content=content,
            )
        )

    def _command_run_summary(self) -> str:
        if not self.state:
            return "No active run."
        next_stage = self._next_stage() or "Done"
        approvals = []
        for stage in sorted(REQUIRED_APPROVAL_STAGES):
            status = "approved" if self.state.approvals.get(stage, False) else "pending"
            approvals.append(f"- {stage}: {status}")
        approvals_block = "\n".join(approvals) or "- none"
        return (
            "## Run Summary\n"
            f"- Run ID: `{self.state.run_id}`\n"
            f"- Input: `{self.state.input_path}`\n"
            f"- Next stage: `{next_stage}`\n"
            f"- Tasks tracked: `{len(self.state.tasks)}`\n"
            "\n**Approvals**\n"
            f"{approvals_block}"
        )

    def _build_runtime_context(self) -> dict[str, Any]:
        if not self.state:
            return {"next_stage": "unknown"}

        next_stage = self._next_stage() or "Done"
        pending_approvals = [stage for stage, approved in self.state.approvals.items() if not approved]
        recent_tasks = self.state.tasks[-20:]
        task_counts: dict[str, int] = {}
        owner_counts: dict[str, int] = {}
        for task in recent_tasks:
            task_counts[task.status] = task_counts.get(task.status, 0) + 1
            owner_counts[task.owner] = owner_counts.get(task.owner, 0) + 1

        return {
            "run_id": self.state.run_id,
            "input_path": self.state.input_path,
            "stage_status": dict(self.state.stage_status),
            "approvals": dict(self.state.approvals),
            "pending_approvals": pending_approvals,
            "next_stage": next_stage,
            "task_status_counts_recent": task_counts,
            "active_agents_recent": owner_counts,
            "last_events": [entry.message for entry in self.state.events[-8:]],
            "objective": (self.package.objective if self.package else ""),
        }

    def _command_agents_summary(self) -> str:
        if not self.state:
            return "No active run."
        owners = sorted({task.owner for task in self.state.tasks})
        if not owners:
            return "No agent tasks have been created yet."

        lines = ["## Agents", "| Agent | Queued | In Progress | Done | Failed |", "|---|---:|---:|---:|---:|"]
        for owner in owners:
            queued = sum(1 for task in self.state.tasks if task.owner == owner and task.status == "queued")
            in_progress = sum(1 for task in self.state.tasks if task.owner == owner and task.status == "in_progress")
            done = sum(1 for task in self.state.tasks if task.owner == owner and task.status == "done")
            failed = sum(1 for task in self.state.tasks if task.owner == owner and task.status == "failed")
            lines.append(f"| `{owner}` | {queued} | {in_progress} | {done} | {failed} |")
        return "\n".join(lines)

    def _command_agent_details(self, agent_id: str) -> str:
        if not self.state:
            return "No active run."
        tasks = [task for task in self.state.tasks if task.owner == agent_id]
        if not tasks:
            return f"No tasks found for agent `{agent_id}`."

        lines = [f"## Agent `{agent_id}`", f"- Total tasks: `{len(tasks)}`", ""]
        for task in tasks[-8:]:
            lines.append(
                (
                    f"### Task `{task.task_id}`\n"
                    f"- Stage: `{task.stage}`\n"
                    f"- Status: `{task.status}`\n"
                    f"- Source: `{task.input_ref}`"
                )
            )
            if task.error:
                lines.append(f"- Error: `{task.error}`")
            if task.output is not None:
                lines.append("**Output**")
                lines.append(self._format_json_block(task.output))
            lines.append("")
        return "\n".join(lines).strip()

    def _command_task_details(self, task_key: str) -> str:
        if not self.state:
            return "No active run."
        needle = task_key.strip().lower()
        matches = [task for task in self.state.tasks if task.task_id.lower().startswith(needle)]
        if not matches:
            return f"No task found for id/prefix `{task_key}`."
        if len(matches) > 1:
            options = "\n".join([f"- `{task.task_id}` ({task.owner}, {task.status})" for task in matches[:10]])
            return (
                f"Task prefix `{task_key}` matched multiple tasks.\n"
                "Use a longer prefix:\n"
                f"{options}"
            )

        task = matches[0]
        lines = [
            f"## Task `{task.task_id}`",
            f"- Owner: `{task.owner}`",
            f"- Stage: `{task.stage}`",
            f"- Status: `{task.status}`",
            f"- Source: `{task.input_ref}`",
        ]
        if task.started_at:
            lines.append(f"- Started: `{task.started_at}`")
        if task.completed_at:
            lines.append(f"- Completed: `{task.completed_at}`")
        if task.error:
            lines.append(f"- Error: `{task.error}`")
        if task.output is not None:
            lines.append("")
            lines.append("**Output**")
            lines.append(self._format_json_block(task.output))
        return "\n".join(lines)

    def _handle_slash_command(self, text: str) -> bool:
        command, _, remainder = text.partition(" ")
        cmd = command.strip().lower()
        arg = remainder.strip()

        if cmd in {"/help", "/commands"}:
            self._post_coordinator_markdown(self._format_help_markdown(), stage=self._next_stage() or "Ingest")
            return True

        if cmd == "/plan":
            self._post_coordinator_markdown(self._format_coordinator_plan_markdown(self.coordinator_plan), stage="Ingest")
            self._set_status("Coordinator plan posted in chat.", level="done")
            return True

        if cmd == "/run":
            self._post_coordinator_markdown(self._command_run_summary())
            return True

        if cmd == "/agents":
            self._post_coordinator_markdown(self._command_agents_summary())
            return True

        if cmd == "/agent":
            if not arg:
                self._post_coordinator_markdown("Usage: `/agent <agent_id>`")
                return True
            self._post_coordinator_markdown(self._command_agent_details(arg))
            return True

        if cmd == "/task":
            if not arg:
                self._post_coordinator_markdown("Usage: `/task <task_id_or_prefix>`")
                return True
            self._post_coordinator_markdown(self._command_task_details(arg))
            return True

        if cmd == "/view":
            value = arg.lower()
            if value not in {"compact", "detailed"}:
                self._post_coordinator_markdown("Usage: `/view compact` or `/view detailed`")
                return True
            self.chat_view_mode = value
            self._post_coordinator_markdown(f"Chat view set to `{value}`.")
            return True

        if cmd == "/scope":
            value = arg.lower()
            if value not in {"focus", "all"}:
                self._post_coordinator_markdown("Usage: `/scope focus` or `/scope all`")
                return True
            self.chat_scope_mode = value
            self._repaint_chat_log()
            self._post_coordinator_markdown(f"Chat scope set to `{value}`.")
            return True

        if cmd == "/internal":
            value = arg.lower()
            if value not in {"on", "off"}:
                self._post_coordinator_markdown("Usage: `/internal on` or `/internal off`")
                return True
            self.show_internal_messages = value == "on"
            self._repaint_chat_log()
            state = "on" if self.show_internal_messages else "off"
            self._post_coordinator_markdown(f"Internal messages set to `{state}`.")
            return True

        if cmd == "/progress":
            value = arg.lower()
            if value not in {"on", "off"}:
                self._post_coordinator_markdown("Usage: `/progress on` or `/progress off`")
                return True
            self.show_progress_updates = value == "on"
            self._repaint_chat_log()
            state = "on" if self.show_progress_updates else "off"
            self._post_coordinator_markdown(f"Progress updates set to `{state}`.")
            return True

        self._post_coordinator_markdown(
            f"Unknown command: `{cmd}`\n\nUse `/help` to see available commands.",
            stage=self._next_stage() or "Ingest",
        )
        return True

    def _is_internal_message(self, message: ChatMessage) -> bool:
        return (
            message.to_agent == "broadcast"
            and message.message_type == "status"
        ) or (
            message.from_agent == "coordinator"
            and message.to_agent != "user"
            and message.message_type in {"status", "task"}
        )

    @staticmethod
    def _is_progress_update(message: ChatMessage) -> bool:
        if not (
            message.from_agent == "coordinator"
            and message.to_agent == "broadcast"
            and message.message_type == "status"
        ):
            return False
        body = (message.content or "").strip()
        return body.startswith(("⏳", "🔄", "⌛", "🔃", "✅", "❌"))

    def _should_display_chat_message(self, message: ChatMessage) -> bool:
        if self.show_progress_updates and self._is_progress_update(message):
            return True

        if self.chat_scope_mode == "focus":
            focus_pair = (
                (message.from_agent == "user" and message.to_agent == "coordinator")
                or (message.from_agent == "coordinator" and message.to_agent == "user")
            )
            if not focus_pair:
                return False

        if not self.show_internal_messages and self._is_internal_message(message):
            return False

        return True

    def _repaint_chat_log(self) -> None:
        chat = self.query_one("#chat-log", RichLog)
        chat.clear()
        if not self.state:
            return
        for message in self.state.messages:
            self._write_chat_renderable(message)

    @staticmethod
    def _type_icon(message_type: str, content: str) -> str:
        lowered = content.lower()
        if "failed" in lowered or "error" in lowered:
            return "❌"
        if message_type == "task":
            return "📌"
        if message_type == "question":
            return "❓"
        if message_type == "result":
            return "📤"
        if message_type == "decision":
            return "✅"
        if message_type == "review":
            return "🧪"
        if message_type == "status":
            if lowered.startswith("✅") or "completed" in lowered or "approved" in lowered:
                return "✅"
            if lowered.startswith("⏳") or "started" in lowered or "revising" in lowered or "analyzing" in lowered:
                return "⏳"
            return "📣"
        return "💬"

    @staticmethod
    def _looks_like_markdown(content: str) -> bool:
        markers = ("## ", "- ", "* ", "1. ", "```", "| ")
        return any(marker in content for marker in markers)

    def _render_chat_header(self, message: ChatMessage) -> Text:
        icon = self._type_icon(message.message_type, message.content)
        task_hint = f" #{message.task_id[:8]}" if message.task_id else ""
        timestamp = message.timestamp.split("T")[1][:8] if "T" in message.timestamp else message.timestamp

        if self.chat_view_mode == "detailed":
            header = Text()
            header.append(f"{icon} {message.from_agent} -> {message.to_agent}", style="bold")
            header.append(f" [{message.message_type}] [{message.stage}] {timestamp}", style="dim")
            if task_hint:
                header.append(task_hint, style="dim")
            header.append(f" id={message.msg_id[:8]}", style="dim")
            return header

        header = Text()
        header.append(f"{icon} {message.from_agent} -> {message.to_agent}", style="bold")
        header.append(f" [{message.message_type}] [{message.stage}] {timestamp}", style="dim")
        if task_hint:
            header.append(task_hint, style="dim")
        return header

    def _write_chat_renderable(self, message: ChatMessage) -> None:
        if not self._should_display_chat_message(message):
            return
        chat = self.query_one("#chat-log", RichLog)
        chat.write(self._render_chat_header(message))

        body = (message.content or "").strip()
        if not body:
            return
        if self._looks_like_markdown(body):
            chat.write(Markdown(body))
        else:
            chat.write(body)
        chat.write("")

    def _set_task_status(
        self,
        task: TaskRecord,
        *,
        status: str,
        output: dict[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        task.status = status
        if status == "in_progress":
            task.started_at = now_iso()
        if status in {"done", "failed"}:
            task.completed_at = now_iso()
        if output is not None:
            task.output = output
        if error is not None:
            task.error = error
        if self.repo is not None:
            self.repo.upsert_task(task)
        self._render_tasks()

    @staticmethod
    def _format_task_instruction_message(objective: str, instructions: str, source: str) -> str:
        return (
            "Task assignment:\n"
            f"- Objective: {objective}\n"
            f"- Instructions: {instructions}\n"
            f"- Source candidate: {source}\n"
            "- Expected output: one evidence-backed claim + evidence note + confidence."
        )

    async def _run_instruction_review_loop(self, tasks: list[TaskRecord]) -> None:
        if not tasks:
            return

        self._log_event("Instruction review loop started.")
        for task in tasks:
            brief = self.task_briefs.get(task.task_id, {})
            objective = brief.get("objective", "Extract one evidence-backed claim.")
            instructions = brief.get("instructions", "Provide claim and confidence.")
            source = task.input_ref

            review = await asyncio.to_thread(
                self.research_engine.review_task_instruction,
                task_objective=objective,
                task_instructions=instructions,
                source=source,
            )
            decision = str(review.get("decision", "clear")).lower()
            message = str(review.get("message", "Instructions look clear; I can proceed."))

            if decision == "question":
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent=task.owner,
                        to_agent="coordinator",
                        message_type="question",
                        stage="Research",
                        priority="normal",
                        timestamp=now_iso(),
                        task_id=task.task_id,
                        content=message,
                    )
                )

                clarification = (
                    "Clarification: focus on one concrete claim directly tied to the task objective; "
                    "include one supporting note and confidence from 0 to 1."
                )
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent="coordinator",
                        to_agent=task.owner,
                        message_type="status",
                        stage="Research",
                        priority="normal",
                        timestamp=now_iso(),
                        task_id=task.task_id,
                        content=clarification,
                    )
                )
                updated_instructions = f"{instructions} {clarification}"
                self.task_briefs[task.task_id]["instructions"] = updated_instructions
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent=task.owner,
                        to_agent="coordinator",
                        message_type="status",
                        stage="Research",
                        priority="normal",
                        timestamp=now_iso(),
                        task_id=task.task_id,
                        content="Thanks, clarification received. I can proceed.",
                    )
                )
            else:
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent=task.owner,
                        to_agent="coordinator",
                        message_type="status",
                        stage="Research",
                        priority="normal",
                        timestamp=now_iso(),
                        task_id=task.task_id,
                        content=message,
                    )
                )
        self._log_event("Instruction review loop complete.")

    def _run_research_subtask(self, task: TaskRecord) -> dict[str, object]:
        time.sleep(0.05)
        objective = self.package.objective if self.package else ""
        audience = self.package.audience if self.package else ""
        tone = self.package.tone if self.package else ""
        constraints = self.package.constraints if self.package else []
        brief = self.task_briefs.get(task.task_id, {})
        task_objective = brief.get("objective", "Extract one evidence-backed claim.")
        task_instructions = brief.get("instructions", "Provide claim with confidence and caveats.")
        analysis = self.research_engine.analyze_source(
            source=task.input_ref,
            objective=objective,
            audience=audience,
            tone=tone,
            constraints=constraints,
            task_objective=task_objective,
            task_instructions=task_instructions,
        )
        return {
            "source_ref": task.input_ref,
            "claim": analysis["claim"],
            "evidence_note": analysis["evidence_note"],
            "confidence": float(analysis["confidence"]),
            "risk_flags": analysis["risk_flags"],
        }

    async def _execute_research_parallel(self) -> dict[str, object]:
        if not self.state:
            return {"summary": "No state", "sources": [], "claims": []}

        candidates: list[str] = []
        if self.package:
            candidates = list(self.package.source_candidates)
        if not candidates:
            candidates = ["No explicit source provided"]

        plan_tasks = self.coordinator_plan.get("analyst_tasks", [])
        inferred_tasks: list[dict[str, str]] = []
        if isinstance(plan_tasks, list):
            for i, item in enumerate(plan_tasks):
                if not isinstance(item, dict):
                    continue
                inferred_tasks.append(
                    {
                        "agent_id": str(item.get("agent_id", f"research_agent_{i+1}")),
                        "objective": str(item.get("objective", "Extract one evidence-backed claim.")),
                        "source_hint": str(item.get("source_hint", "")),
                        "instructions": str(item.get("instructions", "Provide one claim plus confidence.")),
                        "priority": str(item.get("priority", "normal")),
                    }
                )

        if not inferred_tasks:
            for i, source in enumerate(candidates[: min(6, len(candidates))]):
                inferred_tasks.append(
                    {
                        "agent_id": f"research_agent_{(i % 3) + 1}",
                        "objective": "Extract one evidence-backed claim relevant to objective.",
                        "source_hint": source,
                        "instructions": "Provide claim, note, and confidence.",
                        "priority": "normal",
                    }
                )

        capped = inferred_tasks[: min(8, len(inferred_tasks))]
        tasks: list[TaskRecord] = []
        for i, spec in enumerate(capped):
            owner = spec["agent_id"] or f"research_agent_{(i % 3) + 1}"
            source = spec["source_hint"] or candidates[i % len(candidates)]
            record = TaskRecord(
                task_id=str(uuid.uuid4()),
                run_id=self.state.run_id,
                stage="Research",
                owner=owner,
                status="queued",
                input_ref=source,
            )
            self.state.tasks.append(record)
            tasks.append(record)
            self.task_briefs[record.task_id] = {
                "objective": spec["objective"],
                "instructions": spec["instructions"],
            }
            if self.repo is not None:
                self.repo.upsert_task(record)

        self._render_tasks()
        self._log_event(f"Research fan-out: queued {len(tasks)} subtasks.")
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="broadcast",
                message_type="status",
                stage="Research",
                priority="normal",
                timestamp=now_iso(),
                content=f"Research fan-out started with {len(tasks)} subtasks.",
            )
        )
        for task in tasks:
            brief = self.task_briefs.get(task.task_id, {})
            self._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="coordinator",
                    to_agent=task.owner,
                    message_type="task",
                    stage="Research",
                    priority="normal",
                    timestamp=now_iso(),
                    task_id=task.task_id,
                    content=self._format_task_instruction_message(
                        brief.get("objective", "Extract one evidence-backed claim."),
                        brief.get("instructions", "Provide claim and confidence."),
                        task.input_ref,
                    ),
                )
            )

        await self._run_instruction_review_loop(tasks)

        async def run_one(pool: concurrent.futures.ThreadPoolExecutor, task: TaskRecord) -> tuple[TaskRecord, dict[str, object] | None, Exception | None]:
            self._set_task_status(task, status="in_progress")
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(pool, self._run_research_subtask, task)
                return task, result, None
            except Exception as exc:  # noqa: BLE001
                return task, None, exc

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(tasks))) as pool:
            pending = [run_one(pool, task) for task in tasks]
            results = await asyncio.gather(*pending)

        claims: list[dict[str, object]] = []
        source_entries: list[dict[str, object]] = []
        source_id_counter = 1
        for task, result, err in results:
            if err is None and result is not None:
                self._set_task_status(task, status="done", output=result)
                claims.append(result)
                self._log_event(f"Research subtask done: {task.task_id[:8]} by {task.owner}")
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent=task.owner,
                        to_agent="coordinator",
                        message_type="result",
                        stage="Research",
                        priority="normal",
                        timestamp=now_iso(),
                        task_id=task.task_id,
                        content=f"Claim extracted (confidence={result.get('confidence', 0.0)}).",
                    )
                )
                source_ref = str(result.get("source_ref", task.input_ref))
                url = maybe_url(source_ref)
                source_id = f"S{source_id_counter}"
                source_id_counter += 1
                source_entries.append(
                    {
                        "source_id": source_id,
                        "title": source_ref[:120],
                        "url": url or "",
                        "publisher": "",
                        "published_at": "",
                        "retrieved_at": now_iso(),
                        "tier": infer_source_tier(source_ref),
                        "confidence": float(result.get("confidence", 0.5)),
                        "key_claims": [str(result.get("claim", ""))],
                    }
                )
            else:
                self._set_task_status(task, status="failed", error=str(err))
                self._log_event(f"Research subtask failed: {task.task_id[:8]} ({err})")
                self._post_chat_message(
                    ChatMessage(
                        msg_id=str(uuid.uuid4()),
                        from_agent=task.owner,
                        to_agent="coordinator",
                        message_type="status",
                        stage="Research",
                        priority="high",
                        timestamp=now_iso(),
                        task_id=task.task_id,
                        content=f"Task failed: {err}",
                    )
                )

        done_count = sum(1 for task in tasks if task.status == "done")
        self._log_event(f"Research fan-in complete: {done_count}/{len(tasks)} succeeded.")
        return {
            "summary": f"Parallel research complete: {done_count}/{len(tasks)} subtasks succeeded.",
            "sources": source_entries,
            "claims": claims,
        }

    async def action_advance_stage(self) -> None:
        if not self.state:
            return

        next_stage = self._next_stage()
        if not next_stage:
            self._log_event("Run already complete.")
            return

        if not self._start_stage(next_stage):
            self._render_all()
            return

        if next_stage == "Research":
            output = await self._execute_research_parallel()
        elif next_stage == "Outline":
            output = {
                "hook": "Placeholder hook",
                "sections": ["Problem", "Evidence", "Risks", "Takeaway"],
            }
        elif next_stage == "Draft":
            output = "Placeholder first draft with citation markers [S1][S2]."
        elif next_stage == "Critique":
            output = {"score": 24, "issues": ["Tighten opening", "Add one risk source"]}
        elif next_stage == "Revise":
            output = "Placeholder revised draft with changelog applied."
        elif next_stage == "Final":
            output = "Placeholder final post text + references."
        else:
            output = {"ok": True}

        self._complete_stage(next_stage, output)
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="broadcast",
                message_type="status",
                stage=next_stage,
                priority="normal",
                timestamp=now_iso(),
                content=f"{next_stage} stage completed.",
            )
        )
        self._render_all()

    def action_approve_gate(self) -> None:
        if not self.state:
            return

        if not self.state.approvals.get("Ingest", False):
            self._approve_ingest("Plan approved. Continue.")
            return

        if self.state.stage_status.get("Revise") == "completed" and not self.state.approvals.get("Final", False):
            self.state.approvals["Final"] = True
            self._persist_run_status()
            self._log_event("Approval granted: Final checkpoint.")
            self._post_chat_message(
                ChatMessage(
                    msg_id=str(uuid.uuid4()),
                    from_agent="user",
                    to_agent="coordinator",
                    message_type="decision",
                    stage="Final",
                    priority="normal",
                    timestamp=now_iso(),
                    content="Final approval granted.",
                )
            )
            self._render_all()
            return

        self._log_event("No approval gate is currently pending.")

    def action_demo_message(self) -> None:
        if not self.state:
            return
        stage = self._next_stage() or "Final"
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="research_agent_1",
                to_agent="coordinator",
                message_type="status",
                stage=stage if stage in STAGES else "Research",
                priority="normal",
                timestamp=now_iso(),
                content="I can start when task contract is ready.",
            )
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-advance":
            await self.action_advance_stage()
        elif event.button.id == "btn-approve":
            self.action_approve_gate()
        elif event.button.id == "btn-demo":
            self.action_demo_message()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        if text.startswith("/"):
            self._handle_slash_command(text)
            return

        if self.state and not self.state.approvals.get("Ingest", False):
            intent, reason = self.coordinator_engine.classify_intent(
                current_plan=self.coordinator_plan,
                user_message=text,
            )
            self._log_event(f"Coordinator intent classification: {intent} ({reason})")
            if intent == "approve":
                self._approve_ingest(text, auto_advance=True)
                await self.action_advance_stage()
            else:
                await self._iterate_ingest_with_feedback(text)
            return

        stage = self._next_stage() or "Final"
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="user",
                to_agent="coordinator",
                message_type="question",
                stage=stage if stage in STAGES else "Research",
                priority="normal",
                timestamp=now_iso(),
                content=text,
            )
        )
        action, reply = await asyncio.to_thread(
            self.coordinator_engine.runtime_response,
            run_context=self._build_runtime_context(),
            user_message=text,
        )
        self._post_chat_message(
            ChatMessage(
                msg_id=str(uuid.uuid4()),
                from_agent="coordinator",
                to_agent="user",
                message_type="status",
                stage=stage if stage in STAGES else "Research",
                priority="normal",
                timestamp=now_iso(),
                content=reply,
            )
        )
        if action == "advance_stage":
            await self.action_advance_stage()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agentic Tasks TUI")
    parser.add_argument(
        "--input",
        default="data/input.txt",
        help="Path to input brief file (default: data/input.txt)",
    )
    parser.add_argument(
        "--db",
        default=".agentic_tasks.db",
        help="SQLite database file path (default: .agentic_tasks.db)",
    )
    parser.add_argument(
        "--run-id",
        help="Resume an existing run id from SQLite.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    app = AgenticTUI(
        input_path=Path(args.input),
        db_path=Path(args.db),
        resume_run_id=args.run_id,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
