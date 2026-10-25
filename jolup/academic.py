"""윤석현: 성적표 검증 → 인정/제외 → 요건 비교. LLM이 계산하지 않는다."""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .evidence import evidence_for, metadata, rules, validate_rule_bundle

FIELDS = ("row_id", "course_id", "course_name", "term", "category", "credits", "grade", "status", "retake_of", "retake_approved")
ALIASES = {"행번호": "row_id", "과목코드": "course_id", "학수번호": "course_id", "교과목명": "course_name", "과목명": "course_name", "학기": "term", "이수구분": "category", "학점": "credits", "성적": "grade", "상태": "status", "재수강원행": "retake_of", "재수강승인": "retake_approved"}
DOMAIN_COLUMNS = {"school": "성결대학교", "학교": "성결대학교", "department": "컴퓨터공학과", "학과": "컴퓨터공학과", "entry_year": "2024", "입학년도": "2024", "track": "전공심화", "과정": "전공심화", "domain_id": "sungkyul-computer-2024"}
CATEGORIES = {"교필": "교필", "교양필수": "교필", "교선": "교선", "교양선택": "교선", "전필": "전필", "전공필수": "전필", "전선": "전선", "전공선택": "전선", "일선": "일선", "일반선택": "일선"}
STATUSES = {"completed": "completed", "이수": "completed", "이수완료": "completed", "in_progress": "in_progress", "수강중": "in_progress", "수강 중": "in_progress", "planned": "planned", "예정": "planned", "withdrawn": "withdrawn", "철회": "withdrawn"}
GRADES = {"A+": 4.5, "A0": 4.0, "B+": 3.5, "B0": 3.0, "C+": 2.5, "C0": 2.0, "D+": 1.5, "D0": 1.0, "F": 0.0}
PASS_GRADES = set(GRADES) - {"F"} | {"P"}
TERM = re.compile(r"^(20\d{2})-([12SW])$")


def text(value):
    return unicodedata.normalize("NFC", "" if value is None else str(value)).strip()


def number(value):
    d = Decimal(str(value))
    return int(d) if d == d.to_integral() else float(d)


def term_key(term):
    match = TERM.fullmatch(text(term))
    if not match:
        raise ValueError("학기는 YYYY-1/2/S/W 형식이어야 합니다.")
    return int(match[1]), {"1": 1, "S": 2, "2": 3, "W": 4}[match[2]]


def name_key(name):
    return re.sub(r"[\s·ㆍ]", "", text(name)).casefold()


def canonical_key(course):
    """학교 학수번호가 없는 더미 자료도 동일 과목명을 한 번만 인정한다."""
    for required in rules()["general_required_courses"] + rules()["major_required_courses"]:
        if name_key(course["course_name"]) == name_key(required["name"]):
            return required["course_id"]
    return name_key(course["course_name"])


def _normalized_credits(value):
    """Convert only finite half-credit values; preserve exact decimal arithmetic."""
    try:
        d = Decimal(value)
        if not d.is_finite() or d < 0 or d > 21 or d * 2 != (d * 2).to_integral():
            raise ValueError()
        return number(d)
    except (InvalidOperation, ValueError):
        raise ValueError("학점은 0~21 사이의 0.5 단위 숫자여야 합니다.") from None


def _normalized_grade(grade, status):
    """A confirmed grade and an in-progress status must never be combined."""
    grade = grade.upper().replace("O", "0")
    if status == "completed" and grade not in PASS_GRADES | {"F"}:
        raise ValueError("이수완료 과목의 성적이 없거나 지원하지 않는 등급입니다.")
    if status == "in_progress" and grade not in {"", "IP", "수강중", "-"}:
        raise ValueError("수강 중 과목에 확정 성적을 입력할 수 없습니다.")
    return grade


def _normalized_retake_approval(value, original_id):
    """Approval is explicit and must identify the original transcript row."""
    value = value.lower()
    if value not in {"", "false", "0", "n", "아니오", "미승인", "true", "1", "y", "예", "승인"}:
        raise ValueError("재수강 승인 값은 true/false여야 합니다.")
    approved = value in {"true", "1", "y", "예", "승인"}
    if approved and not original_id:
        raise ValueError("승인된 재수강은 원래 성적 행번호(retake_of)가 필요합니다.")
    return approved


