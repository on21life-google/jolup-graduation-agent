"""User-visible mixed answers and bounded dialogue reuse in the real UI."""
from pathlib import Path
from streamlit.testing.v1 import AppTest


def fresh():
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=45).run()
    assert not app.exception
    return app


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_mixed_answer_displays_each_requested_section(monkeypatch):
    from jolup import agent
    monkeypatch.setattr(agent, "respond", lambda *a, **kw: {
        "intent": "diagnosis", "answer": "종합 답변", "rows": [],
        "sections": [
            {"intent": "regulation", "answer": "학교 근거에 따른 답변", "rows": [], "evidence": [], "meta": []},
            {"intent": "calendar", "answer": "요청한 학교 일정", "events": [{"title": "시험일", "start_date": "2026-12-01"}], "evidence": [], "meta": []}],
        "evidence": [], "meta": []})
    app = fresh()
    app.chat_input[0].set_value("규정과 일정을 함께 알려줘").run()
    assert not app.exception
    text = "\n".join(item.value for item in app.markdown)
    assert "학교 근거에 따른 답변" in text
    assert "요청한 학교 일정" in text
    assert any("시험일" in str(table.value) for table in app.dataframe)


def test_local_conversation_is_passed_and_reset_when_profile_changes(monkeypatch):
    from jolup import agent
    seen = []
    def respond(*args, context=None, **kwargs):
        seen.append(context["conversation"])
        return {"answer": "시험 답변", "intent": "diagnosis", "rows": [], "evidence": [], "meta": []}
    monkeypatch.setattr(agent, "respond", respond)
    app = fresh()
    button(app, "나 졸업 괜찮아?").click().run()
    button(app, "나 졸업 괜찮아?").click().run()
    assert len(seen) == 2 and seen[0] is seen[1]
    app.number_input(key="profile_chapel").set_value(6)
    button(app, "학생 조건 적용").click().run()
    button(app, "나 졸업 괜찮아?").click().run()
    assert len(seen) == 3 and seen[2] is not seen[0]


def test_http_mode_keeps_server_session_and_resets_it_without_llm_key(monkeypatch):
    from jolup import chat_client
    seen, resets = [], []
    monkeypatch.setenv("JOLUP_AGENT_API_URL", "http://127.0.0.1:8001")
    def respond(*args, context=None, **kwargs):
        seen.append(context.get("session_id"))
        return {"session_id": "ui-session", "answer": "검증한 결과 재사용", "intent": "diagnosis",
                "memory": {"reused": True}, "rows": [], "evidence": [], "meta": [],
                "study_trace": {"run_id": "api-run", "exportable": True, "steps": [
                    {"id": "root", "name": "respond", "label": "응답", "source_ref": "jolup/agent.py:respond"}]}}
    monkeypatch.setattr(chat_client, "respond_chat", respond)
    monkeypatch.setattr(chat_client, "reset_chat_session", lambda session_id: resets.append(session_id))
    app = fresh()
    assert app.checkbox(key="api_enabled").disabled
    button(app, "나 졸업 괜찮아?").click().run()
    button(app, "나 졸업 괜찮아?").click().run()
    assert seen == [None, "ui-session"]
    assert button(app, "이 더미 실행을 LangSmith로 보내기").disabled
    assert any("최근 결과" in item.value for item in app.caption)
    button(app, "세션 초기화").click().run()
    assert not app.exception and resets == ["ui-session"]
    assert "chat_session_id" not in app.session_state


def test_session_delete_failure_is_visible_and_never_reuses_old_id(monkeypatch):
    from jolup import chat_client
    def failure(session_id):
        raise chat_client.ChatServiceError("fixed error")
    monkeypatch.setattr(chat_client, "reset_chat_session", failure)
    app = fresh()
    app.session_state["chat_session_id"] = "stale-session"
    button(app, "사례 불러오기").click().run()
    assert "chat_session_id" not in app.session_state
    # Warning is emitted in the resetting run, followed by Streamlit's explicit rerun.
    assert not app.exception and app.session_state["messages"] == []
