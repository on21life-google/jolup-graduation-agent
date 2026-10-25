"""Local Sonar management tests use only mock HTTP and fake Docker commands."""
import hashlib
import json
import stat
import subprocess
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from scripts import sonar_local as sonar


def docker_ready(command, **kwargs):
    output = "127.0.0.1:9000\n" if "port" in command else "our-container\n"
    return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")


def save_credentials(root, **changes):
    folder = root / ".quality"
    folder.mkdir(mode=0o700)
    path = folder / "sonar.json"
    data = {"schema_version": 1, "url": "http://127.0.0.1:9000", "username": "admin",
            "password": "original-password-kept-123!", "password_status": "changed",
            "instance_id": "local-instance", "token": "squ_mock_secret_only", "token_name": "test"}
    data.update(changes)
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path, data


def prepare_stage(root, project="jolup-roadmap"):
    stage = root / ".build" / "sonar-analysis-test"
    stage.mkdir(parents=True)
    (root / "app.py").write_text("print('study')\n")
    (stage / "app.py").write_bytes((root / "app.py").read_bytes())
    (stage / "sonar-project.properties").write_text(f"sonar.projectKey={project}\n")
    prepared = {"schema_version": 1, "path": str(stage), "source_root": str(root), "project": project,
                "source_hashes": {"app.py": hashlib.sha256((root / "app.py").read_bytes()).hexdigest()},
                "properties_sha256": hashlib.sha256((stage / "sonar-project.properties").read_bytes()).hexdigest(),
                "coverage_included": False, "coverage_xml_sha256": None}
    (stage / "stage.json").write_text(json.dumps(prepared))
    reports = root / "reports" / "quality"
    reports.mkdir(parents=True)
    (reports / "prepared.json").write_text(json.dumps(prepared))
    return stage, prepared


def test_password_is_saved_privately_before_change_and_survives_token_failure(tmp_path):
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.host == "127.0.0.1" and request.url.port == 9000
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json={"id": "local-instance", "status": "UP"})
        if request.url.path == "/api/users/change_password":
            path = tmp_path / ".quality/sonar.json"
            saved = json.loads(path.read_text())
            form = parse_qs(request.content.decode())
            assert saved["password"] == form["password"][0] != "admin"
            assert len(saved["password"]) >= 32
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            return httpx.Response(204)
        if request.url.path == "/api/user_tokens/generate":
            return httpx.Response(500, text="secret-token-failure-body")
        raise AssertionError(request.url.path)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(sonar.SonarError) as caught:
            sonar.bootstrap(tmp_path, client=client, runner=docker_ready)
    saved = json.loads((tmp_path / ".quality/sonar.json").read_text())
    assert saved["password_status"] == "changed" and not saved.get("token")
    assert saved["password"] not in str(caught.value)
    assert "secret-token-failure-body" not in str(caught.value)


def test_second_bootstrap_reuses_password_and_token_without_reset(tmp_path):
    path, saved = save_credentials(tmp_path)
    before = path.read_bytes()
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json={"id": "local-instance", "status": "UP"})
        if request.url.path == "/api/authentication/validate":
            return httpx.Response(200, json={"valid": True})
        if request.url.path == "/api/components/show":
            return httpx.Response(200, json={"component": {"key": request.url.params["component"]}})
        raise AssertionError("Existing credentials must not be replaced")
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = sonar.bootstrap(tmp_path, client=client, runner=docker_ready)
    assert path.read_bytes() == before
    assert saved["password"] not in json.dumps(result) and saved["token"] not in json.dumps(result)
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize("symlink_target", ["directory", "file"])
def test_secret_symlinks_are_rejected_before_any_network(tmp_path, symlink_target):
    outside = tmp_path / "unrelated"
    outside.mkdir()
    if symlink_target == "directory":
        (tmp_path / ".quality").symlink_to(outside, target_is_directory=True)
    else:
        (tmp_path / ".quality").mkdir()
        (outside / "secret.json").write_text("{}")
        (tmp_path / ".quality/sonar.json").symlink_to(outside / "secret.json")
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("No network expected"))) as client:
        with pytest.raises(sonar.SonarError, match="symlink"):
            sonar.bootstrap(tmp_path, client=client, runner=docker_ready)


