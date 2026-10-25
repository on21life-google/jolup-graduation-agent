"""Independent, hand-calculated checks for the graduation calculation module.

The 123-credit fixture is built from the published 14/20/63/123 boundaries,
not from the calculator's rules or sample-data generator.  Names prefixed 검증
are fictional transcript entries, not claims about actual course offerings.
"""
from copy import deepcopy

import pytest

from jolup.academic import check_requirements, parse_transcript, search_scores
from jolup.planner import plan_courses


def row(identifier, credits=3, *, name=None, term="2024-1", category="전선",
        grade="A0", status="completed", retake_of="", approved=False):
    return dict(row_id=identifier, course_id=identifier, course_name=name or f"검증과목 {identifier}",
                term=term, category=category, credits=credits, grade=grade, status=status,
                retake_of=retake_of, retake_approved=approved)


def profile(**changes):
    base = dict(school="성결대학교", department="컴퓨터공학과", entry_year=2024,
                track="전공심화", enrollment_type="신입학", engineering_accreditation=False,
                current_term="2027-2", target_graduation_term="2027-2",
                registered_semesters=8, completed_semesters=8, chapel_pass_semesters=6,
                graduation_project_status="통과", is_dummy=True)
    return {**base, **changes}


def finished_transcript():
    general = [("기독교로의초대", 3), ("신앙으로의초대", 3),
               ("영어커뮤니케이션(1)", 2), ("영어커뮤니케이션(2)", 2),
               ("기초글쓰기", 2), ("대학생활과진로", 1), ("사회봉사", 1)]
    records = [row(f"G{i}", credit, name=name, category="교필") for i, (name, credit) in enumerate(general)]
    records += [row(f"E{i}", credit, category="교선") for i, credit in enumerate([3, 3, 3, 3, 3, 3, 2])]
    records += [row("CAP1", name="전공종합설계(1)", category="전필"),
                row("CAP2", name="전공종합설계(2)", category="전필")]
    records += [row(f"M{i}") for i in range(19)]
    records += [row(f"O{i}", credit, category="일선") for i, credit in enumerate([3, 3, 3, 3, 3, 3, 3, 3, 2])]
    # A plausible eight-semester distribution, no term above 18 credits.
    terms = ["2024-1", "2024-2", "2025-1", "2025-2", "2026-1", "2026-2", "2027-1", "2027-2"]
    position, current = 0, 0
    for record in records:
        if current + record["credits"] > 18:
            position += 1
            current = 0
        record["term"] = terms[position]
        current += record["credits"]
    return records


def area(result, name):
    return next(item for item in result["rows"] if item["area"] == name)


def test_exact_123_boundary_with_seven_required_general_courses_and_two_design_courses():
    result = check_requirements(profile(), finished_transcript())
    assert result["status"] == "충족"
    assert result["totals"]["total"] == 123
    assert result["totals"]["general_required"] == 14
    assert result["totals"]["general_elective"] == 20
    assert result["totals"]["major_total"] == 63
    assert result["missing_required_courses"] == []
    assert result["official_graduation_confirmed"] is False
    assert result["evidence"] and result["meta"]


def test_122_credits_is_one_short_even_when_all_other_areas_pass():
    records = finished_transcript()
    records[-1]["credits"] = 1
    result = check_requirements(profile(), records)
    assert result["status"] == "미충족"
    assert area(result, "총 졸업학점")["shortage"] == 1


@pytest.mark.parametrize("identifier, affected_area", [
    ("E6", "교양선택"), ("M18", "전공 합계 (주전공42 + 심화21)"), ("O8", "총 졸업학점"),
])
def test_half_credit_below_each_boundary_is_not_rounded_up(identifier, affected_area):
    records = finished_transcript()
    next(record for record in records if record["row_id"] == identifier)["credits"] -= 0.5
    result = check_requirements(profile(), records)
    assert result["status"] == "미충족"
    assert result["totals"]["total"] == 122.5
    assert area(result, "총 졸업학점")["shortage"] == 0.5
    assert area(result, affected_area)["shortage"] == 0.5