def _normalize(raw, i):
    c = {k: text(raw.get(k)) for k in FIELDS}
    c["row_id"] = c["row_id"] or f"R{i:04}"
    c["course_id"] = c["course_id"] or c["course_name"]
    if not c["course_name"] or len(c["course_name"]) > 150:
        raise ValueError("과목명이 없거나 너무 깁니다.")
    if any(ord(ch) < 32 for ch in c["course_name"]):
        raise ValueError("과목명에 제어 문자가 있습니다.")
    term_key(c["term"])
    c["category"] = CATEGORIES.get(c["category"])
    if not c["category"]:
        raise ValueError("이수구분은 교필/교선/전필/전선/일선 중 하나여야 합니다.")
    c["status"] = STATUSES.get(c["status"])
    if not c["status"]:
        raise ValueError("상태는 이수완료/수강중/예정/철회 중 하나여야 합니다.")
    c["credits"] = _normalized_credits(c["credits"])
    c["grade"] = _normalized_grade(c["grade"], c["status"])
    c["retake_approved"] = _normalized_retake_approval(c["retake_approved"], c["retake_of"])
    return c


def validate_courses(courses):
    errors, result, ids = [], [], set()
    if not isinstance(courses, list) or len(courses) > 2000:
        return [], ["과목 목록은 최대 2,000개 행이어야 합니다."]
    for i, raw in enumerate(courses, 1):
        try:
            if not isinstance(raw, dict):
                raise ValueError("과목 행이 객체 형식이 아닙니다.")
            c = _normalize(raw, i)
            if c["row_id"] in ids:
                raise ValueError("행번호가 중복되었습니다.")
            ids.add(c["row_id"])
            result.append(c)
        except ValueError as exc:
            errors.append(f"{i}번째 과목: {exc}")
    return result, errors


def _transcript_content(data):
    """Validate the file envelope and decode supported CSV encodings."""
    if not isinstance(data, (bytes, str)) or len(data) > 2 * 1024 * 1024:
        raise ValueError("2MB 이하의 CSV 파일만 지원합니다.")
    if isinstance(data, str):
        return data.lstrip("\ufeff")
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("UTF-8 또는 CP949 인코딩 CSV가 필요합니다.")


def _transcript_header(content):
    """Resolve supported aliases before reading any student row values."""
    reader = csv.DictReader(io.StringIO(content), strict=True)
    headers = [text(h) for h in (reader.fieldnames or [])]
    mapped = [ALIASES.get(h, h) for h in headers]
    if len(set(mapped)) != len(mapped):
        raise ValueError("중복된 열 이름이 있습니다.")
    required = {"course_name", "term", "category", "credits", "grade", "status"}
    missing = required - set(mapped)
    if missing:
        raise ValueError("필수 열 누락: " + ", ".join(sorted(missing)))
    removed = [h for h, m in zip(headers, mapped) if m not in FIELDS]
    return reader, mapped, removed


def _validate_csv_row(raw, headers, row_number):
    if row_number > 2000:
        raise ValueError("2,000개 행을 초과했습니다.")
    if None in raw:
        raise ValueError(f"{row_number}번째 과목의 열 수가 맞지 않습니다.")
    for header in headers:
        normalized_header = text(header)
        if normalized_header in DOMAIN_COLUMNS and text(raw.get(header)) != DOMAIN_COLUMNS[normalized_header]:
            raise ValueError(f"{row_number}번째 과목의 {normalized_header}이 지원하는 학교·학과·입학년도·과정과 다릅니다.")


def _transcript_rows(reader, mapped):
    rows = []
    for i, raw in enumerate(reader, 1):
        _validate_csv_row(raw, reader.fieldnames, i)
        rows.append({m: raw.get(h) for h, m in zip(reader.fieldnames, mapped) if m in FIELDS})
    if not rows:
        raise ValueError("성적표에 과목이 없습니다.")
    return rows


def parse_transcript(data: bytes | str) -> dict:
    """Decode → validate columns/rows → normalize; accept all rows or none."""
    result = {"ok": False, "courses": [], "errors": [], "warnings": [], "removed_columns": []}
    try:
        reader, mapped, removed = _transcript_header(_transcript_content(data))
        result["removed_columns"] = removed
        normalized, errors = validate_courses(_transcript_rows(reader, mapped))
        result["errors"] = errors
        if not errors:
            result.update(ok=True, courses=normalized)
        if removed:
            result["warnings"].append("미지원 열과 식별정보 열을 제거했습니다. 원본 CSV는 저장하지 않습니다.")
    except (csv.Error, ValueError) as exc:
        result["errors"] = [str(exc)]
    return result


