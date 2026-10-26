"""Offline contracts for quality report validity and scanner input isolation."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import os
import stat

import pytest

from scripts import quality_check as quality


@pytest.fixture
def project(tmp_path):
    for name, content in {
        "app.py": "print('demo')\n",
        "jolup/core.py": "VALUE = 1\n",
        "scripts/run.py": "print('run')\n",
        "tests/test_core.py": "def test_core(): assert True\n",
        ".coveragerc": "[run]\nsource = .\nbranch = True\n",
        "sonar-project.properties": "sonar.projectKey=jolup-roadmap\nsonar.sources=app.py,jolup,scripts\nsonar.tests=tests\nsonar.python.version=3.12\nsonar.token=DO_NOT_COPY_THIS_TOKEN\n",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


def junit(path, contents="<testcase classname='demo' name='test_one' />"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<testsuites><testsuite>{contents}</testsuite></testsuites>")


def fake_runner(project, *, pytest_code=0, cases=None, mutate=False, xml=True):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        action = command[3]
        if action == "run":
            report = next(x.split("=", 1)[1] for x in command if x.startswith("--junitxml="))
            junit(Path(report), cases or "<testcase classname='demo' name='test_one' />")
            if mutate:
                (project / "jolup/core.py").write_text("VALUE = 2\n")
            return SimpleNamespace(returncode=pytest_code, stdout="pytest finished\n")
        if action == "xml" and xml:
            (project / "reports/quality/coverage.xml").write_text("<coverage />")
        return SimpleNamespace(returncode=0, stdout="coverage finished\n")
    return calls, run


def test_environment_removes_keys_endpoints_and_inherited_measurement_flags():
    original = {"PATH": "/local/bin", "HOME": "/local/home", "OPENAI_API_KEY": "private",
                "ANTHROPIC_API_KEY": "private", "LANGSMITH_API_KEY": "private",
                "SONAR_TOKEN": "private", "CUSTOM_SECRET": "private", "MCP_URL": "http://remote",
                "CHROMA_HOST": "remote", "LANGSMITH_TRACING": "true", "JOLUP_TEST_VECTOR": "1",
                "PYTEST_ADDOPTS": "--deselect=important", "COVERAGE_FILE": "/old/result",
                "PYTHONPATH": "/injected", "HTTPS_PROXY": "http://private-proxy",
                "JOLUP_AGENT_API_URL": "http://127.0.0.1:8001"}
    clean = quality.safe_environment(original)
    assert clean["PATH"] == original["PATH"] and clean["HOME"] == original["HOME"]
    assert "private" not in json.dumps(clean)
    assert not set(clean) & {"MCP_URL", "CHROMA_HOST", "JOLUP_TEST_VECTOR", "PYTEST_ADDOPTS", "COVERAGE_FILE", "PYTHONPATH", "HTTPS_PROXY"}
    assert clean["LANGSMITH_TRACING"] == "false"
    assert clean["HF_HUB_OFFLINE"] == clean["TRANSFORMERS_OFFLINE"] == "1"
    assert quality.safe_environment(original, vector=True)["JOLUP_TEST_VECTOR"] == "1"
    assert "JOLUP_AGENT_API_URL" not in clean
    assert original["OPENAI_API_KEY"] == "private"


def test_preparation_only_copies_allowed_sources_and_safe_config(project):
    for name in [".env", "data/student.csv", "output/share.zip", "reports/private.txt", "credentials.py", "jolup/credentials.txt", "learning/tdd/red.py"]:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE_INPUT")
    result = quality.prepare_analysis(project, project="jolup-roadmap-before", without_coverage=True)
    stage = Path(result["path"])
    assert set(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()) == {
        "app.py", "jolup/core.py", "scripts/run.py", "tests/test_core.py", "sonar-project.properties", "stage.json"}
    config = (stage / "sonar-project.properties").read_text()
    assert "sonar.projectKey=jolup-roadmap-before" in config
    assert "sonar.token" not in config and "DO_NOT_COPY" not in config
    assert "sonar.python.coverage.reportPaths" not in config
    assert result["properties_sha256"] == hashlib.sha256((stage / "sonar-project.properties").read_bytes()).hexdigest()
    assert result["coverage_included"] is False
    assert json.loads((project / "reports/quality/prepared.json").read_text())["path"] == str(stage)


def test_test_discovery_config_is_preserved_and_invalidates_changed_measurement(project):
    config = project / "pytest.ini"
    config.write_text("[pytest]\ntestpaths = tests\n")
    _, runner = fake_runner(project)
    measured = quality.run_coverage(project, runner=runner)
    assert measured["status"] == "passed"
    assert measured["input_hashes"]["pytest.ini"] == hashlib.sha256(config.read_bytes()).hexdigest()
    stage = Path(quality.prepare_analysis(project)["path"])
    assert (stage / "pytest.ini").read_bytes() == config.read_bytes()
    config.write_text("[pytest]\ntestpaths = tests team_contributions\n")
    with pytest.raises(ValueError, match="stale coverage"):
        quality.prepare_analysis(project)


@pytest.mark.parametrize("name", ["app.py", "jolup"])
def test_symlink_in_analysis_input_is_rejected(project, tmp_path, name):
    path = project / name
    destination = tmp_path / "external"
    if path.is_dir():
        destination.mkdir()
        (destination / "core.py").write_text("SECRET = 1\n")
        (path / "core.py").unlink()
        path.rmdir()
        path.symlink_to(destination, target_is_directory=True)
    else:
        destination.write_text("SECRET = 1\n")
        path.unlink()
        path.symlink_to(destination)
    with pytest.raises(ValueError, match="symlink"):
        quality.prepare_analysis(project, without_coverage=True)


@pytest.mark.parametrize("body, code, status", [
    ("<testcase name='ok' />", 0, "passed"),
    ("<testcase name='skip'><skipped /></testcase>", 0, "incomplete"),
    ("<testcase name='fail'><failure /></testcase>", 1, "failed"),
    ("<testcase name='error'><error /></testcase>", 0, "error"),
    ("<testcase name='ok' />", 2, "error"),
    ("", 0, "incomplete"),
])
def test_test_summary_never_treats_skips_empty_or_failures_as_success(tmp_path, body, code, status):
    path = tmp_path / "junit.xml"
    junit(path, body)
    result = quality.summarize_tests(path, code)
    assert result["status"] == status and result["exit_code"] == code


def test_missing_junit_is_an_error_even_when_process_exits_zero(tmp_path):
    assert quality.summarize_tests(tmp_path / "missing.xml", 0)["status"] == "error"


def test_failed_test_exit_survives_successful_coverage_report_commands(project):
    calls, runner = fake_runner(project, pytest_code=1, cases="<testcase name='x'><failure /></testcase>")
    result = quality.run_coverage(project, runner=runner)
    assert result["exit_code"] == 1 and result["status"] == "failed"
    assert result["tests"]["failed"] == 1 and result["tests"]["passed"] == 0
    assert [call[0][3] for call in calls] == ["erase", "run", "combine", "xml", "html", "json"]
    with pytest.raises(ValueError, match="verified coverage"):
        quality.prepare_analysis(project)


def test_skipped_test_does_not_produce_a_verified_coverage_manifest(project):
    _, runner = fake_runner(project, cases="<testcase name='x'><skipped /></testcase>")
    result = quality.run_coverage(project, runner=runner)
    assert result["exit_code"] != 0 and result["status"] == "incomplete"


def test_source_edit_during_measurement_marks_result_stale(project):
    _, runner = fake_runner(project, mutate=True)
    result = quality.run_coverage(project, runner=runner)
    assert result["status"] == "stale" and result["exit_code"] != 0
    assert result["source_unchanged"] is False
    assert "jolup/core.py" in result["changed_inputs"]
    with pytest.raises(ValueError, match="verified coverage"):
        quality.prepare_analysis(project)


def test_missing_coverage_xml_does_not_reuse_previous_output(project):
    report = project / "reports/quality/coverage.xml"
    report.parent.mkdir(parents=True)
    report.write_text("<old-coverage />")
    _, runner = fake_runner(project, xml=False)
    result = quality.run_coverage(project, runner=runner)
    assert result["status"] == "error" and result["exit_code"] != 0


def test_verified_coverage_is_staged_and_changes_after_run_are_rejected(project):
    _, runner = fake_runner(project)
    measured = quality.run_coverage(project, runner=runner)
    assert measured["status"] == "passed" and measured["tests"]["passed"] == 1
    prepared = quality.prepare_analysis(project)
    stage = Path(prepared["path"])
    assert prepared["coverage_included"] is True
    assert (stage / "reports/quality/coverage.xml").read_text() == "<coverage />"
    assert quality.source_hashes(stage) == measured["source_hashes"]
    (project / "app.py").write_text("print('changed')\n")
    with pytest.raises(ValueError, match="stale coverage"):
        quality.prepare_analysis(project)


def test_modified_coverage_xml_is_rejected(project):
    _, runner = fake_runner(project)
    quality.run_coverage(project, runner=runner)
    (project / "reports/quality/coverage.xml").write_text("<tampered />")
    with pytest.raises(ValueError, match="coverage XML"):
        quality.prepare_analysis(project)


def test_baseline_source_is_explicit_and_keeps_current_project_untouched(project):
    baseline = project / ".build/baseline"
    for name in ["app.py", "jolup/core.py", "scripts/run.py", "tests/test_core.py"]:
        path = baseline / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# before refactor\n")
    current = (project / "app.py").read_text()
    result = quality.prepare_analysis(project, source=baseline, project="jolup-roadmap-before", without_coverage=True)
    assert (Path(result["path"]) / "app.py").read_text() == "# before refactor\n"
    assert (project / "app.py").read_text() == current


def test_stage_permissions_allow_scanner_without_changing_original_files(project):
    (project / "app.py").chmod(0o600)
    (project / "jolup").chmod(0o700)
    previous = os.umask(0o077)
    try:
        result = quality.prepare_analysis(project, without_coverage=True)
    finally:
        os.umask(previous)
    stage = Path(result["path"])
    assert stat.S_IMODE(stage.stat().st_mode) == 0o777
    for path in stage.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o755 if path.is_dir() else 0o644)
    assert stat.S_IMODE((project / "app.py").stat().st_mode) == 0o600
    assert stat.S_IMODE((project / "jolup").stat().st_mode) == 0o700


def test_project_root_rejects_missing_directory_and_symlink_parent(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        quality._project_root(tmp_path / "missing")
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "child").mkdir()
    link = tmp_path / "linked"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        quality._project_root(link / "child")


@pytest.mark.parametrize("relative", ["../outside", "/outside"])
def test_checked_paths_reject_escape_even_for_nonexistent_outputs(project, relative):
    with pytest.raises(ValueError, match="project-relative"):
        quality._checked_path(project, relative)


def test_missing_required_source_and_directory_are_reported(project):
    (project / "app.py").unlink()
    with pytest.raises(ValueError, match="required file"):
        quality.source_hashes(project)
    (project / "app.py").write_text("# restored")
    (project / "jolup/core.py").unlink()
    (project / "jolup").rmdir()
    with pytest.raises(ValueError, match="source directory"):
        quality.source_hashes(project)


@pytest.mark.parametrize("failure,code", [("timeout", 124), ("os", 127)])
def test_coverage_start_failure_stops_and_records_sanitized_incomplete_status(project, failure, code):
    import subprocess
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 600, output="DO_NOT_ECHO_PRIVATE_ENV")
        raise OSError("DO_NOT_ECHO_PRIVATE_ENV")
    result = quality.run_coverage(project, runner=runner)
    assert result["status"] == "error" and result["exit_code"] == code
    assert len(calls) == 1 and result["tests"]["status"] == "error"
    assert "DO_NOT_ECHO_PRIVATE_ENV" not in (project / "reports/quality/erase.log").read_text()


def test_properties_skip_comments_and_reject_invalid_project(project):
    with (project / "sonar-project.properties").open("a") as stream:
        stream.write("\n# comment=value\n! also=value\nnot_a_property\n")
    values = quality._scanner_properties(project, None, False)
    assert values["sonar.projectKey"] == "jolup-roadmap"
    with pytest.raises(ValueError, match="project key"):
        quality._scanner_properties(project, "bad\nsonar.token=bad", False)


def test_copy_rejects_source_changed_before_read_and_new_files_during_copy(project, monkeypatch):
    hashes = quality.source_hashes(project)
    (project / "app.py").write_text("changed before read")
    with pytest.raises(ValueError, match="source changed"):
        quality._copy_analysis_sources(project, project / ".build/stage-one", hashes)
    hashes = quality.source_hashes(project)
    stage = project / ".build/stage-two"
    original = Path.write_bytes
    def add_source_file(path, content):
        result = original(path, content)
        if path == stage / "app.py":
            (project / "jolup/added.py").write_text("ADDED = True")
        return result
    monkeypatch.setattr(Path, "write_bytes", add_source_file)
    with pytest.raises(ValueError, match="source changed"):
        quality._copy_analysis_sources(project, stage, hashes)


def test_disposable_stage_permissions_do_not_follow_a_symlink(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    original = tmp_path / "private.txt"
    original.write_text("private")
    original.chmod(0o600)
    (stage / "linked.txt").symlink_to(original)
    with pytest.raises(ValueError, match="symlink"):
        quality._set_scanner_permissions(stage)
    assert stat.S_IMODE(original.stat().st_mode) == 0o600


def test_copying_coverage_rechecks_the_verified_snapshot(project, monkeypatch):
    _, runner = fake_runner(project)
    quality.run_coverage(project, runner=runner)
    original = Path.write_bytes
    def corrupt_stage_copy(path, content):
        if path.name == "coverage.xml" and ".build" in path.parts:
            content = b"<wrong-coverage />"
        return original(path, content)
    monkeypatch.setattr(Path, "write_bytes", corrupt_stage_copy)
    with pytest.raises(ValueError, match="coverage XML changed while"):
        quality.prepare_analysis(project)


def test_cli_dispatches_explicit_coverage_and_prepare_without_real_processes(project, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(quality, "ROOT", project)
    def measured(root, vector):
        calls.append((root, vector))
        return {"status": "passed", "exit_code": 0, "tests": {"total": 1}, "changed_inputs": []}
    def prepared(root, source, project_key, without_coverage):
        calls.append((root, source, project_key, without_coverage))
        return {"status": "prepared"}
    monkeypatch.setattr(quality, "run_coverage", measured)
    monkeypatch.setattr(quality, "prepare_analysis", prepared)
    assert quality.main(["coverage", "--vector"]) == 0
    assert quality.main(["prepare", "--source", "snapshot", "--project", "jolup-roadmap-before", "--without-coverage"]) == 0
    assert calls == [(project, True), (project, Path("snapshot"), "jolup-roadmap-before", True)]
    assert "prepared" in capsys.readouterr().out


def test_cli_reports_known_failure_and_entrypoint_help_without_running_checks(monkeypatch, capsys):
    import runpy
    import sys
    def fail(*args, **kwargs):
        raise ValueError("source changed")
    monkeypatch.setattr(quality, "run_coverage", fail)
    assert quality.main(["coverage"]) == 2
    assert "source changed" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["quality_check.py", "--help"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(Path(quality.__file__)), run_name="__main__")
    assert stopped.value.code == 0
    assert "coverage" in capsys.readouterr().out