@pytest.mark.parametrize("mutation", ["reverse", "rename_ids", "pass_grades", "irrelevant_attempts"])
def test_diagnosis_invariants_do_not_depend_on_order_ids_or_noncredit_attempts(mutation):
    records = finished_transcript()
    reference = check_requirements(profile(), records)
    if mutation == "reverse":
        records.reverse()
    elif mutation == "rename_ids":
        for index, record in enumerate(records):
            record.update(row_id=f"RENAMED_R{index}", course_id=f"RENAMED_C{index}")
    elif mutation == "pass_grades":
        for index, record in enumerate(records):
            record["grade"] = ["P", "D0", "A+"][index % 3]
    else:
        records += [row("future", term="2028-1", status="planned", grade=""),
                    row("withdrawn", status="withdrawn", grade=""), row("failed_optional", grade="F")]
    before = deepcopy(records)
    actual = check_requirements(profile(), records)
    assert actual["status"] == "충족"
    for field in ("status", "totals", "rows", "missing_required_courses", "review_items", "projection"):
        assert actual[field] == reference[field]
    assert records == before


def test_surplus_major_credits_can_replace_residual_other_credits():
    records = finished_transcript()
    for record in records:
        if record["category"] == "일선":
            record["category"] = "전선"
    result = check_requirements(profile(), records)
    assert result["status"] == "충족"
    assert result["totals"]["other"] == 0
    assert result["totals"]["major_total"] == 89


def test_legacy_major_required_classification_does_not_create_extra_elective_deficit():
    records = finished_transcript()
    for record in records:
        if record["category"] == "전선":
            record["category"] = "전필"
    result = check_requirements(profile(), records)
    assert result["status"] == "충족"
    assert result["totals"]["major_total"] == 63


@pytest.mark.parametrize("course_name", ["사회봉사", "전공종합설계(2)"])
def test_same_area_credit_total_cannot_replace_a_missing_required_course(course_name):
    records = finished_transcript()
    required = next(record for record in records if record["course_name"] == course_name)
    required["course_name"] = "필수과목을 대신할 수 없는 다른 과목"
    result = check_requirements(profile(), records)
    assert result["totals"]["total"] == 123
    assert result["status"] == "미충족"
    assert course_name in {item["course_name"] for item in result["missing_required_courses"]}


def test_zero_credit_pass_does_not_fulfil_a_three_credit_required_design_course():
    records = finished_transcript()
    next(record for record in records if record["course_name"] == "전공종합설계(2)")["credits"] = 0
    records.append(row("replacement_total", 3, term="2027-2"))
    result = check_requirements(profile(), records)
    assert result["totals"]["total"] == 123
    assert result["status"] == "미충족"
    assert area(result, "필수 설계과목")["shortage"] == 1


@pytest.mark.parametrize("changes, expected_area", [
    ({"chapel_pass_semesters": 5}, "채플"),
    ({"registered_semesters": 7, "completed_semesters": 7}, "등록 학기"),
    ({"graduation_project_status": "미완료"}, "졸업작품 심사"),
])
def test_noncredit_requirement_cannot_be_paid_for_with_extra_credits(changes, expected_area):
    records = finished_transcript() + [row("excess", 3, term="2027-2")]
    result = check_requirements(profile(**changes), records)
    assert result["status"] == "미충족"
    assert area(result, expected_area)["shortage"] == 1


def test_in_progress_final_course_counts_only_in_projection():
    records = finished_transcript()
    records[-1].update(term="2027-2", status="in_progress", grade="")
    result = check_requirements(profile(completed_semesters=7), records)
    assert result["status"] == "미충족"
    assert result["totals"]["total"] == 121
    assert result["totals"]["in_progress"] == 2
    assert result["projection"]["totals"]["total"] == 123
    assert result["projection"]["status"] == "충족"


def test_projection_does_not_automatically_grant_chapel_or_project_pass():
    result = check_requirements(profile(chapel_pass_semesters=5, graduation_project_status="미완료"), finished_transcript())
    assert result["projection"]["status"] == "미충족"
    assert next(r for r in result["projection"]["rows"] if r["area"] == "채플")["earned"] == 5
    assert next(r for r in result["projection"]["rows"] if r["area"] == "졸업작품 심사")["earned"] == 0