def test_foreign_or_absent_compose_server_is_not_bootstrapped(tmp_path):
    def absent(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("No network expected"))) as client:
        with pytest.raises(sonar.SonarError, match="container"):
            sonar.bootstrap(tmp_path, client=client, runner=absent)


@pytest.mark.parametrize("state", ["FAILED", "CANCELED"])
def test_compute_failure_is_not_reported_as_success(state):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"task": {"status": state, "errorMessage": "private-detail"}}))) as client:
        with pytest.raises(sonar.SonarError, match=state) as caught:
            sonar.wait_for_task(client, "squ_test", "task-1")
    assert "private-detail" not in str(caught.value)


def test_compute_success_requires_an_analysis_id_and_timeout_is_bounded():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"task": {"status": "SUCCESS"}}))) as client:
        with pytest.raises(sonar.SonarError, match="analysis"):
            sonar.wait_for_task(client, "squ_test", "task-1")
    ticks = [0.0]
    def sleep(seconds):
        ticks[0] += seconds
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"task": {"status": "PENDING"}}))) as client:
        with pytest.raises(sonar.SonarError, match="timeout"):
            sonar.wait_for_task(client, "squ_test", "task-1", timeout_seconds=3,
                                clock=lambda: ticks[0], sleep=sleep)
    assert ticks[0] <= 3


def test_stage_scope_and_current_source_hashes_are_checked(tmp_path):
    stage, prepared = prepare_stage(tmp_path)
    assert sonar.validate_stage(tmp_path, "jolup-roadmap")["path"] == str(stage)
    (tmp_path / "app.py").write_text("changed after coverage\n")
    with pytest.raises(sonar.SonarError, match="hash"):
        sonar.validate_stage(tmp_path, "jolup-roadmap")
    prepared["path"] = str(tmp_path / "outside-build")
    (tmp_path / "reports/quality/prepared.json").write_text(json.dumps(prepared))
    with pytest.raises(sonar.SonarError, match="build"):
        sonar.validate_stage(tmp_path, "jolup-roadmap")


@pytest.mark.parametrize("changed_location", ["source", "stage"])
def test_added_python_source_invalidates_prepared_inventory(tmp_path, changed_location):
    stage, _ = prepare_stage(tmp_path)
    location = tmp_path if changed_location == "source" else stage
    added = location / "jolup/new_module.py"
    added.parent.mkdir()
    added.write_text("NEW_LOGIC = True\n")
    with pytest.raises(sonar.SonarError, match="source file set"):
        sonar.validate_stage(tmp_path, "jolup-roadmap")


def test_changed_scanner_exclusions_invalidates_unchanged_project_key(tmp_path):
    stage, _ = prepare_stage(tmp_path)
    (stage / "sonar-project.properties").write_text("sonar.projectKey=jolup-roadmap\nsonar.exclusions=**/*.py\n")
    with pytest.raises(sonar.SonarError, match="scanner configuration hash"):
        sonar.validate_stage(tmp_path, "jolup-roadmap")


def test_scanner_failure_saves_only_redacted_logs_and_failed_status(tmp_path):
    _, credentials = save_credentials(tmp_path)
    prepare_stage(tmp_path)
    def runner(command, **kwargs):
        if "run" not in command:
            return docker_ready(command, **kwargs)
        assert credentials["token"] not in " ".join(command)
        assert kwargs["env"]["SONAR_TOKEN"] == credentials["token"]
        return subprocess.CompletedProcess(command, 1, stdout=credentials["token"], stderr=credentials["password"])
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("Failed scanner must not poll"))) as client:
        with pytest.raises(sonar.SonarError, match="Scanner exited"):
            sonar.scan(tmp_path, "jolup-roadmap", client=client, runner=runner)
    log = (tmp_path / "reports/quality/scanner-after.log").read_text()
    assert credentials["token"] not in log and credentials["password"] not in log
    assert json.loads((tmp_path / "reports/quality/scan-after.json").read_text())["status"] == "failed"


