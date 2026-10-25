import copy
import json

import httpx
import pytest

from jolup.llm import (
    ANTHROPIC_VERSION, DEFAULT_MODELS, detect_provider, explain_result, generate,
    list_available_models, redact_text, resolve_config, safe_explanation_input,
)

OPENAI_CONFIG = {"api_key": "sk-proj-dummy-key-for-offline-tests"}
CLAUDE_CONFIG = {"api_key": "sk-ant-dummy-key-for-offline-tests"}


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    for key in ("JOLUP_API_KEY", "JOLUP_LLM_PROVIDER", "JOLUP_LLM_MODEL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID"):
        monkeypatch.delenv(key, raising=False)


def success(provider="openai", text="부족 영역은 계산표에서 확인하세요.", *, structured=None):
    """Real provider envelopes; no LangChain/model method is mocked."""
    content = json.dumps(structured or {"explanation": text}, ensure_ascii=False)
    if provider == "openai":
        return {"id": "resp_offline", "object": "response", "created_at": 0, "status": "completed",
                "model": DEFAULT_MODELS[provider], "error": None,
                "output": [{"id": "msg_offline", "role": "assistant", "type": "message", "status": "completed",
                            "content": [{"type": "output_text", "text": content, "annotations": []}]}],
                "usage": {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40,
                          "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}
    return {"id": "msg_offline", "type": "message", "role": "assistant", "model": DEFAULT_MODELS[provider],
            "stop_reason": "end_turn", "stop_sequence": None,
            "content": [{"type": "text", "text": content}],
            "usage": {"input_tokens": 30, "output_tokens": 10}}


def response_payload(payload, provider="openai"):
    if not isinstance(payload, dict):
        return payload
    return {**success(provider), **payload}


def input_text(body):
    return body["input"][-1]["content"]


@pytest.mark.parametrize("key,expected", [("sk-ant-example", "anthropic"), ("sk-proj-example", "openai"),
    (" sk-svcacct-example ", "openai"), ("sk-legacy-unknown", None), ("", None), (None, None)])
def test_detection_is_local_and_conservative(key, expected):
    assert detect_provider(key) == expected


def test_configuration_never_returns_secret_and_defaults_follow_provider():
    for config, provider in [(OPENAI_CONFIG, "openai"), (CLAUDE_CONFIG, "anthropic")]:
        resolved = resolve_config(config)
        assert resolved["ok"] and resolved["provider"] == provider
        assert resolved["model"] == DEFAULT_MODELS[provider]
        assert config["api_key"] not in json.dumps(resolved)


@pytest.mark.parametrize("config", [{}, {"api_key": "sk-unknown"},
    {**CLAUDE_CONFIG, "provider": "openai"}, {**OPENAI_CONFIG, "provider": "other"},
    {**OPENAI_CONFIG, "model": "claude-haiku-5-5"}, {**CLAUDE_CONFIG, "model": "gpt-6-luna"},
    {**OPENAI_CONFIG, "timeout": float("nan")}, {**OPENAI_CONFIG, "max_output_tokens": 0},
    {"api_key": "키를입력함", "provider": "openai"}, {"api_key": "sk-proj-a\nb", "provider": "openai"}])
def test_invalid_configuration_never_calls_network(config):
    def forbidden(request):
        pytest.fail("Invalid config must not issue HTTP")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        result = generate("system", "user", config, client=client)
    assert not result["ok"] and result["error_code"] == "configuration"


def test_environment_selection_is_explicit_when_multiple_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_CONFIG["api_key"])
    assert resolve_config()["provider"] == "openai"
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_CONFIG["api_key"])
    assert not resolve_config()["ok"]
    assert resolve_config({"provider": "anthropic"})["provider"] == "anthropic"
    monkeypatch.setenv("JOLUP_API_KEY", CLAUDE_CONFIG["api_key"])
    assert resolve_config()["provider"] == "anthropic"