def test_unreviewed_in_progress_retake_blocks_the_plan_with_an_actionable_reason():
    student = profile(current_term="2026-2", target_graduation_term="2027-2",
                      registered_semesters=6, completed_semesters=5)
    records = [row("old", name="승인 대기 재수강", grade="D0"),
               row("current", name="승인 대기 재수강", term="2026-2",
                   grade="", status="in_progress", retake_of="old", approved=False)]
    diagnosis = check_requirements(student, records)
    # Current earned 3 is known; the future replacement is still unverified.
    assert diagnosis["status"] == "미충족"
    assert diagnosis["projection"]["status"] == "확인 필요"
    plan = plan_courses(student, records)
    assert plan["status"] == "확인 필요"
    assert plan["semesters"] == []
    assert any("재수강 승인" in issue for issue in plan["warnings"])


@pytest.mark.parametrize("changes", [
    {"department": "다른학과"}, {"entry_year": 2023}, {"enrollment_type": "편입학"},
    {"engineering_accreditation": True}, {"chapel_pass_semesters": True},
    {"registered_semesters": -1}, {"completed_semesters": 9},
    {"graduation_project_status": None}, {"graduation_project_status": []},
    {"graduation_project_status": {}}, {"current_term": "잘못된 학기"},
])
def test_invalid_or_unsupported_profile_never_returns_graduation_pass(changes):
    result = check_requirements(profile(**changes), finished_transcript())
    assert result["status"] == "확인 필요"
    assert result["review_items"]


def test_future_completed_credit_is_not_sufficient_to_issue_pass():
    records = finished_transcript()
    records[-1]["term"] = "2028-1"
    assert check_requirements(profile(), records)["status"] == "확인 필요"


def test_f_and_p_and_zero_credit_rows_have_hand_calculated_gpa():
    result = search_scores([row("low", grade="D0"), row("failed", grade="F"),
                            row("pass", grade="P"), row("zero", 0, grade="A+")])
    assert result["totals"]["total"] == 6
    assert result["terms"][0]["gpa_credits"] == 6
    assert result["terms"][0]["gpa"] == 0.5


def test_planned_and_withdrawn_records_never_create_earned_credit():
    result = search_scores([row("earned"), row("future", status="planned", grade=""),
                            row("withdraw", status="withdrawn", grade="")])
    assert result["totals"]["total"] == 3


def test_approved_retake_replaces_old_earned_credit_instead_of_adding_it():
    old = row("old", name="동일과목", grade="D0")
    new = row("new", name="동일과목", term="2024-2", retake_of="old", approved=True)
    result = search_scores([old, new])
    assert result["totals"]["total"] == 3
    assert {item["row_id"] for item in result["accepted"]} == {"new"}
    assert result["terms"][0]["gpa"] is None


def test_approved_retake_of_failed_course_earns_credit_once():
    result = search_scores([row("old", name="동일과목", grade="F"),
                            row("new", name="동일과목", term="2024-2", retake_of="old", approved=True)])
    assert result["totals"]["total"] == 3
    assert result["review_items"] == []


@pytest.mark.parametrize("original_id, approved", [("old", False), ("missing", True)])
def test_unverified_retake_is_excluded_from_gpa_as_well_as_earned_credit(original_id, approved):
    result = search_scores([
        row("old", name="평점 검토 과목", grade="D0"),
        row("new", name="평점 검토 과목", term="2024-2", retake_of=original_id, approved=approved),
    ])
    # Independent oracle: original D0 remains 3 credits at 1.0; the unreviewed
    # replacement is not a second GPA-bearing attempt in the following term.
    assert result["status"] == "확인 필요"
    assert [(term["gpa_credits"], term["gpa"]) for term in result["terms"]] == [(3, 1.0), (0, None)]


