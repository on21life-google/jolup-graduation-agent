"""Only mock HTTP transports: these tests never call a LangSmith account."""
from copy import deepcopy
from datetime import datetime
import json
from uuid import uuid4

import httpx
import pytest

from jolup import observability as obs
from jolup.academic import check_requirements, parse_transcript
from jolup.samples import load_sample


CONFIG = {"api_key": "lsv2_test_key_only", "project": "jolup-study", "region": "US", "workspace_id": ""}


def recorded():
    sample = load_sample("on_track")
    recorder = obs.StudyRecorder(sample["profile"], sample["courses"], builtin_sample_id=sample["id"])
    with recorder.activate():
        obs.observe_call("route", lambda: {"intent": "diagnosis"})

        def diagnose():
            result = obs.observe_call("check_requirements", check_requirements, sample["profile"], sample["courses"])
            return {"result": result}

        output = obs.observe_call("diagnosis", diagnose)
    return recorder.finish({**output["result"], "intent": "diagnosis"})


def mocked(status=202):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"detail": "server-body-secret-must-not-return"})

    return requests, httpx.Client(transport=httpx.MockTransport(handler))


def test_local_recording_has_no_network_or_automatic_export(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no network constructor expected")
    monkeypatch.setattr(obs.httpx, "Client", forbidden)
    monkeypatch.setenv("LANGSMITH_API_KEY", "environment-key-must-not-be-used")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    trace = recorded()
    assert trace["exportable"] is True
    assert obs.export_study_trace(trace, {})["status"] == "disabled"
    assert trace["summary"]["totals"]["total"] == 90
    assert trace["summary"]["projection"]["totals"]["total"] == 105


@pytest.mark.parametrize("sample_id", ["on_track", "credit_shortfall", "missing_record", "fully_met", "transfer"])
def test_exact_builtin_profile_and_validated_courses_enable_export(sample_id):
    sample = load_sample(sample_id)
    parsed = parse_transcript(sample["csv_bytes"])
    recorder = obs.StudyRecorder(sample["profile"], parsed["courses"], builtin_sample_id=sample_id)
    assert recorder.exportable is True
    assert recorder.case_id == sample_id


@pytest.mark.parametrize("change", ["profile", "course", "wrong_id", "no_origin", "invalid_course"])
def test_modified_or_uploaded_data_is_local_only(change):
    sample = load_sample("on_track")
    sample_id = sample["id"]
    if change == "profile":
        sample["profile"]["display_name"] = "실제 학생의 이름"
    elif change == "course":
        sample["courses"][0]["grade"] = "A0"
    elif change == "invalid_course":
        sample["courses"][0]["credits"] = "secret-invalid"
    elif change == "wrong_id":
        sample_id = "credit_shortfall"
    else:
        sample_id = None
    recorder = obs.StudyRecorder(sample["profile"], sample["courses"], builtin_sample_id=sample_id)
    with recorder.activate():
        obs.observe_call("route", lambda: {"intent": "diagnosis"})
    trace = recorder.finish({"status": "확인 필요"})
    requests, client = mocked()
    with client:
        result = obs.export_study_trace(trace, CONFIG, client=client)
    assert trace["case_id"] is None and trace["exportable"] is False
    assert result["status"] == "blocked" and not requests


def test_context_records_nested_hierarchy_and_times_without_changing_return():
    trace = recorded()
    route, diagnosis, calculate = trace["steps"]
    assert route["parent_id"] == diagnosis["parent_id"] == trace["run_id"]
    assert calculate["parent_id"] == diagnosis["id"]
    assert [s["name"] for s in trace["steps"]] == ["route", "diagnosis", "check_requirements"]
    for step in trace["steps"]:
        assert step["status"] == "success"
        assert datetime.fromisoformat(trace["start_time"]) <= datetime.fromisoformat(step["start_time"])
        assert datetime.fromisoformat(step["start_time"]) <= datetime.fromisoformat(step["end_time"])
        assert datetime.fromisoformat(step["end_time"]) <= datetime.fromisoformat(trace["end_time"])
        assert step["duration_ms"] >= 0
    assert trace["summary"]["status"] == "미충족"
    assert trace["status"] == "success"
    assert obs._validated_trace(trace) is not None


def test_local_summaries_drop_raw_inputs_outputs_unknown_enums_and_untrusted_sources():
    sample = load_sample("on_track")
    recorder = obs.StudyRecorder(sample["profile"], sample["courses"], builtin_sample_id=sample["id"])
    secret = "sk-proj-private-name-학번-1234"
    raw = {"query": secret, "profile": sample["profile"], "courses": sample["courses"],
           "answer": secret, "ai_explanation": secret, "api_key": secret,
           "status": "미충족", "totals": {"total": 90, "private": secret, "major_total": float("nan")},
           "evidence": [{"source_id": "school_rules", "quote": secret}, {"source_id": secret}],
           "plan": {"status": "조건부 가능", "semesters": [{"term": "2027-1", "total_credits": 9,
                     "courses": sample["courses"], "projected_totals": {"total": 114}}],
                    "solver": {"feasible": True, "optimal": False, "reason": secret}},
           "llm": {"used": True, "text": secret, "model": secret}}
    with recorder.activate():
        returned = obs.observe_call("diagnosis", lambda question: raw, secret)
    trace = recorder.finish(raw)
    assert returned is raw
    text = json.dumps(trace, ensure_ascii=False)
    for forbidden in (secret, "김가온", "DUMMY-2024-001", "course_name", "ai_explanation", "api_key"):
        assert forbidden not in text
    assert trace["summary"]["source_ids"] == ["school_rules"]
    assert trace["summary"]["plan"]["semesters"][0]["projected_totals"]["total"] == 114
    assert trace["summary"]["plan"]["solver"] == {"feasible": True, "optimal": False}
    assert obs._validated_trace(trace) is not None


def test_exceptions_are_reraised_but_message_and_dynamic_type_are_not_recorded():
    recorder = obs.StudyRecorder({}, [])

    class PrivateStudentNameError(Exception):
        pass

    def failed():
        raise PrivateStudentNameError("key-and-student-content")

    with recorder.activate(), pytest.raises(PrivateStudentNameError):
        obs.observe_call("check_requirements", failed)
    trace = recorder.finish({})
    assert trace["steps"][0]["summary"] == {"error_code": "execution_error"}
    assert trace["status"] == trace["steps"][0]["status"] == "error"
    assert "PrivateStudentName" not in json.dumps(trace)
    assert "key-and-student-content" not in json.dumps(trace)
    assert obs._CURRENT.get() is None


def test_nested_recorders_restore_context_and_no_recording_outside_activation():
    a, b = obs.StudyRecorder({}, []), obs.StudyRecorder({}, [])
    calls = []
    with a.activate():
        obs.observe_call("route", lambda: calls.append("a"))
        with b.activate():
            obs.observe_call("calendar", lambda: calls.append("b"))
        obs.observe_call("explain", lambda: calls.append("a2"))
    obs.observe_call("route", lambda: calls.append("outside"))
    assert calls == ["a", "b", "a2", "outside"]
    assert len(a.finish({})["steps"]) == 2
    assert len(b.finish({})["steps"]) == 1


@pytest.mark.parametrize("region,url", [("US", "https://api.smith.langchain.com/runs"),
                                        ("EU", "https://eu.api.smith.langchain.com/runs")])
def test_explicit_export_posts_complete_root_and_children_once(region, url, monkeypatch):
    monkeypatch.setenv("LANGSMITH_METADATA", '{"secret":"hidden-environment-value"}')
    monkeypatch.setenv("LANGSMITH_PROJECT", "unintended-project")
    monkeypatch.setenv("LANGSMITH_RUNS_ENDPOINTS", '{"https://not-allowed.example":"private"}')
    trace = recorded()
    requests, client = mocked()
    workspace = str(uuid4())
    with client:
        result = obs.export_study_trace(trace, dict(CONFIG, region=region, workspace_id=workspace), client=client)
    assert result["status"] == "sent" and "다시 조회하지 않았습니다" in result["message"]
    assert len(requests) == len(trace["steps"]) + 1
    bodies = [json.loads(r.content) for r in requests]
    root = bodies[0]
    assert root["id"] == root["trace_id"] == trace["run_id"]
    by_id = {body["id"]: body for body in bodies}
    for request, body in zip(requests, bodies):
        assert request.method == "POST" and str(request.url) == url
        assert request.headers["x-api-key"] == CONFIG["api_key"]
        assert request.headers["x-tenant-id"] == workspace
        assert body["session_name"] == "jolup-study" and body["end_time"]
        assert body["trace_id"] == trace["run_id"]
        assert body["inputs"]["case_id"] == "on_track"
        assert body["inputs"]["intent"] == "diagnosis"
        assert set(body["inputs"]) <= {"case_id", "intent", "operation", "source_ref"}
        assert body["inputs"]["source_ref"].startswith("jolup/")
        if body.get("parent_run_id"):
            assert body["dotted_order"].startswith(by_id[body["parent_run_id"]]["dotted_order"] + ".")
        assert "extra" not in body and "metadata" not in body
    assert "hidden-environment-value" not in json.dumps(bodies)
    assert CONFIG["api_key"] not in json.dumps(result)
    assert "server-body-secret" not in json.dumps(result)


@pytest.mark.parametrize("tamper", ["summary", "steps", "exportable", "field", "signature"])
def test_modified_trace_is_blocked_without_network(tamper):
    trace = recorded()
    if tamper == "summary":
        trace["summary"]["totals"]["total"] = 999
    elif tamper == "steps":
        trace["steps"][0]["name"] = "other"
    elif tamper == "exportable":
        trace["case_id"] = "credit_shortfall"
    elif tamper == "field":
        trace["raw_query"] = "private content"
    else:
        trace["integrity"] = "0" * 64
    requests, client = mocked()
    with client:
        result = obs.export_study_trace(trace, CONFIG, client=client)
    assert result["status"] == "blocked" and not requests


def test_reallowlist_blocks_unknown_field_even_with_valid_process_signature():
    trace = recorded()
    trace["summary"]["raw_query"] = "private content"
    trace["integrity"] = obs._sign({k: v for k, v in trace.items() if k != "integrity"})
    assert obs._validated_trace(trace) is None


@pytest.mark.parametrize("settings", [{"region": "http://elsewhere.example"}, {"region": []},
                                      {"project": "not an ASCII slug"}, {"workspace_id": "not-a-uuid"},
                                      {"api_key": "key\nprivate"}, {"workspace_id": None}])
def test_invalid_settings_fail_closed(settings):
    requests, client = mocked()
    with client:
        result = obs.export_study_trace(recorded(), dict(CONFIG, **settings), client=client)
    assert result["status"] == "blocked" and not requests


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_server_errors_are_fixed_redacted_messages_and_do_not_retry(status):
    requests, client = mocked(status)
    with client:
        result = obs.export_study_trace(recorded(), CONFIG, client=client)
    assert result["status"] == "error" and len(requests) == 1
    assert "server-body-secret" not in json.dumps(result)
    assert CONFIG["api_key"] not in json.dumps(result)


def test_network_error_does_not_leak_exception_or_retry():
    requests = []
    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("secret key in exception", request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = obs.export_study_trace(recorded(), CONFIG, client=client)
    assert result["status"] == "error" and len(requests) == 1
    assert "secret key" not in json.dumps(result)


def test_partial_acceptance_is_honest_and_not_retried():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(202 if len(requests) == 1 else 403)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = obs.export_study_trace(recorded(), CONFIG, client=client)
    assert result["status"] == "error" and len(requests) == 2
    assert "일부 단계" in result["message"]


def test_finish_is_idempotent_and_does_not_return_mutable_internal_state():
    recorder = obs.StudyRecorder({}, [])
    with recorder.activate():
        obs.observe_call("route", lambda: {"intent": "calendar"})
    trace = recorder.finish({"intent": "calendar"})
    original = deepcopy(trace)
    trace["summary"]["intent"] = "diagnosis"
    assert recorder.finish({}) == original


@pytest.mark.parametrize("prefix", ["sk-proj-", "sk-svcacct-", "sk-ant-", "sk-"])
def test_openai_and_claude_keys_are_not_sent_to_langsmith(prefix):
    requests, client = mocked()
    with client:
        result = obs.export_study_trace(recorded(), dict(CONFIG, api_key=prefix + "other-provider-key"), client=client)
    assert result["status"] == "blocked" and not requests
    assert "별도로 발급한 LangSmith 키" in result["message"]


def test_selected_ascii_project_is_used_for_all_runs():
    requests, client = mocked()
    with client:
        result = obs.export_study_trace(recorded(), dict(CONFIG, project="our-team.study_2026"), client=client)
    assert result["project"] == "our-team.study_2026" and result["status"] == "sent"
    assert all(json.loads(r.content)["session_name"] == result["project"] for r in requests)


def test_overall_deadline_stops_without_sending_more_steps(monkeypatch):
    trace = recorded()
    clock = [0.0]
    monkeypatch.setattr(obs.time, "perf_counter", lambda: clock[0])
    requests = []
    def handler(request):
        requests.append(request)
        clock[0] += 31.0
        return httpx.Response(202)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = obs.export_study_trace(trace, CONFIG, client=client)
    assert result["status"] == "error" and len(requests) == 1
    assert "전체 시간 제한" in result["message"]


@pytest.mark.parametrize("status", ["근거 확인", "확인"])
def test_regulation_and_calendar_statuses_survive_allowlist(status):
    recorder = obs.StudyRecorder({}, [])
    with recorder.activate():
        obs.observe_call("regulation", lambda: {"result": {"status": status}})
    trace = recorder.finish({"status": status})
    assert trace["summary"]["status"] == status
    assert trace["steps"][0]["summary"]["status"] == status


def test_real_respond_trace_can_be_exported_via_mock_transport(monkeypatch):
    from jolup.agent import respond
    monkeypatch.delenv("MCP_URL", raising=False)
    sample = load_sample("on_track")
    response = respond("나 졸업 괜찮아?", sample["profile"], sample["courses"],
                       {"builtin_sample_id": "on_track", "use_llm": False})
    trace = response["study_trace"]
    assert obs._validated_trace(trace) is not None
    requests, client = mocked()
    with client:
        result = obs.export_study_trace(trace, CONFIG, client=client)
    assert result["status"] == "sent"
    assert len(requests) == len(trace["steps"]) + 1
    outputs = json.loads(requests[0].content)["outputs"]
    assert outputs["totals"]["total"] == 90
    assert outputs["projection"]["totals"]["total"] == 105
    assert outputs["plan"]["projected_totals"]["total"] == 123
