from __future__ import annotations

from app.domain.models import CITATION_MARKER_RE


def extract_citation_markers(text: str) -> list[str]:
    return CITATION_MARKER_RE.findall(text or "")


def build_source_index(evidence_pack: dict[str, object]) -> dict[str, dict[str, object]]:
    sources = evidence_pack.get("sources", [])
    if not isinstance(sources, list):
        return {}
    index: dict[str, dict[str, object]] = {}
    for item in sources:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id", "")).strip()
        if source_id:
            index[source_id] = item
    return index
