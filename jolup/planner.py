"""윤석현: 조건을 명시한 학기별 후보 계획. 실제 수강신청을 실행하지 않는다."""
from __future__ import annotations

import json
from copy import deepcopy
from .academic import check_requirements, canonical_key, name_key, term_key, validate_courses, search_scores
from .evidence import evidence_for, metadata, rules, sources
from .settings import DATA


def search_courses(query="", term=None):
    """과목 ID는 내부 ID이며 실제 학수번호·시간표로 오인하지 않게 반환한다."""
    catalog = json.loads((DATA / "courses.json").read_text(encoding="utf-8"))["courses"]
    if term is not None:
        term_key(term)
        semester = str(term).split("-")[1]
        if semester not in {"1", "2"}:
            return []
        catalog = [c for c in catalog if int(semester) in c["offered_semesters"]]
    q = name_key(query)
    return [c for c in catalog if not q or q in name_key(c["course_name"]) or q in name_key(c["category"]) or q in name_key(c["course_id"])]


def _future_terms(current, target):
    current_key, target_key = term_key(current), term_key(target)
    if target_key < current_key or target_key[0] - current_key[0] > 4:
        raise ValueError("목표 마지막 수학 학기는 현재 이후 4년 이내로 입력해 주세요.")
    return [f"{year}-{semester}" for year in range(current_key[0], target_key[0] + 1) for semester in (1, 2) if current_key < term_key(f"{year}-{semester}") <= target_key]


def _semester_projection(profile, assumed_courses, term, elapsed_terms, feasible):
    """Show cumulative course assumptions without inventing future chapel/project passes."""
    if not feasible:
        return dict(projected_totals={}, projected_rows=[], projected_status="확인 필요",
                    projection_assumptions=["수강 최소·상한 등 배치 제약을 만족하는 계획이 확인되지 않아 학기말 전망을 제시하지 않습니다."],
                    milestone_estimates=[])
    registered_estimate = profile["registered_semesters"] + elapsed_terms
    snapshot_profile = {**profile, "current_term": term, "registered_semesters": registered_estimate}
    # Chapel and project values stay at their last supplied state. Registration is
    # explicitly a timeline estimate, not an update to the student's current record.
    snapshot = check_requirements(snapshot_profile, assumed_courses)
    rows = deepcopy(snapshot["rows"])
    for r in rows:
        if r["area"] == "등록 학기":
            r["basis"] = "계속 등록 가정에 따른 시점 추정 · 실제 등록 완료 아님"
        elif r["area"] in {"채플", "졸업작품 심사"}:
            r["basis"] = "현재 제공된 확인값 유지 · 향후 통과를 자동 가정하지 않음"
        else:
            r["basis"] = "현재 수강 중 및 이 학기까지의 후보 과목 합격 가정"
    chapel_now = profile["chapel_pass_semesters"]
    available_terms = elapsed_terms + int(profile["completed_semesters"] < profile["registered_semesters"])
    chapel_possible = chapel_now + min(max(0, rules()["chapel_semesters"] - chapel_now), available_terms)
    project_now = int(profile["graduation_project_status"] == "통과")
    estimates = [
        dict(item="등록 학기", current=profile["registered_semesters"], estimated=registered_estimate,
             required=rules()["registered_semesters"], unit="학기", is_actual_completion=False,
             assumption="목표 시점까지 매 정규학기에 계속 등록한다는 가정"),
        dict(item="채플", current=chapel_now, estimated=chapel_possible,
             required=rules()["chapel_semesters"], unit="학기", is_actual_completion=False,
             assumption="현재 진행 학기와 해당 시점까지의 채플을 필요한 횟수만큼 통과할 수 있다는 조건부 추정. 위 진단표는 현재 확인값 유지"),
        dict(item="졸업작품 심사", current=project_now, estimated=project_now if project_now else None,
             required=1, unit="건", is_actual_completion=False,
             assumption="이미 제공된 통과 기록만 유지. 향후 심사 날짜·결과는 미확인이므로 통과를 자동 배정하지 않음"),
    ]
    return dict(projected_totals=snapshot["totals"], projected_rows=rows, projected_status=snapshot["status"],
                projection_assumptions=[
                    f"{term}까지 현재 수강 중 과목과 그때까지 선택된 후보를 모두 통과한다고 가정한 누적 학점입니다. 이후 학기 후보는 포함하지 않습니다.",
                    f"등록 {registered_estimate}학기는 계속 등록한다는 시점 추정입니다. 현재 등록 기록은 변경하지 않습니다.",
                    "채플·작품 행은 현재 제공된 확인값을 유지합니다. 아래 조건부 추정과 구별해야 하며 실제 완료 기록이 아닙니다.",
                    "전체 계획의 조건부 가능은 채플·작품 등 남은 비학점 요건도 목표까지 별도로 완료해야 한다는 뜻입니다.",
                ], milestone_estimates=estimates)


