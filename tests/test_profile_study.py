"""The local study profiler must explain real core calls without recording data."""
import json
from pathlib import Path
import socket

import pytest

from scripts import profile_study as study


@pytest.mark.parametrize("action,entry", [("calculate", "check_requirements"), ("plan", "plan_courses")])
def test_builtin_run_records_real_calls_without_student_values_or_network(action, entry, monkeypatch):
    def reject_network(*args, **kwargs):
        pytest.fail("the local profiler must not open a network connection")
    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setenv("OPENAI_API_KEY", "PRIVATE_ENV_VALUE_MUST_NOT_APPEAR")
    report = study.run_profile(action, "on_track", 30)
    text = json.dumps(report, ensure_ascii=False)
    assert report["expected_entry"] == entry
    observed = report["call_observation"]
    names = {row["id"]: row["function"] for row in observed["functions"]}
    assert entry in names.values() and report["entry_observed"]
    assert observed["tool"] == "Python sys.monitoring PY_START"
    if action == "plan":
        actual_edges = {(names[edge["caller"]], names[edge["callee"]]) for edge in observed["calls"]}
        assert ("plan_courses", "_select_future_courses") in actual_edges
        assert ("_select_future_courses", "choose_courses") in actual_edges
        assert ("plan_courses", "_plan_conclusion") in actual_edges
    assert all(row["calls"] > 0 for row in observed["functions"])
    assert report["wall_ms"] > 0
    assert all(row["file"] in study.CORE_FILES and row["calls"] > 0 for row in report["functions"])
    assert report["calls"]
    ids = {row["id"] for row in report["functions"]}
    assert all(edge["caller"] in ids and edge["callee"] in ids for edge in report["calls"])
    assert report["warmup_runs"] == report["measured_runs"] == 1
    assert report["arguments_recorded"] is report["return_values_recorded"] is False
    assert "PRIVATE_ENV_VALUE_MUST_NOT_APPEAR" not in text
    assert "김가온" not in text and "DUMMY-2024-001" not in text
    assert "student_id" not in text and "profile_note" not in text
    assert set(report["source_sha256"]) == study.CORE_FILES


@pytest.mark.parametrize("kwargs", [
    {"action": "agent"}, {"sample_id": "../../.env"}, {"sample_id": "upload"},
    {"top": 0}, {"top": 31}, {"top": True}, {"top": 5.5},
])
def test_only_bounded_builtin_calculation_is_accepted(kwargs):
    with pytest.raises(ValueError):
        study.run_profile(**kwargs)


def test_only_observed_edges_and_allowlisted_core_files_are_serialized():
    first = (str(study.ROOT / "jolup/academic.py"), 1, "first")
    second = (str(study.ROOT / "jolup/planner.py"), 5, "second")
    outside = ("/outside/private.py", 2, "sensitive")
    llm = (str(study.ROOT / "jolup/llm.py"), 1, "api_call")
    stats = {
        first: (1, 1, .001, .009, {}),
        second: (2, 3, .002, .008, {first: (2, 3, .002, .008)}),
        outside: (1, 1, 8, 9, {first: (1, 1, 8, 9)}),
        llm: (1, 1, 8, 9, {first: (1, 1, 8, 9)}),
    }
    report = study.summarize_stats(stats, 5)
    assert len(report["functions"]) == 2
    assert report["functions"][0]["function"] == "first"
    assert report["functions"][1]["calls"] == 3
    assert report["functions"][1]["primitive_calls"] == 2
    assert report["calls"] == [{"caller": "jolup/academic.py:1:first", "callee": "jolup/planner.py:5:second",
                                "calls": 3, "cumulative_ms": 8.0}]
    text = json.dumps(report)
    assert "private.py" not in text and "api_call" not in text


def test_graph_limits_report_omissions_instead_of_inventing_full_path():
    keys = [(str(study.ROOT / "jolup/academic.py"), i, f"function_{i}") for i in range(10)]
    stats = {key: (1, 1, .001, .01, {caller: (1, 1, .001, .01) for caller in keys}) for key in keys}
    summary = study.summarize_stats(stats, 5)
    assert len(summary["functions"]) == 5 and summary["functions_omitted"] == 5
    assert len(summary["calls"]) == 10 and summary["edges_omitted_between_selected_functions"] == 15


def test_report_is_readable_mermaid_with_escaped_comprehensions(tmp_path):
    report = study.run_profile("calculate", "fully_met", 8)
    report["functions"][0]["function"] = "<listcomp>"
    report["call_observation"]["functions"][0]["function"] = "<listcomp>"
    paths = study.write_reports(report, tmp_path)
    assert len(paths) == 2 and all(path.parent == tmp_path / "reports/profile_study" for path in paths)
    assert json.loads(paths[0].read_text()) == report
    markdown = paths[1].read_text()
    assert "flowchart LR" in markdown and "&lt;listcomp&gt;" in markdown
    assert "sequenceDiagram" not in markdown
    assert "cumulative를 합산하지 않습니다" in markdown
    assert "실행 순서나 전체 경로를 뜻하지 않습니다" in markdown


