import pytest
from copy import deepcopy
from jolup.academic import check_requirements, parse_transcript, search_scores
from jolup.planner import plan_courses
from jolup.samples import list_samples, load_sample


@pytest.mark.parametrize("sample_id", [s["id"] for s in list_samples()])
def test_independent_scenario_answers(sample_id):
    s = load_sample(sample_id)
    parsed = parse_transcript(s["csv_bytes"])
    assert parsed["ok"] and parsed["removed_columns"] == ["이름", "학번"]
    assert all("이름" not in c and "학번" not in c for c in parsed["courses"])
    d = check_requirements(s["profile"], parsed["courses"])
    e = s["expected"]
    assert d["status"] == e["status"]
    for key in ["total", "major_total", "general_required", "general_elective", "other", "in_progress"]:
        if key in e:
            assert d["totals"][key] == e[key], (sample_id, key)
    if "projected_total" in e:
        assert d["projection"]["totals"]["total"] == e["projected_total"]
    if "missing_course" in e:
        assert e["missing_course"] in [c["course_name"] for c in d["missing_required_courses"]]
    assert all(key in d for key in ["answer", "rows", "evidence", "meta"])
    assert d["official_graduation_confirmed"] is False
    if "plan_status" in e:
        plan = plan_courses(s["profile"], parsed["courses"])
        assert plan["status"] == e["plan_status"], plan


def test_old_major_required_overflow_not_extra_elective_shortage():
    s = load_sample("fully_met")
    for c in s["courses"]:
        if c["category"] == "전선":
            c["category"] = "전필"
    d = check_requirements(s["profile"], s["courses"])
    assert d["status"] == "충족"
    assert d["totals"]["major_total"] == 78
    assert not any("전공선택" in r["area"] for r in d["rows"])


def test_zero_credit_project_is_not_three_credit_course():
    s = load_sample("fully_met")
    s["courses"].append(dict(row_id="ZERO", course_id="ZERO", course_name="졸업작품", term="2027-2", category="전선", credits=0, grade="P", status="completed"))
    d = check_requirements(s["profile"], s["courses"])
    assert d["status"] == "충족" and d["totals"]["total"] == 123


def test_invalid_csv_not_partially_accepted():
    s = load_sample("on_track")
    corrupted = s["csv_bytes"].decode("utf-8-sig").replace(",B+,completed,", ",NO_GRADE,completed,", 1)
    parsed = parse_transcript(corrupted)
    assert not parsed["ok"] and parsed["courses"] == []


def test_plan_never_mutates_current_record_and_respects_18_36():
    s = load_sample("on_track")
    before = deepcopy(s)
    p = plan_courses(s["profile"], s["courses"])
    assert s == before
    assert p["current_totals"]["total"] == 90
    assert p["projected_totals"]["total"] >= 123
    assert all(x["total_credits"] <= 18 for x in p["semesters"])
    assert sum(x["total_credits"] for x in p["semesters"]) <= 36
    ids = [c["course_id"] for sem in p["semesters"] for c in sem["courses"]]
    assert len(set(ids)) == len(ids)
    assert "CS_CAP2" in ids and "CS_CAP1" not in ids
    assert all("미확인" in c["offering_status"] for sem in p["semesters"] for c in sem["courses"])