def test_source_changes_during_scan_invalidate_even_an_accepted_analysis(tmp_path):
    save_credentials(tmp_path)
    stage, _ = prepare_stage(tmp_path)
    def runner(command, **kwargs):
        if "run" not in command:
            return docker_ready(command, **kwargs)
        (stage / ".scannerwork").mkdir()
        (stage / ".scannerwork/report-task.txt").write_text("ceTaskId=task-1\n")
        (tmp_path / "app.py").write_text("changed during analysis\n")
        return subprocess.CompletedProcess(command, 0, stdout="done", stderr="")
    def handle(request):
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json={"id": "local-instance", "status": "UP"})
        if request.url.path == "/api/ce/task":
            return httpx.Response(200, json={"task": {"status": "SUCCESS", "analysisId": "analysis-1"}})
        assert request.url.params["analysisId"] == "analysis-1"
        return httpx.Response(200, json={"projectStatus": {"status": "OK"}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(sonar.SonarError, match="hash"):
            sonar.scan(tmp_path, "jolup-roadmap", client=client, runner=runner)
    assert json.loads((tmp_path / "reports/quality/scan-after.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("fail_measures", [False, True])
def test_report_distinguishes_missing_metrics_failed_gate_and_collection_failure(tmp_path, fail_measures):
    save_credentials(tmp_path)
    def handle(request):
        path = request.url.path
        if path == "/api/system/status":
            return httpx.Response(200, json={"id": "local-instance", "status": "UP"})
        if path == "/api/project_analyses/search":
            return httpx.Response(200, json={"analyses": [{"key": "analysis-1"}]})
        if path == "/api/measures/component":
            return httpx.Response(200, json={"component": {"measures": [{"metric": "complexity", "value": "17"}]}})
        if path == "/api/measures/component_tree":
            if fail_measures:
                return httpx.Response(500, text="private response")
            return httpx.Response(200, json={"components": [], "paging": {"total": 0}})
        if path == "/api/issues/search":
            return httpx.Response(200, json={"issues": [], "paging": {"total": 0}})
        assert path == "/api/qualitygates/project_status" and request.url.params["analysisId"] == "analysis-1"
        return httpx.Response(200, json={"projectStatus": {"status": "ERROR"}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        if fail_measures:
            with pytest.raises(sonar.SonarError):
                sonar.report(tmp_path, "jolup-roadmap", client=client)
        else:
            result = sonar.report(tmp_path, "jolup-roadmap", client=client)
            assert result["quality_gate"]["status"] == "ERROR"
            assert result["metrics"]["complexity"] == "17" and result["metrics"]["coverage"] is None
    saved = json.loads((tmp_path / "reports/quality/sonar-after.json").read_text())
    assert saved["status"] == ("failed" if fail_measures else "collected")


@pytest.mark.parametrize("mode", ["missing_profile", "inactive_rule", "success"])
def test_extended_profile_is_verified_before_linking_only_the_two_projects(tmp_path, mode):
    _, credentials = save_credentials(tmp_path)
    selected = {"key": "extended-profile-key", "name": "Sonar way extended", "language": "py",
                "rulesUpdatedAt": "2026-10-23T00:46:02+0000", "activeRuleCount": 266}
    linked = []
    def handle(request):
        path = request.url.path
        if path == "/api/system/status":
            return httpx.Response(200, json={"id": "local-instance", "status": "UP", "version": "26.10.0.133078"})
        if path == "/api/qualityprofiles/search":
            assert request.url.params["language"] == "py"
            project = request.url.params.get("project")
            if project:
                assert project in linked
            return httpx.Response(200, json={"profiles": [] if mode == "missing_profile" else [selected]})
        if path == "/api/rules/show":
            assert request.url.params["key"] == "python:S3776" and request.url.params["actives"] == "true"
            actives = [] if mode == "inactive_rule" else [{"qProfile": selected["key"], "params": [{"key": "threshold", "value": "15"}]}]
            return httpx.Response(200, json={"rule": {"key": "python:S3776"}, "actives": actives})
        assert path == "/api/qualityprofiles/add_project" and mode == "success"
        form = parse_qs(request.content.decode())
        assert form["language"] == ["py"] and form["qualityProfile"] == ["Sonar way extended"]
        assert form["project"][0] in {"jolup-roadmap-before", "jolup-roadmap"}
        linked.append(form["project"][0])
        return httpx.Response(204)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        if mode != "success":
            with pytest.raises(sonar.SonarError):
                sonar.profile(tmp_path, client=client)
            assert linked == []
        else:
            result = sonar.profile(tmp_path, client=client)
            assert result["status"] == "configured" and result["server_version"] == "26.10.0.133078"
            assert result["profile"] == selected
            assert result["rule"] == {"key": "python:S3776", "threshold": 15}
            assert set(linked) == set(result["projects"]) == {"jolup-roadmap-before", "jolup-roadmap"}
    stored = json.loads((tmp_path / "reports/quality/profile.json").read_text())
    assert stored["status"] == ("configured" if mode == "success" else "failed")
    assert credentials["token"] not in json.dumps(stored) and credentials["password"] not in json.dumps(stored)


def test_paths_and_json_reject_escape_missing_malformed_and_wrong_shape(tmp_path):
    with pytest.raises(sonar.SonarError, match="inside"):
        sonar._safe_path(tmp_path, "../outside")
    with pytest.raises(sonar.SonarError, match="missing or invalid"):
        sonar._json_file(tmp_path / "absent.json")
    path = tmp_path / "data.json"
    path.write_text("invalid")
    with pytest.raises(sonar.SonarError, match="missing or invalid"):
        sonar._json_file(path)
    path.write_text("[]")
    with pytest.raises(sonar.SonarError, match="object"):
        sonar._json_file(path)


def test_failed_atomic_replace_removes_temporary_without_losing_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"saved": true}')
    def fail(*args):
        raise OSError("replace blocked")
    monkeypatch.setattr(sonar.os, "replace", fail)
    with pytest.raises(OSError):
        sonar._atomic_json(path, {"new": True})
    assert json.loads(path.read_text()) == {"saved": True}
    assert list(tmp_path.glob(".sonar-*")) == []


def test_required_missing_or_malformed_credentials_are_not_reset(tmp_path):
    with pytest.raises(sonar.SonarError, match="missing"):
        sonar._credentials(tmp_path)
    path = tmp_path / ".quality/sonar.json"
    path.write_text('{"username":"somebody_else"}')
    before = path.read_bytes()
    with pytest.raises(sonar.SonarError, match="invalid"):
        sonar._credentials(tmp_path)
    assert path.read_bytes() == before


def test_owned_http_client_disables_proxies_redirects_and_closes(monkeypatch):
    original = httpx.Client
    clients = []
    def factory(**kwargs):
        assert kwargs == {"trust_env": False, "follow_redirects": False, "timeout": 10.0}
        client = original(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})), **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(sonar.httpx, "Client", factory)
    with sonar._local_client(None) as client:
        assert sonar._api(client, "GET", "/api/status") == {"ok": True}
    assert clients[0].is_closed


@pytest.mark.parametrize("response,missing_ok,expected", [
    (httpx.Response(404), True, None), (httpx.Response(204), False, {}),
    (httpx.Response(200, content=b""), False, {}),
    (httpx.Response(200, text="private invalid json"), False, "invalid JSON"),
    (httpx.Response(200, json=[]), False, "response shape"),
])
def test_http_response_contracts_never_echo_private_payloads(response, missing_ok, expected):
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
        if isinstance(expected, str):
            with pytest.raises(sonar.SonarError, match=expected) as caught:
                sonar._api(client, "GET", "/api/status", missing_ok=missing_ok)
            assert "private invalid json" not in str(caught.value)
        else:
            assert sonar._api(client, "GET", "/api/status", missing_ok=missing_ok) == expected


def test_http_timeout_and_unexpected_docker_port_are_sanitized(tmp_path):
    def timeout(request):
        raise httpx.ReadTimeout("PRIVATE_TIMEOUT")
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(sonar.SonarError, match="timeout") as caught:
            sonar._api(client, "GET", "/api/status")
    assert "PRIVATE_TIMEOUT" not in str(caught.value)
    def public_port(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="0.0.0.0:9000" if "port" in command else "container")
    with pytest.raises(sonar.SonarError, match="loopback"):
        sonar._owned_container(tmp_path, public_port)


@pytest.mark.parametrize("status", [{"status": "STARTING", "id": "x"}, {"status": "UP", "id": ""}])
def test_server_must_be_up_with_instance_identity(status):
    with pytest.raises(sonar.SonarError, match="instance ID"):
        sonar._server_identity(None, status)


@pytest.mark.parametrize("mode", ["wrong_instance", "invalid_admin", "pending_recovery", "invalid_token"])
def test_existing_admin_recovers_pending_only_after_verification(tmp_path, mode):
    path, credentials = save_credentials(tmp_path, password_status="pending")
    def handle(request):
        valid = not (mode == "invalid_admin" or mode == "invalid_token" and request.headers.get("authorization", "").startswith("Bearer"))
        return httpx.Response(200, json={"valid": valid})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        if mode == "wrong_instance":
            with pytest.raises(sonar.SonarError, match="different"):
                sonar._existing_admin(client, path, credentials, "foreign")
        elif mode == "invalid_admin":
            with pytest.raises(sonar.SonarError, match="password"):
                sonar._existing_admin(client, path, credentials, "local-instance")
        elif mode == "invalid_token":
            with pytest.raises(sonar.SonarError, match="token"):
                sonar._ensure_token(client, path, credentials)
        else:
            sonar._existing_admin(client, path, credentials, "local-instance")
            assert json.loads(path.read_text())["password_status"] == "changed"


@pytest.mark.parametrize("token", [None, "", "bad token"])
def test_invalid_generated_token_preserves_admin_credentials(tmp_path, token):
    path, credentials = save_credentials(tmp_path, token=None)
    before = path.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"token": token}))) as client:
        with pytest.raises(sonar.SonarError, match="usable token"):
            sonar._ensure_token(client, path, credentials)
    assert path.read_bytes() == before