def test_retake_chain_does_not_restore_the_first_failed_grade_to_gpa():
    result = search_scores([
        row("old", name="연속 재수강 과목", grade="F"),
        row("middle", name="연속 재수강 과목", grade="D0", term="2024-2", retake_of="old", approved=True),
        row("latest", name="연속 재수강 과목", grade="A0", term="2025-1", retake_of="middle", approved=True),
    ])
    assert result["status"] == "집계 완료"
    assert result["totals"]["total"] == 3
    assert [(term["gpa_credits"], term["gpa"]) for term in result["terms"]] == [(0, None), (0, None), (3, 4.0)]


@pytest.mark.parametrize("old_status", ["planned", "withdrawn", "in_progress"])
def test_retake_original_requires_a_confirmed_attempt(old_status):
    result = search_scores([
        row("old", name="확정 전 원행", status=old_status, grade=""),
        row("new", name="확정 전 원행", term="2024-2", retake_of="old", approved=True),
    ])
    assert result["status"] == "확인 필요"
    assert result["totals"]["total"] == 0
    assert all(term["gpa"] is None for term in result["terms"])


@pytest.mark.parametrize("second_name", ["동일과목", "승인대체과목"])
def test_two_replacements_for_one_original_attempt_are_not_counted_twice(second_name):
    result = search_scores([row("old", name="동일과목", grade="D0"),
                            row("new1", name="동일과목", term="2024-2", retake_of="old", approved=True),
                            row("new2", name=second_name, term="2025-1", retake_of="old", approved=True)])
    assert result["totals"]["total"] == 3
    assert result["status"] == "확인 필요"


def test_unapproved_same_name_repeat_is_not_counted_twice():
    result = search_scores([row("old", name="동일과목"), row("new", name="동일과목", term="2024-2")])
    assert result["totals"]["total"] == 3
    assert result["status"] == "확인 필요"


def test_failed_course_id_reused_with_different_name_still_requires_retake_review():
    old = row("old", name="기존 과목", grade="F")
    new = row("new", name="이름이 다른 같은 코드", term="2024-2")
    old["course_id"] = new["course_id"] = "SAME_SCHOOL_CODE"
    result = search_scores([old, new])
    assert result["status"] == "확인 필요"
    assert result["totals"]["total"] == 0


@pytest.mark.parametrize("grade", ["A+", "F"])
def test_retake_grade_needing_review_preserves_original_credit(grade):
    records = [row("old", name="재수강 검토 과목", grade="D0"),
               row("new", name="재수강 검토 과목", term="2024-2", grade=grade,
                   retake_of="old", approved=True)]
    original = deepcopy(records)
    result = search_scores(records)
    assert [c["row_id"] for c in result["accepted"]] == ["old"]
    assert result["totals"]["total"] == 3
    assert result["review_items"] == ["재수강 검토 과목: 재수강 최고등급 또는 실패 후 원성적 처리 확인 필요"]
    assert records == original


@pytest.mark.parametrize("old_id, old_term, approved", [
    ("missing", "2024-1", True), ("old", "2024-2", True),
    ("old", "2025-1", True), ("old", "2024-1", False),
])
def test_unverified_retake_link_never_removes_original_attempt(old_id, old_term, approved):
    result = search_scores([
        row("old", name="연결 검토 과목", term=old_term, grade="D0"),
        row("new", name="연결 검토 과목", term="2024-2", retake_of=old_id, approved=approved),
    ])
    assert [c["row_id"] for c in result["accepted"]] == ["old"]
    assert result["totals"]["total"] == 3
    assert "연결 검토 과목: 재수강 승인·이전 성적 연결 확인 필요" in result["review_items"]


@pytest.mark.parametrize("last_credits, expected_ids, expected_review", [
    (10, {"new0", "new1", "new2"}, []),
    (11, {"new0", "new1", "old2"}, ["재수강 누계 30학점 초과: 승인 내역 확인 필요"]),
])
def test_retake_cumulative_thirty_credit_boundary(last_credits, expected_ids, expected_review):
    records = [row(f"old{i}", 10, name=f"한도 과목 {i}", grade="D0") for i in range(3)]
    records += [row(f"new{i}", last_credits if i == 2 else 10, name=f"한도 과목 {i}",
                    term="2024-2", retake_of=f"old{i}", approved=True) for i in range(3)]
    result = search_scores(records)
    assert {c["row_id"] for c in result["accepted"]} == expected_ids
    assert result["totals"]["total"] == 30
    assert result["review_items"] == expected_review


