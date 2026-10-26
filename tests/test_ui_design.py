"""The teammate design renders real results without changing their meaning."""
import base64
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from jolup import ui_calendar, ui_design


class CalendarCells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.days = {}

    def handle_starttag(self, tag, attrs):
        if tag == "td":
            values = dict(attrs)
            self.days[values["aria-label"]] = values.get("class", "")


@pytest.fixture
def events():
    return [
        {"title": "연말 계절학기", "start_date": "2026-12-29", "end_date": "2027-01-04", "url": "https://school.example/calendar"},
        {"title": "하루 일정", "start_date": "2027-01-02"},
        {"title": "다음 달", "start_date": "2027-02-01", "end_date": "2027-02-01"},
    ]


@pytest.mark.parametrize(("today", "expected"), [
    (date(2026, 12, 28), "D-1"), (date(2026, 12, 29), "오늘 시작"),
    (date(2027, 1, 4), "진행 중"), (date(2027, 1, 5), "종료"),
])
def test_dday_uses_inclusive_official_period(events, today, expected):
    assert ui_calendar.event_status(events[0], today) == expected


def test_month_filter_keeps_cross_year_period_and_one_day_event(events):
    january = ui_calendar.month_events(events, 2027, 1)
    assert [item["title"] for item in january] == ["연말 계절학기", "하루 일정"]
    assert ui_calendar.month_events(events, 2026, 11) == []
    rows = ui_calendar.event_rows(january, date(2027, 1, 2))
    assert rows[0]["상태"] == "진행 중"
    assert rows[1]["마지막 날"] == "2027-01-02"
    assert rows[1]["근거 주소"] == ""


def test_calendar_uses_real_weekdays_and_escapes_source_titles(events):
    events[1]["title"] = '<script>fake</script> "title"'
    html = ui_calendar.calendar_html(events, 2027, 1, date(2027, 1, 2))
    assert '<script>' not in html and "&lt;script&gt;" in html and "&quot;title&quot;" in html
    parsed = CalendarCells()
    parsed.feed(html)
    assert list(parsed.days)[0] == "2026-12-27"  # Sunday, before Friday Jan 1.
    assert "outside" in parsed.days["2026-12-31"]
    assert "has-event" in parsed.days["2026-12-31"]
    assert "today" in parsed.days["2027-01-02"]
    assert "has-event" in parsed.days["2027-01-04"]
    assert "has-event" not in parsed.days["2027-01-05"]
    assert "다음 달" not in html


def test_empty_calendar_and_korean_today():
    html = ui_calendar.calendar_html([], 2026, 10, date(2026, 10, 26))
    assert "2026년 10월 학사 달력" in html
    assert 'class="j-event"' not in html
    assert isinstance(ui_calendar.today_seoul(), date)


def test_design_reuses_teammate_assets_without_static_student_statistics():
    for name in ("rabbit-avatar.png", "rabbit-hero.png"):
        uri = ui_design.image_uri(name)
        assert base64.b64decode(uri.split(",", 1)[1]) == (ui_design.ASSETS / name).read_bytes()
    hero = ui_design.hero_html()
    assert "우수진 디자인" in hero and "졸업각" in hero and "CSV" in hero
    for obsolete in ("85", "130학점", "알렉스", "15학점 더"):
        assert obsolete not in hero.replace(ui_design.image_uri("rabbit-avatar.png"), "").replace(ui_design.image_uri("rabbit-hero.png"), "")
    assert "#fff8e1" in ui_design.theme_css()
    assert "done" in ui_design.steps_html(True)
    assert "done" not in ui_design.steps_html(False)
    assert "&lt;script&gt;" in ui_design.panel_title("<script>")


def test_requirement_progress_never_claims_unknown_as_earned_or_overflows():
    rows = [{"area": "학점 <합계>", "unit": "학점", "required": 123, "earned": 126, "status": "충족"},
            {"area": "전공", "unit": "학점", "required": 63, "earned": None},
            {"area": "교양", "unit": "학점", "required": 20, "earned": -1},
            {"area": "채플", "unit": "학기", "required": 6, "earned": 6},
            {"area": "없는영역", "unit": "학점", "required": 0, "earned": 0}]
    html = ui_design.requirement_cards(rows)
    assert "126 / 123학점" in html and "width:100.0%" in html
    assert "확인 필요" in html and "None" not in html
    assert "width:0.0%" in html
    assert "&lt;합계&gt;" in html
    assert "채플" not in html and "없는영역" not in html