def test_first_bootstrap_creates_only_named_private_projects(tmp_path):
    created = []
    def handle(request):
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json={"id": "local-instance", "status": "UP"})
        if request.url.path == "/api/users/change_password":
            return httpx.Response(204)
        if request.url.path == "/api/user_tokens/generate":
            return httpx.Response(200, json={"token": "squ_generated_dummy"})
        if request.url.path == "/api/components/show":
            return httpx.Response(404)
        assert request.url.path == "/api/projects/create"
        form = parse_qs(request.content.decode())
        assert form["visibility"] == ["private"]
        created.append(form["project"][0])
        return httpx.Response(204)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = sonar.bootstrap(tmp_path, client=client, runner=docker_ready)
    assert result["status"] == "ready" and set(created) == set(sonar.PROJECTS)
    assert json.loads((tmp_path / ".quality/sonar.json").read_text())["token"] == "squ_generated_dummy"


def test_unexpected_existing_project_and_unknown_requested_project_are_rejected():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"component": {"key": "foreign"}}))) as client:
        with pytest.raises(sonar.SonarError, match="Unexpected existing"):
            sonar._ensure_projects(client, "dummy")
    with pytest.raises(sonar.SonarError, match="dedicated"):
        sonar._project_label("foreign")


def write_prepared(root, stage, prepared):
    (stage / "stage.json").write_text(json.dumps(prepared))
    (root / "reports/quality/prepared.json").write_text(json.dumps(prepared))