def test_retake_over_limit_does_not_add_unrecognized_gpa_credits():
    records = [row(f"old{i}", 10, name=f"평점 한도 과목 {i}", grade="D0") for i in range(3)]
    records += [row(f"new{i}", 11 if i == 2 else 10, name=f"평점 한도 과목 {i}",
                    term="2024-2", retake_of=f"old{i}", approved=True) for i in range(3)]
    result = search_scores(records)
    assert result["status"] == "확인 필요"
    assert [(term["gpa_credits"], term["gpa"]) for term in result["terms"]] == [(10, 1.0), (20, 4.0)]


def test_colliding_retake_is_rejected_before_cumulative_limit_is_reserved():
    result = search_scores([
        row("old", name="원래 과목", grade="D0"), row("other", name="다른 인정 과목"),
        row("new1", 21, name="다른 인정 과목", term="2024-2", retake_of="old", approved=True),
        row("new2", 21, name="승인 대체 과목", term="2025-1", retake_of="old", approved=True),
    ])
    assert {c["row_id"] for c in result["accepted"]} == {"other", "new2"}
    assert result["totals"]["total"] == 24
    assert result["review_items"] == ["다른 인정 과목: 다른 인정 과목과 재수강 대체과목이 중복됨"]


def test_retake_review_order_preserves_cumulative_attempt_limit():
    # The existing conservative policy reserves workload after collision checks,
    # even when a later original-link check needs review. Refactoring must keep
    # that order; changing the academic policy requires a separate decision.
    result = search_scores([
        row("old1", name="중복 원래 과목"), row("old2", name="중복 원래 과목"),
        row("old3", name="다음 원래 과목", grade="D0"),
        row("new1", 21, name="첫 대체 과목", term="2024-2", retake_of="old2", approved=True),
        row("new2", 10, name="둘째 대체 과목", term="2025-1", retake_of="old3", approved=True),
    ])
    assert {c["row_id"] for c in result["accepted"]} == {"old1", "old3"}
    assert result["totals"]["total"] == 6
    assert result["review_items"][-2:] == [
        "첫 대체 과목: 이미 대체된 성적을 다시 참조함",
        "재수강 누계 30학점 초과: 승인 내역 확인 필요",
    ]


@pytest.mark.parametrize("credits", [True, -1, "nan", "Infinity", "2.25"])
def test_invalid_credit_input_is_not_silently_accepted(credits):
    result = search_scores([row("invalid", credits)])
    assert result["status"] == "확인 필요"
    assert not result["totals"]


def test_csv_drops_identifiers_and_reports_invalid_values_without_echoing_private_cell():
    content = "이름,학번,과목명,학기,이수구분,학점,성적,상태\n가상개인명,202499999,검증,2024-1,전선,3,A0,이수완료\n"
    result = parse_transcript(content)
    assert result["ok"] is True
    assert set(result["removed_columns"]) == {"이름", "학번"}
    assert "가상개인명" not in str(result)
    assert "202499999" not in str(result)


def test_planner_applies_18_per_term_and_36_per_year_including_summer_attempts():
    records = [row(f"existing{i}", 3, term="2026-1") for i in range(6)]
    records += [row("summer1", 3, term="2026-S"), row("summer2", 3, term="2026-S", grade="F")]
    result = plan_courses(profile(current_term="2026-S", target_graduation_term="2027-2", registered_semesters=5, completed_semesters=5), records)
    semesters = {s["term"]: s for s in result["semesters"]}
    assert semesters["2026-2"]["max_credits"] == 12
    assert semesters["2026-2"]["total_credits"] <= 12
    assert all(s["total_credits"] <= 18 for s in result["semesters"])
    assert sum(s["total_credits"] for s in result["semesters"] if s["term"].startswith("2027")) <= 36


