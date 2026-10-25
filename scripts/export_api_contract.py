"""Export the running code's OpenAPI and reproducible, key-free Postman exercises.

Run: .venv/bin/python scripts/export_api_contract.py
This imports schemas and built-in dummy fixtures only; it performs no API requests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jolup.study_api import app


def _request(name, method, path, checks, body=None, description=""):
    request = {"method": method, "url": "{{base_url}}" + path,
               "description": description, "header": []}
    if body is not None:
        request["header"] = [{"key": "Content-Type", "value": "application/json"}]
        request["body"] = {"mode": "raw", "raw": body, "options": {"raw": {"language": "json"}}}
    return {"name": name, "request": request,
            "event": [{"listen": "test", "script": {"type": "text/javascript", "exec": checks}}]}


def _status(code):
    return f'pm.test("HTTP {code}", () => pm.response.to.have.status({code}));'


def _sample(sample_id, label):
    return _request(f"{label} · 더미 입력 받기", "GET", f"/samples/{sample_id}", [
        _status(200), "const sample = pm.response.json();",
        'pm.test("가상 자료", () => pm.expect(sample.profile.is_dummy).to.eql(true));',
        'pm.collectionVariables.set("student_request", JSON.stringify({profile: sample.profile, courses: sample.courses}));',
        'pm.collectionVariables.set("chat_request", JSON.stringify({query: "나 졸업 괜찮아?", profile: sample.profile, courses: sample.courses}));',
    ], description="이 응답의 profile과 courses만 다음 진단·계획 요청으로 전달합니다. 기대 정답은 입력하지 않습니다.")


def _diagnosis(label, status, total, projected=None):
    checks = [_status(200), "const result = pm.response.json();",
              f'pm.test("현재 판정", () => pm.expect(result.status).to.eql("{status}"));',
              f'pm.test("확정 학점", () => pm.expect(result.totals.total).to.eql({total}));',
              'pm.test("판정 근거", () => { pm.expect(result.rows.length).to.be.above(0); pm.expect(result.evidence.length).to.be.above(0); pm.expect(result.meta.length).to.be.above(0); });',
              'pm.test("학교 최종 판정 아님", () => pm.expect(result.official_graduation_confirmed).to.eql(false));']
    if projected is not None:
        checks.append(f'pm.test("예상 학점 별도", () => pm.expect(result.projection.totals.total).to.eql({projected}));')
    return _request(f"{label} · 현재 진단", "POST", "/diagnosis", checks, "{{student_request}}")


def _plan(label, status):
    return _request(f"{label} · 목표 계획", "POST", "/plans", [
        _status(200), "const result = pm.response.json();",
        f'pm.test("계획 판정", () => pm.expect(result.status).to.eql("{status}"));',
        'pm.test("조건과 한계 표시", () => { pm.expect(result.assumptions.length).to.be.above(0); pm.expect(result.warnings.length).to.be.above(0); });',
    ], "{{student_request}}")


def collection():
    items = [
        _request("01 · 서버 확인", "GET", "/health", [_status(200),
                 'pm.test("서버 상태", () => pm.expect(pm.response.json().status).to.eql("ok"));']),
        _request("02 · 더미 목록", "GET", "/samples", [_status(200),
                 'pm.test("11개 사례", () => pm.expect(pm.response.json()).to.have.lengthOf(11));']),
        _sample("on_track", "03"),
        _diagnosis("04 · 준비 가능 사례", "미충족", 90, 105),
        _plan("05 · 준비 가능 사례", "조건부 가능"),
        _sample("fully_met", "06"),
        _diagnosis("07 · 미래 요건 충족 사례", "충족", 123),
        _sample("credit_shortfall", "08"),
        _diagnosis("09 · 학점 부족 사례", "미충족", 38),
        _plan("10 · 학점 부족 사례", "위험"),
        _sample("missing_record", "11"),
        _diagnosis("12 · 자료 누락 사례", "확인 필요", 90),
        _request("13 · 잘못된 입력 거부", "POST", "/diagnosis", [_status(422),
                 'pm.test("입력 위치 안내", () => pm.expect(pm.response.json().errors.length).to.be.above(0));'],
                 '{"profile": {}, "courses": "not-a-list"}'),
        _request("14 · 규정과 출처", "POST", "/regulations", [_status(200),
                 'const result = pm.response.json();',
                 'pm.test("근거에 따른 답변 또는 확인 필요", () => pm.expect(["근거 확인", "확인 필요"]).to.include(result.status));',
                 'pm.test("답변·출처 구조", () => { pm.expect(result.answer).to.be.a("string"); pm.expect(result.evidence).to.be.an("array"); pm.expect(result.meta).to.be.an("array"); });'],
                 '{"query": "2024학번 컴퓨터공학과 전공심화 졸업 총학점은?"}',
                 description="외부 LLM을 호출하지 않습니다. 기존 RAG의 인덱스 상태에 따라 경고와 확인 필요가 나올 수 있습니다."),
        _request("15 · 일정 미리보기", "GET", "/events?query=성적&year=2026", [_status(200),
                 'const preview = pm.response.json();',
                 'pm.test("일정과 승인 요구", () => { pm.expect(preview.events.length).to.be.above(0); pm.expect(preview.approval_required).to.eql(true); });',
                 'pm.collectionVariables.set("preview_events", JSON.stringify(preview.events));']),
        _request("16 · 승인 없는 ICS 거부", "POST", "/calendar/ics", [_status(403),
                 'pm.test("일정 파일 생성 안 함", () => pm.expect(pm.response.text()).not.to.include("BEGIN:VCALENDAR"));'],
                 '{"events": {{preview_events}}, "approved": false}'),
        _request("17 · 확인한 일정의 선택적 ICS 내보내기", "POST", "/calendar/ics", [
                 'const approved = pm.variables.get("calendar_approved") === "true";',
                 'pm.test("명시 승인에 맞는 상태", () => pm.response.to.have.status(approved ? 200 : 403));',
                 'pm.test("승인과 파일 생성 일치", () => pm.expect(pm.response.text().includes("BEGIN:VCALENDAR")).to.eql(approved));'],
                 '{"events": {{preview_events}}, "approved": {{calendar_approved}}}',
                 description="기본값 false라 전체 실행으로 승인되지 않습니다. 15번 응답의 날짜를 직접 확인하고 내보내려는 경우에만 environment의 calendar_approved를 true로 바꾸세요. 외부 캘린더에는 등록하지 않습니다."),
        _sample("on_track", "18 · Agent 채팅 준비"),
        _request("19 · Agent 첫 질문과 세션 받기", "POST", "/chat", [
                 _status(200), 'const result = pm.response.json();',
                 'pm.test("현재와 미래 구분", () => { pm.expect(result.status).to.eql("미충족"); pm.expect(result.plan.status).to.eql("조건부 가능"); });',
                 'pm.test("HTTP 경로 · 외부 AI 호출 없음", () => { pm.expect(result.transport_mode).to.eql("http"); pm.expect(result.ai_enabled).to.eql(false); });',
                 'pm.collectionVariables.set("chat_session_id", result.session_id);',
                 'const student = JSON.parse(pm.collectionVariables.get("student_request"));',
                 'pm.collectionVariables.set("followup_request", JSON.stringify({...student, query: "남은 학기 수강 계획 알려줘", session_id: result.session_id}));',
                 ], "{{chat_request}}", description="입력·출력·출처와 서버가 발급한 세션 ID를 읽습니다. LLM 키는 보내지 않습니다."),
        _request("20 · 같은 세션의 수강 계획", "POST", "/chat", [
                 _status(200), 'const result = pm.response.json();',
                 'pm.test("같은 세션", () => pm.expect(result.session_id).to.eql(pm.collectionVariables.get("chat_session_id")));',
                 'pm.test("계획과 최근 대화", () => { pm.expect(result.intent).to.eql("planning"); pm.expect(result.memory.turn_count).to.eql(2); });',
                 ], "{{followup_request}}", description="각 요청에 학생 자료를 다시 보냅니다. 이전 결과를 클라이언트가 사실처럼 주입하지 않습니다."),
        _request("21 · 임시 대화 초기화", "DELETE", "/sessions/{{chat_session_id}}", [
                 _status(200), 'pm.test("해당 기억 삭제", () => pm.expect(pm.response.json().cleared).to.eql(true));',
                 ], description="서버 메모리의 이 세션만 삭제합니다. 영구 학생 DB는 없습니다."),
    ]
    return {"info": {"name": "졸업로드맵 · Swagger와 Postman 학습", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
                     "description": "API 키 없이 01→21 순서로 실행합니다. 내장 더미 응답을 다음 요청에 연결합니다. Swagger http://127.0.0.1:8001/docs 에서 스키마를 먼저 읽으세요. ICS 승인은 기본 false이며 직접 확인 후에만 변경하세요."},
            "variable": [{"key": "base_url", "value": "http://127.0.0.1:8001"},
                         {"key": "calendar_approved", "value": "false"},
                         {"key": "student_request", "value": "{}"},
                         {"key": "preview_events", "value": "[]"},
                         {"key": "chat_request", "value": "{}"},
                         {"key": "followup_request", "value": "{}"},
                         {"key": "chat_session_id", "value": ""}], "item": items}


def environment():
    return {"name": "졸업로드맵 로컬 · 키 없음", "_postman_variable_scope": "environment",
            "values": [{"key": "base_url", "value": "http://127.0.0.1:8001", "enabled": True},
                       {"key": "calendar_approved", "value": "false", "enabled": True}]}


def main():
    destination = ROOT / "docs" / "api"
    destination.mkdir(parents=True, exist_ok=True)
    artifacts = {"openapi.json": app.openapi(), "jolup.postman_collection.json": collection(),
                 "local.postman_environment.json": environment()}
    for name, content in artifacts.items():
        path = destination / name
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