@pytest.mark.parametrize("change,error", [
    ({"path": None}, "inside .build"), ({"source_hashes": {}}, "hashes are missing"),
    ({"source_hashes": {"app.py": "invalid-digest"}}, "manifest is invalid"),
    ({"coverage_included": True, "coverage_xml_sha256": "bad"}, "coverage hash"),
])
def test_prepared_manifest_requires_valid_paths_digests_and_coverage(tmp_path, change, error):
    stage, prepared = prepare_stage(tmp_path)
    prepared.update(change)
    write_prepared(tmp_path, stage, prepared)
    with pytest.raises(sonar.SonarError, match=error):
        sonar.validate_stage(tmp_path, "jolup-roadmap")


def test_stage_metadata_and_project_key_cannot_be_substituted(tmp_path):
    stage, prepared = prepare_stage(tmp_path)
    (stage / "stage.json").write_text("{}")
    with pytest.raises(sonar.SonarError, match="metadata"):
        sonar.validate_stage(tmp_path, "jolup-roadmap")
    properties = stage / "sonar-project.properties"
    properties.write_text("sonar.projectKey=foreign\n")
    prepared["properties_sha256"] = hashlib.sha256(properties.read_bytes()).hexdigest()
    write_prepared(tmp_path, stage, prepared)
    with pytest.raises(sonar.SonarError, match="project key"):
        sonar.validate_stage(tmp_path, "jolup-roadmap")


