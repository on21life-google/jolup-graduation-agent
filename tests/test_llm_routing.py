import json
import httpx
from jolup.llm import classify_intent
from test_llm import success, input_text


def test_routing_uses_only_fixed_signals_and_enum_not_raw_student_query():
    seen = {}
    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=success(structured={"intent": "planning"}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = classify_intent("김가온 학번202412345이고 그럼 남은 학기에 다 채울 수 있어?", {"api_key": "sk-proj-FAKE_ONLY"}, previous_intent="diagnosis", client=client)
    assert result["intent"] == "planning" and result["used"]
    assert "김가온" not in str(seen) and "202412345" not in str(seen)
    assert json.loads(input_text(seen))["previous_intent"] == "diagnosis"


def test_unknown_llm_route_cannot_name_or_invoke_arbitrary_tool():
    def handler(request):
        return httpx.Response(200, json=success(structured={"intent": "delete_student_records"}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = classify_intent("졸업 괜찮아?", {"api_key": "sk-proj-FAKE_ONLY"}, client=client)
    assert result["intent"] is None and not result["used"]


def test_no_domain_signal_skips_provider_entirely():
    with httpx.Client(transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(AssertionError("No paid request")))) as client:
        result = classify_intent("안녕하세요 홍가상", {"api_key": "sk-proj-FAKE_ONLY"}, client=client)
    assert not result["used"] and result["intent"] is None


def test_invalid_previous_intent_is_not_exported():
    seen = {}
    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=success(structured={"intent": "diagnosis"}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = classify_intent("졸업 괜찮아", {"api_key": "sk-proj-FAKE_ONLY"}, previous_intent="private text", client=client)
    assert result["intent"] == "diagnosis"
    assert json.loads(input_text(seen))["previous_intent"] is None
    assert seen["text"]["format"]["schema"]["properties"]["intent"]["enum"] == ["diagnosis", "planning", "regulation", "calendar", "unsupported"]
