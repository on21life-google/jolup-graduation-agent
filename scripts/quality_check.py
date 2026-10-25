"""Local coverage and allowlisted SonarQube preparation, without server access."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("jolup", "scripts", "tests")
QUALITY = Path("reports/quality")
PROPERTY_KEYS = frozenset({
    "sonar.projectKey", "sonar.projectName", "sonar.sourceEncoding", "sonar.python.version",
    "sonar.sources", "sonar.tests", "sonar.inclusions", "sonar.test.inclusions",
    "sonar.exclusions", "sonar.coverage.exclusions", "sonar.cpd.exclusions",
    "sonar.python.coverage.reportPaths", "sonar.scm.disabled",
})


def _timestamp():
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M%S_%f")


def _blocked_environment_key(key):
    name = key.upper()
    prefixes = ("OPENAI_", "ANTHROPIC_", "LANGSMITH_", "LANGCHAIN_", "CHROMA_", "MCP_", "PYTEST_", "COVERAGE_")
    exact = {"JOLUP_TEST_VECTOR", "JOLUP_TDD_STAGE", "JOLUP_AGENT_API_URL", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT",
             "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
    return name.startswith(prefixes) or name in exact or any(word in name for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))


def safe_environment(source, vector=False):
    """No provider credentials, remote domain endpoints or inherited test filters."""
    clean = {key: value for key, value in source.items() if not _blocked_environment_key(key)}
    clean.update(LANGSMITH_TRACING="false", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if vector:
        clean["JOLUP_TEST_VECTOR"] = "1"
    return clean


def _project_root(root):
    path = Path(root).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("symlink project paths are not allowed")
    if not path.is_dir():
        raise ValueError("project directory is missing")
    return path.resolve()


def _checked_path(root, relative, *, file=False):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("only project-relative paths are allowed")
    path = root
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise ValueError(f"symlink input is not allowed: {relative.as_posix()}")
    if file and not path.is_file():
        raise ValueError(f"required file is missing: {relative.as_posix()}")
    return path


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(root):
    """Only app.py and direct Python children of the three named directories."""
    root = _project_root(root)
    names = ["app.py"]
    for directory in SOURCE_DIRS:
        folder = _checked_path(root, directory)
        if not folder.is_dir():
            raise ValueError(f"source directory is missing: {directory}")
        names.extend(path.relative_to(root).as_posix() for path in folder.glob("*.py"))
    return {name: _sha256(_checked_path(root, name, file=True)) for name in sorted(names)}


def _measurement_inputs(root):
    return {**source_hashes(root), ".coveragerc": _sha256(_checked_path(root, ".coveragerc", file=True))}


def summarize_tests(path, exit_code):
    result = dict(status="error", exit_code=exit_code, total=0, passed=0, failed=0, errors=0, skipped=0)
    try:
        cases = list(ET.parse(path).getroot().iter("testcase"))
    except (OSError, ET.ParseError):
        return result
    result["total"] = len(cases)
    for case in cases:
        outcome = next((key for tag, key in (("error", "errors"), ("failure", "failed"), ("skipped", "skipped"))
                        if case.find(tag) is not None), "passed")
        result[outcome] += 1
    if result["errors"] or exit_code not in (0, 1):
        return result
    if result["failed"] or exit_code == 1:
        result["status"] = "failed"
    elif not cases or result["skipped"]:
        result["status"] = "incomplete"
    else:
        result["status"] = "passed"
    return result


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _report_folder(root):
    folder = _checked_path(root, QUALITY)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _coverage_command(action, root):
    command = [sys.executable, "-m", "coverage", action]
    if action == "run":
        command.extend(["-m", "pytest", "-q", "tests", f"--junitxml={root / QUALITY / 'junit.xml'}"])
    return command


def _execute_coverage(action, root, environment, runner):
    command = _coverage_command(action, root)
    try:
        completed = runner(command, cwd=root, env=environment, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        code, output = completed.returncode, completed.stdout
    except subprocess.TimeoutExpired:
        code, output = 124, "Quality command exceeded 600 seconds; verification is incomplete.\n"
    except OSError:
        code, output = 127, "Quality command could not be started.\n"
    log = _checked_path(root, QUALITY / f"{action}.log")
    log.write_text(output or "", encoding="utf-8")
    return {"action": action, "exit_code": code, "log": log.relative_to(root).as_posix()}


def _coverage_status(commands, tests, changed, xml_exists):
    failed_code = next((item["exit_code"] for item in commands if item["exit_code"]), 0)
    if changed:
        return "stale", failed_code or 1
    if failed_code:
        return ("failed" if tests["status"] == "failed" else "error"), failed_code
    if tests["status"] != "passed":
        return tests["status"], 1
    return ("passed", 0) if xml_exists else ("error", 1)


def run_coverage(root, vector=False, runner=None):
    """Measure once and bind reports to unchanged source/tests/config hashes."""
    root = _project_root(root)
    folder = _report_folder(root)
    before = _measurement_inputs(root)
    for name in ("junit.xml", "coverage.xml", "coverage.json", "manifest.json"):
        _checked_path(root, QUALITY / name).unlink(missing_ok=True)
    environment = safe_environment(os.environ, vector)
    commands = []
    for action in ("erase", "run", "combine", "xml", "html", "json"):
        result = _execute_coverage(action, root, environment, runner or subprocess.run)
        commands.append(result)
        if result["exit_code"] and action != "run":
            break
    pytest_code = next((item["exit_code"] for item in commands if item["action"] == "run"), 127)
    tests = summarize_tests(folder / "junit.xml", pytest_code)
    after = _measurement_inputs(root)
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    xml = _checked_path(root, QUALITY / "coverage.xml")
    status, code = _coverage_status(commands, tests, changed, xml.is_file())
    manifest = dict(schema_version=1, measured_at=_timestamp(), status=status, exit_code=code,
                    source_root=str(root), source_hashes={k: v for k, v in before.items() if k != ".coveragerc"},
                    input_hashes=before, source_unchanged=not changed, changed_inputs=changed,
                    tests=tests, commands=commands, vector=vector,
                    coverage_xml_sha256=_sha256(xml) if xml.is_file() else None)
    _write_json(folder / "manifest.json", manifest)
    return manifest


def _verified_coverage(source, hashes):
    manifest_path = _checked_path(source, QUALITY / "manifest.json", file=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("status") != "passed" or manifest.get("exit_code") != 0
            or manifest.get("source_unchanged") is not True or manifest.get("tests", {}).get("status") != "passed"):
        raise ValueError("verified coverage is required; rerun the complete quality check")
    if manifest.get("source_hashes") != hashes or manifest.get("input_hashes") != _measurement_inputs(source):
        raise ValueError("stale coverage: source, tests or measurement configuration changed")
    xml = _checked_path(source, QUALITY / "coverage.xml", file=True)
    if manifest.get("coverage_xml_sha256") != _sha256(xml):
        raise ValueError("coverage XML changed after measurement")
    return xml


def _scanner_properties(root, project, include_coverage):
    path = _checked_path(root, "sonar-project.properties", file=True)
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith(("#", "!")):
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key in PROPERTY_KEYS:
            values[key] = value
    key = project or values.get("sonar.projectKey", "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", key):
        raise ValueError("invalid SonarQube project key")
    values.update({"sonar.projectKey": key, "sonar.sources": "app.py,jolup,scripts", "sonar.tests": "tests"})
    values.pop("sonar.python.coverage.reportPaths", None)
    if include_coverage:
        values["sonar.python.coverage.reportPaths"] = "reports/quality/coverage.xml"
    return values


def _copy_analysis_sources(source, stage, hashes):
    for name, expected in hashes.items():
        path = _checked_path(source, name, file=True)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("source changed while preparing analysis")
        destination = stage / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    if source_hashes(source) != hashes or source_hashes(stage) != hashes:
        raise ValueError("source changed while preparing analysis")


def _set_scanner_permissions(stage):
    """Only this disposable, credential-free copy is writable by the scanner UID."""
    for path in stage.rglob("*"):
        if path.is_symlink():
            raise ValueError("symlink found in prepared analysis")
        path.chmod(0o755 if path.is_dir() else 0o644)
    # Set the root last for .scannerwork creation; recheck hashes after scanning.
    stage.chmod(0o777)


def prepare_analysis(root, source=None, project=None, without_coverage=False):
    """Copy an explicit code allowlist; never copy credentials or runtime data."""
    root = _project_root(root)
    source = _project_root(source or root)
    hashes = source_hashes(source)
    xml = None if without_coverage else _verified_coverage(source, hashes)
    properties = _scanner_properties(root, project, xml is not None)
    stage = _checked_path(root, f".build/sonar-analysis-{_timestamp()}")
    stage.mkdir(parents=True)
    _copy_analysis_sources(source, stage, hashes)
    if xml is not None:
        destination = stage / QUALITY / "coverage.xml"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(xml.read_bytes())
        if _sha256(destination) != _sha256(_verified_coverage(source, hashes)):
            raise ValueError("coverage XML changed while preparing analysis")
    config = "\n".join(f"{key}={value}" for key, value in sorted(properties.items())) + "\n"
    (stage / "sonar-project.properties").write_text(config, encoding="utf-8")
    result = dict(schema_version=1, prepared_at=_timestamp(), path=str(stage), source_root=str(source),
                  project=properties["sonar.projectKey"], source_hashes=hashes,
                  properties_sha256=_sha256(stage / "sonar-project.properties"),
                  coverage_included=xml is not None,
                  coverage_xml_sha256=_sha256(xml) if xml is not None else None)
    _write_json(stage / "stage.json", result)
    _set_scanner_permissions(stage)
    _write_json(_report_folder(root) / "prepared.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    coverage = commands.add_parser("coverage", help="Run local tests and produce line/branch reports")
    coverage.add_argument("--vector", action="store_true", help="Use the cached embedding model offline")
    prepare = commands.add_parser("prepare", help="Create an isolated allowlisted scanner input folder")
    prepare.add_argument("--source", type=Path, help="Explicit source snapshot, such as .build/sonar-baseline")
    prepare.add_argument("--project", help="SonarQube project key override")
    prepare.add_argument("--without-coverage", action="store_true", help="Explicitly prepare without a measured coverage report")
    args = parser.parse_args(argv)
    try:
        if args.command == "coverage":
            result = run_coverage(ROOT, vector=args.vector)
            print(json.dumps({key: result[key] for key in ("status", "exit_code", "tests", "changed_inputs")}, ensure_ascii=False, indent=2))
            return result["exit_code"]
        result = prepare_analysis(ROOT, args.source, args.project, args.without_coverage)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(f"Quality check stopped: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
