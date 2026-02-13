from __future__ import annotations

import json
import os
import re
from typing import Any

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
                            "findings (array of 4-5 objects). "
                            "Each findings item must have keys: claim (string), evidence_note (string), "
                            "confidence (float 0-1), risk_flags (array of strings). No markdown."
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
                            "Source reference: {source_ref}\n"
                            "Source material excerpt: {source_material}\n"
                            "Task: extract 4 to 5 distinct evidence-backed claims from this source material."
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

    @staticmethod
    def _normalize_findings(parsed: dict[str, Any]) -> list[dict[str, Any]]:
        findings_raw = parsed.get("findings", [])
        if not isinstance(findings_raw, list):
            findings_raw = []

        findings: list[dict[str, Any]] = []
        for item in findings_raw:
            if not isinstance(item, dict):
                continue
            confidence_raw = item.get("confidence", 0.5)
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                confidence = 0.5
            risk_flags_raw = item.get("risk_flags", [])
            if not isinstance(risk_flags_raw, list):
                risk_flags_raw = []
            findings.append(
                {
                    "claim": str(item.get("claim", "")).strip(),
                    "evidence_note": str(item.get("evidence_note", "")).strip(),
                    "confidence": confidence,
                    "risk_flags": [str(x) for x in risk_flags_raw],
                }
            )
            if len(findings) >= 5:
                break

        if findings:
            return findings

        # Backward-compatible fallback for legacy single-claim responses.
        confidence_raw = parsed.get("confidence", 0.5)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.5
        risk_flags_raw = parsed.get("risk_flags", [])
        if not isinstance(risk_flags_raw, list):
            risk_flags_raw = []
        return [
            {
                "claim": str(parsed.get("claim", "")).strip(),
                "evidence_note": str(parsed.get("evidence_note", "")).strip(),
                "confidence": confidence,
                "risk_flags": [str(x) for x in risk_flags_raw],
            }
        ]

    def analyze_source(
        self,
        *,
        source_ref: str,
        source_material: str,
        objective: str,
        audience: str,
        tone: str,
        constraints: list[str],
        task_objective: str,
        task_instructions: str,
    ) -> dict[str, Any]:
        if not self.enabled or self._chain is None:
            return {
                "findings": [
                    {
                        "claim": f"Potentially relevant source candidate: {source_ref[:140]}",
                        "evidence_note": "Fallback mode (no model configured).",
                        "confidence": 0.35,
                        "risk_flags": ["model_unavailable"],
                    }
                ],
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
                    "source_ref": source_ref,
                    "source_material": source_material[:6000],
                }
            )
            parsed = self._parse_llm_json(raw)
            return {
                "findings": self._normalize_findings(parsed),
            }
        except Exception:  # noqa: BLE001
            return {
                "findings": [
                    {
                        "claim": f"Potentially relevant source candidate: {source_ref[:140]}",
                        "evidence_note": "Fallback mode (research inference call failed).",
                        "confidence": 0.3,
                        "risk_flags": ["model_call_failed"],
                    }
                ],
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
