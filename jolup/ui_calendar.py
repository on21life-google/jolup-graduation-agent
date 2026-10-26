"""Source-backed calendar presentation; no schedule or approval side effects."""
from __future__ import annotations

import calendar
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo


def today_seoul() -> date:
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def event_status(event: dict, today: date) -> str:
    """Inclusive school end dates determine upcoming, active, and ended states."""
    start = date.fromisoformat(event["start_date"])
    end = date.fromisoformat(event.get("end_date", event["start_date"]))
    if today < start:
        return f"D-{(start - today).days}"
    if today == start:
        return "오늘 시작"
    if today <= end:
        return "진행 중"
    return "종료"


def month_events(events: list[dict], year: int, month: int) -> list[dict]:
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return [event for event in events
            if date.fromisoformat(event["start_date"]) <= last
            and date.fromisoformat(event.get("end_date", event["start_date"])) >= first]


def event_rows(events: list[dict], today: date) -> list[dict]:
    return [{"일정": event["title"], "시작일": event["start_date"],
             "마지막 날": event.get("end_date", event["start_date"]),
             "상태": event_status(event, today), "근거 주소": event.get("url", "")}
            for event in events]


def _day_html(day: date, events: list[dict], month: int, today: date) -> str:
    active = [event for event in events if event["start_date"] <= day.isoformat()
              <= event.get("end_date", event["start_date"])]
    classes = " ".join(["outside" if day.month != month else "",
                        "today" if day == today else "", "has-event" if active else ""])
    labels = "".join(f'<span class="j-event"><abbr title="{escape(event["title"], quote=True)}">'
                     f'{escape(event["title"])}</abbr></span>' for event in active)
    return f'<td class="{classes}" aria-label="{day.isoformat()}"><b>{day.day}</b>{labels}</td>'


def calendar_html(events: list[dict], year: int, month: int, today: date) -> str:
    """Render real dates, including periods crossing month/year boundaries."""
    selected = month_events(events, year, month)
    weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(year, month)
    header = "".join(f'<th scope="col">{day}</th>' for day in "일월화수목금토")
    rows = "".join("<tr>" + "".join(_day_html(day, selected, month, today) for day in week) + "</tr>"
                   for week in weeks)
    return (f'<table class="j-calendar"><caption>{year}년 {month}월 학사 달력</caption>'
            f'<thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>')