def test_unknown_prefix_works_only_with_explicit_provider():
    assert resolve_config({"api_key": "legacy-key", "provider": "openai"})["ok"]


def test_workspace_header_is_optional_and_only_sent_to_claude():
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=success("anthropic" if request.url.host == "api.anthropic.com" else "openai"))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        generate("system", "user", CLAUDE_CONFIG, client=client)
        generate("system", "user", {**CLAUDE_CONFIG, "workspace_id": "wrkspc_test123"}, client=client)
        generate("system", "user", {**OPENAI_CONFIG, "workspace_id": "wrkspc_test123"}, client=client)
        bad = generate("system", "user", {**CLAUDE_CONFIG, "workspace_id": "bad\r\nheader"}, client=client)
    assert "anthropic-workspace-id" not in seen[0].headers
    assert seen[1].headers["anthropic-workspace-id"] == "wrkspc_test123"
    assert "anthropic-workspace-id" not in seen[2].headers
    assert len(seen) == 3 and bad["error_code"] == "configuration"


@pytest.mark.parametrize("provider,config,host", [("openai", OPENAI_CONFIG, "api.openai.com"),
                                                   ("anthropic", CLAUDE_CONFIG, "api.anthropic.com")])
def test_common_interface_uses_correct_transport(provider, config, host):
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.host == host
        assert request.headers["authorization"] == "Bearer " + config["api_key"] if provider == "openai" else request.headers["x-api-key"] == config["api_key"]
        body = json.loads(request.content)
        assert config["api_key"] not in request.content.decode()
        assert body["model"] == DEFAULT_MODELS[provider]
        assert "temperature" not in body
        if provider == "openai":
            assert input_text(body) == "user" and body["input"][0]["content"] == "system"
            schema = body["text"]["format"]
            assert schema["type"] == "json_schema" and schema["strict"] is True
            assert schema["schema"]["required"] == ["explanation"]
            assert schema["schema"]["additionalProperties"] is False
            assert body["store"] is False and body["reasoning"] == {"effort": "none"}
        else:
            assert request.headers["anthropic-version"] == ANTHROPIC_VERSION
            assert body["messages"] == [{"role": "user", "content": "user"}]
            assert body["system"] == "system"
            assert body["output_config"]["format"]["type"] == "json_schema"
            assert body["output_config"]["format"]["schema"]["required"] == ["explanation"]
        return httpx.Response(200, json=success(provider))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = generate("system", "user", config, client=client)
    assert result["ok"] and result["used"] and result["provider"] == provider
    assert "do not display" not in result["text"]
    assert len(requests) == 1


@pytest.mark.parametrize("status,code", [(401, "authentication"), (403, "model_access"), (404, "model_access"),
    (429, "rate_limit"), (500, "provider_unavailable"), (400, "request_rejected"), (302, "request_rejected")])
def test_http_errors_do_not_echo_body_retry_or_follow_redirects(status, code):
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(status, json={"error": OPENAI_CONFIG["api_key"]},
                              headers={"Location": "https://example.com/steal"})
    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        result = generate("system", "user", OPENAI_CONFIG, client=client)
    assert result["error_code"] == code and not result["used"] and not result["text"]
    assert len(seen) == 1 and OPENAI_CONFIG["api_key"] not in json.dumps(result)


@pytest.mark.parametrize("error,code", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "connection")])
def test_connection_errors_are_redacted(error, code):
    def handle(request):
        raise error("raw error includes " + OPENAI_CONFIG["api_key"], request=request)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = generate("system", "user", OPENAI_CONFIG, client=client)
    assert result["error_code"] == code
    assert OPENAI_CONFIG["api_key"] not in json.dumps(result)


