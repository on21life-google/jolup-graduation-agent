"""Trace data minimization and export lifecycle contracts, always offline."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from jolup import observability as obs
from jolup.samples import load_sample

CONFIG = {"api_key": "lsv2_offline_fixture", "project": "jolup-study", "region": "US"}


def trace_with(operation=lambda: {"intent": "diagnosis"}):
    sample = load_sample("on_track")
    recorder = obs.StudyRecorder(sample["profile"], sample["courses"], builtin_sample_id="on_track")
    with recorder.activate():
        result = obs.observe_call("route", operation)
    return recorder.finish(result)


def sign_fixture(trace):
    trace["integrity"] = obs._sign({key: value for key, value in trace.items() if key != "integrity"})
    return trace


def test_summary_ignores_invalid_containers_and_includes_only_stable_enums():
    assert obs._plan_summary(None) == {} and obs._totals(None) == {}
    assert obs._summary({"result": {"intent": "calendar"}}) == {"intent": "calendar"}
    result = obs._summary({"totals": [], "projection": {"totals": {}, "status": "private"}, "semesters": [
        None, {"term": "private"}, {"term": "2027-1", "total_credits": -1},
    ], "used": True, "error": "private error"})
    assert result == {"totals": {}, "projection": {"totals": {}}, "semesters": [{"term": "2027-1"}], "used": True, "error_code": "provider_error"}
    assert obs._safe_summary({"status": []}) == {"error_code": "summary_unavailable"}


def test_unknown_builtin_id_is_local_only():
    recorder = obs.StudyRecorder({}, [], builtin_sample_id="not_a_builtin")
    assert recorder.case_id is None and recorder.exportable is False


def test_trace_limit_keeps_business_calls_but_blocks_incomplete_export(monkeypatch):
    monkeypatch.setattr(obs, "_MAX_STEPS", 2)
    sample = load_sample("on_track")
    recorder = obs.StudyRecorder(sample["profile"], sample["courses"], builtin_sample_id="on_track")
    calls = []
    with recorder.activate():
        for i in range(3):
            assert obs.observe_call("route", lambda value: calls.append(value) or value, i) == i
    trace = recorder.finish({})
    assert calls == [0, 1, 2]
    assert len(trace["steps"]) == 2 and trace["truncated"] is True and trace["exportable"] is False


@pytest.mark.parametrize("change", [
    "root_case", "root_uuid", "root_time", "empty_steps", "step_shape", "step_operation", "step_parent", "step_summary", "step_time", "timezone", "naive_time",
])
def test_valid_signature_cannot_bypass_trace_schema_and_parent_intervals(change):
    trace = trace_with()
    step = trace["steps"][0]
    if change == "root_case":
        trace["case_id"] = "unknown"
    elif change == "root_uuid":
        trace["run_id"] = trace["run_id"].upper()
    elif change == "root_time":
        trace["end_time"] = "2020-01-01T00:00:00+00:00"
    elif change == "empty_steps":
        trace["steps"] = []
    elif change == "step_shape":
        step["raw_query"] = "private"
    elif change == "step_operation":
        step["source_ref"] = "unknown.py:secret"
    elif change == "step_parent":
        step["parent_id"] = str(uuid4())
    elif change == "step_summary":
        step["summary"]["raw_query"] = "private"
    elif change == "step_time":
        step["end_time"] = "2099-01-01T00:00:00+00:00"
    elif change == "timezone":
        trace["start_time"] = "2020-01-01T00:00:00+09:00"
    else:
        trace["start_time"] = "2020-01-01T00:00:00"
    assert obs._validated_trace(sign_fixture(trace)) is None


def test_nontrace_and_oversized_payloads_are_rejected_before_transport():
    assert obs._validated_trace(None) is None
    assert obs._validated_trace({"data": "x" * 200001}) is None
    assert obs._validated_trace({"data": object()}) is None


def test_failed_operation_has_generic_error_on_root_and_child_payloads():
    trace = trace_with(lambda: {"error_code": "timeout", "error": "private"})
    assert trace["status"] == "error"
    payloads = obs._export_payloads(trace, "jolup-study")
    assert payloads[0]["error"] == "execution_error" and payloads[1]["error"] == "timeout"
    assert "private" not in json.dumps(payloads)


def test_owned_http_client_uses_fixed_transport_and_closes_after_export(monkeypatch):
    captured = {}
    class Client:
        def __init__(self, **kwargs):
            captured["settings"] = kwargs
        def post(self, url, **kwargs):
            captured.setdefault("posts", []).append((url, kwargs))
            return SimpleNamespace(status_code=202)
        def close(self):
            captured["closed"] = True
    monkeypatch.setattr(obs.httpx, "Client", Client)
    result = obs.export_study_trace(trace_with(), CONFIG)
    assert result["status"] == "sent" and captured["closed"] is True
    assert captured["settings"]["trust_env"] is False and captured["settings"]["follow_redirects"] is False
    assert len(captured["posts"]) == 2


def test_client_cleanup_failure_cannot_change_accepted_export(monkeypatch):
    class Client:
        def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=202)
        def close(self):
            raise RuntimeError("private cleanup")
    monkeypatch.setattr(obs.httpx, "Client", lambda **kwargs: Client())
    assert obs.export_study_trace(trace_with(), CONFIG)["status"] == "sent"


@pytest.mark.parametrize("failure", [OSError("private connection"), RuntimeError("private runtime")])
def test_constructor_failure_is_redacted_without_retry(monkeypatch, failure):
    calls = []
    def construct(**kwargs):
        calls.append(kwargs)
        raise failure
    monkeypatch.setattr(obs.httpx, "Client", construct)
    result = obs.export_study_trace(trace_with(), CONFIG)
    assert result["status"] == "error" and len(calls) == 1 and "private" not in json.dumps(result)


def test_projection_summary_survives_revalidation_without_adding_private_payload():
    summary = obs._summary({"current_totals": {"total": 90}, "projected_totals": {"total": 123},
        "solver": {"status": "탐색 제한", "optimal": False}, "event_count": 2,
        "semesters": [{"term": "2027-1", "projected_status": "미충족"}]})
    assert summary == {"current_totals": {"total": 90}, "projected_totals": {"total": 123},
        "solver": {"status": "탐색 제한", "optimal": False}, "event_count": 2,
        "semesters": [{"term": "2027-1", "projected_status": "미충족"}]}
    assert obs._summary(summary) == summary


@pytest.mark.parametrize("error_code", ["schema_mismatch", "empty_output", "provider_unavailable", "request_rejected"])
def test_llm_failure_reason_remains_identifiable_in_learning_trace(error_code):
    sample = load_sample("on_track")
    recorder = obs.StudyRecorder(sample["profile"], sample["courses"], builtin_sample_id="on_track")
    response = {"used": False, "error_code": error_code, "error": "private provider detail"}
    with recorder.activate():
        obs.observe_call("explain_result", lambda: response)
    trace = recorder.finish({"llm": response})
    assert trace["steps"][0]["summary"] == {"used": False, "error_code": error_code}
    assert trace["status"] == "error" and obs._validated_trace(trace) is not None
    child = obs._export_payloads(trace, "jolup-study")[1]
    assert child["error"] == child["outputs"]["error_code"] == error_code
    assert "private provider detail" not in json.dumps(trace)


@pytest.mark.parametrize("count,expected", [(0, 0), (1001, 1000)])
def test_calendar_trace_counts_events_without_copying_event_content(count, expected):
    summary = obs._summary({"events": [{"title": "untrusted event content"}] * count,
                            "event_count": 999})
    assert summary == {"event_count": expected}