def test_valid_coverage_digest_is_accepted_and_corruption_rejected(tmp_path):
    stage, prepared = prepare_stage(tmp_path)
    coverage = stage / "reports/quality/coverage.xml"
    coverage.parent.mkdir(parents=True)
    coverage.write_text("<coverage />")
    prepared.update(coverage_included=True, coverage_xml_sha256=hashlib.sha256(coverage.read_bytes()).hexdigest())
    write_prepared(tmp_path, stage, prepared)
    assert sonar.validate_stage(tmp_path, "jolup-roadmap")["coverage_included"] is True
    coverage.write_text("tampered")
    with pytest.raises(sonar.SonarError, match="coverage hash"):
        sonar.validate_stage(tmp_path, "jolup-roadmap")


def test_unknown_compute_status_and_invalid_gate_do_not_count_as_completed():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"task": {"status": "UNKNOWN"}}))) as client:
        with pytest.raises(sonar.SonarError, match="unknown compute"):
            sonar.wait_for_task(client, "dummy", "task-1")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"projectStatus": {"status": "UNKNOWN"}}))) as client:
        with pytest.raises(sonar.SonarError, match="gate response"):
            sonar._gate(client, "dummy", "analysis-1")


def test_missing_scanner_task_and_reused_task_never_reuse_old_analysis(tmp_path):
    stage, _ = prepare_stage(tmp_path)
    with pytest.raises(sonar.SonarError, match="new report-task"):
        sonar._task_from_stage(tmp_path, stage)
    task = stage / ".scannerwork/report-task.txt"
    task.parent.mkdir()
    task.write_text("ignored line\nceTaskId=old-task\n")
    with pytest.raises(sonar.SonarError, match="new compute task"):
        sonar._run_scanner(tmp_path, "jolup-roadmap", stage, {"token": "dummy"}, docker_ready)


@pytest.mark.parametrize("operation", ["scan", "report", "profile"])
def test_missing_token_records_failed_status_before_any_network(tmp_path, operation):
    save_credentials(tmp_path, token=None)
    prepare_stage(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("No network with missing token"))) as client:
        with pytest.raises(sonar.SonarError, match="token"):
            if operation == "profile":
                sonar.profile(tmp_path, client=client)
            else:
                getattr(sonar, operation)(tmp_path, "jolup-roadmap", client=client)


@pytest.mark.parametrize("operation", ["scan", "report", "profile"])
def test_changed_instance_never_uses_stored_credentials_for_reports(tmp_path, monkeypatch, operation):
    save_credentials(tmp_path)
    prepare_stage(tmp_path)
    monkeypatch.setattr(sonar, "_run_scanner", lambda *args: "task-1")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "UP", "id": "foreign"}))) as client:
        with pytest.raises(sonar.SonarError, match="instance changed"):
            if operation == "profile":
                sonar.profile(tmp_path, client=client)
            elif operation == "scan":
                sonar.scan(tmp_path, "jolup-roadmap", client=client, runner=docker_ready)
            else:
                sonar.report(tmp_path, "jolup-roadmap", client=client)