def _retake_link_issue(course, original, accepted, replaced_rows):
    """Check the replacement link before reserving its cumulative workload."""
    name = course["course_name"]
    if (not course["retake_approved"] or not original or original["status"] != "completed"
            or term_key(original["term"]) >= term_key(course["term"])):
        return (f"{name}: 재수강 승인·이전 성적 연결 확인 필요",
                "재수강 승인 또는 이전 성적 연결 미확인: 제외")
    if course["status"] == "completed" and course["grade"] in {"A+", "F"}:
        return (f"{name}: 재수강 최고등급 또는 실패 후 원성적 처리 확인 필요",
                "재수강 성적 처리 규칙 확인 필요: 추가 인정 안 함")
    if original["row_id"] in replaced_rows:
        return f"{name}: 이미 대체된 원행을 다시 참조함", "재수강 원행 중복 참조: 제외"
    key = canonical_key(course)
    collision = any(
        other["row_id"] != original["row_id"]
        and (canonical_key(other) == key or other["course_id"] == course["course_id"])
        for other in accepted.values()
    )
    if collision:
        return f"{name}: 다른 인정 과목과 재수강 대체과목이 중복됨", "재수강 대체과목 중복: 제외"
    return None


def _review_retake(course, original, accepted, replaced_rows, seen, retake_credits):
    """Return the running workload and the first issue, without replacing a row.

    The existing policy reserves the attempt after link/collision checks, before
    the cumulative-limit and canonical-link checks. A later review does not
    release that workload; preserving this order prevents a refactor from
    silently changing the academic decision.
    """
    issue = _retake_link_issue(course, original, accepted, replaced_rows)
    if issue:
        return retake_credits, issue
    retake_credits += Decimal(str(course["credits"]))
    if retake_credits > 30:
        return retake_credits, ("재수강 누계 30학점 초과: 승인 내역 확인 필요", "재수강 한도 초과: 제외")
    if seen.get(canonical_key(original)) not in (None, original["row_id"]):
        return retake_credits, (f"{course['course_name']}: 이미 대체된 성적을 다시 참조함", "재수강 연결 중복: 제외")
    return retake_credits, None


def _replace_accepted_attempt(original, accepted, trace, replaced_rows, seen):
    """Remove the original only after every replacement check has passed."""
    original_id = original["row_id"]
    if original_id in accepted:
        del accepted[original_id]
    # A replaced F has no earned credits but must also leave the GPA ledger.
    trace[original_id].update(counted=False, gpa_counted=False, reason="승인된 재수강으로 대체됨: 중복 합산하지 않음")
    replaced_rows.add(original_id)
    seen.pop(canonical_key(original), None)


def _accepted_reason(course):
    if course["status"] == "in_progress":
        return "수강 중 과목의 합격을 가정한 예상 인정"
    if course["grade"] == "P":
        return "성적 확정·급제: 인정 (P는 평점 계산 제외)"
    return "성적 확정·급제: 인정"


def _credit_totals(accepted, pending):
    """Sum accepted courses only; pending workload is a separate field."""
    totals = dict(total=0, general_required=0, general_elective=0, major_required=0, major_total=0, other=0, in_progress=number(pending))
    sums = defaultdict(Decimal)
    mapping = {"교필": "general_required", "교선": "general_elective", "전필": "major_required", "전선": "major_elective", "일선": "other"}
    for course in accepted:
        credits = Decimal(str(course["credits"]))
        sums["total"] += credits
        sums[mapping[course["category"]]] += credits
    sums["major_total"] = sums["major_required"] + sums["major_elective"]
    totals.update({key: number(sums[key]) for key in totals if key != "in_progress"})
    return totals


def _calculate(courses, include_in_progress=False):
    """Walk attempts in order, decide recognition, then aggregate accepted rows."""
    ordered = sorted(courses, key=lambda c: (term_key(c["term"]), c["row_id"]))
    by_id = {c["row_id"]: c for c in ordered}
    accepted, seen, trace, review = {}, {}, {}, []
    replaced_rows = set()
    attempted_ids = set()
    pending = Decimal(0)
    retake_credits = Decimal(0)
    for c in ordered:
        cid = c["row_id"]
        trace[cid] = {"row_id": cid, "course_name": c["course_name"], "credits": c["credits"], "category": c["category"], "counted": False, "gpa_counted": False, "reason": ""}
        t = trace[cid]
        if c["status"] in {"planned", "withdrawn"}:
            t["reason"] = "계획/철회 과목: 취득 학점에 포함하지 않음"
            continue
        if c["status"] == "in_progress" and not include_in_progress:
            pending += Decimal(str(c["credits"]))
            t["reason"] = "수강 중: 현재 이수 학점 0, 예상 결과에서만 통과 가정"
            continue
        key = canonical_key(c)
        if c["retake_of"]:
            old = by_id.get(c["retake_of"])
            retake_credits, issue = _review_retake(c, old, accepted, replaced_rows, seen, retake_credits)
            if issue:
                review.append(issue[0])
                t["reason"] = issue[1]
                continue
            _replace_accepted_attempt(old, accepted, trace, replaced_rows, seen)
        elif key in seen or c["course_id"] in attempted_ids:
            t["reason"] = "동일 과목 중복: 나중 수강 성적 제외 (재수강은 승인 연결 필요)"
            review.append(f"{c['course_name']}: 중복 수강 또는 미표시 재수강 확인 필요")
            continue
        if c["grade"] == "F":
            seen[key] = cid
            attempted_ids.add(c["course_id"])
            t.update(gpa_counted=True, reason="F: 취득 학점 0")
            continue
        accepted[cid] = c
        seen[key] = cid
        attempted_ids.add(c["course_id"])
        t.update(counted=True, gpa_counted=c["grade"] in GRADES, reason=_accepted_reason(c))
    totals = _credit_totals(accepted.values(), pending)
    return {"totals": totals, "trace": list(trace.values()), "review_items": list(dict.fromkeys(review)), "accepted": list(accepted.values())}


