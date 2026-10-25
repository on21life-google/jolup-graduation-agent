"""Real Streamlit widget interactions; model calls are mocked explicitly."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from jolup.samples import list_samples, load_sample

APP = Path(__file__).resolve().parents[1] / "app.py"


def button(app, label):
    return next(item for item in app.button if item.label == label)


def fresh():
    app = AppTest.from_file(str(APP), default_timeout=45).run()
    assert not app.exception
    return app


def choose(app, sample_id):
    app.selectbox(key="sample_selector").select(sample_id).run()
    button(app, "사례 불러오기").click().run()
    assert not app.exception
    return app


@pytest.mark.parametrize("sample_id", [sample["id"] for sample in list_samples()])
def test_every_sample_loads_and_agrees_with_fixed_expected_results(sample_id):
    app = choose(fresh(), sample_id)
    expected = load_sample(sample_id)["expected"]
    diagnosis = app.session_state["diagnosis"]
    assert diagnosis["status"] == expected["status"]
    assert diagnosis["totals"]["total"] == expected["total"]
    assert diagnosis["totals"]["major_total"] == expected["major_total"]
    if "plan_status" in expected:
        assert app.session_state["plan"]["status"] == expected["plan_status"]
    if sample_id == "missing_record":
        assert app.number_input(key="profile_chapel").value is None
        button(app, "학생 조건 적용").click().run()
        assert app.session_state["profile"]["chapel_pass_semesters"] is None
        assert app.session_state["diagnosis"]["status"] == "확인 필요"


def test_diagnosis_chat_has_four_sections_and_keeps_current_separate():
    app = fresh()
    button(app, "나 졸업 괜찮아?").click().run()
    assert not app.exception
    result = app.session_state["messages"][-1]["result"]
    assert result["totals"]["total"] == 90
    assert result["projection"]["totals"]["total"] == 105
    assert result["status"] == "미충족"
    assert result["plan"]["status"] == "조건부 가능"
    assert result["rows"] and result["evidence"] and result["meta"]
    assert result["execution_mode"] == "로컬 계산·검색"
    labels = "\n".join(item.value for item in app.markdown)
    for section in ("① 답변", "② reason", "③ 근거자료", "④ meta"):
        assert section in labels
    app.chat_input[0].set_value("그럼 다 채울 수 있어?").run()
    assert not app.exception
    assert app.session_state["messages"][-1]["result"]["intent"] == "planning"
    assert app.session_state["messages"][-1]["result"]["totals"]["total"] == 90


def test_each_planned_term_shows_its_own_projected_diagnosis():
    app = fresh()
    labels = [item.label for item in app.expander]
    assert "2027-1 학기 말 예상 진단표" in labels
    assert "2027-2 학기 말 예상 진단표" in labels
    tables = [item.value for item in app.dataframe]
    assert any("계산 기준" in table.columns for table in tables)
    plan = app.session_state["plan"]
    assert [term["projected_totals"]["total"] for term in plan["semesters"]] == [114, 123]


def test_cached_diagnosis_is_invalidated_when_reviewed_source_changes(monkeypatch):
    from jolup import academic, evidence
    app = fresh()
    assert app.session_state["diagnosis"]["status"] == "미충족"
    changed = lambda: ["검토 후 규정 원본 또는 계산 규칙이 변경되었습니다."]
    monkeypatch.setattr(evidence, "validate_rule_bundle", changed)
    monkeypatch.setattr(academic, "validate_rule_bundle", changed)
    app.run()
    assert not app.exception
    assert app.session_state["diagnosis"]["status"] == "확인 필요"
    assert app.session_state["plan"]["status"] == "확인 필요"


def test_csv_upload_removes_identifiers_and_recalculates_without_old_chat():
    app = fresh()
    button(app, "나 졸업 괜찮아?").click().run()
    raw = load_sample("credit_shortfall")["csv_bytes"].decode("utf-8-sig")
    raw = raw.replace("김가온 (가상)", "업로드가상인물").replace("DUMMY-2024-001", "PRIVATE-STUDENT-ID")
    app.file_uploader(key="upload_csv").upload("fictional.csv", raw.encode("utf-8-sig"), "text/csv").run()
    button(app, "CSV 검증 후 적용").click().run()
    assert not app.exception
    assert app.session_state["parse_feedback"]["ok"]
    assert {"이름", "학번"} <= set(app.session_state["parse_feedback"]["removed_columns"])
    assert "PRIVATE-STUDENT-ID" not in str(app.session_state["courses"])
    assert "업로드가상인물" not in str(app.session_state["courses"])
    assert app.session_state["diagnosis"]["totals"]["total"] == 38
    assert app.session_state["messages"] == []
    assert app.session_state["csv_bytes"] == b""


def test_invalid_csv_reports_error_and_preserves_previous_valid_record():
    app = fresh()
    app.file_uploader(key="upload_csv").upload("bad.csv", b"name\nFAKE", "text/csv").run()
    button(app, "CSV 검증 후 적용").click().run()
    assert not app.exception
    assert not app.session_state["parse_feedback"]["ok"]
    assert app.error
    assert app.session_state["diagnosis"]["totals"]["total"] == 90


def test_common_ai_ui_switches_only_key_and_keeps_calculation(monkeypatch):
    from jolup import llm
    seen = []

    def mocked_explanation(query, result, config):
        info = llm.resolve_config(config)
        seen.append(info)
        return {"used": info["ok"], "text": "모의 API 설명", "provider": info["provider"], "model": info["model"]}

    monkeypatch.setattr(llm, "explain_result", mocked_explanation)
    monkeypatch.setattr(llm, "classify_intent", lambda *args, **kwargs: {"intent": "diagnosis", "used": True}, raising=False)
    app = fresh()
    app.text_input(key="api_key").input("sk-proj-fake-test-only").run()
    app.checkbox(key="api_enabled").check().run()
    button(app, "나 졸업 괜찮아?").click().run()
    app.text_input(key="api_key").input("sk-ant-fake-test-only").run()
    button(app, "나 졸업 괜찮아?").click().run()
    assert not app.exception
    assert [item["provider"] for item in seen] == ["openai", "anthropic"]
    assert app.selectbox(key="api_provider").value == "auto"
    assert app.text_input(key="api_model").value == ""
    for message in app.session_state["messages"]:
        result = message["result"]
        assert result["totals"]["total"] == 90
        assert result["ai_explanation"] == "모의 API 설명"
        assert "sk-" not in str(result)
    app.checkbox(key="api_enabled").uncheck().run()
    button(app, "나 졸업 괜찮아?").click().run()
    assert len(seen) == 2


def test_custom_models_are_remembered_per_provider_without_automatic_calls(monkeypatch):
    from jolup import llm

    def forbidden(*args, **kwargs):
        pytest.fail("Changing keys, service, or model must not make an API call")

    monkeypatch.setattr(llm, "generate", forbidden)
    monkeypatch.setattr(llm, "list_available_models", forbidden)
    app = fresh()
    app.text_input(key="api_key").input("sk-proj-first-dummy").run()
    app.text_input(key="api_model").input("gpt-4.1-mini").run()
    app.checkbox(key="api_enabled").check().run()
    app.text_input(key="api_key").input("sk-proj-second-dummy").run()
    assert app.text_input(key="api_model").value == "gpt-4.1-mini"
    app.text_input(key="api_key").input("sk-ant-first-dummy").run()
    assert app.text_input(key="api_model").value == ""
    app.text_input(key="api_model").input("claude-example-custom").run()
    app.text_input(key="api_key").input("sk-proj-third-dummy").run()
    assert app.text_input(key="api_model").value == "gpt-4.1-mini"
    assert app.checkbox(key="api_enabled").value is True
    app.text_input(key="api_key").input("sk-ant-second-dummy").run()
    assert app.text_input(key="api_model").value == "claude-example-custom"
    assert app.session_state["api_models_by_provider"] == {"openai": "gpt-4.1-mini", "anthropic": "claude-example-custom"}
    assert app.session_state["api_model_provider"] == "anthropic"
    state = str(app.session_state.to_dict())
    for previous_key in ("sk-proj-first-dummy", "sk-proj-second-dummy", "sk-proj-third-dummy", "sk-ant-first-dummy"):
        assert previous_key not in state
    assert not app.exception and not app.session_state["messages"]


def test_provider_model_memory_supports_manual_service_and_keeps_existing_session(monkeypatch):
    from jolup import llm
    monkeypatch.setattr(llm, "generate", lambda *args, **kwargs: pytest.fail("No automatic API calls"))
    app = AppTest.from_file(str(APP), default_timeout=45)
    app.session_state["api_key"] = "sk-proj-existing-session-dummy"
    app.session_state["api_model"] = "gpt-4.1-mini"
    app.session_state["api_provider"] = "auto"
    app.run()
    assert not app.exception
    assert app.text_input(key="api_model").value == "gpt-4.1-mini"
    assert app.session_state["api_models_by_provider"] == {"openai": "gpt-4.1-mini"}
    app.text_input(key="api_key").input("unknown-provider-dummy").run()
    assert app.text_input(key="api_model").value == "gpt-4.1-mini"
    app.selectbox(key="api_provider").select("anthropic").run()
    assert app.text_input(key="api_model").value == ""
    app.text_input(key="api_model").input("claude-example-custom").run()
    app.selectbox(key="api_provider").select("openai").run()
    assert app.text_input(key="api_model").value == "gpt-4.1-mini"
    assert not app.exception


def test_calendar_preview_requires_approval_and_selection_change_resets_it():
    app = fresh()
    app.multiselect(key="calendar_selection_2026").select("EVENT_13").run()
    download = next(item for item in app.download_button if item.label == "승인한 일정 ICS 받기")
    assert download.proto.disabled
    approval = next(item for item in app.checkbox if item.key.startswith("approve_"))
    assert not approval.value
    approval.check().run()
    assert not next(item for item in app.download_button if item.label == "승인한 일정 ICS 받기").proto.disabled
    app.multiselect(key="calendar_selection_2026").select("EVENT_10").run()
    assert not next(item for item in app.checkbox if item.key.startswith("approve_")).value
    assert next(item for item in app.download_button if item.label == "승인한 일정 ICS 받기").proto.disabled
    app.selectbox(key="calendar_year").select(2028).run()
    assert any("확인된 학교 일정이 없습니다" in item.value for item in app.info)
    assert not [item for item in app.download_button if item.label == "승인한 일정 ICS 받기"]


def test_session_reset_discards_uploaded_file_key_chat_and_calendar_approval():
    app = fresh()
    app.file_uploader(key="upload_csv").upload("fictional.csv", load_sample("credit_shortfall")["csv_bytes"], "text/csv").run()
    button(app, "CSV 검증 후 적용").click().run()
    app.text_input(key="api_key").input("sk-proj-session-secret").run()
    button(app, "나 졸업 괜찮아?").click().run()
    app.multiselect(key="calendar_selection_2026").select("EVENT_13").run()
    next(item for item in app.checkbox if item.key.startswith("approve_")).check().run()
    button(app, "세션 초기화").click().run()
    assert not app.exception
    assert app.session_state["sample_id"] == "on_track"
    assert app.session_state["diagnosis"]["totals"]["total"] == 90
    assert app.session_state["messages"] == []
    assert app.text_input(key="api_key").value == ""
    assert app.file_uploader(key="upload_csv").value is None
    assert app.multiselect(key="calendar_selection_2026").value == []
    assert not any(key.startswith("approve_") for key in app.session_state)
    assert "sk-proj-session-secret" not in str(app.session_state.to_dict())
