"""Observe one built-in calculation locally; persist names/counts, never arguments."""
from __future__ import annotations

import argparse
import cProfile
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import html
import json
from pathlib import Path
import pstats
import sys
from threading import get_ident
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
CORE_FILES = frozenset({"jolup/academic.py", "jolup/planner.py", "jolup/course_solver.py", "jolup/evidence.py"})
ACTIONS = ("calculate", "plan")
REPORT_DIR = Path("reports/profile_study")


def _core_key(key):
    """Drop interpreter, library and non-core frames before serialization."""
    filename, line, name = key
    try:
        relative = Path(filename).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return None
    if relative not in CORE_FILES:
        return None
    return {"id": f"{relative}:{line}:{name}", "file": relative, "line": line, "function": name}


def summarize_stats(stats, top):
    """Keep the slowest core functions and only observed edges between them."""
    rows = []
    keys = {}
    for key, (primitive, calls, self_seconds, cumulative, _callers) in stats.items():
        location = _core_key(key)
        if location is None:
            continue
        keys[key] = location["id"]
        rows.append({**location, "primitive_calls": primitive, "calls": calls,
                     "self_ms": round(self_seconds * 1000, 6),
                     "cumulative_ms": round(cumulative * 1000, 6)})
    rows.sort(key=lambda row: (-row["cumulative_ms"], row["id"]))
    selected = rows[:top]
    selected_ids = {row["id"] for row in selected}
    edges = []
    for callee, values in stats.items():
        if keys.get(callee) not in selected_ids:
            continue
        for caller, measurements in values[4].items():
            if keys.get(caller) in selected_ids:
                edges.append({"caller": keys[caller], "callee": keys[callee],
                              "calls": measurements[1],
                              "cumulative_ms": round(measurements[3] * 1000, 6)})
    edges.sort(key=lambda edge: (-edge["cumulative_ms"], edge["caller"], edge["callee"]))
    edge_limit = top * 2
    return {"functions": selected, "calls": edges[:edge_limit],
            "core_functions_observed": len(rows), "functions_omitted": max(0, len(rows) - top),
            "edges_omitted_between_selected_functions": max(0, len(edges) - edge_limit)}


@lru_cache(maxsize=4096)
def _code_location(code):
    return _core_key((code.co_filename, code.co_firstlineno, code.co_name))


class CallObserver:
    """Record actual Python entries and immediate core callers on this thread.

    PY_START observes function entry independently of cProfile's return/timing
    stack. No local variables, arguments, return values or exception values are
    read. Generator resumption is not counted as a new function invocation.
    """

    def __init__(self):
        self.owner = get_ident()
        self.locations = {}
        self.counts = Counter()
        self.edges = Counter()

    def on_start(self, code, offset):
        del offset
        if get_ident() != self.owner:
            return
        location = _code_location(code)
        if location is None:
            return
        identifier = location["id"]
        self.locations[identifier] = location
        self.counts[identifier] += 1
        caller = sys._getframe(1).f_back
        parent = _code_location(caller.f_code) if caller is not None else None
        if parent is not None:
            self.edges[(parent["id"], identifier)] += 1

    def run(self, function, *args):
        monitor = sys.monitoring
        slot = next((slot for slot in (4, 3) if monitor.get_tool(slot) is None), None)
        if slot is None:
            raise ValueError("function observation slots are busy; close the other monitoring tool and retry")
        monitor.use_tool_id(slot, "jolup-study-calls")
        try:
            monitor.register_callback(slot, monitor.events.PY_START, self.on_start)
            monitor.set_events(slot, monitor.events.PY_START)
            return function(*args)
        finally:
            monitor.set_events(slot, 0)
            monitor.register_callback(slot, monitor.events.PY_START, None)
            monitor.free_tool_id(slot)

    def summary(self):
        return {"tool": "Python sys.monitoring PY_START", "thread_scope": "calling thread only",
                "functions": [{**location, "calls": self.counts[identifier]}
                              for identifier, location in sorted(self.locations.items())],
                "calls": [{"caller": caller, "callee": callee, "calls": count}
                          for (caller, callee), count in sorted(self.edges.items())
                          if caller in self.locations and callee in self.locations]}


