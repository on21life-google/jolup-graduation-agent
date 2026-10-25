"""An explanation may select known facts but cannot invent a new academic claim."""
import copy

import pytest

from jolup import llm


def diagnosis(status="미충족", plan="조건부 가능", reviews=0):
    return {"intent": "diagnosis", "status": status, "totals": {"total": 90, "in_progress": 15},
            "review_items": ["needs evidence"] * reviews, "plan": {"status": plan},
            "evidence": [{"source_id": "school", "url": "https://www.sungkyul.ac.kr/", "page": 9}]}


def selection(monkeypatch, ids):
    def generate(system, payload, config, **kwargs):
        assert kwargs["response_schema"] is llm.ExplanationSelection
        return {"ok": True, "used": True, "text": "모델이 만든 999학점 졸업 보장",
                "structured": {"fact_ids": ids}, "error": None, "error_code": None}
    monkeypatch.setattr(llm, "generate", generate)


def test_model_cannot_replace_current_result_or_omit_conditional_warning(monkeypatch):
    source = diagnosis()
    before = copy.deepcopy(source)
    selection(monkeypatch, ["evidence"])
    result = llm.explain_result("나 졸업 괜찮아?", source)
    assert result["ok"] and result["grounding"] == "verified_fact_selection"
    assert "999" not in result["text"] and "미충족" in result["text"]
    assert "조건부 가능" in result["text"] and "개설" in result["text"] and "합격" in result["text"]
    assert "현재 취득 학점에 포함되지" in result["text"]
    assert "학교의 최종 졸업 승인" in result["text"]
    assert source == before


@pytest.mark.parametrize("ids", [["invented"], ["evidence", "evidence"], [], [1], "evidence", None])
def test_unknown_duplicate_or_malformed_fact_selection_is_not_displayed(monkeypatch, ids):
    selection(monkeypatch, ids)
    result = llm.explain_result("나 졸업 괜찮아?", diagnosis())
    assert not result["used"] and result["text"] == ""
    assert result["error_code"] == "schema_mismatch"


@pytest.mark.parametrize("status,plan,reviews", [("확인 필요", "확인 필요", 1), ("미충족", "위험", 0)])
def test_review_and_risk_are_mandatory_even_if_model_selects_only_evidence(monkeypatch, status, plan, reviews):
    selection(monkeypatch, ["evidence"])
    result = llm.explain_result("괜찮아?", diagnosis(status, plan, reviews))
    assert plan in result["text"]
    if reviews:
        assert "보류" in result["text"]


def test_provider_failure_keeps_the_existing_fallback(monkeypatch):
    failed = {"ok": False, "used": False, "text": "", "error_code": "connection"}
    monkeypatch.setattr(llm, "generate", lambda *a, **kw: failed)
    assert llm.explain_result("질문", diagnosis()) is failed


@pytest.mark.parametrize("intent", ["regulation", "calendar"])
def test_nonpersonal_lookup_never_invents_a_student_diagnosis(monkeypatch, intent):
    selection(monkeypatch, ["evidence"])
    result = llm.explain_result("규정 또는 일정 확인", dict(diagnosis("확인 필요", "확인 필요", 1), intent=intent))
    assert result["ok"]
    assert "현재 졸업요건 판정" not in result["text"]
    assert "진단표" not in result["text"]
    assert not {"current", "in_progress", "plan", "review"}.intersection(result["displayed_fact_ids"])


def test_missing_calculation_fields_do_not_become_an_invented_verdict(monkeypatch):
    selection(monkeypatch, ["evidence"])
    result = llm.explain_result("나 졸업 괜찮아?", {"intent": "diagnosis"})
    assert result["ok"]
    assert result["displayed_fact_ids"] == ["scope", "evidence"]
    assert "확인된 학교 근거가 없어" in result["text"]


def test_known_but_unavailable_fact_is_rejected(monkeypatch):
    selection(monkeypatch, ["plan"])
    result = llm.explain_result("나 졸업 괜찮아?", {"intent": "diagnosis"})
    assert not result["used"] and result["text"] == ""
    assert result["error_code"] == "schema_mismatch"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_fact_selection_uses_official_sdk_contract_and_keeps_required_caveats(provider):
    import json
    import httpx
    from test_llm import success, OPENAI_CONFIG, CLAUDE_CONFIG

    requests = []
    def handle(request):
        body = json.loads(request.content)
        schema = (body["text"]["format"]["schema"] if provider == "openai"
                  else body["output_config"]["format"]["schema"])
        assert schema["required"] == ["fact_ids"]
        assert "current" in schema["properties"]["fact_ids"]["items"]["enum"]
        requests.append(request.url.host)
        return httpx.Response(200, json=success(provider, structured={"fact_ids": ["scope"]}))

    config = OPENAI_CONFIG if provider == "openai" else CLAUDE_CONFIG
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = llm.explain_result("나 졸업 괜찮아?", diagnosis(), config, client=client)
    assert result["used"] and len(requests) == 1
    assert {"current", "in_progress", "plan", "scope"}.issubset(result["displayed_fact_ids"])
    assert "미충족" in result["text"] and "조건부 가능" in result["text"]
