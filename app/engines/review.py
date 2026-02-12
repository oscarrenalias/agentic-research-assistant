from __future__ import annotations

import json
import os
import re
from typing import Any

from app.domain.models import RUBRIC_DIMENSIONS, CITATION_MARKER_RE


def extract_citation_markers(text: str) -> list[str]:
    return CITATION_MARKER_RE.findall(text or "")


class ReviewEngine:
    def __init__(self) -> None:
        self.enabled = False
        self._chain = None
        self._init_error: str | None = None

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("REVIEW_MODEL", os.getenv("RESEARCH_MODEL", "gpt-4o-mini")).strip()
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
                            "You are a strict editorial reviewer. Return ONLY compact JSON with keys: "
                            "scores (object with keys factual_accuracy,evidence_quality,structure_and_coherence,"
                            "clarity_and_readability,tone_and_audience_fit,originality_and_insight; each 0-5 int), "
                            "issues (array of strings), revision_tasks (array of strings), "
                            "hard_gates (object with keys factual_accuracy_min,evidence_quality_min,no_fabricated_citations; booleans)."
                        ),
                    ),
                    (
                        "human",
                        (
                            "Objective: {objective}\nAudience: {audience}\nTone: {tone}\n"
                            "Source ids available: {source_ids}\n"
                            "Draft:\n{draft}\n"
                            "Evaluate using the rubric and hard gates."
                        ),
                    ),
                ]
            )
            llm = ChatOpenAI(model=model, temperature=0)
            self._chain = prompt | llm | StrOutputParser()
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

    def evaluate_draft(
        self,
        *,
        objective: str,
        audience: str,
        tone: str,
        draft: str,
        source_ids: list[str],
    ) -> dict[str, object]:
        markers = extract_citation_markers(draft)
        marker_ids = sorted(set(markers))
        unresolved = [marker for marker in marker_ids if marker.strip("[]") not in set(source_ids)]

        if not self.enabled or self._chain is None:
            facts = 4 if marker_ids else 2
            evidence = 4 if marker_ids and not unresolved else 2
            scores = {
                "factual_accuracy": facts,
                "evidence_quality": evidence,
                "structure_and_coherence": 3,
                "clarity_and_readability": 3,
                "tone_and_audience_fit": 3,
                "originality_and_insight": 3,
            }
            return self._finalize_review(scores=scores, issues=[], revision_tasks=[], unresolved_markers=unresolved)

        try:
            raw = self._chain.invoke(
                {
                    "objective": objective,
                    "audience": audience,
                    "tone": tone,
                    "source_ids": ", ".join(source_ids),
                    "draft": draft,
                }
            )
            parsed = self._parse_llm_json(raw)
            scores_raw = parsed.get("scores", {})
            scores = {dim: int(scores_raw.get(dim, 0)) for dim in RUBRIC_DIMENSIONS}
            issues = [str(x) for x in parsed.get("issues", [])]
            tasks = [str(x) for x in parsed.get("revision_tasks", [])]
            return self._finalize_review(scores=scores, issues=issues, revision_tasks=tasks, unresolved_markers=unresolved)
        except Exception:
            facts = 4 if marker_ids else 2
            evidence = 4 if marker_ids and not unresolved else 2
            scores = {
                "factual_accuracy": facts,
                "evidence_quality": evidence,
                "structure_and_coherence": 3,
                "clarity_and_readability": 3,
                "tone_and_audience_fit": 3,
                "originality_and_insight": 3,
            }
            return self._finalize_review(scores=scores, issues=[], revision_tasks=[], unresolved_markers=unresolved)

    @staticmethod
    def _finalize_review(
        *,
        scores: dict[str, int],
        issues: list[str],
        revision_tasks: list[str],
        unresolved_markers: list[str],
    ) -> dict[str, object]:
        normalized_scores = {dim: max(0, min(5, int(scores.get(dim, 0)))) for dim in RUBRIC_DIMENSIONS}
        total = sum(normalized_scores.values())
        hard_gates = {
            "factual_accuracy_min": normalized_scores["factual_accuracy"] >= 4,
            "evidence_quality_min": normalized_scores["evidence_quality"] >= 4,
            "no_fabricated_citations": len(unresolved_markers) == 0,
        }
        passed = total >= 24 and all(hard_gates.values())
        all_issues = list(issues)
        if unresolved_markers:
            all_issues.append(f"Unresolved citation markers: {', '.join(unresolved_markers)}")
        return {
            "scores": normalized_scores,
            "total_score": total,
            "hard_gates": hard_gates,
            "pass": passed,
            "issues": all_issues,
            "revision_tasks": revision_tasks,
            "unresolved_citation_markers": unresolved_markers,
        }


