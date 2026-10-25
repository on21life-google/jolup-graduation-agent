"""Real Streamlit UI recovery paths; only external/domain boundaries are replaced."""
from copy import deepcopy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from jolup.mcp_client import ServiceError
from jolup.samples import load_sample

APP = Path(__file__).resolve().parents[1] / "app.py"


def fresh():
    app = AppTest.from_file(str(APP), default_timeout=45).run()
    assert not app.exception
    return app


def button(app, label):
    return next(item for item in app.button if item.label == label)


def text_of(app, kind):
    return "\n".join(item.value for item in getattr(app, kind))


def test_source_page_context_and_gpa_decision_are_visible(monkeypatch):
    from jolup import agent
    context = "표 머리글: 2024학번 전공심화\n예외 조항: 원문 전체 문맥"
    monkeypatch.setattr(agent, "respond", lambda *a, **kw: {
        "answer": "근거 확인", "rows": [], "meta": [], "evidence": [
            {"source_id": "test", "page": 24, "text": "짧은 검색 조각", "context_text": context}]})
    app = fresh()
    button(app, "나 졸업 괜찮아?").click().run()
    assert not app.exception
    assert context in text_of(app, "text")
    assert any("표 머리글·예외" in item.label for item in app.expander)
    assert any("평점 반영" in table.value.columns for table in app.dataframe)


def test_projection_review_is_shown_before_planning(monkeypatch):
    from jolup import mcp_client
    original = mcp_client.check_requirements
    def with_review(*args, **kwargs):
        result = original(*args, **kwargs)
        result["projection"]["review_items"] = ["수강 중 재수강 승인 자료 확인 필요"]
        return result
    monkeypatch.setattr(mcp_client, "check_requirements", with_review)
    app = fresh()
    assert "수강 중 재수강 승인 자료 확인 필요" in text_of(app, "warning")


@pytest.mark.parametrize("response,expected", [
    ({"ok": True, "models": ["gpt-4.1-mini"], "has_more": True}, "추가 페이지"),
    ({"ok": False, "error": "모델 목록 권한 확인 필요"}, "모델 목록 권한 확인 필요"),
])
def test_model_list_is_explicit_and_does_not_replace_model(monkeypatch, response, expected):
    from jolup import llm
    calls = []
    monkeypatch.setattr(llm, "list_available_models", lambda config: calls.append(config) or response)
    app = fresh()
    app.text_input(key="api_key").input("sk-proj-ui-fixture").run()
    app.text_input(key="api_model").input("gpt-4.1-mini").run()
    assert not calls
    button(app, "사용 가능한 모델 확인").click().run()
    assert len(calls) == 1 and not app.exception
    assert app.text_input(key="api_model").value == "gpt-4.1-mini"
    assert expected in text_of(app, "caption") + text_of(app, "warning")
    if response["ok"]:
        assert app.text_area(key="api_model_list_display").value == "gpt-4.1-mini"
    app.run()
    assert len(calls) == 1


def test_env_download_requires_explicit_model_and_uses_only_session_key():
    app = fresh()
    assert button(app, "키 포함 .env 다운로드 준비").disabled
    app.text_input(key="api_key").input("sk-proj-ui-download-fixture").run()
    button(app, "키 포함 .env 다운로드 준비").click().run()
    assert "모델 ID를 먼저 입력" in text_of(app, "warning")
    assert not any(item.label == ".env 파일 다운로드" for item in app.download_button)
    app.text_input(key="api_model").input("gpt-4.1-mini").run()
    button(app, "키 포함 .env 다운로드 준비").click().run()
    assert not app.exception
    download = next(item for item in app.download_button if item.label == ".env 파일 다운로드")
    assert download.proto.url and not download.proto.disabled
    assert "실제 키가 포함됩니다" in text_of(app, "caption")
    assert "sk-proj-ui-download-fixture" not in text_of(app, "caption")


def test_csv_transport_failure_preserves_previous_courses_and_chat(monkeypatch):
    from jolup import mcp_client
    app = fresh()
    button(app, "나 졸업 괜찮아?").click().run()
    before_courses = deepcopy(app.session_state["courses"])
    before_messages = deepcopy(app.session_state["messages"])
    def unavailable(data):
        raise ServiceError("성적표 검사 서비스 연결을 확인해 주세요.")
    monkeypatch.setattr(mcp_client, "parse_transcript", unavailable)
    app.file_uploader(key="upload_csv").upload("dummy.csv", load_sample("credit_shortfall")["csv_bytes"], "text/csv").run()
    button(app, "CSV 검증 후 적용").click().run()
    assert not app.exception
    assert "성적표 검사 서비스" in text_of(app, "error")
    assert app.session_state["courses"] == before_courses
    assert app.session_state["messages"] == before_messages
    assert app.session_state["diagnosis"]["totals"]["total"] == 90


def test_failed_recalculation_preserves_previous_complete_pair_and_recovers(monkeypatch):
    from jolup import mcp_client
    app = fresh()
    before_diagnosis = deepcopy(app.session_state["diagnosis"])
    before_plan = deepcopy(app.session_state["plan"])
    before_fingerprint = app.session_state["calculation_fingerprint"]
    original = mcp_client.plan_courses
    def unavailable(*args, **kwargs):
        raise ServiceError("계획 서비스 연결을 확인해 주세요.")
    monkeypatch.setattr(mcp_client, "plan_courses", unavailable)
    app.number_input(key="profile_chapel").set_value(6)
    button(app, "학생 조건 적용").click().run()
    assert not app.exception
    assert "계획 서비스 연결" in text_of(app, "error")
    assert not app.metric  # A failed fresh calculation must not display stale results.
    assert app.session_state["diagnosis"] == before_diagnosis
    assert app.session_state["plan"] == before_plan
    assert app.session_state["calculation_fingerprint"] == before_fingerprint
    monkeypatch.setattr(mcp_client, "plan_courses", original)
    app.run()
    assert not app.exception and app.metric
    assert app.session_state["calculation_fingerprint"] != before_fingerprint
    chapel = next(row for row in app.session_state["diagnosis"]["rows"] if row["area"] == "채플")
    assert chapel["earned"] == 6