def test_planner_does_not_put_design_two_before_design_one():
    result = plan_courses(profile(current_term="2026-2", target_graduation_term="2028-1", registered_semesters=6, completed_semesters=5), [row("existing")])
    design_terms = {c["course_id"]: semester["term"] for semester in result["semesters"] for c in semester["courses"] if c["course_id"] in {"CS_CAP1", "CS_CAP2"}}
    assert design_terms == {"CS_CAP1": "2027-2", "CS_CAP2": "2028-1"}


def test_planner_does_not_trust_a_forged_precomputed_diagnosis():
    result = plan_courses(profile(department="다른학과"), finished_transcript(), diagnosis={"status": "충족", "totals": {"total": 123}})
    assert result["status"] == "확인 필요"
    assert result["semesters"] == []


def test_planner_keeps_spring_capacity_for_general_courses_unavailable_in_fall():
    # Current 87 = general-required 14 + general-elective 14 + major 33 + other 26.
    # Missing 36 = major 30 + general-elective 6.  A feasible route is
    # spring major 12 + general 6, then fall major 18.  All fall electives
    # below have already passed, so postponing the general area is invalid.
    records = finished_transcript()
    records = [c for c in records if c["row_id"] not in {f"M{i}" for i in range(10)} and not c["row_id"].startswith("E")]
    records += [row(f"fall{i}", name=name, category="교선") for i, name in enumerate(["성과인간심리", "생태환경과윤리", "현대사회와범죄", "영화로만나는기독교"])]
    records += [row("general_extra", 2, category="교선")]
    historical_terms = ["2024-1", "2024-2", "2025-1", "2025-2", "2026-1"]
    term_index, term_credits = 0, 0
    for c in records:
        if term_credits + c["credits"] > 18:
            term_index += 1
            term_credits = 0
        c["term"] = historical_terms[term_index]
        term_credits += c["credits"]
    student = profile(current_term="2026-2", target_graduation_term="2027-2", registered_semesters=6, completed_semesters=5)
    assert check_requirements(student, records)["totals"]["total"] == 87
    result = plan_courses(student, records)
    assert result["status"] == "조건부 가능"
    assert result["remaining_shortages"] == []
    spring = next(s for s in result["semesters"] if s["term"] == "2027-1")
    assert sum(c["credits"] for c in spring["courses"] if c["category"] == "교선") >= 6


def test_planning_does_not_mutate_original_student_record():
    student = profile(current_term="2026-2", target_graduation_term="2027-2", registered_semesters=6, completed_semesters=5)
    records = [row("current", term="2026-2", status="in_progress", grade="")]
    before = deepcopy((student, records))
    plan_courses(student, records)
    assert (student, records) == before


def test_on_track_plan_uses_nine_each_senior_term_and_finishes_at_123():
    # These expectations are fixed arithmetic, not loaded from sample.expected.
    from jolup.samples import load_sample
    sample = load_sample("on_track")
    before = deepcopy(sample)
    diagnosis = check_requirements(sample["profile"], sample["courses"])
    result = plan_courses(sample["profile"], sample["courses"])
    assert diagnosis["totals"]["total"] == 90
    assert diagnosis["totals"]["in_progress"] == 15
    assert result["status"] == "조건부 가능"
    assert [(s["term"], s["total_credits"]) for s in result["semesters"]] == [
        ("2027-1", 9), ("2027-2", 9)]
    assert all(s["min_credits"] == 9 and 9 <= s["total_credits"] <= 18
               for s in result["semesters"])
    assert result["projected_totals"]["total"] == 90 + 15 + 9 + 9 == 123
    assert result["projected_totals"]["major_total"] == 45 + 15 + 18 == 78
    assert result["remaining_shortages"] == []
    assert result["solver"]["optimal"] is True
    assert sample == before


