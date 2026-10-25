"""Study records explain real graph execution without tracing raw state."""
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from jolup.agent import respond
from jolup.samples import load_sample


def _run(case="on_track", context=None, query="나 졸업 괜찮아?"):
    sample = load_sample(case)
    return respond(query, sample["profile"], sample["courses"],
                   context if context is not None else {"builtin_sample_id": case})


def test_real_graph_study_records_nodes_and_nested_calculation_without_changing_results():
    result = _run()
    trace = result["study_trace"]
    assert trace["exportable"] is True
    assert trace["case_id"] == "on_track"
    names = [step["name"] for step in trace["steps"]]
    assert {"route", "diagnosis", "check_requirements", "plan_courses", "explain"} <= set(names)
    assert names.index("route") < names.index("diagnosis") < names.index("explain")
    diagnosis = next(s for s in trace["steps"] if s["name"] == "diagnosis")
    for name in ("check_requirements", "plan_courses"):
        step = next(s for s in trace["steps"] if s["name"] == name)
        assert step["parent_id"] == diagnosis["id"]
        assert step["duration_ms"] >= 0
        assert step["source_ref"]
    assert result["totals"]["total"] == 90
    assert result["projection"]["totals"]["total"] == 105
    assert result["plan"]["status"] == "조건부 가능"


def test_local_study_does_not_require_export_permission_or_api_key(monkeypatch):
    import httpx
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_fake_environment_key")
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    result = _run(context={})
    assert result["study_trace"]["steps"]
    assert result["study_trace"]["exportable"] is False
    assert result["totals"]["total"] == 90


def test_profile_edit_disables_export_even_with_dummy_label():
    sample = load_sample("on_track")
    profile = deepcopy(sample["profile"])
    profile["display_name"] = "PRIVATE_STUDENT_MARKER"
    result = respond("나 졸업 괜찮아?", profile, sample["courses"],
                     {"builtin_sample_id": "on_track"})
    trace = result["study_trace"]
    assert trace["exportable"] is False
    assert "PRIVATE_STUDENT_MARKER" not in json.dumps(trace, ensure_ascii=False)


def test_ai_child_records_exclude_questions_keys_and_free_form_response(monkeypatch):
    from jolup import llm
    secret = "sk-proj-PRIVATE_KEY_MARKER"
    question_marker = "PRIVATE_QUESTION_MARKER"
    response_marker = "PRIVATE_AI_RESPONSE_MARKER"
    monkeypatch.setattr(llm, "classify_intent", lambda *a, **k: {
        "used": True, "intent": "diagnosis", "provider": "openai", "model": "test-model"})
    monkeypatch.setattr(llm, "explain_result", lambda *a, **k: {
        "used": True, "text": response_marker, "provider": "openai", "model": "test-model"})
    result = _run(context={"builtin_sample_id": "on_track", "use_llm": True,
                           "llm_config": {"api_key": secret}},
                  query="나 졸업 괜찮아? " + question_marker)
    trace = result["study_trace"]
    names = {s["name"] for s in trace["steps"]}
    assert {"classify_intent", "explain_result"} <= names
    encoded = json.dumps(trace, ensure_ascii=False)
    assert all(value not in encoded for value in (secret, question_marker, response_marker,
                                                  "김가온", "DUMMY-2024-001"))
    assert result["ai_explanation"] == response_marker


def test_parallel_graph_runs_keep_local_trace_context_separate():
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_run, ("on_track", "credit_shortfall")))
    traces = [r["study_trace"] for r in results]
    assert traces[0]["run_id"] != traces[1]["run_id"]
    ids = [{s["id"] for s in trace["steps"]} for trace in traces]
    assert ids[0].isdisjoint(ids[1])
    for trace in traces:
        valid_parents = {trace["run_id"], None} | {s["id"] for s in trace["steps"]}
        assert all(s["parent_id"] in valid_parents for s in trace["steps"])
    assert results[0]["plan"]["status"] == "조건부 가능"
    assert results[1]["plan"]["status"] == "위험"


def test_failed_tool_keeps_a_sanitized_learning_record_without_partial_verdict(monkeypatch):
    from jolup import agent
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_EXCEPTION_MARKER Authorization: sk-proj-FAKE_SECRET")
    monkeypatch.setattr(agent, "check_requirements", fail)
    result = _run()
    assert result["status"] == "확인 필요"
    assert result["execution_mode"] == "처리 오류"
    assert result["rows"] == []
    assert result["study_trace"]["status"] == "error"
    failed = next(s for s in result["study_trace"]["steps"] if s["name"] == "check_requirements")
    assert failed["status"] == "error"
    assert "PRIVATE_EXCEPTION_MARKER" not in json.dumps(result)
    assert "FAKE_SECRET" not in json.dumps(result)
