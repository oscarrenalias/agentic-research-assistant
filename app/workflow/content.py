from __future__ import annotations

import os
from typing import TYPE_CHECKING

from app.domain.models import PUBLISHED_DATE_RE, URL_RE
from app.services.citations import build_source_index, extract_citation_markers
from app.services.sources import maybe_url

if TYPE_CHECKING:
    from app.tui import AgenticTUI


def evidence_pack(app: AgenticTUI) -> dict[str, object]:
    if not app.state:
        return {"sources": [], "claims": []}
    payload = app.state.artifacts.get("evidence_pack", {})
    return payload if isinstance(payload, dict) else {"sources": [], "claims": []}


def outline_stage_output(app: AgenticTUI) -> dict[str, object]:
    evidence = evidence_pack(app)
    claims = evidence.get("claims", [])
    claims_list = claims if isinstance(claims, list) else []
    objective = app.package.objective if app.package else ""
    audience = app.package.audience if app.package else ""
    tone = app.package.tone if app.package else ""
    return app.writing_engine.create_outline(
        objective=objective,
        audience=audience,
        tone=tone,
        claims=[c for c in claims_list if isinstance(c, dict)],
    )


def draft_stage_output(app: AgenticTUI) -> str:
    evidence = evidence_pack(app)
    claims = evidence.get("claims", [])
    claims_list = [c for c in claims if isinstance(c, dict)] if isinstance(claims, list) else []
    outline = app.state.artifacts.get("approved_outline", {}) if app.state else {}
    outline_payload = outline if isinstance(outline, dict) else {}
    objective = app.package.objective if app.package else ""
    audience = app.package.audience if app.package else ""
    tone = app.package.tone if app.package else ""
    constraints = app.package.constraints if app.package else []
    return app.writing_engine.create_draft(
        objective=objective,
        audience=audience,
        tone=tone,
        constraints=constraints,
        outline=outline_payload,
        claims=claims_list,
    )


def validate_citation_integrity(app: AgenticTUI, draft_text: str, evidence_pack_payload: dict[str, object]) -> dict[str, object]:
    source_index = build_source_index(evidence_pack_payload)
    markers = sorted(set(extract_citation_markers(draft_text)))
    cited_source_ids = [marker.strip("[]") for marker in markers]
    errors: list[str] = []

    unresolved = [sid for sid in cited_source_ids if sid not in source_index]
    if unresolved:
        errors.append(f"Unresolved citation markers: {', '.join(unresolved)}")

    known_urls = {
        str(source.get("url", "")).strip()
        for source in source_index.values()
        if isinstance(source, dict) and str(source.get("url", "")).strip()
    }
    for url in URL_RE.findall(draft_text):
        if url not in known_urls:
            errors.append(f"Draft contains URL not present in evidence pack: {url}")

    required_fields = ("title", "url", "retrieved_at")
    for source_id in cited_source_ids:
        source = source_index.get(source_id)
        if not isinstance(source, dict):
            continue
        missing_fields = [field for field in required_fields if not str(source.get(field, "")).strip()]
        if missing_fields:
            errors.append(f"Source {source_id} missing required fields: {', '.join(missing_fields)}")
        url = str(source.get("url", "")).strip()
        if url and maybe_url(url) is None:
            errors.append(f"Source {source_id} has invalid URL: {url}")
        published_at = str(source.get("published_at", "")).strip()
        if published_at and not PUBLISHED_DATE_RE.match(published_at):
            errors.append(f"Source {source_id} has invalid published_at format: {published_at}")
        title = str(source.get("title", "")).strip().lower()
        if any(token in title for token in ("placeholder", "unknown", "n/a", "todo")):
            errors.append(f"Source {source_id} title looks fabricated or placeholder-like.")

    return {
        "pass": len(errors) == 0,
        "cited_source_ids": cited_source_ids,
        "unresolved_source_ids": unresolved,
        "errors": errors,
    }