def test_senior_minimum_requires_nine_even_when_only_six_credits_are_missing():
    # Published boundary: 123 - (two 3-credit general-elective/free-choice rows) = 117.
    # All mandatory areas remain fulfilled, but a planned regular senior semester
    # must still contain at least 9 credits without an approved exception.
    records = [c for c in finished_transcript() if c["row_id"] not in {"O0", "O1"}]
    student = profile(current_term="2027-1", target_graduation_term="2027-2",
                      registered_semesters=7, completed_semesters=7)
    assert check_requirements(student, records)["totals"]["total"] == 117
    result = plan_courses(student, records)
    assert result["status"] == "조건부 가능"
    assert len(result["semesters"]) == 1
    semester = result["semesters"][0]
    assert semester["term"] == "2027-2"
    assert semester["min_credits"] == 9
    assert semester["total_credits"] == 9
    assert result["projected_totals"]["total"] == 117 + 9 == 126
    assert result["remaining_shortages"] == []


def test_academic_warning_limits_each_senior_term_to_fifteen():
    # A large shortfall ensures the optimizer wants all available capacity, so
    # merely displaying a max_credits label cannot make this assertion pass.
    from jolup.samples import load_sample
    sample = load_sample("credit_shortfall")
    student = dict(sample["profile"], academic_warning=True)
    result = plan_courses(student, sample["courses"])
    assert result["status"] == "위험"
    assert len(result["semesters"]) == 2
    assert all(s["max_credits"] == 15 and 9 <= s["total_credits"] <= 15
               for s in result["semesters"])
    assert sum(s["total_credits"] for s in result["semesters"]) == 30
    assert result["projected_totals"]["total"] == 38 + 15 + 30 == 83
    assert next(r for r in result["remaining_shortages"] if r["area"] == "총 졸업학점")["shortage"] == 40
    assert any("학사경고" in warning and "15학점" in warning for warning in result["warnings"])


def test_explicitly_unknown_academic_warning_does_not_assume_eighteen():
    from jolup.samples import load_sample
    sample = load_sample("on_track")
    student = dict(sample["profile"], academic_warning=None)
    result = plan_courses(student, sample["courses"])
    assert result["status"] == "확인 필요"
    assert result["semesters"] == []
    assert "학사경고" in result["answer"]
    assert any("미확인" in warning for warning in result["warnings"])


def test_preferred_eight_credit_cap_cannot_bypass_senior_nine_credit_minimum():
    from jolup.samples import load_sample
    sample = load_sample("on_track")
    result = plan_courses(sample["profile"], sample["courses"], preferences={"max_credits": 8})
    assert result["status"] == "확인 필요"
    assert result["solver"]["optimal"] is False
    assert "최소 수강학점" in result["solver"]["reason"]
    assert result["remaining_shortages"]
    assert all(s["courses"] == [] for s in result["semesters"])


def test_annual_capacity_below_regular_minimum_is_not_a_successful_plan():
    # 18 spring + 6 summer reserves 24, leaving only 12 of the annual 36.
    # The next term is still year 3, so its regular minimum is 15, not 9.
    records = [row(f"spring{i}", 3, term="2026-1") for i in range(6)]
    records += [row("summer1", 3, term="2026-S"), row("summer2", 3, term="2026-S")]
    student = profile(current_term="2026-S", target_graduation_term="2027-2",
                      registered_semesters=5, completed_semesters=5)
    result = plan_courses(student, records)
    first = next(s for s in result["semesters"] if s["term"] == "2026-2")
    assert first["min_credits"] == 15
    assert first["max_credits"] == 12
    assert result["status"] == "확인 필요"
    assert result["solver"]["optimal"] is False
    assert result["remaining_shortages"]
    assert not any(s["courses"] for s in result["semesters"])


def test_registration_only_shortfall_still_requires_a_valid_minimum_load_plan():
    # All credits are already earned, but the eighth registered semester remains.
    # Projecting registration alone must not hide an infeasible 8-credit cap
    # against the required 9-credit minimum in that future semester.
    student = profile(current_term="2027-1", target_graduation_term="2027-2",
                      registered_semesters=7, completed_semesters=7)
    records = finished_transcript()
    diagnosis = check_requirements(student, records)
    assert diagnosis["status"] == "미충족"
    assert diagnosis["totals"]["total"] == 123
    assert [r["area"] for r in diagnosis["rows"] if r["status"] != "충족"] == ["등록 학기"]
    result = plan_courses(student, records, preferences={"max_credits": 8})
    assert result["solver"]["optimal"] is False
    assert result["solver"]["feasible"] is False
    assert result["status"] == "확인 필요"
    assert not any(s["courses"] for s in result["semesters"])
    assert all(s["projected_rows"] == [] and s["projected_totals"] == {}
               for s in result["semesters"])


