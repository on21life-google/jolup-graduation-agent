"""Test-first reading and reconstructed TDD exercises; never rewrites the app.

Run from the project with: python scripts/tdd_study.py --help
The Red logs are executions made now, not a reconstruction of historical logs.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/tdd/requirements.json"


def _local_file(name, root):
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"프로젝트 내부 파일만 지정하세요: {name}")
    target = (root / path).resolve()
    if root.resolve() not in target.parents or not target.is_file():
        raise ValueError(f"파일을 찾을 수 없습니다: {name}")
    return target


def _validate_test_selector(selector, root, functions):
    parts = selector.split("::")
    if len(parts) != 2 or not re.fullmatch(r"test_[A-Za-z0-9_]+", parts[1]):
        raise ValueError(f"테스트 파일::함수 형식이 필요합니다: {selector}")
    path = _local_file(parts[0], root)
    if path.suffix != ".py":
        raise ValueError(f"Python 테스트가 아닙니다: {selector}")
    if path not in functions:
        functions[path] = {n.name for n in ast.parse(path.read_text(encoding="utf-8")).body
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if parts[1] not in functions[path]:
        raise ValueError(f"테스트 함수가 없습니다: {selector}")


def validate_requirements(items, root=ROOT):
    if not isinstance(items, list) or not items:
        raise ValueError("요구사항 목록이 비어 있습니다.")
    seen, functions = set(), {}
    for item in items:
        identifier = item.get("id", "")
        if not re.fullmatch(r"REQ-[A-Z0-9-]+", identifier) or identifier in seen:
            raise ValueError(f"요구사항 ID가 잘못됐거나 중복됩니다: {identifier}")
        seen.add(identifier)
        if not isinstance(item.get("tests"), list):
            raise ValueError(f"tests 목록이 필요합니다: {identifier}")
        for selector in item["tests"]:
            _validate_test_selector(selector, root, functions)
        for location in item.get("implementation", []):
            _local_file(location.split(":", 1)[0], root)
    return items


def select_tests(items, selected_ids=None):
    available = {item["id"] for item in items}
    selected = available if selected_ids is None else set(selected_ids)
    if selected - available:
        raise ValueError("알 수 없는 요구사항: " + ", ".join(sorted(selected - available)))
    return list(dict.fromkeys(test for item in items if item["id"] in selected for test in item["tests"]))


def _count_test_outcomes(cases, result):
    failures = []
    for case in cases:
        failure = case.find("failure")
        if case.find("error") is not None:
            result["errors"] += 1
        elif failure is not None:
            result["failed"] += 1
            failures.append(failure.get("message", ""))
        elif case.find("skipped") is not None:
            result["skipped"] += 1
        else:
            result["passed"] += 1
    return failures


def read_junit(path, exit_code, phase="run", red_marker="TDD_REPLAY_RED"):
    result = {"status": "error", "exit_code": exit_code, "total": 0, "passed": 0,
              "failed": 0, "errors": 0, "skipped": 0}
    try:
        cases = list(ET.parse(path).getroot().iter("testcase"))
    except (ET.ParseError, OSError):
        return result
    result["total"] = len(cases)
    result["test_ids"] = [f"{case.get('classname', '')}::{case.get('name', '')}" for case in cases]
    failures = _count_test_outcomes(cases, result)
    if phase == "red":
        intended = cases and len(failures) == len(cases) and all(
            message.startswith(f"NotImplementedError: {red_marker}") for message in failures)
        result["status"] = "expected_red" if exit_code == 1 and intended else "failed"
    elif result["errors"] or exit_code not in (0, 1):
        result["status"] = "error"
    elif result["failed"] or exit_code == 1:
        result["status"] = "failed"
    elif not cases or result["skipped"]:
        result["status"] = "incomplete"
    else:
        result["status"] = "passed"
    return result


def replay_contract_matches(results, expected_case_count):
    if not results or type(expected_case_count) is not int or expected_case_count <= 0:
        return False
    identities = None
    for result in results:
        expected_status = "expected_red" if result.get("phase") == "red" else "passed"
        current = set(result.get("test_ids", []))
        if (result.get("status") != expected_status or result.get("total") != expected_case_count
                or len(current) != expected_case_count or "::" in current):
            return False
        if identities is not None and current != identities:
            return False
        identities = current
    return True


def child_environment(source, vector=False):
    clean = {key: value for key, value in source.items()
             if not key.endswith("API_KEY") and key not in {
                 "MCP_URL", "JOLUP_AGENT_API_URL", "PYTEST_ADDOPTS", "JOLUP_TEST_VECTOR", "JOLUP_TDD_STAGE",
                 "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2", "CHROMA_HOST", "CHROMA_PORT"}}
    clean["LANGSMITH_TRACING"] = "false"
    if vector:
        clean["JOLUP_TEST_VECTOR"] = "1"
    return clean


def _run_tests(selectors, folder, phase="run", vector=False):
    report = folder / f"{phase}.xml"
    log = folder / f"{phase}.txt"
    env = child_environment(os.environ, vector)
    if phase in {"red", "green", "refactor"}:
        env["JOLUP_TDD_STAGE"] = phase
    command = [sys.executable, "-m", "pytest", "-q", *selectors, f"--junitxml={report}"]
    label = " ".join(selectors) if len(selectors) <= 3 else f"요구사항에 연결된 테스트 함수 {len(selectors)}개"
    print(f"\n[{phase}] " + label, flush=True)
    try:
        completed = subprocess.run(command, cwd=ROOT, env=env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        log.write_text(completed.stdout, encoding="utf-8")
        # Raw pytest output is preserved in the log; the concise summary explains expected Red.
        result = read_junit(report, completed.returncode, phase)
    except subprocess.TimeoutExpired:
        log.write_text("180초 제한을 넘었습니다. 완료된 검증으로 처리하지 않습니다.\n", encoding="utf-8")
        result = {"status": "error", "reason": "timeout", "exit_code": None}
    result.update(phase=phase, junit=str(report.relative_to(ROOT)), log=str(log.relative_to(ROOT)))
    print(json.dumps({k: v for k, v in result.items() if k != "test_ids"}, ensure_ascii=False, indent=2), flush=True)
    return result


def _show(item):
    print(f"\n{item['id']} · {item['title']}")
    for key, label in [("basis", "요구 근거"), ("technique", "테스트 설계"),
                       ("risk", "잘못되었을 때의 영향"), ("acceptance_criteria", "기대 결과"),
                       ("tests", "먼저 읽고 실행할 테스트"), ("implementation", "이후 읽을 구현"),
                       ("scope_notes", "검증 범위")]:
        value = item.get(key, "")
        print(f"{label}: " + ("\n  - " + "\n  - ".join(str(x) for x in value) if isinstance(value, list) else str(value)))


def _replay_summary(stage, folder):
    spec = json.loads((ROOT / "learning/tdd/replay_spec.json").read_text(encoding="utf-8"))
    _local_file(spec["test_file"], ROOT)
    if spec["stage_env"] != "JOLUP_TDD_STAGE" or spec["red_marker"] != "TDD_REPLAY_RED":
        raise ValueError("실습 환경 변수 또는 Red 표식이 실행 도구와 일치하지 않습니다.")
    phases = ["red", "green", "refactor"] if stage == "all" else [stage]
    results = [_run_tests([spec["test_file"]], folder, phase) for phase in phases]
    passed = replay_contract_matches(results, spec["case_count"])
    summary = {"kind": "reconstructed_tdd_exercise", "historical_tdd_claim": False,
               "expected_case_count": spec["case_count"],
               "same_contract_verified": passed if len(phases) == 3 else None,
               "status": "verified" if passed else "failed", "results": results}
    print("Red는 명시된 미구현 실패만 재현 성공으로 인정합니다. 다른 오류는 성공이 아닙니다.")
    print(f"명세의 {spec['case_count']}개 검사 실행: {'확인' if passed else '실패'}")
    print("단계별 검사 목록 비교: " + (("일치" if passed else "실패") if len(phases) == 3 else "미실시 · 한 단계만 실행"))
    return summary, passed


def _requirements_summary(args, selected, selectors, manual, folder):
    result = _run_tests(selectors, folder, vector=args.vector)
    passed = result["status"] == "passed"
    summary = {"kind": "requirements_test_run", "status": result["status"],
               "requirement_ids": [i["id"] for i in selected], "manual_requirements": manual,
               "coverage_claim": "선택한 테스트 실행 결과. 전체 조건·분기 커버리지와 다릅니다.",
               "rag_mode": "vector" if args.vector else "lexical", "result": result}
    if manual:
        print("별도 수동 확인 필요: " + ", ".join(manual))
    return summary, passed


def _execute_study(args, items, parser):
    if args.command == "run":
        if bool(args.ids) == bool(args.all):
            parser.error("요구사항 ID 또는 --all 중 하나를 선택하세요.")
        selected = [i for i in items if args.all or i["id"] in args.ids]
        selectors = select_tests(items, None if args.all else args.ids)
        manual = [i["id"] for i in selected if not i["tests"]]
        if not selectors:
            print("자동 테스트가 연결되지 않았습니다. show로 수동 확인 절차를 읽으세요: " + ", ".join(manual))
            return 2
    stamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M%S_%f")
    folder = ROOT / "reports/tdd_study" / stamp
    folder.mkdir(parents=True)
    if args.command == "replay":
        summary, passed = _replay_summary(args.stage, folder)
    else:
        summary, passed = _requirements_summary(args, selected, selectors, manual, folder)
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("결과: " + str(folder.relative_to(ROOT) / "summary.json"))
    return 0 if passed else 1


def _list_requirements(items):
    for item in items:
        count = len(item["tests"])
        print(f"{item['id']} | {item['title']} | " + (f"테스트 함수 {count}개 연결" if count else "수동 검증 연결"))
    print("함수 수·실행 사례 수는 커버리지 비율이 아닙니다.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="완료 앱을 테스트부터 읽는 학습 도구 · 과거 개발 이력의 증명이 아닙니다.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="전체 요구사항과 자동 시험 연결 보기")
    show = commands.add_parser("show", help="기대 결과→테스트→구현 순서로 읽기")
    show.add_argument("ids", nargs="+")
    run = commands.add_parser("run", help="선택한 요구사항에 연결된 실제 테스트 실행")
    run.add_argument("ids", nargs="*")
    run.add_argument("--all", action="store_true")
    run.add_argument("--vector", action="store_true", help="실제 임베딩·Chroma 사용. 최초 실행은 모델 다운로드가 필요할 수 있음")
    replay = commands.add_parser("replay", help="같은 계약 시험의 Red→Green→Refactor 재현")
    replay.add_argument("--stage", choices=["all", "red", "green", "refactor"], default="all")
    args = parser.parse_args(argv)
    try:
        items = validate_requirements(json.loads(MANIFEST.read_text(encoding="utf-8")))
        if args.command == "list":
            _list_requirements(items)
            return 0
        if args.command == "show":
            select_tests(items, args.ids)
            for item in items:
                if item["id"] in args.ids:
                    _show(item)
            return 0
        return _execute_study(args, items, parser)
    except (ValueError, OSError, SyntaxError, TypeError, KeyError) as exc:
        print(f"학습 실행을 중단했습니다: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