def run_profile(action="calculate", sample_id="on_track", top=12):
    """Profile fixed offline functions using an unchanged built-in dummy fixture."""
    if action not in ACTIONS:
        raise ValueError("action must be calculate or plan")
    if type(top) is not int or not 5 <= top <= 30:
        raise ValueError("top must be an integer from 5 to 30")
    from jolup.samples import list_samples, load_sample
    if sample_id not in {sample["id"] for sample in list_samples()}:
        raise ValueError("only a built-in dummy sample ID is allowed")
    from jolup.academic import check_requirements
    from jolup.planner import plan_courses
    function = check_requirements if action == "calculate" else plan_courses
    sample = load_sample(sample_id)
    # Warm imports/caches before measurement; no UI, Agent, LLM or MCP entry point.
    function(sample["profile"], sample["courses"])
    profiler = cProfile.Profile()
    observer = CallObserver()
    started = perf_counter()
    observer.run(profiler.runcall, function, sample["profile"], sample["courses"])
    wall_ms = (perf_counter() - started) * 1000
    stats = pstats.Stats(profiler).stats
    timing_entry_seen = any(key[2] == function.__name__ and _core_key(key) for key in stats)
    observed = observer.summary()
    entry_seen = any(row["function"] == function.__name__ for row in observed["functions"])
    result = summarize_stats(stats, top)
    return {"schema_version": 2, "tool": "Python sys.monitoring calls + cProfile timing (not AppMap)",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "action": action, "builtin_sample_id": sample_id,
            "warmup_runs": 1, "measured_runs": 1, "top": top,
            "wall_ms": round(wall_ms, 6), "expected_entry": function.__name__,
            "entry_observed": entry_seen,
            "timing_entry_observed": timing_entry_seen, "call_observation": observed,
            "measurement_warning": None if timing_entry_seen else "cProfile 시간 통계에는 진입 함수가 없습니다. 호출 관계는 별도의 sys.monitoring 관찰 기록을 사용하며 누락된 시간을 추정하지 않습니다.",
            "arguments_recorded": False, "return_values_recorded": False,
            "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                              for name in sorted(CORE_FILES)}, **result}


def render_markdown(report):
    """The graph is an aggregated call graph, never a fabricated sequence."""
    rows = report["functions"]
    observed = report["call_observation"]
    graph_rows = observed["functions"]
    lines = ["# 더미 사례 함수 분석", "",
             f"- 도구: {report['tool']}",
             f"- 대상: `{report['action']}` / 내장 사례 `{report['builtin_sample_id']}`",
             "- 준비 실행 1회 후 1회 측정. 입력·반환값·환경변수는 보고서에 저장하지 않습니다.",
             "- self는 함수 자체, cumulative는 하위 호출을 포함한 시간입니다. cumulative를 합산하지 않습니다.",
             "- 계측 비용과 실행 환경의 영향을 받는 학습용 측정이며 성능 보장 수치가 아닙니다.", "",
             "| 함수 위치 | 호출 수 | self ms | cumulative ms |",
             "|---|---:|---:|---:|"]
    for row in rows:
        label = f"{row['file']}:{row['line']} {row['function']}"
        lines.append(f"| `{label}` | {row['calls']} | {row['self_ms']:.3f} | {row['cumulative_ms']:.3f} |")
    lines.extend(["", f"측정 구간 전체 경과 시간: {report['wall_ms']:.3f} ms. "
                  f"진입 함수 `{report['expected_entry']}`의 호출 관찰 여부: {report['entry_observed']}. "
                  f"cProfile 시간 통계의 진입 함수 포함 여부: {report['timing_entry_observed']}."])
    if report["measurement_warning"]:
        lines.extend(["", f"**시간 통계 주의:** {report['measurement_warning']}"])
    lines.extend(["", "## 실제 호출 관계", "",
                  "화살표는 이번 측정에서 관찰한 호출자 → 피호출자 관계입니다. 실행 순서나 전체 경로를 뜻하지 않습니다.",
                  f"sys.monitoring이 이번 실행에서 관찰한 코어 함수 {len(graph_rows)}개와 직접 호출 관계 "
                  f"{len(observed['calls'])}개를 모두 표시합니다. 다른 입력의 실행 경로나 외부 라이브러리 내부는 포함하지 않습니다. "
                  f"위 cProfile 시간 표에서만 함수 {report['functions_omitted']}개를 표시 한도로 생략했습니다. "
                  "호출 수는 PY_START 횟수이며 생성기의 재개는 새 호출로 세지 않습니다.",
                  "", "```mermaid", "flowchart LR"])
    ids = {row["id"]: f"f{index}" for index, row in enumerate(graph_rows)}
    for row in graph_rows:
        label = html.escape(f"{row['function']} · {row['file']}:{row['line']}", quote=True)
        lines.append(f'    {ids[row["id"]]}["{label}"]')
    for edge in observed["calls"]:
        lines.append(f'    {ids[edge["caller"]]} -->|"{edge["calls"]}회"| {ids[edge["callee"]]}')
    lines.extend(["```", "", "## 읽는 순서", "",
                  "1. 진입 함수 `check_requirements` 또는 `plan_courses`에서 시작합니다.",
                  "2. 관찰된 화살표를 따라 하위 함수의 역할과 호출 수를 확인합니다. 시간 표는 보조 자료입니다.",
                  "3. 관련 테스트의 기대 결과를 확인한 뒤 리팩터링합니다.",
                  "4. 동일 사례·동일 옵션으로 재측정하되 호출 수와 책임 분리가 이해하기 쉬워졌는지도 함께 비교합니다.", ""])
    return "\n".join(lines)


def write_reports(report, root=ROOT):
    folder = Path(root) / REPORT_DIR
    # Fixed output scope; do not follow a report directory symlink elsewhere.
    if folder.is_symlink() or folder.parent.is_symlink():
        raise ValueError("report directory must not be a symlink")
    folder.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    paths = [folder / f"{stem}.json", folder / f"{stem}.md"]
    contents = [json.dumps(report, ensure_ascii=False, indent=2) + "\n", render_markdown(report)]
    for path, content in zip(paths, contents):
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=ACTIONS, default="calculate")
    parser.add_argument("--sample", default="on_track", help="built-in dummy sample ID")
    parser.add_argument("--top", type=int, default=12, help="number of core functions to show (5–30)")
    args = parser.parse_args(argv)
    try:
        paths = write_reports(run_profile(args.action, args.sample, args.top))
    except (ValueError, OSError) as error:
        parser.error(str(error))
    for path in paths:
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