def _planning_limits(profile, preferences):
    """Validate the target and requested load before constructing any plan."""
    future = _future_terms(profile["current_term"], profile["target_graduation_term"])
    cap = preferences.get("max_credits", rules()["term_limit"])
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 18:
        raise ValueError("희망 학점은 1~18의 정수로 입력해 주세요. 초과 이수는 별도 승인 확인이 필요합니다.")
    if profile.get("academic_warning") is True:
        cap = min(cap, 15)
    return future, cap


def _courses_assuming_passes(clean):
    """Build a separate projection ledger; never update the student's records."""
    assumed = deepcopy(clean)
    for c in assumed:
        if c["status"] == "in_progress":
            c.update(status="completed", grade="P")
    return assumed


def _annual_workload(clean):
    """F and repeated attempts reserve workload, even if no credit was earned."""
    annual = {}
    for c in clean:
        if c["status"] in {"completed", "in_progress"}:
            year = c["term"][:4]
            annual[year] = annual.get(year, 0) + c["credits"]
    return annual


def _select_future_courses(profile, assumed, future, cap, annual):
    """Exclude already accepted courses, then solve all future terms together."""
    from .course_solver import choose_courses
    baseline = check_requirements(profile, assumed)
    accepted = search_scores(assumed)["accepted"]
    known = {canonical_key(c) for c in accepted}
    known_names = {name_key(c["course_name"]) for c in accepted}
    candidates = [c for c in search_courses() if c["course_id"] not in known and name_key(c["course_name"]) not in known_names]
    minimums = {term: rules()["term_min_senior"] if profile["registered_semesters"] + i + 1 >= 7 else rules()["term_min_regular"] for i, term in enumerate(future)}
    selected, solver = choose_courses(candidates, future, cap, annual, baseline["totals"], baseline["missing_required_courses"], known, rules(), minimums)
    return selected, solver, minimums


def _candidate_reason(course, current):
    """Explain priority using the cumulative diagnosis before this candidate."""
    totals = current["totals"]
    missing_ids = {m["course_id"] for m in current["missing_required_courses"]}
    if course["course_id"] in missing_ids:
        return "아직 이수하지 않은 필수 과목"
    if course["category"] in {"전필", "전선"} and totals["major_total"] < rules()["major_total"]:
        return f"전공 합계 부족 {max(0, rules()['major_total'] - totals['major_total'])}학점 보완"
    if course["category"] == "교선" and totals["general_elective"] < rules()["general_elective"]:
        return f"교양선택 부족 {max(0, rules()['general_elective'] - totals['general_elective'])}학점 보완"
    if totals["total"] < rules()["total_credits"]:
        return f"총 졸업학점 부족 {max(0, rules()['total_credits'] - totals['total'])}학점 보완"
    return "학기 최소 수강학점 충족을 위한 추가 후보"


def _planned_course(course, term, clean, reason):
    """Return a displayed candidate, a hypothetical passed row and any approval note."""
    chosen = {**course, "reason": reason}
    record = dict(row_id=f"PLAN_{term}_{course['course_id']}", course_id=course["course_id"], course_name=course["course_name"], term=term, category=course["category"], credits=course["credits"], grade="P", status="completed", retake_of="", retake_approved=False)
    failures = [old for old in clean if (name_key(old["course_name"]) == name_key(course["course_name"]) or old["course_id"] == course["course_id"]) and old["status"] == "completed" and old["grade"] == "F"]
    if not failures:
        return chosen, record, None
    previous = sorted(failures, key=lambda old: term_key(old["term"]))[-1]
    record.update(retake_of=previous["row_id"], retake_approved=True)
    chosen.update(approval_required=True, reason=reason + " · 공식 재수강 신청·승인 후 합격을 가정")
    note = f"{course['course_name']}: 기존 F 성적의 재수강 신청·승인이 필요합니다. 현재 승인 완료를 뜻하지 않습니다."
    return chosen, record, note


