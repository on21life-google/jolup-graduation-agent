"""The generated learning contracts stay importable, reproducible, and approval-aware."""
import json
import runpy
import sys
from pathlib import Path

from jolup.study_api import app
from scripts import export_api_contract


def test_export_writes_matching_openapi_collection_and_local_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(export_api_contract, "ROOT", tmp_path)
    export_api_contract.main()
    destination = tmp_path / "docs" / "api"
    assert {path.name for path in destination.iterdir()} == {
        "openapi.json", "jolup.postman_collection.json", "local.postman_environment.json"}
    spec = json.loads((destination / "openapi.json").read_text())
    assert spec == app.openapi()
    collection = json.loads((destination / "jolup.postman_collection.json").read_text())
    environment = json.loads((destination / "local.postman_environment.json").read_text())
    assert len(collection["item"]) == 21
    variables = {item["key"]: item["value"] for item in environment["values"]}
    assert variables == {"base_url": "http://127.0.0.1:8001", "calendar_approved": "false"}
    for item in collection["item"]:
        request = item["request"]
        path = request["url"].removeprefix("{{base_url}}").split("?")[0]
        if path.startswith("/samples/"):
            path = "/samples/{sample_id}"
        if path.startswith("/sessions/"):
            path = "/sessions/{session_id}"
        assert request["method"].lower() in spec["paths"][path]
        assert item["event"][0]["script"]["exec"], item["name"]
    assert "docs/api/openapi.json" in capsys.readouterr().out


def test_postman_chains_actual_student_records_and_preserves_explicit_approval():
    collection = export_api_contract.collection()
    requests = collection["item"]
    for item in requests:
        request = item["request"]
        if request["method"] == "POST":
            assert request["header"] == [{"key": "Content-Type", "value": "application/json"}]
        if "더미 입력 받기" in item["name"]:
            script = "\n".join(item["event"][0]["script"]["exec"])
            assert "profile: sample.profile, courses: sample.courses" in script
            assert "sample.expected" not in script
    assert requests[3]["request"]["body"]["raw"] == "{{student_request}}"
    assert requests[15]["request"]["body"]["raw"].endswith('"approved": false}')
    assert requests[16]["request"]["body"]["raw"].endswith('"approved": {{calendar_approved}}}')
    variables = {item["key"]: item["value"] for item in collection["variable"]}
    assert variables["calendar_approved"] == "false"
    assert "HTTP 422" in "\n".join(requests[12]["event"][0]["script"]["exec"])
    assert "HTTP 403" in "\n".join(requests[15]["event"][0]["script"]["exec"])


def test_export_script_entrypoint_adds_project_import_path_without_network_or_real_writes(monkeypatch):
    """Exercise the direct-file bootstrap while capturing its three output writes."""
    saved = {}
    def capture_write(path, content, encoding):
        assert encoding == "utf-8"
        saved[path.name] = json.loads(content)
        return len(content)
    monkeypatch.setattr(Path, "write_text", capture_write)
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "path", [path for path in sys.path if path != str(export_api_contract.ROOT)])
    runpy.run_path(str(Path(export_api_contract.__file__)), run_name="__main__")
    assert str(export_api_contract.ROOT) == sys.path[0]
    assert saved["openapi.json"]["paths"] == app.openapi()["paths"]
    assert len(saved["jolup.postman_collection.json"]["item"]) == 21
    assert saved["local.postman_environment.json"]["_postman_variable_scope"] == "environment"


def test_postman_chat_sequence_uses_new_session_then_followup_then_reset():
    requests = export_api_contract.collection()['item']
    chat = requests[18]
    followup = requests[19]
    reset = requests[20]
    assert chat['request']['url'].endswith('/chat')
    assert chat['request']['body']['raw'] == '{{chat_request}}'
    assert followup['request']['body']['raw'] == '{{followup_request}}'
    chat_checks = '\n'.join(chat['event'][0]['script']['exec'])
    assert 'session_id: result.session_id' in chat_checks
    assert 'ai_enabled' in chat_checks
    assert reset['request']['method'] == 'DELETE'
    assert reset['request']['url'].endswith('/sessions/{{chat_session_id}}')
