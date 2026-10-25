"""Local learning spans, with a separate explicit export of unchanged built-in dummies.

No raw prompts, student rows, API keys, model prose or exception messages enter a
trace.  Export uses LangSmith's documented REST API rather than auto tracing:
https://docs.langchain.com/langsmith/trace-with-api
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from uuid import UUID, uuid4

import httpx


# Fixed labels and code references are safe to show locally and send externally.
OPERATIONS = {
    "route": ("질문 유형 선택", "jolup/agent.py:_route"),
    "diagnosis": ("현재 진단과 목표 계획", "jolup/agent.py:_diagnose"),
    "planning": ("수강 계획 요청", "jolup/agent.py:_plan"),
    "regulation": ("규정 질문 처리", "jolup/agent.py:_regulation"),
    "calendar": ("일정 질문 처리", "jolup/agent.py:_calendar"),
    "unsupported": ("지원 범위 안내", "jolup/agent.py:_unsupported"),
    "compose": ("분야별 근거와 결과 합성", "jolup/agent.py:_compose"),
    "reuse": ("이전 검증 결과 재사용", "jolup/agent.py:_reuse"),
    "explain": ("결과 정리", "jolup/agent.py:_explain"),
    "check_requirements": ("이수 요건 계산", "jolup/academic.py:check_requirements"),
    "plan_courses": ("학기별 후보 배치", "jolup/planner.py:plan_courses"),
    "classify_intent": ("AI 의도 분류", "jolup/llm.py:classify_intent"),
    "explain_result": ("AI 설명 생성", "jolup/llm.py:explain_result"),
    "answer_regulations": ("공개 규정 검색", "jolup/rag.py:KnowledgeBase.answer"),
    "search_academic_events": ("공식 일정 검색", "jolup/calendar.py:search_events"),
}
INTENTS = frozenset({"diagnosis", "planning", "regulation", "calendar", "unsupported"})
STATUSES = frozenset({"충족", "미충족", "확인 필요", "조건부 가능", "위험", "검증 완료", "탐색 제한", "근거 확인", "확인"})
SOURCES = frozenset({"school_rules", "course_guide", "major_notice", "curriculum_year1",
                     "curriculum_year2", "curriculum_year3", "curriculum_year4"})
TOTAL_KEYS = frozenset({"total", "general_required", "general_elective", "major_required",
                        "major_elective", "major_total", "other", "in_progress"})
ERROR_CODES = frozenset({"execution_error", "summary_unavailable", "configuration", "input",
                         "authentication", "permission", "model_access", "rate_limit",
                         "server", "invalid_response", "schema_mismatch", "timeout", "connection", "refusal",
                         "incomplete", "empty_response", "empty_output", "provider_unavailable",
                         "request_rejected", "provider_error"})
_CURRENT = ContextVar("jolup_study_recorder", default=None)
_PARENTS = ContextVar("jolup_study_parent_ids", default=())
_SIGNING_KEY = secrets.token_bytes(32)  # Process-local integrity key, never exported.
_MAX_STEPS = 64
_EXPORT_DEADLINE_SECONDS = 30.0
_ENDPOINTS = {"US": "https://api.smith.langchain.com/runs",
              "EU": "https://eu.api.smith.langchain.com/runs"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1_000_000


def _totals(value):
    if type(value) is not dict:
        return {}
    return {key: value[key] for key in sorted(TOTAL_KEYS) if key in value and _number(value[key])}


def _semester_summary(item):
    if type(item) is not dict or type(item.get("term")) is not str or not re.fullmatch(r"20\d{2}-[12]", item["term"]):
        return None
    semester = {"term": item["term"]}
    for key in ("min_credits", "max_credits", "total_credits"):
        if _number(item.get(key)):
            semester[key] = item[key]
    if "projected_totals" in item:
        semester["projected_totals"] = _totals(item["projected_totals"])
    if item.get("projected_status") in STATUSES:
        semester["projected_status"] = item["projected_status"]
    return semester


def _plan_summary(value):
    if type(value) is not dict:
        return {}
    result = {}
    if value.get("status") in STATUSES:
        result["status"] = value["status"]
    for key in ("current_totals", "projected_totals"):
        if key in value:
            result[key] = _totals(value[key])
    if type(value.get("solver")) is dict:
        solver = value["solver"]
        result["solver"] = {key: solver[key] for key in ("feasible", "optimal")
                            if type(solver.get(key)) is bool}
        if solver.get("status") in STATUSES:
            result["solver"]["status"] = solver["status"]
    if type(value.get("semesters")) is list:
        semesters = [_semester_summary(item) for item in value["semesters"][:10]]
        result["semesters"] = [item for item in semesters if item is not None]
    return result


def _public_source_ids(source):
    source_ids = set()
    for key in ("evidence", "meta"):
        if type(source.get(key)) is list:
            for item in source[key][:100]:
                if type(item) is dict and type(item.get("source_id")) is str and item["source_id"] in SOURCES:
                    source_ids.add(item["source_id"])
    if type(source.get("source_ids")) is list:
        source_ids.update(s for s in source["source_ids"] if type(s) is str and s in SOURCES)
    return sorted(source_ids)


def _model_summary(source):
    result = {}
    for key in ("used", "llm_used"):
        if type(source.get(key)) is bool:
            result[key] = source[key]
    if type(source.get("llm")) is dict and type(source["llm"].get("used")) is bool:
        result["llm_used"] = source["llm"]["used"]
    if source.get("error_code") in ERROR_CODES:
        result["error_code"] = source["error_code"]
    elif source.get("error"):
        result["error_code"] = "provider_error"
    return result


def _summary(value):
    """Select fixed enums, bounded numbers, and known public source IDs only."""
    if type(value) is not dict:
        return {}
    # LangGraph nodes return {result: ...}; their complete state is never copied.
    source = value["result"] if type(value.get("result")) is dict else value
    result = _plan_summary(source)
    if value.get("intent") in INTENTS:
        result["intent"] = value["intent"]
    elif source.get("intent") in INTENTS:
        result["intent"] = source["intent"]
    if "totals" in source:
        result["totals"] = _totals(source["totals"])
    if type(source.get("projection")) is dict:
        projection = source["projection"]
        result["projection"] = {"totals": _totals(projection.get("totals"))}
        if projection.get("status") in STATUSES:
            result["projection"]["status"] = projection["status"]
    if type(source.get("plan")) is dict:
        result["plan"] = _plan_summary(source["plan"])
    if type(source.get("events")) is list:
        result["event_count"] = min(len(source["events"]), 1000)
    elif type(source.get("event_count")) is int and 0 <= source["event_count"] <= 1000:
        result["event_count"] = source["event_count"]
    source_ids = _public_source_ids(source)
    if source_ids:
        result["source_ids"] = source_ids
    result.update(_model_summary(source))
    return result


def _safe_summary(value):
    try:
        return _summary(value)
    except Exception:
        # Observability must not break the business operation, even for odd data.
        return {"error_code": "summary_unavailable"}


def _builtin_case(profile, courses, sample_id):
    if type(sample_id) is not str:
        return None
    try:
        from .academic import validate_courses
        from .samples import load_sample
        sample = load_sample(sample_id)
        actual, errors = validate_courses(courses)
        expected, expected_errors = validate_courses(sample["courses"])
        if not errors and not expected_errors and profile == sample["profile"] and actual == expected:
            return sample["id"]
    except Exception:
        pass
    return None


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sign(value):
    return hmac.new(_SIGNING_KEY, _encoded(value), hashlib.sha256).hexdigest()


class StudyRecorder:
    """Record sanitized, process-local spans; construction never contacts a service."""

    def __init__(self, profile, courses, *, builtin_sample_id=None):
        self.case_id = _builtin_case(profile, courses, builtin_sample_id)
        self.exportable = self.case_id is not None
        self.run_id = str(uuid4())
        self._start_time = _now()
        self._start_clock = time.perf_counter()
        self._steps = []
        self._finished = None
        self._truncated = False

    @contextmanager
    def activate(self):
        token = _CURRENT.set(self)
        parents = _PARENTS.set(())
        try:
            yield self
        finally:
            _PARENTS.reset(parents)
            _CURRENT.reset(token)

    def finish(self, result):
        if self._finished is None:
            closed = all(step["end_time"] is not None for step in self._steps)
            trace = {"schema_version": 1, "run_id": self.run_id, "case_id": self.case_id,
                     "exportable": bool(self.exportable and closed and not self._truncated),
                     "start_time": self._start_time, "end_time": _now(),
                     "duration_ms": round((time.perf_counter() - self._start_clock) * 1000, 3),
                     "status": "error" if any(s["status"] == "error" for s in self._steps) else "success",
                     "steps": deepcopy(self._steps), "summary": _safe_summary(result),
                     "truncated": self._truncated}
            trace["integrity"] = _sign(trace)
            self._finished = trace
        return deepcopy(self._finished)


def observe_call(name, function, *args, **kwargs):
    """Call the real function once; retain no arguments or raw return value."""
    recorder = _CURRENT.get()
    if recorder is None or type(name) is not str or name not in OPERATIONS or recorder._finished is not None:
        return function(*args, **kwargs)
    if len(recorder._steps) >= _MAX_STEPS:
        recorder._truncated = True
        return function(*args, **kwargs)
    parents = _PARENTS.get()
    label, source_ref = OPERATIONS[name]
    step = {"id": str(uuid4()), "parent_id": parents[-1] if parents else recorder.run_id,
            "name": name, "label": label, "source_ref": source_ref,
            "start_time": _now(), "end_time": None, "duration_ms": 0,
            "status": "running", "summary": {}}
    recorder._steps.append(step)
    started = time.perf_counter()
    token = _PARENTS.set((*parents, step["id"]))
    try:
        value = function(*args, **kwargs)
        step["summary"] = _safe_summary(value)
        step["status"] = "error" if "error_code" in step["summary"] else "success"
        return value
    except Exception:
        step["summary"] = {"error_code": "execution_error"}
        step["status"] = "error"
        raise
    finally:
        step["end_time"] = _now()
        step["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        _PARENTS.reset(token)


def _uuid(value):
    return type(value) is str and str(UUID(value)) == value


def _date(value):
    date = datetime.fromisoformat(value)
    if date.tzinfo is None or date.utcoffset().total_seconds() != 0:
        raise ValueError("invalid timestamp")
    return date


def _has_valid_trace_integrity(trace):
    """Check the closed schema before trusting any trace field or signature."""
    if type(trace) is not dict or len(_encoded(trace)) > 200_000:
        return False
    keys = {"schema_version", "run_id", "case_id", "exportable", "start_time", "end_time",
            "duration_ms", "status", "steps", "summary", "truncated", "integrity"}
    if set(trace) != keys or trace["schema_version"] != 1 or trace["exportable"] is not True or trace["truncated"] is not False:
        return False
    signature = trace["integrity"]
    unsigned = {key: value for key, value in trace.items() if key != "integrity"}
    return type(signature) is str and hmac.compare_digest(signature, _sign(unsigned))


def _has_safe_completed_summary(record):
    return (record["status"] in {"success", "error"}
            and _number(record["duration_ms"])
            and record["summary"] == _summary(record["summary"]))


def _validated_root_interval(trace):
    from .samples import list_samples
    if trace["case_id"] not in {s["id"] for s in list_samples()} or not _uuid(trace["run_id"]):
        return None
    if not _has_safe_completed_summary(trace):
        return None
    start, end = _date(trace["start_time"]), _date(trace["end_time"])
    if end < start or type(trace["steps"]) is not list or not 1 <= len(trace["steps"]) <= _MAX_STEPS:
        return None
    return start, end


def _validated_step_interval(step, intervals):
    """Reapply the allowlist, then require an existing parent enclosing the step."""
    keys = {"id", "parent_id", "name", "label", "source_ref", "start_time", "end_time",
            "duration_ms", "status", "summary"}
    if type(step) is not dict or set(step) != keys:
        return None
    if step["name"] not in OPERATIONS or (step["label"], step["source_ref"]) != OPERATIONS[step["name"]]:
        return None
    if not _uuid(step["id"]) or step["id"] in intervals or step["parent_id"] not in intervals:
        return None
    if not _has_safe_completed_summary(step):
        return None
    start, end = _date(step["start_time"]), _date(step["end_time"])
    parent_start, parent_end = intervals[step["parent_id"]]
    return (start, end) if parent_start <= start <= end <= parent_end else None


def _validated_trace(trace):
    """Reject altered, restarted-process, unknown-field or incomplete traces."""
    try:
        if not _has_valid_trace_integrity(trace):
            return None
        root_interval = _validated_root_interval(trace)
        if root_interval is None:
            return None
        intervals = {trace["run_id"]: root_interval}
        for step in trace["steps"]:
            interval = _validated_step_interval(step, intervals)
            if interval is None:
                return None
            intervals[step["id"]] = interval
        return deepcopy(trace)
    except Exception:
        return None


def _dotted(start_time, run_id):
    return _date(start_time).strftime("%Y%m%dT%H%M%S%fZ") + run_id


def _valid_export_key(key):
    return (type(key) is str and 8 <= len(key) <= 512 and key.isascii()
            and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key))


def _prepare_export_settings(config):
    """Return validated connection settings separately from the public reply."""
    reply = {"status": "disabled", "message": "LangSmith 전송은 API 키를 입력하고 전송 버튼을 눌렀을 때만 실행됩니다.",
             "run_id": None, "project": "jolup-study"}
    if type(config) is not dict or not config.get("api_key"):
        return None, reply
    key, region = config.get("api_key"), config.get("region", "US")
    project = config.get("project", "jolup-study")
    workspace = config.get("workspace_id", "")
    if type(key) is str and key.startswith("sk-"):
        return None, dict(reply, status="blocked", message="OpenAI·Claude 키는 사용할 수 없습니다. 별도로 발급한 LangSmith 키를 입력해 주세요.")
    if (not _valid_export_key(key)
            or type(region) is not str or region not in _ENDPOINTS
            or type(project) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", project)):
        return None, dict(reply, status="blocked", message="LangSmith 키·지역·프로젝트 설정을 확인해 주세요.")
    reply["project"] = project
    try:
        if type(workspace) is not str or (workspace and not _uuid(workspace)):
            raise ValueError("invalid workspace")
    except Exception:
        return None, dict(reply, status="blocked", message="워크스페이스 ID는 UUID 형식이어야 합니다.")
    return {"api_key": key, "region": region, "project": project, "workspace_id": workspace}, reply


def _export_payloads(safe, project):
    """Build complete root/child spans from the validated summary only."""
    root_id = safe["run_id"]
    dotted = {root_id: _dotted(safe["start_time"], root_id)}
    intent = safe["summary"].get("intent", "unsupported")
    inputs = {"case_id": safe["case_id"], "intent": intent}
    root = {"id": root_id, "trace_id": root_id, "name": "jolup-study", "run_type": "chain",
            "session_name": project, "start_time": safe["start_time"], "end_time": safe["end_time"],
            "dotted_order": dotted[root_id], "inputs": {**inputs, "source_ref": "jolup/agent.py:respond"}, "outputs": safe["summary"]}
    if safe["status"] == "error":
        root["error"] = "execution_error"
    payloads = [root]
    for step in safe["steps"]:
        dotted[step["id"]] = dotted[step["parent_id"]] + "." + _dotted(step["start_time"], step["id"])
        payload = {"id": step["id"], "trace_id": root_id, "parent_run_id": step["parent_id"],
                   "dotted_order": dotted[step["id"]], "name": step["name"], "run_type": "chain",
                   "session_name": project, "start_time": step["start_time"], "end_time": step["end_time"],
                   "inputs": {**inputs, "operation": step["name"], "source_ref": step["source_ref"]}, "outputs": step["summary"]}
        if step["status"] == "error":
            payload["error"] = step["summary"].get("error_code", "execution_error")
        payloads.append(payload)
    return payloads


def _export_rejection_message(status, sent_count):
    messages = {
        401: "LangSmith 인증에 실패했습니다. API 키를 확인해 주세요.",
        403: "LangSmith 접근 권한이 없습니다. 프로젝트·워크스페이스 권한을 확인해 주세요.",
        429: "LangSmith 요청 한도에 도달했습니다. 자동 재전송하지 않았습니다.",
    }
    message = messages.get(status, "LangSmith가 전송 요청을 수락하지 않았습니다. 설정을 확인해 주세요.")
    return message + (" 일부 단계는 이미 수락됐을 수 있습니다." if sent_count else "")


def _close_export_client(client):
    try:
        client.close()
    except Exception:
        pass


def _send_export_payloads(payloads, settings, reply, client):
    """Send once within a shared deadline; close only a client created here."""
    headers = {"x-api-key": settings["api_key"], "Content-Type": "application/json"}
    if settings["workspace_id"]:
        headers["x-tenant-id"] = settings["workspace_id"]
    owned = client is None
    sent_count = 0
    deadline = time.perf_counter() + _EXPORT_DEADLINE_SECONDS
    try:
        if owned:
            client = httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0), follow_redirects=False,
                                  trust_env=False, transport=httpx.HTTPTransport(retries=0, trust_env=False))
        for payload in payloads:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return dict(reply, status="error", message="학습 기록 전송의 전체 시간 제한을 넘었습니다. 일부 요청이 수락됐을 수 있으며 자동 재전송하지 않았습니다.")
            response = client.post(_ENDPOINTS[settings["region"]], headers=headers, json=payload, follow_redirects=False,
                                   timeout=httpx.Timeout(min(5.0, remaining), connect=min(3.0, remaining)))
            if not 200 <= response.status_code < 300:
                return dict(reply, status="error", message=_export_rejection_message(response.status_code, sent_count))
            sent_count += 1
    except (httpx.HTTPError, OSError):
        return dict(reply, status="error", message="LangSmith 연결 또는 응답 시간 제한 오류입니다. 일부 요청이 수락됐을 수 있으며 자동 재전송하지 않았습니다.")
    except Exception:
        return dict(reply, status="error", message="학습 기록 전송을 완료하지 못했습니다. 계산 결과는 유지됩니다.")
    finally:
        if owned and client is not None:
            _close_export_client(client)
    return dict(reply, status="sent", message=f"완료된 학습 기록 {sent_count}개 전송 요청이 수락됐습니다. 클라우드 저장 결과는 다시 조회하지 않았습니다.")


def export_study_trace(trace, config, *, client=None):
    """Explicit button action only. POST complete allowlisted spans, never auto trace.

    `client` is an injectable HTTPX client for deterministic offline tests. Default
    transport ignores environment proxies, disables redirects/retries, and has no
    LangSmith SDK runtime metadata or sampling/background callbacks.
    """
    settings, reply = _prepare_export_settings(config)
    if settings is None:
        return reply
    safe = _validated_trace(trace)
    if safe is None:
        return dict(reply, status="blocked", message="변경하지 않은 내장 더미로 이번 실행에서 만든 완료 기록만 전송할 수 있습니다. 사례를 다시 실행해 주세요.")
    reply["run_id"] = safe["run_id"]
    payloads = _export_payloads(safe, settings["project"])
    return _send_export_payloads(payloads, settings, reply, client)
