"""Fictional fixtures only. A future graduation snapshot is explicitly labelled."""
from __future__ import annotations
import csv
import io
import json
from copy import deepcopy
from functools import lru_cache
from .settings import DATA, DEFAULT_PROFILE


@lru_cache(maxsize=1)
def _fixtures():
    catalog = json.loads((DATA / "courses.json").read_text(encoding="utf-8"))["courses"]
    by_name = {c["course_name"]: c for c in catalog}
    def row(c, term="2024-1", status="completed", grade="B+"):
        return {"row_id": c["course_id"], "course_id": c["course_id"], "course_name": c["course_name"], "term": term, "category": c["category"], "credits": c["credits"], "grade": grade, "status": status, "retake_of": "", "retake_approved": False}
    base = [row(c) for c in catalog if c["category"] == "교필"]
    majors = [c for c in catalog if c["category"] == "전선"][:15]
    base.extend(row(c) for c in majors)
    base.extend(row(c) for c in [c for c in catalog if c["category"] == "교선"][:7])
    for i, credits in enumerate([3, 3, 2, 2], 1):
        base.append(row(dict(course_id=f"DUMMY_O{i}", course_name=f"가상 자유선택 과목 {i}", category="일선", credits=credits)))
    # 90 confirmed credits across five semesters (18 each). Fixtures are not school records.
    loads = {term: 0 for term in ["2024-1", "2024-2", "2025-1", "2025-2", "2026-1"]}
    for c in sorted(base, key=lambda x: -x["credits"]):
        term = next(t for t, used in loads.items() if used + c["credits"] <= 18)
        c["term"] = term
        loads[term] += c["credits"]
    progress = [row(by_name[n], "2026-2", "in_progress", "") for n in ["전공종합설계(1)", "컴퓨터구조", "운영체제", "임베디드시스템", "머신러닝"]]
    profile = {**DEFAULT_PROFILE, "display_name": "김가온 (가상)", "student_id": "DUMMY-2024-001", "profile_note": "프로젝트용 가상 인물. 성적·수강·학적 상태는 전부 더미이며 공식 증명서가 아님."}
    samples = {}
    def add(sid, label, description, p, cs, status, total, major, **expected):
        samples[sid] = {"id": sid, "label": label, "description": description, "profile": {**deepcopy(p), "scenario_id": sid}, "courses": deepcopy(cs), "expected": {"status": status, "total": total, "major_total": major, **expected}}
    add("on_track", "01 · 목표까지 준비 가능한 사례", "2026-2 재학. 확정 90학점 + 수강 중 15학점. 남은 학기 계획과 비학점 요건을 충족하면 목표 졸업 경로가 있습니다.", profile, base + progress, "미충족", 90, 45, general_required=14, general_elective=21, other=10, in_progress=15, projected_total=105, plan_status="조건부 가능")

    full = deepcopy(base + progress)
    for c in full:
        if c["status"] == "in_progress":
            c.update(status="completed", grade="B+")
    for name in ["전공종합설계(2)", "고급네트워킹", "텍스트마이닝", "정보보안", "엔터프라이즈애플리케이션", "IT창업실습"]:
        term = "2027-1" if name in ["전공종합설계(2)", "고급네트워킹", "텍스트마이닝"] else "2027-2"
        full.append(row(by_name[name], term))
    future_profile = {**profile, "current_term": "2027-2", "completed_semesters": 8, "registered_semesters": 8, "chapel_pass_semesters": 6, "graduation_project_status": "통과", "profile_note": "2027-2 완료 시점을 가정한 미래 경계 시험. 현재 3학년 학생이 이미 졸업했다는 뜻이 아님."}
    add("fully_met", "02 · 모든 요건 충족 (미래 경계 시험)", "2027-2 완료를 가정한 별도 더미. 123학점·필수과목·채플·8학기·작품 심사를 모두 충족합니다.", future_profile, full, "충족", 123, 78, general_required=14, general_elective=21, other=10, in_progress=0, plan_status="충족")

    remove = {c["course_id"] for c in majors[:10]}
    ge_remove = {c["course_id"] for c in [c for c in catalog if c["category"] == "교선"][:4]}
    short = [c for c in base if c["course_id"] not in remove | ge_remove and c["category"] != "일선"]
    add("credit_shortfall", "03 · 총학점 부족 위험", "확정 38학점. 수강 중 15학점과 향후 정규 두 학기 한도를 반영해도 목표까지 총학점이 부족합니다.", profile, short + progress, "미충족", 38, 15, general_required=14, general_elective=9, other=0, in_progress=15, plan_status="위험")
    major_short = deepcopy(base)
    replace = {c["course_id"] for c in majors[:12]}
    for c in major_short:
        if c["course_id"] in replace:
            c.update(course_id="DUMMY_OTHER_" + c["course_id"], course_name="가상 타전공 과목 " + c["course_id"], category="일선")
    add("major_shortfall", "04 · 총학점은 쌓였지만 전공 부족", "확정 총 90학점 중 전공은 9학점. 향후 전공을 집중 수강해도 현재의 목표 학기까지 63학점에 도달하지 못합니다.", profile, major_short + progress, "미충족", 90, 9, general_required=14, general_elective=21, other=46, in_progress=15, plan_status="위험")

    core = deepcopy(full)
    c = next(c for c in core if c["course_id"] == "GE007")
    c.update(course_id="DUMMY_REPLACEMENT", course_name="가상 자유선택 1학점", category="일선")
    add("missing_core", "05 · 123학점이지만 필수과목 누락", "미래 경계 시험. 총학점만 맞고 사회봉사 1학점이 없으므로 충족으로 판정하면 안 됩니다.", future_profile, core, "미충족", 123, 78, general_required=13, general_elective=21, other=11, in_progress=0, missing_course="사회봉사")
    add("chapel_short", "06 · 채플 1학기 부족", "미래 경계 시험. 학점과 작품은 충족했지만 채플은 5/6학기입니다.", {**future_profile, "chapel_pass_semesters": 5}, full, "미충족", 123, 78, chapel_shortage=1)
    add("project_pending", "07 · 졸업작품 심사 미완료", "미래 경계 시험. 설계 과목 이수만으로 졸업작품 심사 통과를 대신할 수 없습니다.", {**future_profile, "graduation_project_status": "미완료"}, full, "미충족", 123, 78, project_shortage=1)
    pending = deepcopy(full)
    c = next(c for c in pending if c["course_id"] == "CS_CAP2")
    c.update(term="2027-2", status="in_progress", grade="")
    add("in_progress", "08 · 수강 중 3학점은 아직 미이수", "미래 경계 시험. 확정 120학점, 수강 중 설계(2) 3학점. 예상 123과 현재 120을 구분합니다.", {**future_profile, "completed_semesters": 7}, pending, "미충족", 120, 75, in_progress=3, projected_total=123, plan_status="조건부 가능")
    retake = deepcopy(full)
    c = next(c for c in retake if c["course_name"] == "C프로그래밍")
    original = deepcopy(c)
    original.update(row_id="OLD_C_FAIL", term="2024-1", grade="F")
    c.update(term="2025-1", grade="A0", retake_of="OLD_C_FAIL", retake_approved=True)
    retake.append(original)
    add("approved_retake", "09 · 승인 재수강을 한 번만 인정", "미래 경계 시험. C프로그래밍 F 후 승인 재수강 A0. 두 행이 있지만 학점은 3학점만 인정합니다.", future_profile, retake, "충족", 123, 78, plan_status="충족")
    add("missing_record", "10 · 채플 자료 누락", "현재 재학 사례. 채플 확인값이 없으므로 0학기로 추정하거나 졸업 가능을 확정하면 안 됩니다.", {**profile, "chapel_pass_semesters": None}, base + progress, "확인 필요", 90, 45)
    add("transfer", "11 · 미지원 편입 조건", "편입생은 별도 인정학점 규정이 필요합니다. 신입학 2024학번 규칙을 그대로 적용해 확정하지 않습니다.", {**profile, "enrollment_type": "편입학"}, base + progress, "확인 필요", 90, 45)
    return samples


def list_samples():
    return [{k: s[k] for k in ("id", "label", "description")} for s in _fixtures().values()]


def load_sample(sample_id):
    if sample_id not in _fixtures():
        raise ValueError("알 수 없는 더미 사례입니다.")
    sample = deepcopy(_fixtures()[sample_id])
    buf = io.StringIO(newline="")
    fields = ["이름", "학번", "row_id", "course_id", "course_name", "term", "category", "credits", "grade", "status", "retake_of", "retake_approved"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for c in sample["courses"]:
        writer.writerow({"이름": "김가온 (가상)", "학번": "DUMMY-2024-001", **c})
    sample["csv_bytes"] = buf.getvalue().encode("utf-8-sig")
    return sample