def search_scores(courses):
    clean, errors = validate_courses(courses)
    if errors:
        return {"status": "확인 필요", "totals": {}, "trace": [], "review_items": errors, "terms": []}
    result = _calculate(clean)
    result["status"] = "확인 필요" if result["review_items"] else "집계 완료"
    terms = []
    # Recognition decisions come from the ledger, never from display wording.
    gpa_ids = {entry["row_id"] for entry in result["trace"] if entry["gpa_counted"]}
    for term in sorted({c["term"] for c in clean}, key=term_key):
        attempted = [c for c in clean if c["term"] == term and c["row_id"] in gpa_ids]
        denom = sum(Decimal(str(c["credits"])) for c in attempted)
        weighted = sum(Decimal(str(c["credits"])) * Decimal(str(GRADES[c["grade"]])) for c in attempted)
        earned = sum(c["credits"] for c in result["accepted"] if c["term"] == term)
        terms.append({"term": term, "earned_credits": earned, "gpa": round(float(weighted / denom), 3) if denom else None, "gpa_credits": number(denom)})
    result["terms"] = terms
    return result


def _profile_scope_issues(profile):
    issues = []
    for key, expected in [("school", "성결대학교"), ("department", "컴퓨터공학과"), ("entry_year", 2024), ("track", "전공심화"), ("enrollment_type", "신입학")]:
        if profile.get(key) != expected:
            issues.append(f"{key}: 이 버전은 {expected} 기준만 지원합니다.")
    if profile.get("engineering_accreditation") is not False:
        issues.append("공학인증 제외 설정 확인 필요")
    return issues


def _profile_semester_issues(profile):
    issues = []
    for key in ("registered_semesters", "completed_semesters", "chapel_pass_semesters"):
        value = profile.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 30:
            issues.append(f"{key}: 확인된 0~30 범위 정수가 필요합니다.")
    return issues


def _profile_course_term_issues(profile, courses):
    issues = []
    try:
        current = term_key(profile.get("current_term"))
        for c in courses:
            if c["status"] == "completed" and term_key(c["term"]) > current:
                issues.append("현재 학기보다 미래인 과목이 이수완료로 입력되었습니다.")
            if c["status"] == "in_progress" and c["term"] != profile["current_term"]:
                issues.append("수강 중 과목의 학기가 현재 학기와 다릅니다.")
    except ValueError:
        issues.append("현재 학기 정보가 없거나 잘못되었습니다.")
    return issues


def _profile_issues(profile, courses):
    """Keep scope, semester counts and course chronology separately readable."""
    issues = _profile_scope_issues(profile) + _profile_semester_issues(profile)
    if not issues and profile["completed_semesters"] > profile["registered_semesters"]:
        issues.append("수료 학기가 등록 학기보다 많습니다.")
    if profile.get("graduation_project_status") not in ("통과", "미완료", "불합격"):
        issues.append("졸업작품 심사 결과 확인 필요")
    return list(dict.fromkeys(issues + _profile_course_term_issues(profile, courses)))


def _diagnostic_row(area, required, earned, unit, source_ids):
    shortage = None if earned is None else max(0, number(Decimal(str(required)) - Decimal(str(earned))))
    status = "확인 필요" if earned is None else "충족" if shortage == 0 else "미충족"
    return dict(area=area, required=required, earned=earned, shortage=shortage,
                unit=unit, status=status, source_ids=source_ids)


def _required_course_met(required, accepted, categories):
    matches = [c for c in accepted if canonical_key(c) == required["course_id"] and c["category"] in categories]
    return bool(matches) and matches[0]["credits"] >= required["credits"]


