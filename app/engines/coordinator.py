from __future__ import annotations

import json
import os
import re
from typing import Any

from app.domain.models import NormalizedTaskPackage


class CoordinatorEngine:

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


