"""Study UI uses local records and requires an explicit, one-run export click."""
from copy import deepcopy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from jolup.samples import load_sample

APP = Path(__file__).resolve().parents[1] / "app.py"


def button(app, label):
    return next(item for item in app.button if item.label == label)


@pytest.fixture
def study_calls(monkeypatch):
    from jolup import agent, observability
    calls = {"respond": [], "export": []}

    def respond(query, profile, courses, context=None):
        context = context or {}
        calls["respond"].append(deepcopy(context))
        sample_id = context.get("builtin_sample_id")
        run_number = len(calls["respond"])
        return {
            "answer": "현재는 미충족, 목표까지는 조건부 가능해요.",
            "intent": "diagnosis", "rows": [], "meta": [], "evidence": [],
            "study_trace": {
                "schema_version": 1, "run_id": f"study-test-{run_number}",
                "case_id": sample_id, "exportable": bool(sample_id),
                "summary": {"status": "미충족", "total": 90},
                "steps": [
                    {"id": "root", "parent_id": None, "name": "respond", "label": "응답 실행",
                     "source_ref": "jolup/agent.py:respond", "duration_ms": 12.4, "status": "ok",
                     "summary": {"intent": "diagnosis"}},
                    {"id": "diagnosis", "parent_id": "root", "name": "diagnosis", "label": "현재 이수 진단",
                     "source_ref": "jolup/academic.py:check_requirements", "duration_ms": 4.2, "status": "ok",
                     "summary": {"current_total": 90, "in_progress": 15}},
                ],
            },
        }

    def export(trace, config):
        assert isinstance(config["workspace_id"], str), "The export API requires a string, including for an omitted workspace"
        calls["export"].append((deepcopy(trace), deepcopy(config)))
        return {"status": "sent"}

    monkeypatch.setattr(agent, "respond", respond)
    monkeypatch.setattr(observability, "export_study_trace", export)
    return calls


def fresh():
    app = AppTest.from_file(str(APP), default_timeout=45).run()
    assert not app.exception
    return app


def ask(app):
    button(app, "나 졸업 괜찮아?").click().run()
    assert not app.exception
    return app


def test_local_steps_show_source_duration_summary_without_a_key(study_calls):
    app = ask(fresh())
    assert any(item.label == "실행 과정 살펴보기" for item in app.expander)
    tables = [item.value for item in app.dataframe]
    trace_table = next(table for table in tables if "함수·파일" in table.columns)
    assert list(trace_table["상위 단계"]) == ["—", "응답 실행"]
    assert trace_table.iloc[1]["소요 ms"] == 4.2
    assert "jolup/academic.py:check_requirements" in trace_table.iloc[1]["함수·파일"]
    app.selectbox(key="study_step_study-test-1").select("diagnosis").run()
    assert any("current_total" in item.value for item in app.json)
    assert button(app, "이 더미 실행을 LangSmith로 보내기").disabled
    assert study_calls["respond"][0]["builtin_sample_id"] == "on_track"
    assert study_calls["export"] == []


def test_settings_and_question_do_not_export_and_leave_model_key_unchanged(study_calls):
    app = fresh()
    app.text_input(key="api_key").input("sk-proj-existing-dummy-key").run()
    app.text_input(key="api_model").input("gpt-4.1-mini").run()
    app.text_input(key="langsmith_api_key").input("ls-test-private-key").run()
    app.text_input(key="langsmith_project").input("my-study").run()
    app.selectbox(key="langsmith_region").select("EU").run()
    app.text_input(key="langsmith_workspace").input("test-workspace").run()
    ask(app)
    assert not button(app, "이 더미 실행을 LangSmith로 보내기").disabled
    assert study_calls["export"] == []
    assert app.text_input(key="api_key").value == "sk-proj-existing-dummy-key"
    assert app.text_input(key="api_model").value == "gpt-4.1-mini"
    assert "ls-test-private-key" not in str(study_calls["respond"])
    assert "ls-test-private-key" not in str(app.session_state["messages"])


