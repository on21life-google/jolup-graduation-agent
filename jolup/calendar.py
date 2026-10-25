"""Confirmed academic dates → review → explicitly approved local ICS export."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from typing import Any

from icalendar import Calendar, Event

from jolup.settings import DATA


def _events() -> list[dict[str, Any]]:
    path = DATA / "events.json"
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    return value.get("events", []) if isinstance(value, dict) else value


def search_events(query: str = "", year: int | None = 2026) -> list[dict]:
    """Return only supplied, source-backed dates; never invent a future-year date."""
    normalize = lambda value: re.sub(r"[\s·ㆍ-]", "", str(value).lower())
    query = normalize(query)
    synonyms = {"졸업": ["졸업", "학위수여"], "졸업식": ["학위수여", "졸업식"],
                "수강철회": ["수강과목철회", "수강철회"], "전과": ["전부과", "전과"],
                "개강": ["개강", "학기개시", "학기시작"], "종강": ["종강", "학기종료", "학기종강"]}
    terms = synonyms.get(query, [query])
    result = []
    for item in _events():
        start = date.fromisoformat(item["start_date"])
        end = date.fromisoformat(item.get("end_date", item["start_date"]))
        if year is not None and not start.year <= int(year) <= end.year:
            continue
        searchable = " ".join(str(item.get(k, "")) for k in ("title", "description", "tags"))
        if query and not any(term in normalize(searchable) for term in terms):
            continue
        result.append(dict(item, end_date=end.isoformat()))
    return sorted(result, key=lambda x: (x["start_date"], x["id"]))


def preview_events(event_ids: list[str]) -> dict:
    known = {x["id"]: x for x in search_events(year=None)}
    unique = list(dict.fromkeys(event_ids))
    unknown = [x for x in unique if x not in known]
    events = [known[x] for x in unique if x in known]
    return {
        "status": "확인 필요" if unknown or not events else "미리보기",
        "events": events,
        "unknown_ids": unknown,
        "approval_required": True,
        "message": "표시한 날짜를 확인하고 승인하면 ICS 파일을 만듭니다. 외부 캘린더에는 직접 등록하지 않습니다.",
    }


def _validated_events(events: list[dict]) -> list[dict]:
    """Require the selected preview to match the source, then remove duplicate IDs."""
    known = {x["id"]: x for x in search_events(year=None)}
    validated = []
    seen = set()
    for item in events:
        original = known.get(item.get("id"))
        if not original:
            raise ValueError("학교 자료에서 확인되지 않은 일정입니다.")
        for key in ("title", "start_date", "end_date", "source_id", "url"):
            if item.get(key) != original.get(key):
                raise ValueError("미리보기와 일정 내용이 다릅니다. 다시 선택해 확인하세요.")
        if original["id"] not in seen:
            validated.append(original)
            seen.add(original["id"])
    return validated


def _calendar_component(item: dict) -> Event:
    start = date.fromisoformat(item["start_date"])
    final_day = date.fromisoformat(item["end_date"])
    if final_day < start:
        raise ValueError("일정 종료일이 시작일보다 빠릅니다.")
    event = Event()
    event.add("uid", sha256(f"sungkyul:{item['id']}".encode()).hexdigest()[:32] + "@jolup.local")
    event.add("summary", item["title"])
    event.add("dtstart", start)
    event.add("dtend", final_day + timedelta(days=1))
    event.add("dtstamp", datetime.combine(start, time.min, tzinfo=timezone.utc))
    event.add("description", f"출처: {item.get('source_id', '')}\n{item.get('url') or '제공된 학교 자료'}\n학교의 변경 공지를 확인하세요.")
    if item.get("url"):
        event.add("url", item["url"])
    event.add("transp", "TRANSPARENT")
    return event


def create_calendar_event(events: list[dict], approved: bool = False) -> bytes:
    """Export a source-validated event set, only after explicit caller approval.

    The inclusive final date shown to the user becomes exclusive DTEND in ICS.
    Stable UID + deterministic timestamps make repeated downloads idempotent.
    """
    if approved is not True:
        raise PermissionError("일정 미리보기 확인 후 명시적인 승인이 필요합니다.")
    if not events:
        raise ValueError("선택한 일정이 없습니다.")
    validated = _validated_events(events)
    cal = Calendar()
    cal.add("prodid", "-//Jolup Roadmap//Sungkyul 2024 CS//KO")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    for item in validated:
        cal.add_component(_calendar_component(item))
    return cal.to_ical()
