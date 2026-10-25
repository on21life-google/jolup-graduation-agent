"""One unchanged contract suite for the three learning-adapter stages.

Run explicitly with pytest; this name is deliberately outside normal collection.
Expectations are hand calculated, never taken from sample.expected or the adapter.
"""
from copy import deepcopy
from datetime import date
import importlib
import json
import os

from icalendar import Calendar
import pytest

from jolup import calendar
from jolup.rag import KnowledgeBase
from jolup.samples import load_sample


@pytest.fixture(scope="module")
def adapter():
    stage = os.environ.get("JOLUP_TDD_STAGE", "refactor")
    if stage not in {"red", "green", "refactor"}:
        raise ValueError("JOLUP_TDD_STAGE must be red, green, or refactor")
    return importlib.import_module(f"learning.tdd.stages.{stage}")


def _report(adapter, sample_id, regulation_result=None):
    sample = load_sample(sample_id)
    return adapter.build_learning_report(sample["profile"], sample["courses"], regulation_result)


def _area(rows, name):
    return next(row for row in rows if row["area"] == name)


def test_current_and_pass_projection_are_separate(adapter):
    result = _report(adapter, "on_track")
    assert result["current"]["credits"] == 90
    assert result["projection"]["credits"] == 105
    assert result["current"]["status"] == "미충족"
    assert result["current"]["is_assumption"] is False
    assert result["projection"]["is_assumption"] is True
    assert result["answer"] and result["reason"] and result["evidence"] and result["meta"]
    assert result["official_graduation_confirmed"] is False


def test_three_in_progress_credits_do_not_create_current_pass(adapter):
    result = _report(adapter, "in_progress")
    assert result["current"]["credits"] == 120
    assert result["projection"]["credits"] == 123
    assert result["current"]["status"] == "미충족"
    assert result["projection"]["status"] == "충족"


def test_all_requirements_met_is_still_not_official_graduation(adapter):
    result = _report(adapter, "fully_met")
    assert result["current"]["credits"] == 123
    assert result["current"]["status"] == "충족"
    assert all(row["shortage"] == 0 for row in result["reason"])
    assert result["official_graduation_confirmed"] is False


def test_large_shortfall_does_not_become_a_positive_plan(adapter):
    result = _report(adapter, "credit_shortfall")
    assert result["current"]["credits"] == 38
    assert result["projection"]["credits"] == 53
    assert result["readiness"]["status"] == "위험"
    assert result["readiness"]["remaining_shortages"]


def test_missing_record_stays_unknown_instead_of_zero(adapter):
    result = _report(adapter, "missing_record")
    row = _area(result["reason"], "채플")
    assert result["current"]["status"] == "확인 필요"
    assert row["earned"] is None
    assert row["shortage"] is None
    assert row["status"] == "확인 필요"
    assert result["readiness"]["status"] == "확인 필요"


def test_total_123_does_not_replace_a_required_course(adapter):
    result = _report(adapter, "missing_core")
    assert result["current"]["credits"] == 123
    assert result["current"]["status"] == "미충족"
    assert _area(result["reason"], "필수 교양과목")["shortage"] == 1


def test_timeline_uses_each_real_semester_and_labels_assumptions(adapter):
    result = _report(adapter, "on_track")
    assert [(row["stage"], row["term"], row["credits"]) for row in result["timeline"]] == [
        ("current", "2026-2", 90),
        ("in_progress_passed", "2026-2", 105),
        ("planned_semester", "2027-1", 114),
        ("planned_semester", "2027-2", 123),
    ]
    assert [row["is_assumption"] for row in result["timeline"]] == [False, True, True, True]
    assert [term["total_credits"] for term in result["readiness"]["semesters"]] == [9, 9]
    assert result["readiness"]["status"] == "조건부 가능"
    assert result["readiness"]["assumptions"]


def test_noncredit_values_are_not_automatically_completed(adapter):
    result = _report(adapter, "on_track")
    for snapshot in [result["current"], result["projection"], *result["timeline"][2:]]:
        assert _area(snapshot["rows"], "채플")["earned"] == 5
        assert _area(snapshot["rows"], "졸업작품 심사")["earned"] == 0