def critique_for_draft(app: AgenticTUI, draft_text: str) -> dict[str, object]:
    evidence = evidence_pack(app)
    source_index = build_source_index(evidence)
    source_ids = sorted(source_index.keys())
    objective = app.package.objective if app.package else ""
    audience = app.package.audience if app.package else ""
    tone = app.package.tone if app.package else ""
    review = app.review_engine.evaluate_draft(
        objective=objective,
        audience=audience,
        tone=tone,
        draft=draft_text,
        source_ids=source_ids,
    )
    citation_validation = validate_citation_integrity(app, draft_text, evidence)
    review["citation_validation"] = citation_validation
    if not bool(citation_validation.get("pass", False)):
        hard_gates = review.get("hard_gates", {})
        if not isinstance(hard_gates, dict):
            hard_gates = {}
        hard_gates["no_fabricated_citations"] = False
        review["hard_gates"] = hard_gates
        issues = review.get("issues", [])
        if not isinstance(issues, list):
            issues = []
        for err in citation_validation.get("errors", []):
            issues.append(str(err))
        review["issues"] = issues
        total = int(review.get("total_score", 0))
        review["pass"] = bool(total >= 24 and all(bool(v) for v in hard_gates.values()))
    return review


def critique_stage_output(app: AgenticTUI) -> dict[str, object]:
    draft_payload = app.state.artifacts.get("first_draft", "") if app.state else ""
    draft_text = str(draft_payload)
    return critique_for_draft(app, draft_text)


def revise_stage_output(app: AgenticTUI) -> dict[str, object]:
    if not app.state:
        return {"revised_draft": "", "changelog": [], "passes_quality_gate": False}
    draft_text = str(app.state.artifacts.get("first_draft", ""))
    critique_payload = app.state.artifacts.get("critique_feedback", {})
    critique = critique_payload if isinstance(critique_payload, dict) else {}
    evidence = evidence_pack(app)
    claims = evidence.get("claims", [])
    claims_list = [c for c in claims if isinstance(c, dict)] if isinstance(claims, list) else []
    objective = app.package.objective if app.package else ""
    audience = app.package.audience if app.package else ""
    tone = app.package.tone if app.package else ""
    constraints = app.package.constraints if app.package else []

    passes_gate = bool(critique.get("pass", False))
    changelog: list[str] = []
    revision_attempts = 0
    max_rounds = max(1, int(os.getenv("MAX_REVISION_ROUNDS", "3")))
    current_draft = draft_text
    current_critique = critique

    while not passes_gate and revision_attempts < max_rounds:
        revision_attempts += 1
        revised = app.writing_engine.revise_draft(
            objective=objective,
            audience=audience,
            tone=tone,
            constraints=constraints,
            draft=current_draft,
            critique=current_critique,
            claims=claims_list,
        )
        current_draft = str(revised.get("revised_draft", current_draft))
        for item in revised.get("changelog", []):
            changelog.append(str(item))
        current_critique = critique_for_draft(app, current_draft)
        passes_gate = bool(current_critique.get("pass", False))

    if not changelog:
        changelog = ["No revision changes required; critique already passed."]

    app._persist_artifact("critique_feedback", current_critique)
    return {
        "revised_draft": current_draft,
        "changelog": changelog,
        "revision_attempts": revision_attempts,
        "passes_quality_gate": passes_gate,
        "final_critique": current_critique,
    }


def final_stage_output(app: AgenticTUI) -> dict[str, object]:
    if not app.state:
        return {"post_text": "", "references": []}
    revised_payload = app.state.artifacts.get("revised_draft", "")
    draft_text = ""
    if isinstance(revised_payload, dict):
        draft_text = str(revised_payload.get("revised_draft", ""))
    if not draft_text:
        draft_text = str(app.state.artifacts.get("first_draft", ""))

    evidence = evidence_pack(app)
    citation_validation = validate_citation_integrity(app, draft_text, evidence)
    if not bool(citation_validation.get("pass", False)):
        errors = citation_validation.get("errors", [])
        detail = "; ".join([str(err) for err in errors[:4]])
        raise ValueError(f"Citation integrity check failed: {detail}")

    source_index = build_source_index(evidence)
    markers = sorted(set(extract_citation_markers(draft_text)))
    references: list[dict[str, str]] = []
    for marker in markers:
        source_id = marker.strip("[]")
        source = source_index.get(source_id, {})
        references.append(
            {
                "source_id": source_id,
                "title": str(source.get("title", "")),
                "url": str(source.get("url", "")),
            }
        )
    return {
        "post_text": draft_text,
        "references": references,
        "citation_validation": citation_validation,
    }