def test_real_ui_keeps_four_tabs_and_official_month_switching():
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=45).run()
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["진단·수강 계획", "대화로 확인", "학사일정", "계산 과정·출처"]
    app.selectbox(key="calendar_year").select(2027).run()
    assert not app.exception
    app.selectbox(key="calendar_month_2027").select(12).run()
    assert not app.exception
    assert any("이 달에 확인된 학교 일정이 없습니다" in item.value for item in app.info)
    assert app.multiselect(key="calendar_selection_2027")
    # Calendar browsing alone cannot approve or download a calendar.
    assert not any(item.key.startswith("approve_") for item in app.checkbox)
    assert not any(item.label == "승인한 일정 ICS 받기" for item in app.download_button)


def _button(app, label):
    return next(item for item in app.button if item.label == label)


def test_interest_list_filters_adds_real_credits_removes_and_does_not_change_plan():
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=45).run()
    original_plan = app.session_state["plan"]
    original_diagnosis = app.session_state["diagnosis"]
    app.selectbox(key="interest_category").select("교필").run()
    app.selectbox(key="interest_semester").select("1학기").run()
    app.text_input(key="interest_query").input("영어").run()
    options = app.multiselect(key="interest_add").options
    assert options and all("영어" in label and "교필" in label for label in options)
    # GE003 is a real two-credit candidate; never assume every course is 3 credits.
    app.multiselect(key="interest_add").select("GE003").run()
    _button(app, "선택한 과목 관심 목록에 담기").click().run()
    _button(app, "선택한 과목 관심 목록에 담기").click().run()
    assert app.session_state["interest_selected_ids"] == ["GE003"]
    assert any("합계 2학점" in item.value for item in app.markdown)
    assert app.session_state["plan"] == original_plan
    assert app.session_state["diagnosis"] == original_diagnosis
    app.multiselect(key="interest_remove").select("GE003").run()
    _button(app, "선택한 과목 관심 목록에서 빼기").click().run()
    assert not app.session_state["interest_selected_ids"]
    app.text_input(key="interest_query").input("존재하지않는과목").run()
    assert any("검색 조건에 맞는" in item.value for item in app.info)
    assert not app.exception


def test_interest_list_resets_when_student_conditions_change():
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=45).run()
    app.multiselect(key="interest_add").select("GE001").run()
    _button(app, "선택한 과목 관심 목록에 담기").click().run()
    assert app.session_state["interest_selected_ids"] == ["GE001"]
    app.number_input(key="profile_chapel").set_value(6)
    _button(app, "학생 조건 적용").click().run()
    assert app.session_state["interest_selected_ids"] == []
    assert app.multiselect(key="interest_add").value == []
    assert not app.exception


def test_source_filename_hint_does_not_become_a_registration_date():
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=45).run()
    source = {"file_name": "학칙20260506.pdf", "page": 1, "registered_date": None,
              "version_date_hint": "2026-05-06", "document_type": "regulation", "section": "제2조"}
    app.session_state["messages"] = [{"query": "학칙 근거", "result": {
        "answer": "확인한 학교 근거", "rows": [], "evidence": [source], "meta": [source]}}]
    app.run()
    assert not app.exception
    assert any("파일명 날짜(참고): 2026-05-06" in item.value for item in app.caption)
    source_tables = [item.value for item in app.dataframe if "파일명 날짜(참고)" in item.value.columns]
    assert any(((table["등록일"] == "미확인") & (table["파일명 날짜(참고)"] == "2026-05-06")).any()
               for table in source_tables)


def test_school_revision_refreshes_calculations_and_removes_stale_interests(monkeypatch):
    from jolup import conversation, mcp_client, planner
    revision = {"value": "original"}
    calls = []
    original_check, original_search = mcp_client.check_requirements, planner.search_courses

    def counted_check(*args, **kwargs):
        calls.append(revision["value"])
        return original_check(*args, **kwargs)

    def revised_catalog(*args, **kwargs):
        courses = original_search(*args, **kwargs)
        return [course for course in courses
                if revision["value"] == "original" or course["course_id"] != "GE001"]

    monkeypatch.setattr(conversation, "school_revision", lambda: revision["value"])
    monkeypatch.setattr(mcp_client, "check_requirements", counted_check)
    monkeypatch.setattr(planner, "search_courses", revised_catalog)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=45).run()
    app.multiselect(key="interest_add").select("GE001").run()
    _button(app, "선택한 과목 관심 목록에 담기").click().run()
    assert app.session_state["interest_selected_ids"] == ["GE001"]
    before = app.session_state["calculation_fingerprint"]
    assert calls == ["original"]
    revision["value"] = "new-school-catalog"
    app.run()
    assert not app.exception  # Removed IDs must not crash the updated catalog.
    assert app.session_state["calculation_fingerprint"] != before
    assert app.session_state["interest_selected_ids"] == []
    assert calls == ["original", "new-school-catalog"]
