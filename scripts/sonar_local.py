"""Local SonarQube administration and reports; never print passwords or tokens.

Only the dedicated compose stack and fixed loopback API are supported. Tests
inject HTTP/Docker doubles; importing this module never starts a service.
"""
from __future__ import annotations

import argparse
from base64 import b64encode
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:9000"
PROJECTS = {"jolup-roadmap-before": "before", "jolup-roadmap": "after"}
METRICS = ("cognitive_complexity", "complexity", "ncloc", "coverage", "branch_coverage",
           "duplicated_lines_density", "bugs", "vulnerabilities", "code_smells")


class SonarError(RuntimeError):
    """A fixed, credential-free operational error suitable for the terminal."""


def _now():
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")


def _safe_path(root, value):
    root = Path(root).resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    if ".." in path.parts or not path.is_relative_to(root):
        raise SonarError("Path must stay inside this project.")
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise SonarError("A symlink is not allowed in local quality paths.")
    return path


def _json_file(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SonarError("Required local JSON file is missing or invalid.") from None
    if not isinstance(value, dict):
        raise SonarError("Required local JSON must be an object.")
    return value


def _atomic_json(path, value, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix=".sonar-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _secret_path(root):
    folder = _safe_path(root, ".quality")
    path = _safe_path(root, ".quality/sonar.json")
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    return path


def _credentials(root, required=True):
    path = _secret_path(root)
    if not path.exists():
        if required:
            raise SonarError("Local credentials are missing; run bootstrap first.")
        return path, None
    data = _json_file(path)
    if (data.get("url") != URL or data.get("username") != "admin"
            or not isinstance(data.get("password"), str) or not data["password"]):
        raise SonarError("Existing local credentials are invalid; they were not reset.")
    path.chmod(0o600)
    return path, data


@contextmanager
def _local_client(client):
    if client is not None:
        yield client
    else:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=10.0) as owned:
            yield owned


def _api(client, method, endpoint, *, token=None, auth=None, params=None, data=None,
         missing_ok=False, timeout=10.0):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = client.request(method, URL + endpoint, headers=headers, auth=auth,
                                  params=params, data=data, follow_redirects=False, timeout=timeout)
    except httpx.HTTPError:
        raise SonarError("Local SonarQube connection or response timeout failed.") from None
    if missing_ok and response.status_code == 404:
        return None
    if not 200 <= response.status_code < 300:
        raise SonarError(f"SonarQube {endpoint} returned HTTP {response.status_code}; response body omitted.")
    if response.status_code == 204 or not response.content:
        return {}
    try:
        body = response.json()
    except ValueError:
        raise SonarError("SonarQube returned invalid JSON; response body omitted.") from None
    if not isinstance(body, dict):
        raise SonarError("SonarQube returned an unexpected response shape.")
    return body


def _compose(root, *arguments):
    return ["docker", "compose", "-f", str(Path(root) / "compose.quality.yaml"), *arguments]


def _owned_container(root, runner):
    result = runner(_compose(root, "ps", "-q", "sonarqube"), cwd=root,
                    capture_output=True, text=True, timeout=30)
    if result.returncode or not result.stdout.strip():
        raise SonarError("The dedicated compose SonarQube container is not running.")
    binding = runner(_compose(root, "port", "sonarqube", "9000"), cwd=root,
                     capture_output=True, text=True, timeout=30)
    if binding.returncode or binding.stdout.strip() != "127.0.0.1:9000":
        raise SonarError("The dedicated container is not bound to the expected loopback port.")


def _server_identity(client, status=None):
    if status is None:
        status = _api(client, "GET", "/api/system/status")
    if status.get("status") != "UP" or not isinstance(status.get("id"), str) or not status["id"]:
        raise SonarError("The local SonarQube server is not UP with a known instance ID.")
    return status["id"]


def _existing_admin(client, path, credentials, instance):
    if credentials.get("instance_id") != instance:
        raise SonarError("Saved credentials belong to a different SonarQube instance; no reset performed.")
    auth = httpx.BasicAuth("admin", credentials["password"])
    if _api(client, "GET", "/api/authentication/validate", auth=auth).get("valid") is not True:
        raise SonarError("Saved admin password could not be verified; no reset performed.")
    if credentials.get("password_status") != "changed":
        credentials["password_status"] = "changed"
        _atomic_json(path, credentials)


def _initialize_admin(client, path, instance):
    credentials = {"schema_version": 1, "url": URL, "username": "admin",
                   "password": "Jolup-" + secrets.token_urlsafe(36) + "!9aA",
                   "password_status": "pending", "instance_id": instance}
    # Save before the mutation. A lost response must never lose the candidate password.
    _atomic_json(path, credentials)
    _api(client, "POST", "/api/users/change_password", auth=httpx.BasicAuth("admin", "admin"),
         data={"login": "admin", "previousPassword": "admin", "password": credentials["password"]})
    credentials["password_status"] = "changed"
    _atomic_json(path, credentials)
    return credentials


def _ensure_token(client, path, credentials):
    if credentials.get("token"):
        valid = _api(client, "GET", "/api/authentication/validate", token=credentials["token"])
        if valid.get("valid") is not True:
            raise SonarError("Saved token is invalid; existing credentials were preserved.")
        return
    name = "jolup-local-" + uuid4().hex
    response = _api(client, "POST", "/api/user_tokens/generate",
                    auth=httpx.BasicAuth("admin", credentials["password"]),
                    data={"name": name, "type": "USER_TOKEN"})
    token = response.get("token")
    if not isinstance(token, str) or not token or any(char.isspace() for char in token):
        raise SonarError("Token generation did not return a usable token; admin password was preserved.")
    credentials.update(token=token, token_name=name)
    _atomic_json(path, credentials)


def _ensure_projects(client, token):
    for project, label in PROJECTS.items():
        found = _api(client, "GET", "/api/components/show", token=token,
                     params={"component": project}, missing_ok=True)
        if found is None:
            _api(client, "POST", "/api/projects/create", token=token,
                 data={"project": project, "name": f"Graduation Roadmap ({label})", "visibility": "private"})
        elif found.get("component", {}).get("key") != project:
            raise SonarError("Unexpected existing project response; no project was replaced.")


def bootstrap(root, *, client=None, runner=None):
    root = Path(root).resolve()
    path, credentials = _credentials(root, required=False)
    _owned_container(root, runner or subprocess.run)
    with _local_client(client) as connection:
        instance = _server_identity(connection)
        if credentials is None:
            credentials = _initialize_admin(connection, path, instance)
        else:
            _existing_admin(connection, path, credentials, instance)
        _ensure_token(connection, path, credentials)
        _ensure_projects(connection, credentials["token"])
    return {"status": "ready", "url": URL, "credentials_file": str(path), "projects": list(PROJECTS)}


def _project_label(project):
    if project not in PROJECTS:
        raise SonarError("Only the dedicated before/after project keys are allowed.")
    return PROJECTS[project]


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_inventory(location):
    """Match the scanner's Python scope, including any newly added nested files."""
    names = {"app.py"} if (location / "app.py").is_file() else set()
    for folder in ("jolup", "scripts", "tests"):
        names.update(path.relative_to(location).as_posix() for path in (location / folder).rglob("*.py"))
    return names


def _verify_source_file(root, location, relative, expected):
    path = _safe_path(root, location / relative)
    if not path.is_relative_to(location) or not path.is_file() or _hash(path) != expected:
        raise SonarError("Prepared/current source hash mismatch; prepare analysis again.")


def _verify_hashes(root, prepared, stage):
    source = _safe_path(root, prepared.get("source_root", ""))
    hashes = prepared.get("source_hashes")
    if not isinstance(hashes, dict) or not hashes:
        raise SonarError("Prepared source hashes are missing.")
    if any(_source_inventory(location) != set(hashes) for location in (source, stage)):
        raise SonarError("Prepared/current source file set changed; prepare analysis again.")
    for relative, expected in hashes.items():
        if Path(relative).is_absolute() or not re.fullmatch(r"[a-f0-9]{64}", str(expected)):
            raise SonarError("Prepared source hash manifest is invalid.")
        for location in (source, stage):
            _verify_source_file(root, location, relative, expected)
    if prepared.get("coverage_included"):
        coverage = _safe_path(root, stage / "reports/quality/coverage.xml")
        if not coverage.is_file() or _hash(coverage) != prepared.get("coverage_xml_sha256"):
            raise SonarError("Prepared coverage hash mismatch.")


def validate_stage(root, project):
    _project_label(project)
    root = Path(root).resolve()
    prepared = _json_file(_safe_path(root, "reports/quality/prepared.json"))
    raw_path = prepared.get("path")
    if not isinstance(raw_path, str):
        raise SonarError("Prepared stage must be inside .build.")
    stage = _safe_path(root, raw_path)
    build = root / ".build"
    if stage == build or not stage.is_relative_to(build) or not stage.is_dir():
        raise SonarError("Prepared stage must be a directory inside .build.")
    if prepared.get("project") != project or _json_file(_safe_path(root, stage / "stage.json")) != prepared:
        raise SonarError("Prepared project or stage metadata changed.")
    properties_bytes = _safe_path(root, stage / "sonar-project.properties").read_bytes()
    if hashlib.sha256(properties_bytes).hexdigest() != prepared.get("properties_sha256"):
        raise SonarError("Prepared scanner configuration hash changed; prepare analysis again.")
    properties = properties_bytes.decode("utf-8")
    keys = re.findall(r"^sonar\.projectKey\s*=\s*(\S+)\s*$", properties, re.MULTILINE)
    if keys != [project]:
        raise SonarError("Prepared scanner project key does not match the requested project.")
    _verify_hashes(root, prepared, stage)
    return prepared


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise SonarError(f"Missing or invalid {name} identifier.")
    return value


def wait_for_task(client, token, task_id, *, timeout_seconds=180, clock=None, sleep=None):
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    task_id = _identifier(task_id, "compute task")
    deadline = clock() + timeout_seconds
    while clock() < deadline:
        reply = _api(client, "GET", "/api/ce/task", token=token, params={"id": task_id},
                     timeout=max(0.1, min(10.0, deadline - clock())))
        task = reply.get("task", {})
        status = task.get("status")
        if status == "SUCCESS":
            return _identifier(task.get("analysisId"), "analysis")
        if status in {"FAILED", "CANCELED"}:
            raise SonarError(f"SonarQube compute task {status}; analysis was not completed.")
        if status not in {"PENDING", "IN_PROGRESS"}:
            raise SonarError("SonarQube returned an unknown compute task status.")
        sleep(max(0, min(2.0, deadline - clock())))
    raise SonarError("SonarQube compute task timeout; completion is unverified.")


def _gate(client, token, analysis_id):
    result = _api(client, "GET", "/api/qualitygates/project_status", token=token,
                  params={"analysisId": analysis_id}).get("projectStatus")
    if not isinstance(result, dict) or result.get("status") not in {"OK", "WARN", "ERROR", "NONE"}:
        raise SonarError("Quality gate response is missing or invalid.")
    return result


def _redact(text, credentials):
    values = [credentials.get("password", ""), credentials.get("token", "")]
    values.append(b64encode(("admin:" + credentials.get("password", "")).encode()).decode())
    for value in values:
        if value:
            text = text.replace(value, "[REDACTED]")
    return re.sub(r"\b(?:squ|sqp|sqa)_[A-Za-z0-9_-]+", "[REDACTED]", text)


def _report_path(root, filename):
    folder = _safe_path(root, "reports/quality")
    folder.mkdir(parents=True, exist_ok=True)
    return _safe_path(root, folder / filename)


def _save_report(root, filename, value, credentials):
    safe = json.loads(_redact(json.dumps(value, ensure_ascii=False), credentials))
    path = _report_path(root, filename)
    _atomic_json(path, safe, mode=0o644)
    return path


def _scanner_environment(stage, token):
    environment = {key: value for key, value in os.environ.items()
                   if not key.endswith("API_KEY") and not key.startswith("SONAR_")}
    environment.update(SONAR_STAGE_DIR=str(stage), SONAR_TOKEN=token)
    return environment


def _task_from_stage(root, stage):
    path = _safe_path(root, stage / ".scannerwork/report-task.txt")
    if not path.is_file():
        raise SonarError("Scanner did not write a new report-task.txt file.")
    fields = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    return _identifier(fields.get("ceTaskId"), "compute task")


def _run_scanner(root, project, stage, credentials, runner):
    task_file = _safe_path(root, stage / ".scannerwork/report-task.txt")
    previous_task = _task_from_stage(root, stage) if task_file.exists() else None
    result = runner(_compose(root, "run", "--rm", "--no-deps", "scanner"), cwd=root,
                    env=_scanner_environment(stage, credentials["token"]), capture_output=True,
                    text=True, timeout=900)
    log = _report_path(root, f"scanner-{_project_label(project)}.log")
    log.write_text(_redact((result.stdout or "") + "\n" + (result.stderr or ""), credentials), encoding="utf-8")
    if result.returncode:
        raise SonarError(f"Scanner exited with code {result.returncode}; see the redacted local log.")
    task_id = _task_from_stage(root, stage)
    if task_id == previous_task:
        raise SonarError("Scanner did not produce a new compute task; previous analysis was not reused.")
    return task_id


def scan(root, project, *, client=None, runner=None):
    label = _project_label(project)
    credentials = {}
    try:
        prepared = validate_stage(root, project)
        _, credentials = _credentials(root)
        if not credentials.get("token"):
            raise SonarError("No local scanner token; finish bootstrap first.")
        runner = runner or subprocess.run
        _owned_container(root, runner)
        task_id = _run_scanner(root, project, Path(prepared["path"]), credentials, runner)
        with _local_client(client) as connection:
            if _server_identity(connection) != credentials.get("instance_id"):
                raise SonarError("The local SonarQube instance changed.")
            analysis_id = wait_for_task(connection, credentials["token"], task_id)
            gate = _gate(connection, credentials["token"], analysis_id)
        if validate_stage(root, project) != prepared:
            raise SonarError("Prepared stage changed while scanning; analysis is invalidated.")
        result = {"status": "analyzed", "project": project, "completed_at": _now(),
                  "task_id": task_id, "analysis_id": analysis_id, "quality_gate": gate,
                  "prepared": prepared}
        _save_report(root, f"scan-{label}.json", result, credentials)
        return result
    except (SonarError, OSError, subprocess.SubprocessError):
        _save_report(root, f"scan-{label}.json", {"status": "failed", "project": project,
                     "recorded_at": _now(), "message": "Scan failed or could not be verified."}, credentials)
        raise


def _pages(client, token, endpoint, params, list_key):
    rows = []
    for page in range(1, 101):
        response = _api(client, "GET", endpoint, token=token, params={**params, "p": page, "ps": 500})
        batch = response.get(list_key)
        total = response.get("paging", {}).get("total", response.get("total"))
        if not isinstance(batch, list) or not isinstance(total, int) or total < 0:
            raise SonarError("SonarQube returned incomplete pagination data.")
        rows.extend(batch)
        if len(rows) == total:
            return rows
        if not batch or len(rows) > total:
            raise SonarError("SonarQube pagination changed while reading; report is incomplete.")
    raise SonarError("SonarQube report exceeded the local pagination limit.")


def _latest_analysis(client, token, project):
    body = _api(client, "GET", "/api/project_analyses/search", token=token, params={"project": project, "ps": 1})
    analyses = body.get("analyses")
    if not isinstance(analyses, list) or not analyses or not isinstance(analyses[0], dict):
        raise SonarError("This project has no completed analysis to report.")
    return _identifier(analyses[0].get("key"), "analysis")


def _measure_values(component):
    values = {key: None for key in METRICS}
    for measure in component.get("measures", []):
        if measure.get("metric") in values:
            values[measure["metric"]] = measure.get("value")
    return values


def _collect_report(client, token, project):
    analysis_id = _latest_analysis(client, token, project)
    metric_keys = ",".join(METRICS)
    measure = _api(client, "GET", "/api/measures/component", token=token,
                   params={"component": project, "metricKeys": metric_keys})
    if not isinstance(measure.get("component"), dict):
        raise SonarError("Project measures are missing.")
    files = _pages(client, token, "/api/measures/component_tree",
                   {"component": project, "qualifiers": "FIL", "metricKeys": metric_keys}, "components")
    all_issues = _pages(client, token, "/api/issues/search", {"componentKeys": project}, "issues")
    complex_issues = _pages(client, token, "/api/issues/search",
                           {"componentKeys": project, "rules": "python:S3776", "resolved": "false"}, "issues")
    gate = _gate(client, token, analysis_id)
    if _latest_analysis(client, token, project) != analysis_id:
        raise SonarError("The latest analysis changed while reading; report is not a consistent snapshot.")
    return {"status": "collected", "project": project, "analysis_id": analysis_id,
            "collected_at": _now(), "quality_gate": gate, "metrics": _measure_values(measure["component"]),
            "file_metrics": [{"key": item.get("key"), "path": item.get("path"),
                              "metrics": _measure_values(item)} for item in files],
            "all_issue_count": len(all_issues), "all_issues": all_issues,
            "open_s3776_count": len(complex_issues), "open_s3776_issues": complex_issues,
            "scope_note": "all_issues includes resolved issues; open_s3776_issues uses resolved=false. Missing metrics are null."}


def report(root, project, *, client=None):
    label = _project_label(project)
    credentials = {}
    try:
        _, credentials = _credentials(root)
        if not credentials.get("token"):
            raise SonarError("No local reporting token; finish bootstrap first.")
        with _local_client(client) as connection:
            if _server_identity(connection) != credentials.get("instance_id"):
                raise SonarError("The local SonarQube instance changed.")
            result = _collect_report(connection, credentials["token"], project)
        _save_report(root, f"sonar-{label}.json", result, credentials)
        return result
    except (SonarError, OSError):
        _save_report(root, f"sonar-{label}.json", {"status": "failed", "project": project,
                     "recorded_at": _now(), "message": "Report could not be collected completely."}, credentials)
        raise


def _extended_profile(client, token, project=None):
    params = {"language": "py"}
    if project is not None:
        params["project"] = project
    response = _api(client, "GET", "/api/qualityprofiles/search", token=token, params=params)
    profiles = response.get("profiles")
    if not isinstance(profiles, list):
        raise SonarError("Python quality profile response is invalid.")
    matching = [item for item in profiles if isinstance(item, dict)
                and item.get("language") == "py" and item.get("name") == "Sonar way extended"]
    if len(matching) != 1:
        raise SonarError("The exact Python Sonar way extended profile was not found.")
    selected = matching[0]
    _identifier(selected.get("key"), "quality profile")
    if (type(selected.get("activeRuleCount")) is not int or selected["activeRuleCount"] < 1
            or not isinstance(selected.get("rulesUpdatedAt"), str) or not selected["rulesUpdatedAt"]):
        raise SonarError("Python quality profile metadata is incomplete.")
    return {key: selected[key] for key in ("key", "name", "language", "rulesUpdatedAt", "activeRuleCount")}


def _verified_complexity_rule(client, token, profile_key):
    response = _api(client, "GET", "/api/rules/show", token=token,
                    params={"key": "python:S3776", "actives": "true"})
    actives = response.get("actives")
    if response.get("rule", {}).get("key") != "python:S3776" or not isinstance(actives, list):
        raise SonarError("Python S3776 rule activation response is invalid.")
    matching = [item for item in actives if isinstance(item, dict) and item.get("qProfile") == profile_key]
    if len(matching) != 1 or not isinstance(matching[0].get("params"), list):
        raise SonarError("Python S3776 is not active in the selected quality profile.")
    thresholds = [item.get("value") for item in matching[0]["params"]
                  if isinstance(item, dict) and item.get("key") == "threshold"]
    if len(thresholds) != 1 or str(thresholds[0]) != "15":
        raise SonarError("Python S3776 must use the verified threshold of 15.")
    return {"key": "python:S3776", "threshold": 15}


def profile(root, *, client=None):
    """Bind both study projects to verified extended rules, without changing defaults."""
    credentials, linked = {}, []
    try:
        _, credentials = _credentials(root)
        token = credentials.get("token")
        if not token:
            raise SonarError("No local profile token; finish bootstrap first.")
        with _local_client(client) as connection:
            server = _api(connection, "GET", "/api/system/status")
            if _server_identity(connection, server) != credentials.get("instance_id"):
                raise SonarError("The local SonarQube instance changed.")
            version = server.get("version")
            if not isinstance(version, str) or not version:
                raise SonarError("SonarQube server version is missing.")
            selected = _extended_profile(connection, token)
            rule = _verified_complexity_rule(connection, token, selected["key"])
            for project in PROJECTS:
                _api(connection, "POST", "/api/qualityprofiles/add_project", token=token,
                     data={"project": project, "language": "py", "qualityProfile": selected["name"]})
                if _extended_profile(connection, token, project) != selected:
                    raise SonarError("Project quality profile association could not be verified.")
                linked.append(project)
        result = {"status": "configured", "configured_at": _now(), "server_version": version,
                  "profile": selected, "rule": rule, "projects": linked}
        _save_report(root, "profile.json", result, credentials)
        return result
    except (SonarError, OSError):
        _save_report(root, "profile.json", {"status": "failed", "recorded_at": _now(),
                     "verified_projects": linked, "message": "Quality profile setup was not fully verified."}, credentials)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap")
    commands.add_parser("profile")
    for name in ("scan", "report"):
        commands.add_parser(name).add_argument("--project", choices=PROJECTS, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "bootstrap":
            result = bootstrap(ROOT)
            print(f"Local SonarQube ready. Credentials saved privately: {result['credentials_file']}")
            return 0
        if args.command == "profile":
            result = profile(ROOT)
            print(f"Python profile verified: {result['profile']['name']}; python:S3776 threshold=15; both study projects linked.")
            return 0
        result = scan(ROOT, args.project) if args.command == "scan" else report(ROOT, args.project)
        gate = result["quality_gate"]["status"]
        print(f"{args.project}: {result['status']}; analysis={result['analysis_id']}; Quality Gate={gate}")
        return 0 if gate == "OK" else 2
    except (SonarError, OSError, subprocess.SubprocessError) as exc:
        # Do not echo arbitrary OS/Docker errors, which can contain environment data.
        print(str(exc) if isinstance(exc, SonarError) else "Local SonarQube operation failed; check local setup.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