def test_each_semester_diagnosis_is_cumulative_and_keeps_unconfirmed_noncredit_values():
    from jolup.samples import load_sample
    sample = load_sample("on_track")
    before = deepcopy(sample)
    result = plan_courses(sample["profile"], sample["courses"])
    spring, fall = result["semesters"]
    assert spring["projected_totals"]["total"] == 90 + 15 + 9 == 114
    assert fall["projected_totals"]["total"] == 90 + 15 + 9 + 9 == 123
    assert spring["projected_totals"]["major_total"] == 45 + 15 + 9 == 69
    assert fall["projected_totals"]["major_total"] == 78
    for semester, registered, credit_gap in [(spring, 7, 9), (fall, 8, 0)]:
        table = {r["area"]: r for r in semester["projected_rows"]}
        assert table["총 졸업학점"]["shortage"] == credit_gap
        assert table["등록 학기"]["earned"] == registered
        assert "시점 추정" in table["등록 학기"]["basis"]
        assert table["채플"]["earned"] == 5 and table["채플"]["shortage"] == 1
        assert table["졸업작품 심사"]["earned"] == 0
        assert "현재 제공된 확인값" in table["채플"]["basis"]
        assert semester["projected_status"] == "미충족"
        estimates = {e["item"]: e for e in semester["milestone_estimates"]}
        assert estimates["등록 학기"]["estimated"] == registered
        assert estimates["채플"]["current"] == 5
        assert estimates["채플"]["estimated"] == 6
        assert estimates["졸업작품 심사"]["estimated"] is None
        assert all(e["is_actual_completion"] is False for e in estimates.values())
        assert all(r["basis"] for r in semester["projected_rows"])
        assert any("이후 학기 후보는 포함하지 않습니다" in a for a in semester["projection_assumptions"])
    # Final conditional feasibility has additional non-credit completion conditions;
    # it is not a claim that the per-semester current-value rows already passed.
    assert result["status"] == "조건부 가능"
    assert result["current_totals"]["total"] == 90
    assert sample == before


def test_already_passed_chapel_and_project_are_preserved_in_semester_diagnosis():
    records = [c for c in finished_transcript() if c["row_id"] not in {"O0", "O1"}]
    student = profile(current_term="2027-1", target_graduation_term="2027-2",
                      registered_semesters=7, completed_semesters=7)
    result = plan_courses(student, records)
    semester = result["semesters"][0]
    table = {r["area"]: r for r in semester["projected_rows"]}
    assert table["채플"]["earned"] == 6
    assert table["졸업작품 심사"]["earned"] == 1
    assert table["등록 학기"]["earned"] == 8
    assert semester["projected_status"] == "충족"
    assert semester["projection_assumptions"]
    assert student["registered_semesters"] == 7


def test_valid_time_limited_incumbent_is_distinct_from_infeasible_assignment(monkeypatch):
    from jolup.samples import load_sample
    import jolup.course_solver as solver
    original_milp = solver.milp

    def stop_after_valid_solution(*args, **kwargs):
        solved = original_milp(*args, **kwargs)
        assert solved.success and solved.x is not None
        solved.success = False  # A limited search can still supply a valid incumbent.
        return solved

    monkeypatch.setattr(solver, "milp", stop_after_valid_solution)
    sample = load_sample("on_track")
    result = plan_courses(sample["profile"], sample["courses"])
    assert result["solver"]["optimal"] is False
    assert result["solver"]["feasible"] is True
    assert result["solver"]["status"] == "탐색 제한"
    assert result["status"] == "조건부 가능"
    assert result["remaining_shortages"] == []
    assert all(9 <= s["total_credits"] <= 18 for s in result["semesters"])