def test_explicit_export_once_and_success_prevents_duplicate(study_calls):
    app = fresh()
    app.text_input(key="langsmith_api_key").input("ls-test-private-key").run()
    app.text_input(key="langsmith_project").input("my-study").run()
    app.selectbox(key="langsmith_region").select("EU").run()
    app.text_input(key="langsmith_workspace").input("test-workspace").run()
    ask(app)
    button(app, "이 더미 실행을 LangSmith로 보내기").click().run()
    assert not app.exception
    assert len(study_calls["export"]) == 1
    trace, config = study_calls["export"][0]
    assert config == {"api_key": "ls-test-private-key", "project": "my-study", "region": "EU", "workspace_id": "test-workspace"}
    assert trace["run_id"] == "study-test-1"
    assert button(app, "이 더미 실행을 LangSmith로 보내기").disabled
    assert any("프로젝트 my-study" in item.value and "study-test-1" in item.value for item in app.success)
    assert any("전송 요청 수락" in item.value for item in app.success)
    assert any("클라우드 저장 결과는 다시 조회하지 않았습니다" in item.value for item in app.caption)
    app.selectbox(key="study_step_study-test-1").select("diagnosis").run()
    app.text_input(key="langsmith_project").input("another-project").run()
    assert len(study_calls["export"]) == 1
    assert button(app, "이 더미 실행을 LangSmith로 보내기").disabled
    assert any("프로젝트 my-study" in item.value for item in app.success)


def test_failure_is_redacted_and_retries_only_on_new_click(study_calls, monkeypatch):
    from jolup import observability

    def failing(trace, config):
        study_calls["export"].append((trace, config))
        raise RuntimeError("Authorization: ls-never-show-this-exception")

    monkeypatch.setattr(observability, "export_study_trace", failing)
    app = fresh()
    app.text_input(key="langsmith_api_key").input("ls-test-private-key").run()
    ask(app)
    button(app, "이 더미 실행을 LangSmith로 보내기").click().run()
    assert not app.exception
    assert len(study_calls["export"]) == 1
    assert any("자동으로 재전송하지 않습니다" in item.value for item in app.warning)
    assert any("일부 단계는 이미 전송됐을 수 있습니다" in item.value for item in app.warning)
    assert "ls-never-show-this-exception" not in str(app.session_state.to_dict())
    assert not any("ls-never-show-this-exception" in item.value for item in app.warning)
    app.text_input(key="langsmith_project").input("try-later").run()
    assert len(study_calls["export"]) == 1
    button(app, "이 더미 실행을 LangSmith로 보내기").click().run()
    assert len(study_calls["export"]) == 2


def test_uploaded_csv_disables_export_until_builtin_sample_is_reloaded(study_calls):
    app = fresh()
    app.text_input(key="langsmith_api_key").input("ls-test-private-key").run()
    app.file_uploader(key="upload_csv").upload("dummy.csv", load_sample("on_track")["csv_bytes"], "text/csv").run()
    button(app, "CSV 검증 후 적용").click().run()
    ask(app)
    assert app.session_state["study_builtin_sample_id"] is None
    assert study_calls["respond"][-1]["builtin_sample_id"] is None
    assert button(app, "이 더미 실행을 LangSmith로 보내기").disabled
    assert study_calls["export"] == []
    button(app, "사례 불러오기").click().run()
    ask(app)
    assert app.session_state["study_builtin_sample_id"] == "on_track"
    assert not button(app, "이 더미 실행을 LangSmith로 보내기").disabled


def test_old_session_does_not_infer_builtin_origin(study_calls):
    app = fresh()
    del app.session_state["study_builtin_sample_id"]
    app.text_input(key="langsmith_api_key").input("ls-test-private-key").run()
    ask(app)
    assert study_calls["respond"][-1]["builtin_sample_id"] is None
    assert button(app, "이 더미 실행을 LangSmith로 보내기").disabled
    assert study_calls["export"] == []


def test_session_reset_clears_langsmith_keys_settings_and_sent_status(study_calls):
    app = fresh()
    app.text_input(key="langsmith_api_key").input("ls-test-private-key").run()
    app.text_input(key="langsmith_project").input("previous-study").run()
    app.selectbox(key="langsmith_region").select("EU").run()
    app.text_input(key="langsmith_workspace").input("old-workspace").run()
    ask(app)
    button(app, "이 더미 실행을 LangSmith로 보내기").click().run()
    assert app.session_state["study_sent_runs"]
    # The exporter accepts an optional workspace as an empty string, not None.
    # Verify this contract separately with the ordinary blank-workspace path.
    app.text_input(key="langsmith_workspace").input("   ").run()
    ask(app)
    button(app, "이 더미 실행을 LangSmith로 보내기").click().run()
    assert study_calls["export"][-1][1]["workspace_id"] == ""
    button(app, "세션 초기화").click().run()
    assert not app.exception
    assert app.text_input(key="langsmith_api_key").value == ""
    assert app.text_input(key="langsmith_project").value == "jolup-study"
    assert app.selectbox(key="langsmith_region").value == "US"
    assert app.text_input(key="langsmith_workspace").value == ""
    assert "study_sent_runs" not in app.session_state
    assert "study_export_results" not in app.session_state
    assert app.session_state["messages"] == []
    assert "ls-test-private-key" not in str(app.session_state.to_dict())