def _semester_courses(profile, term, candidates, clean, assumed):
    """Append to the private assumption ledger in course order, not to clean input."""
    future_profile = {**profile, "current_term": term}
    chosen_courses, notes = [], []
    ordered = sorted(candidates, key=lambda c: (c["category"] != "전필", c["category"] != "교필", c["course_id"]))
    for course in ordered:
        current = check_requirements(future_profile, assumed)
        chosen, record, note = _planned_course(course, term, clean, _candidate_reason(course, current))
        chosen_courses.append(chosen)
        assumed.append(record)
        if note:
            notes.append(note)
    return chosen_courses, notes


def _future_milestones(profile, future):
    """Build explicit non-credit assumptions without changing the current profile."""
    chapel_missing = max(0, rules()["chapel_semesters"] - profile["chapel_pass_semesters"])
    possible_chapel_terms = len(future) + (1 if profile["completed_semesters"] < profile["registered_semesters"] else 0)
    estimated_chapel = profile["chapel_pass_semesters"] + min(chapel_missing, possible_chapel_terms)
    estimated_registered = profile["registered_semesters"] + len(future)
    project_future = profile["graduation_project_status"] == "통과" or bool(future)
    milestones = [
        {"item": "채플", "remaining": chapel_missing, "action": f"추가 {chapel_missing}학기 통과 확인", "feasible": chapel_missing <= possible_chapel_terms},
        {"item": "등록 학기", "remaining": max(0, 8 - profile["registered_semesters"]), "action": f"목표까지 등록을 계속하면 {estimated_registered}학기", "feasible": estimated_registered >= 8},
        {"item": "졸업작품 심사", "remaining": 0 if profile["graduation_project_status"] == "통과" else 1, "action": "학과의 목표 연도 공지를 확인하고 작품 심사 통과", "feasible": project_future},
    ]
    future_profile = {**profile, "current_term": profile["target_graduation_term"], "registered_semesters": estimated_registered, "chapel_pass_semesters": estimated_chapel, "graduation_project_status": "통과" if project_future else profile["graduation_project_status"]}
    return milestones, future_profile


def _include_candidate_evidence(result):
    """Include each candidate source once, then rebuild public metadata."""
    for semester in result["semesters"]:
        for candidate in semester["courses"]:
            for source_id in candidate["source_ids"]:
                s = sources()[source_id]
                if not any(e["source_id"] == source_id for e in result["evidence"]):
                    result["evidence"].append({k: s.get(k) for k in ("source_id", "file_name", "url", "registered_date", "date_note")})
    result["meta"] = metadata(result["evidence"])


def _plan_conclusion(diagnosis, final, remaining_shortages, solver):
    """Keep current eligibility, conditional feasibility and search limits distinct."""
    if diagnosis["status"] == "충족":
        return dict(status="충족", answer="현재 제공된 자료 기준 요건을 충족했어. 학교의 최종 졸업사정 결과를 확인하자.")
    if not remaining_shortages and not final["review_items"]:
        return dict(status="조건부 가능", answer="목표 졸업까지 준비는 가능한 경로가 있어! 현재 수강 과목과 아래 후보를 통과하고, 채플·등록·작품 심사를 완료하는 조건이야. 실제 개설 확인이 남아 있어.")
    if not solver["optimal"]:
        return dict(status="확인 필요", answer="계획 탐색이 완료되지 않았어. 현재 결과만으로 가능 여부를 확정할 수 없어.")
    return dict(status="위험", answer="현재 자료와 학점 한도로는 목표 졸업까지 부족분이 남아. 아래 부족 영역을 확인하고 학과 상담 또는 목표 일정 조정이 필요해.")


