"""Exact course placement under explicit constraints, independent of any LLM.

Each variable means 'take this candidate in this semester' (0 or 1).
Shortage variables keep an impossible problem inspectable instead of crashing.
"""
from collections import defaultdict
import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp


def _graduation_shortfalls(base_totals, missing, requirements):
    """Each target supplies its missing amount and a course's contribution to it."""
    targets = [
        ("total", requirements["total_credits"], lambda c: c["credits"]),
        ("general_required", requirements["general_required"], lambda c: c["credits"] if c["category"] == "교필" else 0),
        ("general_elective", requirements["general_elective"], lambda c: c["credits"] if c["category"] == "교선" else 0),
        ("major_total", requirements["major_total"], lambda c: c["credits"] if c["category"] in {"전필", "전선"} else 0),
    ]
    needs = [(name, max(0, required - base_totals[name]), coef) for name, required, coef in targets]
    needs += [(c["course_id"], 1, lambda x, cid=c["course_id"]: int(x["course_id"] == cid)) for c in missing]
    return needs


def _selection_costs(choices, future, needs):
    """Prefer filling shortfalls, then fewer credits and earlier placements."""
    n, m = len(choices), len(needs)
    objective = np.zeros(n + m)
    for i, (term, c) in enumerate(choices):
        objective[i] = c["credits"] * (1 + future.index(term) * .01) + i * .000001
    # Any avoidable shortfall costs far more than all possible course selections.
    objective[n:] = [10000 if name == "total" else 20000 for name, _, _ in needs]
    return objective


def _constraint_row(size, coefficients, lo=-np.inf, hi=np.inf):
    """One linear rule: lo <= sum(coefficient * variable) <= hi."""
    row = np.zeros(size)
    for index, value in coefficients.items():
        row[index] = value
    return row, lo, hi


def _credit_limit_rows(choices, future, cap, annual, requirements, minimums, size):
    for term in future:
        coefficients = {i: c["credits"] for i, (t, c) in enumerate(choices) if t == term}
        yield _constraint_row(size, coefficients, lo=minimums.get(term, 0), hi=cap)
    for year in {t[:4] for t in future}:
        coefficients = {i: c["credits"] for i, (t, c) in enumerate(choices) if t[:4] == year}
        remaining = max(0, requirements["year_limit"] - annual.get(year, 0))
        yield _constraint_row(size, coefficients, hi=remaining)


def _one_attempt_rows(choices, size):
    """A candidate may be chosen in at most one future semester."""
    for cid in {c["course_id"] for _, c in choices}:
        coefficients = {i: 1 for i, (_, c) in enumerate(choices) if c["course_id"] == cid}
        yield _constraint_row(size, coefficients, hi=1)


def _prerequisite_rows(choices, known_ids, size):
    """A prerequisite must already be known or placed in a strictly earlier term."""
    for i, (term, c) in enumerate(choices):
        for prereq in c.get("prerequisites", []):
            if prereq in known_ids:
                continue
            coefficients = {j: -1 for j, (t, other) in enumerate(choices)
                            if other["course_id"] == prereq and t < term}
            coefficients[i] = 1
            yield _constraint_row(size, coefficients, hi=0)


def _shortfall_rows(choices, needs, size):
    """Selected credits plus an explicit slack variable must cover each target."""
    n = len(choices)
    for j, (_, need, coefficient) in enumerate(needs):
        coefficients = {i: coefficient(c) for i, (_, c) in enumerate(choices)}
        coefficients[n + j] = 1
        yield _constraint_row(size, coefficients, lo=need)


def _model_constraints(choices, future, cap, annual, requirements, minimums, known_ids, needs):
    """Keep the original rule ordering: loads, duplicates, prerequisites, shortfalls."""
    size = len(choices) + len(needs)
    constraints = [
        *_credit_limit_rows(choices, future, cap, annual, requirements, minimums, size),
        *_one_attempt_rows(choices, size),
        *_prerequisite_rows(choices, known_ids, size),
        *_shortfall_rows(choices, needs, size),
    ]
    rows, lower, upper = zip(*constraints)
    return LinearConstraint(np.array(rows), np.array(lower), np.array(upper))


def _selected_courses(choices, values):
    selected = defaultdict(list)
    for i, (term, c) in enumerate(choices):
        if values[i] > .5:
            selected[term].append(c)
    return selected


def _semester_loads_valid(selected, future, minimums, cap):
    below_cap = all(sum(c["credits"] for c in cs) <= cap + 1e-6 for cs in selected.values())
    above_minimum = all(sum(c["credits"] for c in selected.get(term, [])) >= minimums.get(term, 0) - 1e-6
                        for term in future)
    return below_cap and above_minimum


def _annual_loads_valid(selected, future, annual, year_limit):
    for year in {t[:4] for t in future}:
        used = sum(c["credits"] for t, cs in selected.items() if t[:4] == year for c in cs)
        within_limit = used + annual.get(year, 0) <= year_limit + 1e-6
        if not within_limit:
            return False
    return True


def _prerequisites_valid(selected, known_ids):
    for t, cs in selected.items():
        before = known_ids | {c["course_id"] for earlier, prev in selected.items() if earlier < t for c in prev}
        if not all(set(c.get("prerequisites", [])).issubset(before) for c in cs):
            return False
    return True


def _selection_valid(selected, future, minimums, cap, annual, year_limit, known_ids):
    """Recheck hard rules independently of solver success or its time-limit status."""
    chosen_ids = [c["course_id"] for cs in selected.values() for c in cs]
    return (len(chosen_ids) == len(set(chosen_ids))
            and _semester_loads_valid(selected, future, minimums, cap)
            and _annual_loads_valid(selected, future, annual, year_limit)
            and _prerequisites_valid(selected, known_ids))


def choose_courses(candidates, future, cap, annual, base_totals, missing, known_ids, requirements, minimums=None):
    """Build the unchanged MILP, solve once, then independently validate its selection."""
    minimums = minimums or {}
    choices = [(term, c) for term in future for c in candidates if int(term[-1]) in c["offered_semesters"]]
    needs = _graduation_shortfalls(base_totals, missing, requirements)
    n, m = len(choices), len(needs)
    objective = _selection_costs(choices, future, needs)
    constraints = _model_constraints(choices, future, cap, annual, requirements, minimums, known_ids, needs)
    upper_bounds = np.array([1] * n + [need for _, need, _ in needs], dtype=float)
    result = milp(objective, integrality=np.array([1] * n + [0] * m),
                  bounds=Bounds(np.zeros(n + m), upper_bounds), constraints=constraints,
                  options={"time_limit": 10, "mip_rel_gap": 0.0})
    if result.x is None:
        return {}, {"algorithm": "MILP", "status": "확인 필요", "optimal": False, "feasible": False, "reason": "최소 수강학점·연간 한도·후보 개설 조건을 함께 만족하는 배치를 확인하지 못했습니다. 학과의 예외 승인 또는 개설 자료 확인이 필요합니다."}
    selected = _selected_courses(choices, result.x)
    report = {"algorithm": "MILP: 과목·학기 동시 배치", "status": "검증 완료" if result.success else "탐색 제한", "optimal": bool(result.success), "feasible": True, "remaining_slack": {name: round(float(result.x[n + j]), 4) for j, (name, _, _) in enumerate(needs)}}
    if not _selection_valid(selected, future, minimums, cap, annual, requirements["year_limit"], known_ids):
        return {}, {"algorithm": "MILP", "status": "확인 필요", "optimal": False, "feasible": False, "reason": "배치 결과의 학점·중복·순서 검증에 실패했습니다."}
    return dict(selected), report