@pytest.mark.parametrize("payload,config,code", [
    ({"status": "incomplete", "output": [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}]}, OPENAI_CONFIG, "incomplete"),
    ({"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}, OPENAI_CONFIG, "refusal"),
    ({"status": "completed", "output": []}, OPENAI_CONFIG, "empty_output"),
    ({"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]}, CLAUDE_CONFIG, "incomplete"),
    ({"stop_reason": "end_turn", "stop_details": {"type": "refusal"}, "content": []}, CLAUDE_CONFIG, "refusal"),
    ({"stop_reason": "refusal", "content": []}, CLAUDE_CONFIG, "refusal"),
    ({"stop_reason": "end_turn", "content": []}, CLAUDE_CONFIG, "empty_output"),
    ([], OPENAI_CONFIG, "invalid_response"),
])
def test_non_successful_content_is_never_displayed(payload, config, code):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response_payload(payload, "anthropic" if config == CLAUDE_CONFIG else "openai")))) as client:
        result = generate("system", "user", config, client=client)
    assert result["error_code"] == code and result["text"] == "" and not result["used"]


def test_non_json_response_is_handled():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>error</html>"))) as client:
        assert generate("system", "user", OPENAI_CONFIG, client=client)["error_code"] == "invalid_response"


@pytest.mark.parametrize("payload,expected_code,expected_text", [
    ({"status": "completed", "output": [{"type": "message", "status": "in_progress",
      "content": [{"type": "output_text", "text": "partial"}]}]}, "incomplete", ""),
    ({"status": "incomplete", "output": [{"type": "message",
      "content": [{"type": "refusal"}, {"type": "output_text", "text": "partial"}]}]}, "incomplete", ""),
    ({"status": "completed", "output": [None, {"type": "tool", "content": [{"type": "output_text", "text": "tool text"}]},
      {"type": "message", "content": [None, {"type": "output_text", "text": 17},
       {"type": "output_text", "text": "유효 설명"}]}]}, "invalid_response", ""),
])
def test_openai_message_completion_and_displayable_blocks(payload, expected_code, expected_text):
    """A completed envelope is insufficient; incomplete/refused messages stay hidden."""
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response_payload(payload)))) as client:
        result = generate("system", "user", OPENAI_CONFIG, client=client)
        assert not client.is_closed  # The injected transport remains caller-owned.
    assert result["error_code"] == expected_code
    assert result["text"] == expected_text


def test_private_fields_and_raw_query_are_never_sent_and_result_not_mutated():
    result = {"intent": "diagnosis", "status": "미충족", "answer": "홍가상 학생 202412345",
        "profile": {"name": "홍가상", "student_id": "202412345"},
        "courses": [{"course_name": "개인정보포함", "grade": "A+"}], "raw_csv": "NAME,ID",
        "trace": [{"course_name": "민감한원문"}], "review_items": ["student@example.com"],
        "totals": {"total": 100, "major_total": 54, "student_name": "홍가상"},
        "rows": [{"area": "총학점", "required": 123, "earned": 100, "shortage": 23, "unit": "학점", "status": "미충족"}],
        "evidence": [{"source_id": "school-reg", "url": "https://www.sungkyul.ac.kr/skukr/253/subview.do",
                      "page": 9, "text": "졸업 요건 자료"},
                     {"url": "https://outside.invalid", "text": "not a school source"}]}
    before = copy.deepcopy(result)
    seen = []
    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=success(structured={"fact_ids": ["current", "evidence"]}))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        response = explain_result("홍가상 202412345 나 졸업 괜찮아?", result, OPENAI_CONFIG, client=client)
    request = json.dumps(seen, ensure_ascii=False)
    for private in ("홍가상", "202412345", "개인정보포함", "NAME,ID", "민감한원문", "student@example.com", "outside.invalid"):
        assert private not in request
    assert '"total": 100' in input_text(seen[0])
    assert response["ok"] and result == before