def test_monitoring_slots_are_released_when_the_observed_function_fails():
    import sys
    observer = study.CallObserver()
    before = {slot: sys.monitoring.get_tool(slot) for slot in (3, 4)}
    def fail():
        raise RuntimeError("expected test failure")
    with pytest.raises(RuntimeError, match="expected test failure"):
        observer.run(fail)
    assert {slot: sys.monitoring.get_tool(slot) for slot in (3, 4)} == before


def test_busy_monitoring_slots_are_not_stolen(monkeypatch):
    import sys
    observer = study.CallObserver()
    monkeypatch.setattr(sys.monitoring, "get_tool", lambda slot: "another-tool")
    with pytest.raises(ValueError, match="slots are busy"):
        observer.run(lambda: None)


def test_occupied_first_slot_is_preserved_while_the_second_is_used():
    import sys
    sys.monitoring.use_tool_id(4, "test-other-tool")
    try:
        assert study.CallObserver().run(lambda: "result") == "result"
        assert sys.monitoring.get_tool(4) == "test-other-tool"
        assert sys.monitoring.get_tool(3) is None
    finally:
        sys.monitoring.free_tool_id(4)


def test_observer_does_not_invent_missing_callers_or_mix_threads(monkeypatch):
    from types import SimpleNamespace
    from jolup.academic import number
    observer = study.CallObserver()
    with monkeypatch.context() as patch:
        patch.setattr(study.sys, "_getframe", lambda depth: SimpleNamespace(f_back=None))
        observer.on_start(number.__code__, 0)
    assert len(observer.summary()["functions"]) == 1
    assert observer.summary()["calls"] == []
    observer.edges[("not-observed", next(iter(observer.locations)))] = 1
    assert observer.summary()["calls"] == []
    monkeypatch.setattr(study, "get_ident", lambda: observer.owner + 1)
    observer.on_start(number.__code__, 0)
    assert next(iter(observer.counts.values())) == 1


def test_missing_cprofile_timing_does_not_remove_observed_call_graph(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(study.pstats, "Stats", lambda profiler: SimpleNamespace(stats={}))
    report = study.run_profile("calculate", "on_track", 5)
    assert report["entry_observed"] and not report["timing_entry_observed"]
    assert report["functions"] == [] and report["call_observation"]["functions"]
    markdown = study.render_markdown(report)
    assert "시간 통계 주의" in markdown and "check_requirements" in markdown


def test_entry_callback_uses_code_locations_and_only_the_immediate_caller(monkeypatch):
    from types import SimpleNamespace
    from jolup.academic import number, name_key
    study._code_location.cache_clear()
    assert study._code_location(number.__code__)["function"] == "number"
    observer = study.CallObserver()
    observer.on_start(test_entry_callback_uses_code_locations_and_only_the_immediate_caller.__code__, 0)
    assert observer.summary()["functions"] == []
    caller = SimpleNamespace(f_code=number.__code__)
    with monkeypatch.context() as patch:
        patch.setattr(study.sys, "_getframe", lambda depth: SimpleNamespace(f_back=caller))
        observer.on_start(number.__code__, 0)
        observer.on_start(name_key.__code__, 0)
    observed = observer.summary()
    names = {entry["id"]: entry["function"] for entry in observed["functions"]}
    assert ("number", "name_key") in {(names[edge["caller"]], names[edge["callee"]]) for edge in observed["calls"]}


def test_script_entry_point_reports_invalid_sample_without_writing_files(monkeypatch):
    import runpy
    import sys
    monkeypatch.setattr(sys, "argv", ["profile_study.py", "--sample", "not-a-dummy"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(study.ROOT / "scripts/profile_study.py"), run_name="__main__")
    assert error.value.code == 2


def test_symlink_output_is_rejected(tmp_path):
    outside = tmp_path / "other"
    outside.mkdir()
    (tmp_path / "reports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        study.write_reports({}, tmp_path)
    assert not list(outside.iterdir())


def test_cli_writes_fixed_local_scope_and_rejects_unknown_sample(monkeypatch, capsys):
    written = []
    def fake_write(report):
        written.append(report)
        return [study.ROOT / "reports/profile_study/example.json"]
    monkeypatch.setattr(study, "write_reports", fake_write)
    assert study.main(["--sample", "fully_met", "--top", "5"]) == 0
    assert written[0]["builtin_sample_id"] == "fully_met"
    assert "reports/profile_study/example.json" in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        study.main(["--sample", "not-a-dummy"])
    assert error.value.code == 2