def plan_courses(profile, courses, diagnosis=None, preferences=None):
    """Validate → select courses → project each term → assess the conditional target."""
    preferences = preferences or {}
    # Keep the diagnosis argument for compatibility, but always recompute current facts.
    current_diagnosis = check_requirements(profile, courses)
    evidence = evidence_for("credits", "general", "major", "noncredit", "limits")
    result = dict(status="확인 필요", answer="", semesters=[], warnings=[], remaining_shortages=[], evidence=evidence, meta=metadata(evidence), milestones=[], assumptions=[])
    review_items = current_diagnosis["projection"]["review_items"]
    if review_items:
        result["answer"] = "이수 내역의 확인 필요 항목을 먼저 해결해야 신뢰할 수 있는 수강 계획을 만들 수 있어."
        result["warnings"] = review_items
        return result
    if "academic_warning" in profile and type(profile["academic_warning"]) is not bool:
        result["answer"] = "학사경고 여부에 따라 수강 상한이 달라져. 경고 여부를 확인한 뒤 계획을 계산하자."
        result["warnings"] = ["학사경고 미확인: 일반 18학점 또는 제한 15학점 적용 여부 확인 필요"]
        return result
    try:
        future, cap = _planning_limits(profile, preferences)
    except (ValueError, KeyError) as exc:
        result["answer"] = "목표 학기와 수강 한도 확인이 필요해."
        result["warnings"] = [str(exc)]
        return result
    clean, _ = validate_courses(courses)
    assumed = _courses_assuming_passes(clean)
    annual = _annual_workload(clean)
    progress_count = sum(1 for c in clean if c["status"] == "in_progress")
    result["assumptions"] = ["향후 실제 개설 여부·시간표·여석·수강 자격은 확인되지 않았습니다.", "후보 과목에 합격하고 필요한 비학점 요건도 별도로 완료해야 합니다.", "목표 졸업 학기는 마지막 수학 학기 기준입니다. 예: 2027-2 → 통상 2028년 2월 졸업 목표."]
    if progress_count:
        result["assumptions"].append(f"현재 수강 중 {progress_count}과목에 모두 합격한다고 가정합니다. 현재 취득 학점은 변하지 않습니다.")
    result["warnings"] = ["후보 목록은 공개 교육과정 기준입니다. 실제 강의시간표가 없어 시간 중복·분반·정원은 검증하지 못했습니다.", "설계(1) 이후 설계(2)를 배치하는 보수적 계획 정책을 적용합니다. 공식 수강 제한은 학과 확인이 필요합니다."]
    selected, solver, minimums = _select_future_courses(profile, assumed, future, cap, annual)
    result["solver"] = solver
    result["assumptions"].append("최소 수강 기준은 1~3학년 15학점, 4학년 9학점으로 적용합니다. 예외·초과 신청은 승인 정보가 없어 자동 적용하지 않습니다.")
    if profile.get("academic_warning") is True:
        result["warnings"].append("학사경고에 따른 15학점 상한을 적용했습니다. 제한 해제 승인 여부는 별도 확인해야 합니다.")
    for elapsed_terms, term in enumerate(future, 1):
        semester = {"term": term, "min_credits": minimums[term], "max_credits": min(cap, max(0, rules()["year_limit"] - annual.get(term[:4], 0)))}
        semester["courses"], notes = _semester_courses(profile, term, selected.get(term, []), clean, assumed)
        semester["total_credits"] = sum(c["credits"] for c in semester["courses"])
        result["assumptions"].extend(notes)
        annual[term[:4]] = annual.get(term[:4], 0) + semester["total_credits"]
        semester.update(_semester_projection(profile, assumed, term, elapsed_terms, solver["feasible"]))
        result["semesters"].append(semester)
    if not solver["feasible"]:
        # Non-credit assumptions cannot rescue an impossible course assignment.
        result["current_totals"] = current_diagnosis["totals"]
        result["remaining_shortages"] = [r for r in current_diagnosis["rows"] if r["status"] != "충족"]
        result["answer"] = "최소 수강학점과 학기·연간 상한을 함께 만족하는 계획이 확인되지 않았어. 예외 승인 또는 수강 조건을 먼저 확인해야 해."
        result["warnings"].append(solver.get("reason", "수강 계획의 제약을 만족하는 배치가 없습니다."))
        return result
    result["milestones"], future_profile = _future_milestones(profile, future)
    final = check_requirements(future_profile, assumed)
    result["remaining_shortages"] = [row for row in final["rows"] if row["status"] != "충족"]
    result["projected_totals"] = final["totals"]
    result["current_totals"] = current_diagnosis["totals"]
    result["projected_rows"] = final["rows"]
    _include_candidate_evidence(result)
    result.update(_plan_conclusion(current_diagnosis, final, result["remaining_shortages"], solver))
    return result