def test_safe_input_only_allows_numeric_totals_and_safe_sources():
    safe = safe_explanation_input({"totals": {"total": "202412345", "major_total": float("nan")},
        "evidence": [{"url": "https://sungkyul.ac.kr.evil.invalid", "text": "bad"}],
        "plan": {"status": "확인 필요", "semesters": [{"term": "2027-1", "total_credits": 18, "max_credits": 18,
                                                      "courses": [{"course_name": "private"}]}]}})
    assert safe["totals"] == {"total": None, "major_total": None}
    assert not safe["evidence"] and "private" not in json.dumps(safe)


def test_redaction_catches_secrets_contact_details_and_student_ids():
    redacted = redact_text("sk-ant-long-secret student@example.com 010-1234-5678 202412345")
    for private in ("sk-ant-long-secret", "student@example.com", "010-1234-5678", "202412345"):
        assert private not in redacted


def test_accidental_provider_key_echo_is_removed():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=success(text=OPENAI_CONFIG["api_key"])))) as client:
        result = generate("system", "user", OPENAI_CONFIG, client=client)
    assert result["ok"] and OPENAI_CONFIG["api_key"] not in json.dumps(result)


@pytest.mark.parametrize("provider,config,model_id,host", [
    ("openai", OPENAI_CONFIG, "gpt-example", "api.openai.com"),
    ("anthropic", CLAUDE_CONFIG, "claude-example", "api.anthropic.com"),
])
def test_model_catalog_is_one_read_only_get_without_model_selection(provider, config, model_id, host):
    seen = []
    original = {**config, "workspace_id": "wrkspc_test123", "model": "wrong model\nID", "max_output_tokens": 0}
    before = copy.deepcopy(original)

    def handle(request):
        seen.append(request)
        assert request.method == "GET" and request.url.host == host and request.url.path == "/v1/models"
        assert not request.content
        assert request.headers["authorization"] == "Bearer " + config["api_key"] if provider == "openai" else request.headers["x-api-key"] == config["api_key"]
        if provider == "anthropic":
            assert request.headers["anthropic-version"] == ANTHROPIC_VERSION
            assert request.headers["anthropic-workspace-id"] == "wrkspc_test123"
            assert dict(request.url.params) == {"limit": "1000"}
        else:
            assert "anthropic-version" not in request.headers
            assert "anthropic-workspace-id" not in request.headers
            assert not request.url.params
        return httpx.Response(200, json={"data": [{"id": model_id, "owned_by": "private-account"}], "has_more": True})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = list_available_models(original, client=client)
    assert result["ok"] and result["models"] == [model_id] and result["provider"] == provider
    assert result["has_more"] is True and len(seen) == 1
    assert "private-account" not in json.dumps(result) and config["api_key"] not in json.dumps(result)
    assert original == before and "model" not in result


def test_model_catalog_returns_only_valid_unique_ids_and_never_secret_echoes():
    payload = {"data": [{"id": "gpt-example"}, {"id": "gpt-example"}, {"id": "ft:gpt-example:team:custom"},
                        {"id": OPENAI_CONFIG["api_key"]}, {"id": "sk-other-secret-echo"}, {"id": "bad\nvalue"},
                        {"id": 123}, {"id": "x" * 129}, {"id": "student@example.com"}, "not an object"],
               "metadata": {"api_key": OPENAI_CONFIG["api_key"]}}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        result = list_available_models(OPENAI_CONFIG, client=client)
    assert result["models"] == ["gpt-example", "ft:gpt-example:team:custom"]
    assert "secret-echo" not in json.dumps(result) and OPENAI_CONFIG["api_key"] not in json.dumps(result)


@pytest.mark.parametrize("status,code", [(401, "authentication"), (403, "model_access"), (404, "model_access"),
    (429, "rate_limit"), (500, "provider_unavailable"), (302, "request_rejected")])
def test_model_catalog_errors_never_echo_body_retry_or_follow_redirects(status, code):
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(status, json={"error": OPENAI_CONFIG["api_key"]},
                              headers={"Location": "https://example.com/steal"})
    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        result = list_available_models(OPENAI_CONFIG, client=client)
    assert not result["ok"] and result["models"] == [] and result["error_code"] == code
    assert len(seen) == 1 and OPENAI_CONFIG["api_key"] not in json.dumps(result)