def _required_course_rows(accepted):
    rows, missing = [], []
    groups = [("필수 교양과목", "general_required_courses", {"교필"}, "교필", ["course_guide"]),
              ("필수 설계과목", "major_required_courses", {"전필", "전선"}, "전필", ["major_notice"])]
    for label, group, categories, category, source_ids in groups:
        required_courses = rules()[group]
        found = 0
        for required in required_courses:
            if _required_course_met(required, accepted, categories):
                found += 1
            else:
                missing.append({"course_id": required["course_id"], "course_name": required["name"],
                                "credits": required["credits"], "category": category})
        rows.append(_diagnostic_row(label, len(required_courses), found, "과목", source_ids))
    return rows, missing


def _project_completion(profile):
    project = profile.get("graduation_project_status")
    return 1 if project == "통과" else 0 if project in ("미완료", "불합격") else None


def _diagnostic_rows(profile, calc):
    """Compare credits, named required courses and non-credit milestones."""
    r, totals = rules(), calc["totals"]
    rows = [
        _diagnostic_row("총 졸업학점", r["total_credits"], totals["total"], "학점", ["school_rules", "course_guide"]),
        _diagnostic_row("교양필수", r["general_required"], totals["general_required"], "학점", ["course_guide"]),
        _diagnostic_row("교양선택", r["general_elective"], totals["general_elective"], "학점", ["course_guide"]),
        _diagnostic_row("전공 합계 (주전공42 + 심화21)", r["major_total"], totals["major_total"], "학점", ["course_guide", "major_notice"]),
    ]
    required_rows, missing = _required_course_rows(calc["accepted"])
    rows.extend(required_rows)
    for label, key, required in [("채플", "chapel_pass_semesters", r["chapel_semesters"]),
                                 ("등록 학기", "registered_semesters", r["registered_semesters"])]:
        earned = profile.get(key) if type(profile.get(key)) is int else None
        rows.append(_diagnostic_row(label, required, earned, "학기", ["school_rules"]))
    rows.append(_diagnostic_row("졸업작품 심사", 1, _project_completion(profile), "건", ["school_rules", "major_notice"]))
    return rows, missing


def check_requirements(profile, courses):
    """현재와 수강중 통과 가정 결과를 반환. 결과를 공식 졸업사정으로 부르지 않는다."""
    profile = profile if isinstance(profile, dict) else {}
    clean, errors = validate_courses(courses)
    calc = _calculate(clean)
    review = errors + validate_rule_bundle() + _profile_issues(profile, clean) + calc["review_items"]
    if not clean:
        review.append("이수 내역이 없습니다. 성적표 확인 필요")
    rows, missing = _diagnostic_rows(profile, calc)
    status = "확인 필요" if review else "충족" if all(row["status"] == "충족" for row in rows) else "미충족"
    if status == "충족":
        answer = "응, 제공된 자료 기준으로 졸업요건은 충족했어! 최종 졸업 확정은 학교의 졸업사정 결과를 확인해야 해."
    elif status == "확인 필요":
        answer = "지금 자료만으로는 졸업이 괜찮다고 확정할 수 없어. 확인이 필요한 항목을 먼저 보완해야 해."
    else:
        answer = "현재는 졸업요건을 아직 모두 충족하지 않았어. 재학 중이라면 아래 부족분과 목표 졸업까지의 계획을 함께 확인하자."
    projected = _calculate(clean, include_in_progress=True)
    p_rows, p_missing = _diagnostic_rows(profile, projected)
    p_review = list(dict.fromkeys(review + projected["review_items"]))
    p_status = "확인 필요" if p_review else "충족" if all(x["status"] == "충족" for x in p_rows) else "미충족"
    evidence = evidence_for("credits", "general", "major", "noncredit", "grades", "retakes")
    evidence.append({"source_id": "student_input", "file_name": "가상 성적표·프로필 (세션 입력)", "url": None, "registered_date": None, "page": None, "date_note": "사용자/더미 입력. 공식 학적부·증명서가 아니며 외부 사실 검증을 대신하지 않음."})
    return dict(status=status, answer=answer, rows=rows, totals=calc["totals"], missing_required_courses=missing, review_items=list(dict.fromkeys(review)), trace=calc["trace"], evidence=evidence, meta=metadata(evidence), projection=dict(status=p_status, answer="수강 중 과목에 모두 합격한다고 가정한 학점 전망. 채플·등록 학기·작품 심사 결과는 자동 증가시키지 않음.", rows=p_rows, totals=projected["totals"], missing_required_courses=p_missing, review_items=p_review), rules_version=rules()["version"], official_graduation_confirmed=False)