def test_report_does_not_mutate_or_alias_student_inputs(adapter):
    sample = load_sample("on_track")
    before = deepcopy((sample["profile"], sample["courses"]))
    result = adapter.build_learning_report(sample["profile"], sample["courses"])
    assert (sample["profile"], sample["courses"]) == before
    result["reason"][0]["earned"] = -100
    result["current"]["rows"][0]["earned"] = -200
    result["readiness"]["semesters"][0]["courses"][0]["credits"] = -300
    assert (sample["profile"], sample["courses"]) == before


def test_real_lexical_rag_citations_and_unknown_dates_are_preserved(adapter):
    sample = load_sample("on_track")
    kb = KnowledgeBase(mode="lexical")
    kb.build_index()
    regulations = kb.answer("졸업학점은 총 몇 학점인가요?", sample["profile"])
    original = deepcopy(regulations)
    result = adapter.build_learning_report(sample["profile"], sample["courses"], regulations)
    assert result["regulation"]["status"] == "근거 확인"
    assert result["regulation"] == original
    for item in original["evidence"]:
        assert any(all(meta[key] == item.get(key) for key in
                       ("source_id", "file_name", "url", "page", "registered_date"))
                   for meta in result["meta"])
    school = [item for item in result["meta"] if item["source_id"] == "school_rules"]
    assert school and all(item["registered_date"] is None for item in school)
    result["regulation"]["evidence"][0]["file_name"] = "changed only in returned report"
    assert regulations == original


def test_missing_rag_sources_remain_unconfirmed(adapter, tmp_path):
    manifest = tmp_path / "sources.json"
    manifest.write_text('{"sources": []}', encoding="utf-8")
    regulation = KnowledgeBase(manifest, mode="lexical").answer("졸업학점 123인가요?")
    result = _report(adapter, "on_track", regulation)
    assert result["regulation"]["status"] == "확인 필요"
    assert result["regulation"]["evidence"] == []
    assert result["regulation"]["meta"] == []


@pytest.fixture
def events(tmp_path, monkeypatch):
    rows = [{"id": "learning-year-end", "title": "가상 성적 정정 기간",
             "start_date": "2026-12-29", "end_date": "2026-12-31",
             "source_id": "guide", "url": "https://www.sungkyul.ac.kr/"}]
    (tmp_path / "events.json").write_text(json.dumps({"events": rows}), encoding="utf-8")
    monkeypatch.setattr(calendar, "DATA", tmp_path)
    return rows


@pytest.mark.parametrize("approval", [False, None, 1, "true"])
def test_calendar_requires_explicit_boolean_approval(adapter, events, approval):
    with pytest.raises(PermissionError):
        adapter.export_reviewed_events(events, approved=approval)


def test_calendar_inclusive_last_day_has_exclusive_ics_end(adapter, events):
    raw = adapter.export_reviewed_events(events, approved=True)
    event = Calendar.from_ical(raw).walk("VEVENT")[0]
    assert event.decoded("dtstart") == date(2026, 12, 29)
    assert event.decoded("dtend") == date(2027, 1, 1)
    assert str(event["summary"]) == "가상 성적 정정 기간"


@pytest.mark.parametrize("alteration", [{"start_date": "2028-01-01"}, {"id": "unknown"}, {"title": "changed"}])
def test_calendar_rejects_modified_or_unknown_events(adapter, events, alteration):
    with pytest.raises(ValueError):
        adapter.export_reviewed_events([{**events[0], **alteration}], approved=True)


def test_repeated_calendar_export_is_stable_and_deduplicated(adapter, events):
    before = deepcopy(events)
    first = adapter.export_reviewed_events(events + events, approved=True)
    second = adapter.export_reviewed_events(events, approved=True)
    assert first == second
    assert len(Calendar.from_ical(first).walk("VEVENT")) == 1
    assert events == before


def test_calendar_empty_selection_is_not_exported(adapter):
    with pytest.raises(ValueError):
        adapter.export_reviewed_events([], approved=True)