@pytest.mark.parametrize("error,code", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "connection")])
def test_model_catalog_connection_errors_are_sanitized(error, code):
    def handle(request):
        raise error("raw error includes " + OPENAI_CONFIG["api_key"], request=request)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = list_available_models(OPENAI_CONFIG, client=client)
    assert result["error_code"] == code and OPENAI_CONFIG["api_key"] not in json.dumps(result)


@pytest.mark.parametrize("body", [[], {}, {"data": None}, {"data": {}}, "non-json"])
def test_model_catalog_malformed_responses_are_handled(body):
    def handle(request):
        return httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert list_available_models(OPENAI_CONFIG, client=client)["error_code"] == "invalid_response"


def test_model_catalog_missing_key_never_calls_network():
    def forbidden(request):
        pytest.fail("Missing credentials must not issue HTTP")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        result = list_available_models({}, client=client)
    assert result["error_code"] == "configuration"


def test_model_catalog_ui_requires_click_and_never_generates_or_changes_model(monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from jolup import llm
    seen = []

    def lookup(config):
        seen.append(config)
        return {"ok": True, "models": [f"gpt-example-{len(seen)}"], "has_more": False}

    def forbidden(*args, **kwargs):
        pytest.fail("Model lookup must never generate an answer")

    monkeypatch.setattr(llm, "list_available_models", lookup)
    monkeypatch.setattr(llm, "generate", forbidden)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=45).run()
    app.text_input(key="api_key").set_value(OPENAI_CONFIG["api_key"]).run()
    app.text_input(key="api_model").set_value("existing-model").run()
    assert not seen and not app.exception
    next(item for item in app.button if item.label == "사용 가능한 모델 확인").click().run()
    assert not app.exception and len(seen) == 1
    assert app.text_area(key="api_model_list_display").value == "gpt-example-1"
    assert app.text_input(key="api_model").value == "existing-model"
    assert app.checkbox(key="api_enabled").value is False
    next(item for item in app.button if item.label == "사용 가능한 모델 확인").click().run()
    assert not app.exception and len(seen) == 2
    assert app.text_area(key="api_model_list_display").value == "gpt-example-2"
    app.text_input(key="api_key").set_value(CLAUDE_CONFIG["api_key"]).run()
    assert not app.exception and not app.text_area
    assert "available_models_result" not in app.session_state
    assert len(seen) == 2


@pytest.mark.parametrize("config", [
    {**OPENAI_CONFIG, "model": "bad model"}, {**OPENAI_CONFIG, "timeout": None},
    {**OPENAI_CONFIG, "timeout": 0}, {**OPENAI_CONFIG, "timeout": 121},
    {**OPENAI_CONFIG, "max_output_tokens": float("inf")},
    {**OPENAI_CONFIG, "max_output_tokens": 8193},
    {"api_key": "a" * 2049, "provider": "openai"},
    {"api_key": "sk-proj-secret\x7f", "provider": "openai"},
])
def test_configuration_boundaries_fail_before_invocation(config):
    assert not resolve_config(config)["ok"]


@pytest.mark.parametrize("system,user", [(None, "query"), ("system", None), ("system", "  "), ("x" * 60000, "query")])
def test_invalid_and_oversized_input_does_not_send(system, user):
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no request expected"))) as client:
        assert generate(system, user, OPENAI_CONFIG, client=client)["error_code"] == "input"


def test_model_catalog_owns_and_closes_only_its_own_client(monkeypatch):
    from jolup import llm
    owned = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []})))
    monkeypatch.setattr(llm.httpx, "Client", lambda **kwargs: owned)
    result = list_available_models(OPENAI_CONFIG)
    assert result["ok"] and not result["has_more"] and owned.is_closed


