"""First passing learning adapter: explicit field assembly using real engines.

This is new code for learning, not a reconstruction of historical commits.
"""
from copy import deepcopy

from jolup.academic import check_requirements
from jolup.calendar import create_calendar_event
from jolup.planner import plan_courses


def build_learning_report(profile, courses, regulation_result=None):
    diagnosis = check_requirements(profile, courses)
    plan = plan_courses(profile, courses)
    current = {
        "status": diagnosis["status"],
        "credits": diagnosis["totals"].get("total"),
        "rows": deepcopy(diagnosis["rows"]),
        "is_assumption": False,
    }
    projection = {
        "status": diagnosis["projection"]["status"],
        "credits": diagnosis["projection"]["totals"].get("total"),
        "rows": deepcopy(diagnosis["projection"]["rows"]),
        "is_assumption": True,
    }
    timeline = [
        {"stage": "current", "term": profile.get("current_term"), **deepcopy(current)},
        {"stage": "in_progress_passed", "term": profile.get("current_term"), **deepcopy(projection)},
    ]
    for semester in plan["semesters"]:
        timeline.append({
            "stage": "planned_semester", "term": semester["term"],
            "status": semester["projected_status"],
            "credits": semester["projected_totals"].get("total"),
            "rows": deepcopy(semester["projected_rows"]), "is_assumption": True,
            "assumptions": deepcopy(semester["projection_assumptions"]),
        })
    evidence = []
    seen = set()
    for item in diagnosis["evidence"] + plan["evidence"] + (regulation_result or {}).get("evidence", []):
        key = (item.get("source_id"), item.get("page"), item.get("url"), item.get("chunk_id"))
        if key not in seen:
            evidence.append(deepcopy(item))
            seen.add(key)
    meta = []
    seen_meta = set()
    for item in evidence:
        key = (item.get("source_id"), item.get("page"), item.get("url"))
        if key not in seen_meta:
            meta.append({key: item.get(key) for key in
                         ("source_id", "file_name", "url", "page", "registered_date", "date_note")})
            seen_meta.add(key)
    return {
        "answer": diagnosis["answer"], "reason": deepcopy(diagnosis["rows"]),
        "current": current, "projection": projection,
        "readiness": deepcopy(plan), "timeline": timeline,
        "evidence": evidence, "meta": meta,
        "regulation": deepcopy(regulation_result),
        "official_graduation_confirmed": diagnosis["official_graduation_confirmed"],
    }


def export_reviewed_events(events, approved=False):
    # Real calendar code owns validation, strict approval, date conversion and UID.
    return create_calendar_event(events, approved=approved)
