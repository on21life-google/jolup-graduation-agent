"""Readable source labels without inventing publication dates or changing quotes.

Adapted from Kim Jumi's submitted RAG test expectations. Her implementation
package was absent; these helpers implement the expectations independently.
"""
from __future__ import annotations

from datetime import date
import re
import unicodedata


_DOCUMENT_TYPES = (
    ("schedule", ("학사일정",)),
    ("curriculum", ("교육과정", "요람")),
    ("course", ("강의시간표", "수강안내")),
    ("regulation", ("학칙", "규정", "졸업요건")),
)
_FILE_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})([-.]?)(\d{2})\2(\d{2})(?!\d)")
_SECTION = re.compile(
    r"^[ \t]*(제[ \t]*\d+[ \t]*조(?:의[ \t]*\d+)?(?:[ \t]*\([^\r\n)]+\))?)(?=[ \t]|$)",
    re.MULTILINE,
)


def normalize_text(text: str) -> str:
    """Normalize comparison text only; callers keep raw source excerpts."""
    return unicodedata.normalize("NFC", text)


def classify_document_type(filename: str) -> str:
    normalized = normalize_text(filename)
    for document_type, keywords in _DOCUMENT_TYPES:
        if any(keyword in normalized for keyword in keywords):
            return document_type
    return "other"


def filename_date_hint(filename: str) -> str | None:
    """A filename hint is never a verified written/registered/effective date."""
    match = _FILE_DATE.search(filename)
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[3]), int(match[4])).isoformat()
    except ValueError:
        return None


def extract_section(text: str) -> str | None:
    """Return a single actual line heading; mixed sections stay unlabeled."""
    headings = {match[1] for match in _SECTION.finditer(normalize_text(text))}
    return next(iter(headings)) if len(headings) == 1 else None


def section_for_chunk(page: str, chunk: str) -> str | None:
    """A chunk boundary must not turn an inline reference into a heading.

    Match in the complete page after the same whitespace normalization used by
    the chunker. Repeated excerpts or multiple headings have no single label.
    """
    page = re.sub(r"[ \t]+", " ", normalize_text(page))
    chunk = re.sub(r"[ \t]+", " ", normalize_text(chunk))
    start = page.find(chunk)
    if start < 0 or page.find(chunk, start + 1) >= 0:
        return None
    headings = {match[1] for match in _SECTION.finditer(page)
                if start <= match.start(1) and match.end(1) <= start + len(chunk)}
    return next(iter(headings)) if len(headings) == 1 else None