def test_chat_failure_is_redacted_then_next_question_can_recover(monkeypatch):
    from jolup import agent
    original = agent.respond
    def unavailable(*args, **kwargs):
        raise RuntimeError("provider private message")
    monkeypatch.setattr(agent, "respond", unavailable)
    app = fresh()
    button(app, "나 졸업 괜찮아?").click().run()
    assert not app.exception
    result = app.session_state["messages"][-1]["result"]
    assert result["status"] == "확인 필요" and result["execution_mode"] == "처리 오류"
    assert "private message" not in str(app.session_state.to_dict())
    assert "오류가 발생" in text_of(app, "markdown")
    monkeypatch.setattr(agent, "respond", original)
    button(app, "2026년 졸업 관련 일정 알려줘").click().run()
    assert not app.exception and len(app.session_state["messages"]) == 2
    assert app.session_state["messages"][-1]["result"]["events"]
    assert any("일정" in table.value.columns for table in app.dataframe)


def test_calendar_search_outage_is_visible_without_losing_academic_result(monkeypatch):
    from jolup import mcp_client
    original = mcp_client.call_domain
    def service(name, arguments):
        if name == "search_academic_events":
            raise ServiceError("일정 서비스 연결을 확인해 주세요.")
        return original(name, arguments)
    monkeypatch.setattr(mcp_client, "call_domain", service)
    app = fresh()
    assert "일정 서비스 연결" in text_of(app, "error")
    assert "확인된 학교 일정이 없습니다" in text_of(app, "info")
    assert app.session_state["diagnosis"]["totals"]["total"] == 90
    assert not any(item.label == "승인한 일정 ICS 받기" for item in app.download_button)


def test_approved_calendar_export_failure_disables_download(monkeypatch):
    from jolup import mcp_client
    app = fresh()
    app.multiselect(key="calendar_selection_2026").select("EVENT_13").run()
    original = mcp_client.call_domain
    def service(name, arguments):
        if name == "create_calendar_event":
            raise ServiceError("ICS 생성 서비스 연결을 확인해 주세요.")
        return original(name, arguments)
    monkeypatch.setattr(mcp_client, "call_domain", service)
    next(item for item in app.checkbox if item.key.startswith("approve_")).check().run()
    assert not app.exception and "ICS 생성 서비스 연결" in text_of(app, "error")
    assert next(item for item in app.download_button if item.label == "승인한 일정 ICS 받기").proto.disabled


@pytest.mark.parametrize("meta", [{"filename": "legacy.txt", "url": None}, [None, {"source_id": "legacy"}], [None]])
def test_legacy_evidence_is_readable_and_partial_plan_does_not_claim_completion(monkeypatch, meta):
    from jolup import agent
    def response(*args, **kwargs):
        return {"answer": "일부 근거만 확인됨", "rows": [], "evidence": ["학교 상담 기록", {"file_name": "학교 자료", "excerpt": "원문 구절"}],
            "meta": meta, "llm": {"error": "설명 API 연결 확인 필요"}, "routing": {"error": "분류 API 연결 확인 필요"},
            "plan": {"semesters": [{"term": "2027-1", "courses": []}], "warnings": ["실제 개설 확인 필요"]},
            "study_trace": {"steps": []}}
    monkeypatch.setattr(agent, "respond", response)
    app = fresh()
    button(app, "나 졸업 괜찮아?").click().run()
    assert not app.exception
    assert "학교 상담 기록" in text_of(app, "markdown")
    assert "원문 구절" in text_of(app, "caption")
    assert "배치한 수강 후보가 없습니다" in text_of(app, "caption")
    assert "설명 API 연결 확인 필요" in text_of(app, "warning")
    assert "로컬 분류로 처리했어요" in text_of(app, "warning")
    assert not any(item.label == "실행 과정 살펴보기" for item in app.expander)
    assert any("파일명" in item.value.columns for item in app.dataframe)
    if meta == [None]:
        assert "표시할 항목이 없습니다" in text_of(app, "caption")


@pytest.mark.parametrize("status,expected", [("blocked", "기본 더미 기록"), ("disabled", "입력만으로 전송되지는"), ("unknown", "자동으로 재전송하지")])
def test_trace_export_non_success_feedback_is_actionable(monkeypatch, status, expected):
    from jolup import observability
    calls = []
    monkeypatch.setattr(observability, "export_study_trace", lambda trace, config: calls.append(trace) or {"status": status})
    app = fresh()
    app.text_input(key="langsmith_api_key").input("lsv2-offline-ui-fixture").run()
    button(app, "나 졸업 괜찮아?").click().run()
    button(app, "이 더미 실행을 LangSmith로 보내기").click().run()
    assert not app.exception and len(calls) == 1
    assert expected in text_of(app, "warning") + text_of(app, "info")
    assert not button(app, "이 더미 실행을 LangSmith로 보내기").disabled
    app.run()
    assert len(calls) == 1