@pytest.mark.parametrize("change_metadata", [False, True])
def test_successful_scan_requires_stable_prepared_metadata(tmp_path, change_metadata):
    save_credentials(tmp_path)
    stage, prepared = prepare_stage(tmp_path)
    def runner(command, **kwargs):
        if "run" not in command:
            return docker_ready(command, **kwargs)
        (stage / ".scannerwork").mkdir()
        (stage / ".scannerwork/report-task.txt").write_text("ceTaskId=task-1\n")
        return subprocess.CompletedProcess(command, 0, stdout="done", stderr="")
    def handle(request):
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json={"status": "UP", "id": "local-instance"})
        if request.url.path == "/api/ce/task":
            return httpx.Response(200, json={"task": {"status": "SUCCESS", "analysisId": "analysis-1"}})
        if change_metadata:
            prepared["note"] = "changed during scan"
            write_prepared(tmp_path, stage, prepared)
        return httpx.Response(200, json={"projectStatus": {"status": "OK"}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        if change_metadata:
            with pytest.raises(sonar.SonarError, match="stage changed"):
                sonar.scan(tmp_path, "jolup-roadmap", client=client, runner=runner)
        else:
            result = sonar.scan(tmp_path, "jolup-roadmap", client=client, runner=runner)
            assert result["status"] == "analyzed" and result["analysis_id"] == "analysis-1"


@pytest.mark.parametrize("body,error", [
    ({"issues": [], "total": "2"}, "incomplete pagination"),
    ({"issues": [], "total": 1}, "pagination changed"),
    ({"issues": [{}, {}], "total": 1}, "pagination changed"),
])
def test_report_pagination_rejects_missing_empty_or_inconsistent_pages(body, error):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
        with pytest.raises(sonar.SonarError, match=error):
            sonar._pages(client, "dummy", "/api/issues/search", {}, "issues")


def test_report_pagination_collects_multiple_pages_and_has_a_hard_bound():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"issues": [{"page": int(request.url.params["p"])}], "total": 2}))) as client:
        assert sonar._pages(client, "dummy", "/api/issues/search", {}, "issues") == [{"page": 1}, {"page": 2}]
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"issues": [{}], "total": 101}))) as client:
        with pytest.raises(sonar.SonarError, match="pagination limit"):
            sonar._pages(client, "dummy", "/api/issues/search", {}, "issues")


def test_missing_analysis_and_unrecognized_metrics_are_not_invented():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"analyses": []}))) as client:
        with pytest.raises(sonar.SonarError, match="no completed analysis"):
            sonar._latest_analysis(client, "dummy", "jolup-roadmap")
    metrics = sonar._measure_values({"measures": [{"metric": "unknown", "value": "999"}]})
    assert all(value is None for value in metrics.values())


