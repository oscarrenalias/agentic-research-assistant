from __future__ import annotations

import html
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

from app.domain.models import now_iso


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


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _extract_html_text(raw_html: str) -> tuple[str, str, str]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, flags=re.IGNORECASE | re.DOTALL)
    title = html.unescape(_clean_text(title_match.group(1))) if title_match else ""

    published_match = re.search(
        r"""<meta[^>]+(?:property|name)=["'](?:article:published_time|publishdate|datePublished|pubdate)["'][^>]+content=["']([^"']+)["']""",
        raw_html,
        flags=re.IGNORECASE,
    )
    published_at = _clean_text(published_match.group(1)) if published_match else ""

    no_scripts = re.sub(r"<script\b[^<]*(?:(?!</script>)<[^<]*)*</script>", " ", raw_html, flags=re.IGNORECASE)
    no_styles = re.sub(r"<style\b[^<]*(?:(?!</style>)<[^<]*)*</style>", " ", no_scripts, flags=re.IGNORECASE)
    no_tags = re.sub(r"<[^>]+>", " ", no_styles)
    text = html.unescape(_clean_text(no_tags))
    return title, published_at, text


def fetch_source_material(source_ref: str, *, timeout_s: float = 8.0) -> dict[str, str]:
    retrieved_at = now_iso()
    url = maybe_url(source_ref)
    if not url:
        return {
            "source_ref": source_ref,
            "url": "",
            "title": _clean_text(source_ref)[:120],
            "publisher": "",
            "published_at": "",
            "retrieved_at": retrieved_at,
            "source_material": _clean_text(source_ref)[:4000],
            "fetch_status": "inline_text",
        }

    parsed = urlparse(url)
    publisher = parsed.netloc.lower()
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; AgenticTasksResearch/0.1; +https://example.local)"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            raw_bytes = response.read(250_000)
            content_type = response.headers.get("Content-Type", "").lower()
        text = raw_bytes.decode("utf-8", errors="replace")

        title = ""
        published_at = ""
        if "html" in content_type or "<html" in text[:500].lower():
            title, published_at, extracted = _extract_html_text(text)
        else:
            extracted = _clean_text(text)

        if not extracted:
            extracted = _clean_text(source_ref)

        return {
            "source_ref": source_ref,
            "url": url,
            "title": title[:160] if title else _clean_text(source_ref)[:120],
            "publisher": publisher,
            "published_at": published_at[:50],
            "retrieved_at": retrieved_at,
            "source_material": extracted[:8000],
            "fetch_status": "fetched",
        }
    except (TimeoutError, urllib.error.URLError, ValueError):
        return {
            "source_ref": source_ref,
            "url": url,
            "title": _clean_text(source_ref)[:120],
            "publisher": publisher,
            "published_at": "",
            "retrieved_at": retrieved_at,
            "source_material": _clean_text(source_ref)[:4000],
            "fetch_status": "fetch_failed",
        }