def test_catalog_limits_result_to_first_thousand_rows():
    data = [{"id": f"gpt-test-{number}"} for number in range(1001)]
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data}))) as client:
        result = list_available_models(OPENAI_CONFIG, client=client)
    assert len(result["models"]) == 1000 and result["has_more"] is True


def test_minimal_and_malformed_derived_input_stays_bounded():
    from jolup.llm import _safe_usage
    safe = safe_explanation_input({"intent": "unknown", "totals": [], "rows": [None,
        {"area": "과제", "required": "충족", "earned": "통과", "shortage": "미완료", "status": "확인 필요"}],
        "review_items": None, "projection": {"status": "확인 필요", "rows": "invalid"},
        "plan": {"semesters": [None, {"term": "invalid"}, {"term": "2027-2"}]},
        "evidence": [None, {"url": "http://sungkyul.ac.kr"},
                     {"url": "https://sungkyul.ac.kr", "quote": "학칙", "page": 1}]})
    assert safe["task"] == "diagnosis" and safe["review_item_count"] == 0
    assert safe["rows"][0]["earned"] == "통과" and not safe["projection"]["rows"]
    assert len(safe["plan"]["semesters"]) == 1 and len(safe["evidence"]) == 1
    assert safe_explanation_input({"rows": {}, "evidence": {}, "plan": {"semesters": {}}})["evidence"] == []
    assert _safe_usage(None) == {} and _safe_usage({"input_tokens": -1, "output_tokens": 4}) == {"output_tokens": 4}


@pytest.mark.parametrize("provider,config", [("openai", OPENAI_CONFIG), ("anthropic", CLAUDE_CONFIG)])
@pytest.mark.parametrize("structured", [{"explanation": 123}, {"explanation": " "},
    {"explanation": "문장", "verdict": "졸업 보장"}, {"wrong_field": "문장"}])
def test_provider_schema_mismatch_never_becomes_a_displayed_explanation(provider, config, structured):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=success(provider, structured=structured)))) as client:
        result = generate("system", "user", config, client=client)
    assert not result["used"] and result["error_code"] == "schema_mismatch" and result["text"] == ""


def test_sdk_owns_its_httpx2_client_and_disables_automatic_tracing(monkeypatch):
    from jolup import llm
    from langsmith import Client
    import httpx2
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setattr(Client, "create_run", lambda *a, **k: pytest.fail("automatic tracing is disabled"))
    clients, requests = [], []
    class CapturingClient(httpx2.Client):
        def __init__(self, **kwargs):
            def handle(request):
                requests.append(request)
                return httpx2.Response(200, json=success())
            kwargs["transport"] = httpx2.MockTransport(handle)
            super().__init__(**kwargs)
            clients.append(self)
    monkeypatch.setattr(llm.httpx2, "Client", CapturingClient)
    result = generate("system", "user", {**OPENAI_CONFIG, "model": "gpt-4.1-mini"})
    assert result["used"] and len(requests) == 1
    assert "reasoning" not in json.loads(requests[0].content)
    assert all(client.is_closed for client in clients)


def test_provider_error_envelope_never_displays_even_a_schema_valid_message():
    payload = success()
    payload["error"] = {"code": "server_error", "message": "private diagnostic"}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        result = generate("system", "user", OPENAI_CONFIG, client=client)
    assert not result["used"] and result["error_code"] == "incomplete"


def test_claude_catalog_uses_sdk_auth_convention_without_optional_workspace():
    def handle(request):
        assert request.headers["x-api-key"] == CLAUDE_CONFIG["api_key"]
        assert "authorization" not in request.headers and "anthropic-workspace-id" not in request.headers
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(200, json={"data": []})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert list_available_models(CLAUDE_CONFIG, client=client)["ok"]


def test_http_error_takes_precedence_over_an_untrusted_output_field():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"output": []}))) as client:
        result = generate("system", "user", OPENAI_CONFIG, client=client)
    assert result["error_code"] == "authentication"