@pytest.mark.parametrize("mode", ["missing_measures", "changed_analysis", "with_files"])
def test_report_snapshot_is_complete_and_stable(mode):
    analyses = []
    def handle(request):
        path = request.url.path
        if path == "/api/project_analyses/search":
            analyses.append(True)
            key = "analysis-2" if mode == "changed_analysis" and len(analyses) > 1 else "analysis-1"
            return httpx.Response(200, json={"analyses": [{"key": key}]})
        if path == "/api/measures/component":
            return httpx.Response(200, json={} if mode == "missing_measures" else {"component": {"measures": []}})
        if path == "/api/measures/component_tree":
            return httpx.Response(200, json={"components": [{"key": "file-1", "path": "app.py", "measures": [{"metric": "complexity", "value": "4"}]}], "total": 1})
        if path == "/api/issues/search":
            return httpx.Response(200, json={"issues": [], "total": 0})
        return httpx.Response(200, json={"projectStatus": {"status": "OK"}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        if mode == "with_files":
            result = sonar._collect_report(client, "dummy", "jolup-roadmap")
            assert result["file_metrics"][0]["metrics"]["complexity"] == "4"
        else:
            with pytest.raises(sonar.SonarError, match="measures are missing" if mode == "missing_measures" else "analysis changed"):
                sonar._collect_report(client, "dummy", "jolup-roadmap")


@pytest.mark.parametrize("body,error", [
    ({"profiles": None}, "response is invalid"),
    ({"profiles": [{"key": "valid", "name": "Sonar way extended", "language": "py", "activeRuleCount": 0, "rulesUpdatedAt": "date"}]}, "metadata is incomplete"),
])
def test_profile_metadata_must_be_complete(body, error):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
        with pytest.raises(sonar.SonarError, match=error):
            sonar._extended_profile(client, "dummy")


@pytest.mark.parametrize("body,error", [
    ({"rule": {"key": "wrong"}, "actives": []}, "activation response"),
    ({"rule": {"key": "python:S3776"}, "actives": [{"qProfile": "profile", "params": [{"key": "threshold", "value": "99"}]}]}, "threshold of 15"),
])
def test_complexity_rule_cannot_silently_use_another_rule_or_threshold(body, error):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
        with pytest.raises(sonar.SonarError, match=error):
            sonar._verified_complexity_rule(client, "dummy", "profile")


@pytest.mark.parametrize("mode", ["missing_version", "wrong_association"])
def test_profile_setup_requires_server_version_and_verified_project_association(tmp_path, mode):
    save_credentials(tmp_path)
    def handle(request):
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json={"id": "local-instance", "status": "UP", "version": "1.0" if mode != "missing_version" else None})
        if request.url.path == "/api/qualityprofiles/search":
            return httpx.Response(200, json={"profiles": [{"key": "other" if "project" in request.url.params else "selected", "name": "Sonar way extended", "language": "py", "activeRuleCount": 2, "rulesUpdatedAt": "today"}]})
        if request.url.path == "/api/rules/show":
            return httpx.Response(200, json={"rule": {"key": "python:S3776"}, "actives": [{"qProfile": "selected", "params": [{"key": "threshold", "value": "15"}]}]})
        return httpx.Response(204)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(sonar.SonarError, match="version is missing" if mode == "missing_version" else "association"):
            sonar.profile(tmp_path, client=client)


def test_redaction_handles_missing_credentials_and_scanner_environment_drops_secrets(monkeypatch):
    assert sonar._redact("plain squ_example_token", {}) == "plain [REDACTED]"
    monkeypatch.setenv("OPENAI_API_KEY", "private")
    monkeypatch.setenv("SONAR_OLD", "private")
    environment = sonar._scanner_environment(Path("stage"), "explicit-scanner-token")
    assert "OPENAI_API_KEY" not in environment and "SONAR_OLD" not in environment
    assert environment["SONAR_TOKEN"] == "explicit-scanner-token"


@pytest.mark.parametrize("command,gate,code", [("bootstrap", "OK", 0), ("profile", "OK", 0), ("scan", "OK", 0), ("report", "ERROR", 2)])
def test_cli_dispatch_and_exit_status(command, gate, code, monkeypatch, capsys):
    calls = []
    def action(*args):
        calls.append(args)
        return {"credentials_file": "private-path", "profile": {"name": "Sonar way extended"},
                "quality_gate": {"status": gate}, "status": "complete", "analysis_id": "analysis-1"}
    monkeypatch.setattr(sonar, command, action)
    arguments = [command] if command in {"bootstrap", "profile"} else [command, "--project", "jolup-roadmap"]
    assert sonar.main(arguments) == code
    assert calls and capsys.readouterr().out


@pytest.mark.parametrize("failure", [sonar.SonarError("known fixed error"), OSError("PRIVATE_OS_ENV")])
def test_cli_errors_and_entrypoint_help_do_not_leak_environment(failure, monkeypatch, capsys):
    import runpy
    import sys
    def fail(*args):
        raise failure
    monkeypatch.setattr(sonar, "bootstrap", fail)
    assert sonar.main(["bootstrap"]) == 1
    assert "PRIVATE_OS_ENV" not in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["sonar_local.py", "--help"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(Path(sonar.__file__)), run_name="__main__")
    assert stopped.value.code == 0
    assert "bootstrap" in capsys.readouterr().out
