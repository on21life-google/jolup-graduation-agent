"""Same contracts and outputs as Green, with repeated assembly extracted."""
from copy import deepcopy

from jolup.academic import check_requirements
from jolup.calendar import create_calendar_event
from jolup.planner import plan_courses


def _snapshot(status, totals, rows, *, assumed):
    """Do not turn missing credit data into zero or assumed work into completion."""
    return {"status": status, "credits": totals.get("total"),
            "rows": deepcopy(rows), "is_assumption": assumed}


def _unique_records(records, keys):
    """Preserve source order and keep page/chunk distinctions where requested."""
    seen, unique = set(), []
    for record in records:
        identity = tuple(record.get(key) for key in keys)
        if identity not in seen:
            unique.append(deepcopy(record))
            seen.add(identity)
    return unique


def _source_cards(diagnosis, plan, regulation_result):
    records = diagnosis["evidence"] + plan["evidence"] + (regulation_result or {}).get("evidence", [])
    evidence = _unique_records(records, ("source_id", "page", "url", "chunk_id"))
    metadata_keys = ("source_id", "file_name", "url", "page", "registered_date", "date_note")
    meta = [{key: item.get(key) for key in metadata_keys}
            for item in _unique_records(evidence, ("source_id", "page", "url"))]
    return evidence, meta


def _timeline(profile, current, projection, plan):
    rows = [
        {"stage": "current", "term": profile.get("current_term"), **deepcopy(current)},
        {"stage": "in_progress_passed", "term": profile.get("current_term"), **deepcopy(projection)},
    ]
    for semester in plan["semesters"]:
        rows.append({
            "stage": "planned_semester", "term": semester["term"],
            **_snapshot(semester["projected_status"], semester["projected_totals"],
                        semester["projected_rows"], assumed=True),
            "assumptions": deepcopy(semester["projection_assumptions"]),
        })
    return rows


def build_learning_report(profile, courses, regulation_result=None):
    """Arrange real domain results for study; never calculate a parallel verdict."""
    diagnosis = check_requirements(profile, courses)
    plan = plan_courses(profile, courses)
    current = _snapshot(diagnosis["status"], diagnosis["totals"], diagnosis["rows"], assumed=False)
    projected = diagnosis["projection"]
    projection = _snapshot(projected["status"], projected["totals"], projected["rows"], assumed=True)
    evidence, meta = _source_cards(diagnosis, plan, regulation_result)
    return {
        "answer": diagnosis["answer"], "reason": deepcopy(diagnosis["rows"]),
        "current": current, "projection": projection,
        "readiness": deepcopy(plan), "timeline": _timeline(profile, current, projection, plan),
        "evidence": evidence, "meta": meta,
        "regulation": deepcopy(regulation_result),
        "official_graduation_confirmed": diagnosis["official_graduation_confirmed"],
    }


def export_reviewed_events(events, approved=False):
    # The one-line Green implementation has no duplicate to refactor.
    return create_calendar_event(events, approved=approved)
