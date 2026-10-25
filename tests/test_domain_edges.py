"""Boundary contracts for academic inputs, plan assumptions and source evidence."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from icalendar import Calendar

from jolup import academic, calendar, course_solver, evidence, planner
from jolup.samples import load_sample


def row(**changes):
    value = dict(row_id="r", course_id="c", course_name="과목", term="2026-1", category="전선", credits=3, grade="B0", status="completed", retake_of="", retake_approved=False)
    return dict(value, **changes)


@pytest.mark.parametrize("changes,issue", [
    ({"course_name": ""}, "과목명"), ({"course_name": "가" * 151}, "과목명"),
    ({"course_name": "가\n나"}, "제어 문자"), ({"category": "외부"}, "이수구분"),
    ({"status": "unknown"}, "상태"), ({"status": "in_progress", "grade": "A0"}, "확정 성적"),
    ({"retake_approved": "maybe"}, "true/false"), ({"retake_approved": True}, "원래 성적"),
])
def test_invalid_course_fields_have_actionable_errors(changes, issue):
    normalized, errors = academic.validate_courses([row(**changes)])
    assert normalized == [] and issue in errors[0]


@pytest.mark.parametrize("rows,issue", [(None, "2,000"), ([{}] * 2001, "2,000"), ([None], "객체"), ([row(), row()], "중복")])
def test_transcript_collection_constraints(rows, issue):
    _, errors = academic.validate_courses(rows)
    assert issue in errors[0]


CSV_HEADER = "course_name,term,category,credits,grade,status\n"
CSV_ROW = "과목,2026-1,전선,3,B0,completed\n"


@pytest.mark.parametrize("data,issue", [
    (None, "2MB"), ("x" * (2 * 1024 * 1024 + 1), "2MB"), (b"\xff", "인코딩"),
    (CSV_HEADER.replace("status", "course_name") + CSV_ROW, "중복된 열"),
    (CSV_HEADER + CSV_ROW * 2001, "2,000개 행"),
    (CSV_HEADER + CSV_ROW.rstrip() + ",extra\n", "열 수"),
    (CSV_HEADER, "과목이 없습니다"),
])
def test_csv_errors_do_not_accept_partial_rows(data, issue):
    result = academic.parse_transcript(data)
    assert result["ok"] is False and result["courses"] == []
    assert issue in result["errors"][0]


def test_cp949_and_clean_columns_are_supported_without_false_removal_warning():
    result = academic.parse_transcript((CSV_HEADER + CSV_ROW).encode("cp949"))
    assert result["ok"] and result["courses"][0]["course_name"] == "과목"
    assert result["removed_columns"] == result["warnings"] == []


def test_future_progress_and_missing_transcript_require_review():
    sample = load_sample("on_track")
    result = academic.check_requirements(sample["profile"], [row(status="in_progress", grade="IP")])
    assert "수강 중 과목의 학기가 현재 학기와 다릅니다." in result["review_items"]
    empty = academic.check_requirements(sample["profile"], [])
    assert "이수 내역이 없습니다. 성적표 확인 필요" in empty["review_items"]


def test_catalog_filters_regular_terms_and_never_invents_seasonal_offerings():
    catalog = planner.search_courses(term="2027-1")
    assert catalog and all(1 in c["offered_semesters"] for c in catalog)
    assert planner.search_courses(term="2027-S") == []


@pytest.mark.parametrize("target", ["2025-2", "2031-1"])
def test_target_outside_planning_horizon_is_a_review_not_a_plan(target):
    sample = load_sample("on_track")
    sample["profile"]["target_graduation_term"] = target
    plan = planner.plan_courses(sample["profile"], sample["courses"])
    assert plan["status"] == "확인 필요" and plan["semesters"] == []
    assert "4년" in plan["warnings"][0]


def test_academic_warning_reduces_cap_without_altering_current_records():
    sample = load_sample("on_track")
    sample["profile"]["academic_warning"] = True
    plan = planner.plan_courses(sample["profile"], sample["courses"])
    assert all(s["max_credits"] <= 15 for s in plan["semesters"])
    assert any("학사경고" in warning for warning in plan["warnings"])


def test_plan_reserves_only_attempted_workload():
    assert planner._annual_workload([row(), row(status="planned"), row(status="withdrawn")]) == {"2026": 3}


def test_failed_course_candidate_requires_future_retake_approval():
    candidate = dict(course_id="c", course_name="과목", category="전선", credits=3)
    clean = [row(row_id="older", grade="F", term="2024-1"), row(row_id="latest", grade="F", term="2025-1")]
    original = deepcopy(clean)
    selected, hypothetical, warning = planner._planned_course(candidate, "2027-1", clean, "부족분 보완")
    assert selected["approval_required"] is True
    assert hypothetical["retake_of"] == "latest" and hypothetical["retake_approved"] is True
    assert "현재 승인 완료를 뜻하지" in warning
    assert clean == original


def test_semester_retains_retake_note(monkeypatch):
    sample = load_sample("on_track")
    candidate = dict(course_id="c", course_name="과목", category="전선", credits=3)
    clean = [row(grade="F")]
    assumed = deepcopy(clean)
    selected, notes = planner._semester_courses(sample["profile"], "2027-1", [candidate], clean, assumed)
    assert selected[0]["approval_required"] and len(notes) == 1
    assert len(assumed) == 2 and len(clean) == 1


def test_candidate_source_is_added_once(monkeypatch):
    monkeypatch.setattr(planner, "sources", lambda: {"extra": {"source_id": "extra", "file_name": "public.txt"}})
    result = {"semesters": [{"courses": [{"source_ids": ["extra", "extra"]}]}], "evidence": []}
    planner._include_candidate_evidence(result)
    assert len(result["evidence"]) == len(result["meta"]) == 1
    assert result["meta"][0]["file_name"] == "public.txt"


def test_infeasible_plan_cannot_be_rescued_by_assumed_chapel_or_project(monkeypatch):
    sample = load_sample("on_track")
    monkeypatch.setattr(planner, "_select_future_courses", lambda *args: ({}, {"feasible": False, "optimal": False}, {"2027-1": 9, "2027-2": 9}))
    result = planner.plan_courses(sample["profile"], sample["courses"])
    assert result["status"] == "확인 필요"
    assert result["milestones"] == [] and result["current_totals"]["total"] == 90
    assert result["remaining_shortages"] and all(not s["projected_totals"] for s in result["semesters"])


@pytest.mark.parametrize("selected,annual,known,issue", [
    ({"2027-1": [{"course_id": "c", "credits": 3}]}, {"2027": 35}, set(), "annual"),
    ({"2027-1": [{"course_id": "c", "credits": 3, "prerequisites": ["before"]}]}, {}, set(), "prerequisite"),
])
def test_independent_solver_validation_rejects_false_success(monkeypatch, selected, annual, known, issue):
    candidate = selected["2027-1"][0] | {"category": "전선", "offered_semesters": [1]}
    monkeypatch.setattr(course_solver, "milp", lambda *a, **k: SimpleNamespace(x=[1, 0, 0, 0, 0], success=True))
    chosen, report = course_solver.choose_courses([candidate], ["2027-1"], 18, annual,
        {"total": 123, "general_required": 14, "general_elective": 20, "major_total": 63}, [], known, evidence.rules())
    assert chosen == {} and report["feasible"] is False and "검증에 실패" in report["reason"]


def test_missing_and_malformed_review_record_block_verdict(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence, "DATA", tmp_path)
    assert "기록이 없습니다" in evidence.validate_rule_bundle()[0]
    (tmp_path / "rules").mkdir()
    manifest = tmp_path / "rules/reviewed_bundle.json"
    manifest.write_text('{"files": [{"path":"outside.txt","sha256":"x"}]}')
    assert "파일이 없습니다" in evidence.validate_rule_bundle()[0]
    manifest.write_text("not json")
    assert "확인할 수 없습니다" in evidence.validate_rule_bundle()[0]


def test_calendar_handles_missing_manifest_and_empty_approved_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(calendar, "DATA", tmp_path)
    assert calendar.search_events() == []
    with pytest.raises(ValueError, match="선택한 일정"):
        calendar.create_calendar_event([], approved=True)


def test_calendar_rejects_reverse_range_and_exports_offline_source_without_url(monkeypatch):
    event = {"id": "offline", "title": "학사", "start_date": "2026-10-23", "end_date": "2026-10-22", "source_id": "school_rules", "url": None}
    monkeypatch.setattr(calendar, "search_events", lambda **kwargs: [event])
    with pytest.raises(ValueError, match="종료일"):
        calendar.create_calendar_event([event], approved=True)
    event["end_date"] = event["start_date"]
    parsed = Calendar.from_ical(calendar.create_calendar_event([event], approved=True))
    item = parsed.walk("VEVENT")[0]
    assert item.get("url") is None and "제공된 학교 자료" in str(item["description"])


def test_missing_csv_required_headers_name_the_missing_field():
    result = academic.parse_transcript("course_name,credits\n과목,3\n")
    assert not result["ok"] and "필수 열 누락" in result["errors"][0]
    assert "term" in result["errors"][0]


@pytest.mark.parametrize("cap", [True, 0, 19, 18.5, "18"])
def test_invalid_preferred_credit_limit_requires_review(cap):
    sample = load_sample("on_track")
    result = planner.plan_courses(sample["profile"], sample["courses"], preferences={"max_credits": cap})
    assert result["status"] == "확인 필요" and result["semesters"] == []
    assert "1~18의 정수" in result["warnings"][0]


def test_time_limited_plan_with_remaining_shortage_is_not_declared_impossible(monkeypatch):
    sample = load_sample("credit_shortfall")
    original = planner._select_future_courses
    def limited(*args):
        selected, report, minimums = original(*args)
        assert report["feasible"]
        return selected, dict(report, optimal=False), minimums
    monkeypatch.setattr(planner, "_select_future_courses", limited)
    result = planner.plan_courses(sample["profile"], sample["courses"])
    assert result["status"] == "확인 필요" and result["remaining_shortages"]
    assert "탐색이 완료되지" in result["answer"]
