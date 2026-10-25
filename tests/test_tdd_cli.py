"""CLI contracts: honest Red/Green results and reproducible local reports."""
import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import tdd_study as study


@pytest.mark.parametrize("items", [[], {}, [{"id": "bad"}], [{"id": "REQ-A", "tests": None}]])
def test_invalid_requirement_manifests_are_rejected(items):
    with pytest.raises(ValueError):
        study.validate_requirements(items)


def test_traceability_rejects_missing_files_nonpython_and_malformed_selectors(tmp_path):
    (tmp_path / "test.txt").write_text("def test_ok(): pass")
    (tmp_path / "test.py").write_text("def test_ok(): pass")
    for selector in ["test.py", "test.py::not_test", "absent.py::test_ok", "test.txt::test_ok"]:
        with pytest.raises(ValueError):
            study.validate_requirements([{"id": "REQ-A", "tests": [selector]}], tmp_path)
    item = {"id": "REQ-A", "tests": ["test.py::test_ok"], "implementation": ["test.py:1"]}
    assert study.validate_requirements([item], tmp_path) == [item]
    with pytest.raises(ValueError):
        study.validate_requirements([item, item], tmp_path)


@pytest.fixture
def cli(monkeypatch, tmp_path):
    selector = "tests/test_tdd_study.py::test_selecting_requirements_deduplicates_tests_and_rejects_unknown_id"
    rows = [{"id": "REQ-A", "title": "계약", "tests": [selector], "basis": "기대 결과"},
            {"id": "REQ-MANUAL", "title": "수동", "tests": []}]
    manifest = tmp_path / "requirements.json"
    manifest.write_text(json.dumps(rows))
    monkeypatch.setattr(study, "ROOT", tmp_path)
    monkeypatch.setattr(study, "MANIFEST", manifest)
    replay = tmp_path / "learning/tdd"
    replay.mkdir(parents=True)
    (replay / "checks.py").write_text("def test_contract(): pass")
    spec = {"test_file": "learning/tdd/checks.py", "stage_env": "JOLUP_TDD_STAGE", "red_marker": "TDD_REPLAY_RED", "case_count": 1}
    (replay / "replay_spec.json").write_text(json.dumps(spec))
    calls = []
    def run(selectors, folder, phase="run", vector=False):
        calls.append((selectors, phase, vector))
        return {"status": "expected_red" if phase == "red" else "passed", "phase": phase,
                "total": 1, "test_ids": ["contract::test_one"]}
    monkeypatch.setattr(study, "_run_tests", run)
    return tmp_path, calls, spec


def test_cli_list_show_and_invalid_selection(cli, capsys):
    root, calls, _ = cli
    assert study.main(["list"]) == 0
    assert "수동 검증 연결" in capsys.readouterr().out
    assert study.main(["show", "REQ-A"]) == 0
    out = capsys.readouterr().out
    assert "기대 결과" in out and "먼저 읽고 실행할 테스트" in out
    assert study.main(["show", "REQ-ABSENT"]) == 2
    assert "알 수 없는 요구사항" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("args", [["run"], ["run", "REQ-A", "--all"]])
def test_cli_requires_exactly_one_selection_mode(cli, args):
    with pytest.raises(SystemExit) as error:
        study.main(args)
    assert error.value.code == 2


def test_manual_only_does_not_run_tests_or_claim_pass(cli, capsys):
    root, calls, _ = cli
    assert study.main(["run", "REQ-MANUAL"]) == 2
    assert calls == [] and not (root / "reports").exists()
    assert "수동 확인" in capsys.readouterr().out


@pytest.mark.parametrize("args", [["run", "--all", "--vector"], ["run", "REQ-A"]])
def test_cli_records_selected_scope_and_manual_requirements(cli, args):
    root, calls, _ = cli
    assert study.main(args) == 0
    summary = json.loads(next(root.glob("reports/tdd_study/*/summary.json")).read_text())
    assert summary["kind"] == "requirements_test_run" and summary["status"] == "passed"
    assert summary["manual_requirements"] == (["REQ-MANUAL"] if "--all" in args else [])
    assert calls[0][2] == ("--vector" in args)


@pytest.mark.parametrize("stage", ["all", "green"])
def test_replay_reports_identical_contracts_and_single_stage_limits(cli, stage):
    root, calls, _ = cli
    assert study.main(["replay", "--stage", stage]) == 0
    summary = json.loads(next(root.glob("reports/tdd_study/*/summary.json")).read_text())
    assert summary["historical_tdd_claim"] is False
    assert summary["same_contract_verified"] == (True if stage == "all" else None)
    assert len(calls) == (3 if stage == "all" else 1)


def test_replay_mismatch_and_invalid_spec_do_not_claim_success(cli, monkeypatch):
    root, _, spec = cli
    monkeypatch.setattr(study, "_run_tests", lambda *a, **k: {"status": "error"})
    assert study.main(["replay"]) == 1
    spec["stage_env"] = "WRONG"
    (root / "learning/tdd/replay_spec.json").write_text(json.dumps(spec))
    assert study.main(["replay"]) == 2


@pytest.mark.parametrize("phase,count", [("red", 1), ("run", 4)])
def test_runner_preserves_output_and_sanitizes_child_environment(tmp_path, monkeypatch, phase, count):
    monkeypatch.setattr(study, "ROOT", tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "private-dummy")
    def run(command, **kwargs):
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert kwargs["cwd"] == tmp_path and kwargs["timeout"] == 180
        assert kwargs["env"].get("JOLUP_TDD_STAGE") == ("red" if phase == "red" else None)
        failure = '<failure message="NotImplementedError: TDD_REPLAY_RED"/>' if phase == "red" else ""
        (tmp_path / f"{phase}.xml").write_text(f'<testsuite><testcase name="case">{failure}</testcase></testsuite>')
        return SimpleNamespace(stdout="pytest transcript", returncode=1 if phase == "red" else 0)
    monkeypatch.setattr(subprocess, "run", run)
    result = study._run_tests(["tests/test_one.py"] * count, tmp_path, phase)
    assert result["status"] == ("expected_red" if phase == "red" else "passed")
    assert (tmp_path / f"{phase}.txt").read_text() == "pytest transcript"


def test_runner_timeout_is_recorded_as_unverified(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "ROOT", tmp_path)
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("pytest", 180)
    monkeypatch.setattr(subprocess, "run", timeout)
    result = study._run_tests(["test.py"], tmp_path)
    assert result["reason"] == "timeout" and result["status"] == "error"
    assert "완료된 검증으로 처리하지 않습니다" in (tmp_path / "run.txt").read_text()


def test_tdd_script_entrypoint_lists_real_manifest(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["tdd_study.py", "list"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(Path(study.__file__)), run_name="__main__")
    assert error.value.code == 0 and "REQ-" in capsys.readouterr().out
