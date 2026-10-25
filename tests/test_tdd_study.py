"""Runner contracts written before its implementation; no external calls."""
from pathlib import Path
import pytest

from scripts.tdd_study import child_environment, read_junit, replay_contract_matches, select_tests, validate_requirements


def test_traceability_rejects_missing_test_and_path_escape(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/check.py").write_text("def test_real():\n    assert True\n")
    rows = [{"id": "REQ-EXAMPLE", "title": "예시", "tests": ["tests/check.py::test_real"], "implementation": []}]
    assert validate_requirements(rows, tmp_path) == rows
    rows[0]["tests"] = ["tests/check.py::test_typo"]
    with pytest.raises(ValueError, match="test_typo"):
        validate_requirements(rows, tmp_path)
    rows[0]["tests"] = ["../external.py::test_real"]
    with pytest.raises(ValueError):
        validate_requirements(rows, tmp_path)


def test_selecting_requirements_deduplicates_tests_and_rejects_unknown_id():
    rows = [{"id": "REQ-A", "tests": ["tests/a.py::test_one"]},
            {"id": "REQ-B", "tests": ["tests/a.py::test_one", "tests/b.py::test_two"]},
            {"id": "REQ-MANUAL", "tests": []}]
    assert select_tests(rows) == ["tests/a.py::test_one", "tests/b.py::test_two"]
    assert select_tests(rows, ["REQ-B"]) == ["tests/a.py::test_one", "tests/b.py::test_two"]
    assert select_tests(rows, ["REQ-MANUAL"]) == []
    with pytest.raises(ValueError, match="REQ-NO"):
        select_tests(rows, ["REQ-NO"])


def junit(tmp_path, inner):
    target = tmp_path / "result.xml"
    target.write_text(f"<testsuites><testsuite>{inner}</testsuite></testsuites>")
    return target


def test_red_accepts_only_intended_unimplemented_contract_failure(tmp_path):
    result = read_junit(junit(tmp_path, '<testcase name="card"><failure message="NotImplementedError: TDD_REPLAY_RED"/></testcase>'), 1, "red")
    assert result["status"] == "expected_red"
    assert result["failed"] == 1 and result["passed"] == 0
    for unexpected in ['<failure message="AssertionError: unrelated"/>', '<error message="ImportError"/>', '<skipped/>', '']:
        result = read_junit(junit(tmp_path, f'<testcase name="card">{unexpected}</testcase>'), 1, "red")
        assert result["status"] != "expected_red"


def test_empty_skipped_and_collection_errors_never_report_green(tmp_path):
    assert read_junit(junit(tmp_path, ''), 5)["status"] != "passed"
    assert read_junit(junit(tmp_path, '<testcase name="x"><skipped/></testcase>'), 0)["status"] != "passed"
    assert read_junit(junit(tmp_path, '<testcase name="x"><error message="collection"/></testcase>'), 2)["status"] != "passed"
    assert read_junit(tmp_path / "missing.xml", 2)["status"] == "error"
    assert read_junit(junit(tmp_path, '<testcase name="real"/>'), 0)["status"] == "passed"


def test_one_failing_contract_cannot_be_hidden_by_other_passes(tmp_path):
    result = read_junit(junit(tmp_path, '<testcase name="a"/><testcase name="b"><failure message="wrong number"/></testcase>'), 1)
    assert result["status"] == "failed"
    assert result["passed"] == 1 and result["failed"] == 1


def test_replay_requires_the_declared_count_and_identical_case_identities(tmp_path):
    green = read_junit(junit(tmp_path, '<testcase classname="contracts" name="a"/><testcase classname="contracts" name="b"/>'), 0)
    green["phase"] = "green"
    refactor = dict(green, phase="refactor")
    red = dict(green, phase="red", status="expected_red")
    assert replay_contract_matches([red, green, refactor], 2)
    assert replay_contract_matches([green], 2)
    assert not replay_contract_matches([green], 3)
    assert not replay_contract_matches([], 2)
    assert not replay_contract_matches([green], 0)
    assert not replay_contract_matches([green, dict(refactor, test_ids=["contracts::a", "contracts::different"])], 2)
    assert not replay_contract_matches([dict(green, test_ids=["contracts::a", "contracts::a"])], 2)
    assert not replay_contract_matches([dict(green, status="incomplete")], 2)


def test_child_process_has_no_provider_keys_or_inherited_live_services():
    original = {"PATH": "/bin", "OPENAI_API_KEY": "private", "JOLUP_API_KEY": "private",
                "ANTHROPIC_API_KEY": "private", "LANGSMITH_API_KEY": "private", "LANGCHAIN_API_KEY": "private",
                "MCP_URL": "https://private.invalid", "JOLUP_AGENT_API_URL": "http://private.invalid", "LANGSMITH_TRACING": "true",
                "PYTEST_ADDOPTS": "--unexpected", "JOLUP_TEST_VECTOR": "1"}
    clean = child_environment(original)
    assert "private" not in str(clean)
    assert clean["PATH"] == "/bin" and clean["LANGSMITH_TRACING"] == "false"
    assert "MCP_URL" not in clean and "PYTEST_ADDOPTS" not in clean
    assert "JOLUP_AGENT_API_URL" not in clean
    assert original["JOLUP_AGENT_API_URL"] == "http://private.invalid"
    assert "JOLUP_TEST_VECTOR" not in clean
    assert child_environment(original, vector=True)["JOLUP_TEST_VECTOR"] == "1"
    assert original["OPENAI_API_KEY"] == "private"


def test_vector_study_uses_local_chroma_even_when_remote_is_configured():
    original = {"CHROMA_HOST": "private.invalid", "CHROMA_PORT": "8443"}
    assert child_environment(original, vector=True) == {
        "LANGSMITH_TRACING": "false", "JOLUP_TEST_VECTOR": "1"}
    assert original["CHROMA_HOST"] == "private.invalid"
