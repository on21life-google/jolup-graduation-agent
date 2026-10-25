"""HTTP contracts preserve domain outcomes and the explicit calendar approval gate."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from icalendar import Calendar

from jolup import study_api
from jolup.samples import load_sample


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("MCP_URL", raising=False)
    with TestClient(study_api.app, raise_server_exceptions=False) as test_client:
        yield test_client


def sample_request(client, sample_id="on_track"):
    response = client.get(f"/samples/{sample_id}")
    assert response.status_code == 200
    sample = response.json()
    return {"profile": sample["profile"], "courses": sample["courses"]}


def test_swagger_and_openapi_explain_real_request_bodies(client):
    assert client.get("/health").json()["status"] == "ok"
    assert "swagger-ui" in client.get("/docs").text
    spec = client.get("/openapi.json").json()
    assert {"/health", "/samples", "/samples/{sample_id}", "/diagnosis", "/plans", "/regulations", "/events", "/calendar/ics"} <= set(spec["paths"])
    assert spec["paths"]["/diagnosis"]["post"]["requestBody"]["required"]
    assert "StudentProfile" in spec["components"]["schemas"]
    assert "Course" in spec["components"]["schemas"]
    assert "text/calendar" in spec["paths"]["/calendar/ics"]["post"]["responses"]["200"]["content"]


def test_samples_are_fictional_and_unknown_id_is_not_a_file_path(client):
    samples = client.get("/samples").json()
    assert len(samples) == 11
    assert client.get("/samples/not_a_sample").status_code == 404
    sample = client.get("/samples/on_track").json()
    assert sample["profile"]["is_dummy"] is True
    assert "csv_bytes" not in sample


@pytest.mark.parametrize("sample_id,total,status", [
    ("on_track", 90, "미충족"), ("fully_met", 123, "충족"),
    ("credit_shortfall", 38, "미충족"), ("missing_record", 90, "확인 필요"),
])
def test_diagnosis_uses_records_and_preserves_current_vs_future(client, sample_id, total, status):
    response = client.post("/diagnosis", json=sample_request(client, sample_id))
    assert response.status_code == 200
    result = response.json()
    assert result["totals"]["total"] == total
    assert result["status"] == status
    assert result["official_graduation_confirmed"] is False
    assert result["rows"] and result["evidence"] and result["meta"]
    if sample_id == "on_track":
        assert result["projection"]["totals"]["total"] == 105


def test_uploaded_course_change_changes_the_actual_result(client):
    body = sample_request(client, "fully_met")
    body["courses"][0]["grade"] = "F"
    result = client.post("/diagnosis", json=body).json()
    assert result["totals"]["total"] == 120
    assert result["status"] == "미충족"


@pytest.mark.parametrize("sample_id,status", [("on_track", "조건부 가능"), ("credit_shortfall", "위험")])
def test_plans_preserve_conditional_outcomes(client, sample_id, status):
    response = client.post("/plans", json=sample_request(client, sample_id))
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == status
    assert result["assumptions"] and result["warnings"]


def test_http_validation_does_not_echo_bad_input(client):
    body = sample_request(client)
    body["courses"][0]["credits"] = "PRIVATE_INPUT_MUST_NOT_ECHO"
    response = client.post("/diagnosis", json=body)
    assert response.status_code == 422
    assert "PRIVATE_INPUT_MUST_NOT_ECHO" not in response.text
    assert response.json()["errors"][0]["location"][-1] == "credits"
    assert client.post("/diagnosis", json={}).status_code == 422
    assert client.post("/diagnosis", content="{broken").status_code == 422


def test_business_unknowns_are_diagnoses_not_http_failures(client):
    body = sample_request(client, "transfer")
    response = client.post("/diagnosis", json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "확인 필요"


def test_missing_accreditation_setting_stays_unknown(client):
    body = sample_request(client, "fully_met")
    del body["profile"]["engineering_accreditation"]
    response = client.post("/diagnosis", json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "확인 필요"
    assert "공학인증 제외 설정 확인 필요" in response.json()["review_items"]


@pytest.mark.parametrize("warning_value,expected_status", [
    ("omitted", "조건부 가능"), (None, "확인 필요"), (False, "조건부 가능"),
])
def test_plans_preserve_omitted_null_and_false_facts(client, warning_value, expected_status):
    body = sample_request(client)
    if warning_value == "omitted":
        del body["profile"]["academic_warning"]
    else:
        body["profile"]["academic_warning"] = warning_value
    response = client.post("/plans", json=body)
    assert response.status_code == 200
    assert response.json()["status"] == expected_status


def test_regulation_profile_preserves_only_explicit_input(client, monkeypatch):
    seen = []
    body = sample_request(client)
    del body["profile"]["engineering_accreditation"]
    del body["profile"]["academic_warning"]
    def answer(name, arguments):
        seen.append(arguments)
        return {"status": "확인 필요", "answer": "추가 근거 확인", "evidence": [], "meta": [], "warnings": []}
    monkeypatch.setattr(study_api, "call_domain", answer)
    request = {"query": "졸업 총학점은?", "profile": body["profile"]}
    response = client.post("/regulations", json=request)
    assert response.status_code == 200
    assert seen == [request]


def test_regulations_reuses_domain_facade_without_llm_connection(client, monkeypatch):
    seen = []
    def answer(name, arguments):
        seen.append((name, arguments))
        return {"status": "확인 필요", "answer": "추가 근거 확인", "evidence": [], "meta": [], "warnings": []}
    monkeypatch.setattr(study_api, "call_domain", answer)
    response = client.post("/regulations", json={"query": "졸업 총학점은?"})
    assert response.status_code == 200
    assert seen == [("answer_regulations", {"query": "졸업 총학점은?"})]
    assert client.post("/regulations", json={"query": "   "}).status_code == 422


@pytest.mark.parametrize("approval", [False, None, 1, "true"])
def test_calendar_requires_literal_boolean_approval(client, approval):
    events = client.get("/events", params={"query": "성적", "year": 2026}).json()["events"]
    response = client.post("/calendar/ics", json={"events": events, "approved": approval})
    assert response.status_code in {403, 422}
    assert "BEGIN:VCALENDAR" not in response.text


def test_calendar_preview_tamper_and_export_end_to_end(client):
    preview = client.get("/events", params={"query": "성적", "year": 2026}).json()
    assert preview["approval_required"] is True
    events = preview["events"]
    assert events
    assert client.post("/calendar/ics", json={"events": events}).status_code == 403
    changed = deepcopy(events)
    changed[0]["title"] = "Changed by client"
    assert client.post("/calendar/ics", json={"events": changed, "approved": True}).status_code == 422
    response = client.post("/calendar/ics", json={"events": events, "approved": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert len(Calendar.from_ical(response.content).walk("VEVENT")) == len(events)


def test_unexpected_failures_are_sanitized(client, monkeypatch):
    def failure(*args, **kwargs):
        raise RuntimeError("PRIVATE_BACKEND_DETAIL")
    monkeypatch.setattr(study_api, "call_domain", failure)
    response = client.post("/diagnosis", json=sample_request(client))
    assert response.status_code == 503
    assert "PRIVATE_BACKEND_DETAIL" not in response.text


def test_sample_data_covers_supported_input_contract():
    for sample in study_api.list_samples():
        raw = load_sample(sample["id"])
        parsed = study_api.DiagnosisRequest(profile=raw["profile"], courses=raw["courses"])
        assert parsed.profile.is_dummy is True


def test_all_swagger_post_examples_execute_with_the_advertised_outcome(client, monkeypatch):
    """Exercise the exact examples rendered by Swagger, not separately crafted bodies."""
    monkeypatch.setenv("JOLUP_RAG_MODE", "lexical")
    expected = {
        "/chat": {"diagnosis": "미충족", "regulation": "근거 확인"},
        "/diagnosis": {"on_track": "미충족", "fully_met": "충족", "credit_shortfall": "미충족", "missing_record": "확인 필요"},
        "/plans": {"on_track": "조건부 가능", "fully_met": "충족", "credit_shortfall": "위험", "missing_record": "확인 필요"},
        "/regulations": {"total_credits": "근거 확인", "with_profile": "근거 확인", "unverified_date": "확인 필요"},
        "/calendar/ics": {"approval_required": 403, "explicit_approval": 200},
    }
    spec = client.get("/openapi.json").json()
    post_paths = {path for path, methods in spec["paths"].items() if "post" in methods}
    assert post_paths == set(expected)
    for path, scenarios in expected.items():
        examples = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"].get("examples", {})
        assert set(examples) == set(scenarios), path
        for name, outcome in scenarios.items():
            example = examples[name]
            assert example["summary"] and example["description"]
            response = client.post(path, json=example["value"])
            if isinstance(outcome, int):
                assert response.status_code == outcome, (path, name, response.text)
            else:
                assert response.status_code == 200, (path, name, response.text)
                assert response.json()["status"] == outcome, (path, name, response.text)
            if path in {"/diagnosis", "/plans"}:
                assert "expected" not in example["value"]
    calendar_examples = spec["paths"]["/calendar/ics"]["post"]["requestBody"]["content"]["application/json"]["examples"]
    assert next(iter(calendar_examples)) == "approval_required"
    assert calendar_examples["approval_required"]["value"]["approved"] is False


def test_all_swagger_get_defaults_and_path_examples_are_usable(client):
    spec = client.get("/openapi.json").json()
    assert spec["servers"][0]["url"] == "/"  # Use the actual host/port that serves /docs.
    for path, methods in spec["paths"].items():
        if "get" not in methods:
            continue
        parameters = methods["get"].get("parameters", [])
        query = {item["name"]: item["schema"]["default"] for item in parameters if item["in"] == "query"}
        if "{sample_id}" in path:
            example = next(item for item in parameters if item["name"] == "sample_id")
            assert example["examples"]["on_track"]["value"] == "on_track"
            for value in example["examples"].values():
                assert client.get(path.replace("{sample_id}", value["value"])).status_code == 200
        else:
            assert client.get(path, params=query).status_code == 200


def test_validation_error_explains_what_to_fix_without_raw_values(client):
    body = sample_request(client)
    body["courses"][0]["credits"] = "PRIVATE_CREDIT_VALUE"
    response = client.post("/diagnosis", json=body)
    error = response.json()["errors"][0]
    assert "message" in error
    assert "숫자" in error["message"]
    assert "PRIVATE_CREDIT_VALUE" not in response.text
    malformed = client.post("/diagnosis", content="{broken", headers={"Content-Type": "application/json"})
    assert "JSON" in malformed.json()["errors"][0]["message"]


def test_backend_permission_error_still_denies_ics(client, monkeypatch):
    body = {"events": client.get("/events").json()["events"], "approved": True}
    def deny(*args, **kwargs):
        raise PermissionError("PRIVATE_PERMISSION_DETAIL")
    monkeypatch.setattr(study_api, "call_domain", deny)
    response = client.post("/calendar/ics", json=body)
    assert response.status_code == 403
    assert "승인" in response.json()["detail"]
    assert "PRIVATE_PERMISSION_DETAIL" not in response.text


def test_server_cli_uses_local_defaults_and_supports_explicit_overrides(monkeypatch):
    import runpy
    import sys
    from pathlib import Path
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(sys, "argv", ["study_api"])
    study_api.main()
    assert calls.pop() == (("jolup.study_api:app",), {"host": "127.0.0.1", "port": 8001})
    monkeypatch.setattr(sys, "argv", ["study_api", "--host", "127.0.0.1", "--port", "8123"])
    runpy.run_path(str(Path(study_api.__file__)), run_name="__main__")
    assert calls.pop() == (("jolup.study_api:app",), {"host": "127.0.0.1", "port": 8123})
