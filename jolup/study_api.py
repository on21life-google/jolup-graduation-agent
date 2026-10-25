"""Inspect the existing domain functions through a small, local HTTP boundary.

Swagger and Postman use this adapter; academic rules remain in the domain modules.
No LLM key, automatic calendar registration, or record persistence is added here.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import asynccontextmanager
from threading import RLock
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import Body, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from jolup.agent import respond
from jolup.calendar import search_events
from jolup.mcp_client import call_domain
from jolup.samples import list_samples, load_sample

Term = Annotated[str, Field(pattern=r"^20\d{2}-[12SW]$")]
ShortText = Annotated[str, Field(min_length=1, max_length=150)]
SessionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
MAX_CHAT_SESSIONS = 64
_sessions: OrderedDict = OrderedDict()
_session_lock = RLock()

# Operation-level examples populate Swagger's editor with real, reproducible input.
# Expected answers stay outside these bodies and never enter a domain calculation.
STUDENT_EXAMPLES = {
    sample_id: {"summary": sample["label"], "description": sample["description"],
                "value": {"profile": sample["profile"], "courses": sample["courses"]}}
    for sample_id in ("on_track", "fully_met", "credit_shortfall", "missing_record")
    for sample in [load_sample(sample_id)]
}
SAMPLE_PATH_EXAMPLES = {
    sample["id"]: {"summary": sample["label"], "description": sample["description"], "value": sample["id"]}
    for sample in list_samples()
}
REGULATION_EXAMPLES = {
    "total_credits": {"summary": "총 졸업학점 · 200 근거 확인", "description": "프로필을 생략한 기본 규정 질문. 가짜 string 프로필을 보내지 않습니다.",
                      "value": {"query": "2024학번 컴퓨터공학과 전공심화 졸업 총학점은?"}},
    "with_profile": {"summary": "학생 조건과 함께 질문 · 200 근거 확인", "description": "지원되는 가상 학생 프로필을 명시한 같은 질문입니다.",
                     "value": {"query": "졸업 총학점은?", "profile": load_sample("on_track")["profile"]}},
    "unverified_date": {"summary": "미확정 미래 날짜 · 200 확인 필요", "description": "자료가 없는 미래 날짜를 추측하지 않는지 확인합니다. 확인 필요는 정상적인 학사 응답입니다.",
                        "value": {"query": "2028년 2월 졸업식 날짜는?"}},
}
CALENDAR_EXAMPLES = {
    "approval_required": {"summary": "기본 · 승인 전 확인 → 403 (의도된 차단)",
                          "description": "학교 자료의 실제 일정입니다. 먼저 제목과 날짜를 읽으세요. approved=false이므로 파일 생성이 차단되는 것이 정상입니다.",
                          "value": {"events": search_events("성적", 2026)[-1:], "approved": False}},
    "explicit_approval": {"summary": "내용 확인·승인 후 선택 → 200 ICS",
                          "description": "이 예제를 선택해 실행하면 아래 일정의 로컬 ICS 내보내기를 승인합니다. 외부 캘린더에는 등록하지 않습니다.",
                          "value": {"events": search_events("성적", 2026)[-1:], "approved": True}},
}


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StudentProfile(InputModel):
    """Unknown non-credit facts stay null; they are not silently counted as zero."""
    school: ShortText
    department: ShortText
    entry_year: int = Field(ge=2000, le=2099)
    track: ShortText
    enrollment_type: ShortText
    current_term: Term
    target_graduation_term: Term
    completed_semesters: int | None = Field(default=None, ge=0, le=30)
    registered_semesters: int | None = Field(default=None, ge=0, le=30)
    chapel_pass_semesters: int | None = Field(default=None, ge=0, le=30)
    graduation_project_status: str | None = Field(default=None, max_length=50)
    engineering_accreditation: StrictBool = False
    academic_warning: StrictBool | None = None
    is_dummy: StrictBool = True
    scenario_id: str = Field(default="", max_length=100)
    display_name: str = Field(default="", max_length=100)
    student_id: str = Field(default="", max_length=100)
    profile_note: str = Field(default="", max_length=1000)


class Course(InputModel):
    row_id: ShortText
    course_id: ShortText
    course_name: ShortText
    term: Term
    category: Literal["교필", "교선", "전필", "전선", "일선"]
    credits: float = Field(ge=0, le=21, multiple_of=0.5)
    grade: str = Field(max_length=10)
    status: Literal["completed", "in_progress", "planned", "withdrawn"]
    retake_of: str = Field(default="", max_length=150)
    retake_approved: StrictBool = False


class DiagnosisRequest(InputModel):
    profile: StudentProfile
    courses: list[Course] = Field(max_length=2000, description="성적표 행. 수강 중 학점은 현재 취득 학점에 포함하지 않습니다.")


class PlanPreferences(InputModel):
    max_credits: int = Field(default=18, ge=1, le=21, description="일반 희망 상한은 1~18입니다. 19~21은 도메인에서 초과 승인 확인 필요로 응답하며, 학사경고·연간 제한도 기존 계산기가 확인합니다.")


class PlanRequest(DiagnosisRequest):
    preferences: PlanPreferences | None = None


class RegulationRequest(InputModel):
    query: str = Field(min_length=1, max_length=1000, examples=["2024학번 컴퓨터공학과 전공심화 졸업 총학점은?"])
    profile: StudentProfile | None = None

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("질문을 입력하세요.")
        return value.strip()


class ChatRequest(InputModel):
    """A browser session owns its ID; records are supplied afresh on every call."""
    query: str = Field(min_length=1, max_length=4000)
    profile: StudentProfile | None = None
    courses: list[Course] = Field(default_factory=list, max_length=2000)
    session_id: SessionId | None = None
    builtin_sample_id: str | None = Field(default=None, max_length=100)

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("질문을 입력하세요.")
        return value.strip()


class DomainResult(BaseModel):
    """Keep extra domain details visible instead of removing their learning trace."""
    model_config = ConfigDict(extra="allow")
    status: str
    answer: str
    evidence: list[dict[str, Any]]
    meta: list[dict[str, Any]]


class ChatResponse(DomainResult):
    """Preserve the agent's diagnosis, evidence, sections and learning trace."""
    intent: str
    rows: list[dict[str, Any]]
    session_id: SessionId
    transport_mode: Literal["http"]
    ai_enabled: Literal[False] = Field(description="HTTP chat uses local routing/calculation; frontend API keys are never transferred.")


class RequirementRow(BaseModel):
    model_config = ConfigDict(extra="allow")
    area: str
    required: float
    earned: float | None
    shortage: float | None
    unit: str
    status: str
    source_ids: list[str]


class DiagnosisResponse(DomainResult):
    rows: list[RequirementRow] = Field(description="영역/필요/이수/부족 진단표. 학점과 비학점 조건을 함께 확인합니다.")
    totals: dict[str, float]
    projection: dict[str, Any] = Field(description="현재 수강 중 과목을 통과한다는 별도 예상. 현재 확정 사실이 아닙니다.")
    review_items: list[str]
    official_graduation_confirmed: Literal[False]


class PlanResponse(DomainResult):
    semesters: list[dict[str, Any]]
    warnings: list[str]
    remaining_shortages: list[dict[str, Any]]
    assumptions: list[str]


class RegulationResponse(DomainResult):
    warnings: list[str]


class SampleSummary(BaseModel):
    id: str
    label: str
    description: str


class SampleResponse(SampleSummary):
    profile: StudentProfile
    courses: list[Course]
    expected: dict[str, Any] = Field(description="학습용 더미의 기대 정답. 진단 요청에는 profile과 courses만 전달합니다.")


class AcademicEvent(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    id: ShortText
    title: ShortText
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    source_id: ShortText
    url: str | None = Field(default=None, max_length=2000)


class EventsResponse(BaseModel):
    status: str
    events: list[AcademicEvent]
    unknown_ids: list[str]
    approval_required: Literal[True]
    message: str


class CalendarRequest(InputModel):
    events: list[AcademicEvent] = Field(min_length=1, max_length=100, description="GET /events에서 확인한 일정 객체를 그대로 전달합니다.")
    approved: StrictBool = Field(default=False, description="표시된 일정에 대한 명시 승인. true일 때만 ICS를 반환합니다.")


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    docs: str


class ErrorResponse(BaseModel):
    detail: str
    errors: list[dict[str, Any]] = Field(default_factory=list)


@asynccontextmanager
async def chat_lifespan(_app):
    """Only in-memory sessions live with this API process; no student database."""
    with _session_lock:
        _sessions.clear()
    yield
    with _session_lock:
        _sessions.clear()


def _conversation(session_id):
    from jolup.conversation import ConversationMemory
    conversation = _sessions.pop(session_id, None)
    if conversation is None:
        conversation = ConversationMemory()
    _sessions[session_id] = conversation
    if len(_sessions) > MAX_CHAT_SESSIONS:
        _sessions.popitem(last=False)
    return conversation


CHAT_EXAMPLES = {
    "diagnosis": {"summary": "현재 진단과 목표 계획 · AI 호출 없음",
                  "description": "학교 근거와 가상 학생을 계산합니다. 현재90학점 미충족, 목표까지 조건부 가능을 구분합니다.",
                  "value": dict(STUDENT_EXAMPLES["on_track"]["value"], query="나 졸업 괜찮아?")},
    "regulation": {"summary": "학생 기록 없이 공개 규정 질문",
                   "description": "RAG 경로를 실행하고 근거와 meta를 반환합니다. 외부 LLM 키는 받지 않습니다.",
                   "value": {"query": "2024학번 컴퓨터공학과 전공심화 졸업 총학점은?"}},
}


app = FastAPI(
    title="졸업로드맵 Agent·학습 API", version="1.1.0", lifespan=chat_lifespan,
    description="2024학번 컴퓨터공학과 더미 자료로 요청→기존 계산기→응답을 학습합니다. "
                "샘플을 받은 뒤 profile과 courses를 진단·계획에 전달하세요. "
                "현재 요건 충족과 미래 준비 상태는 별개이며, 공식 졸업 판정을 대신하지 않습니다. "
                "/chat은 Streamlit과 Agent를 연결하며 외부 LLM을 호출하지 않습니다. API 키를 요청에 넣지 마세요. "
                "세션별 최근4턴은 프로세스 메모리에만 남고 초기화·재시작·64세션 초과 시 지워집니다. "
                "각 요청의 Examples에서 실제 더미 예제를 선택하세요. 200은 요청 처리 성공이며 충족·미충족·확인 필요는 응답 status로 구별합니다. "
                "ICS 기본 예제는 승인 전이므로 403이 의도된 결과입니다.",
    servers=[{"url": "/", "description": "현재 Swagger를 연 서버의 주소·포트"}],
    responses={422: {"model": ErrorResponse, "description": "입력 형식·값 오류"},
               503: {"model": ErrorResponse, "description": "도메인 서비스 처리 실패"}},
)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, exc: RequestValidationError):
    # Give actionable hints without echoing student values or exception context.
    messages = {"missing": "필수 항목을 입력하세요.", "float_type": "학점은 문자열 대신 숫자로 입력하세요.",
                "int_type": "정수를 입력하세요.", "bool_type": "문자열·숫자 대신 true 또는 false를 입력하세요.",
                "list_type": "대괄호로 감싼 목록을 입력하세요.", "model_type": "필드 이름과 값이 있는 JSON 객체를 입력하세요.",
                "json_invalid": "JSON 문법을 확인하세요. 따옴표·쉼표·괄호가 올바른지 확인하세요.",
                "extra_forbidden": "지원하지 않는 항목입니다. 요청 예제와 스키마를 확인하세요."}
    errors = [{"location": list(error["loc"]), "type": error["type"],
               "message": messages.get(error["type"], "입력 형식과 허용 범위를 확인하세요.")} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": "입력 형식과 값을 확인하세요. Swagger의 요청 스키마를 참고하세요.", "errors": errors})


def invoke(name: str, arguments: dict):
    """Map errors at the transport boundary without leaking backend exception text."""
    try:
        return call_domain(name, arguments)
    except PermissionError:
        raise HTTPException(403, "일정 미리보기 확인 후 명시적인 승인이 필요합니다.") from None
    except ValueError:
        raise HTTPException(422, "입력 자료를 확인하세요. 일정은 미리보기의 원본 값을 사용해야 합니다.") from None
    except Exception:
        raise HTTPException(503, "요청을 처리하지 못했습니다. 입력 자료와 로컬 서비스 상태를 확인하세요.") from None


@app.get("/health", response_model=HealthResponse, tags=["시작"], summary="API 프로세스 상태 확인")
def health():
    """프로세스 응답 확인이며 RAG 인덱스·외부 MCP 준비 상태를 보증하지 않습니다."""
    return {"status": "ok", "service": "jolup-study-api", "docs": "/docs"}


@app.post("/chat", response_model=ChatResponse, tags=["Agent"],
          summary="질문을 Agent로 처리 · 세션별 최근4턴 · AI 호출 없음")
def chat(body: Annotated[ChatRequest, Body(openapi_examples=CHAT_EXAMPLES)]):
    data = body.model_dump(exclude_unset=True)
    session_id = body.session_id or uuid4().hex
    with _session_lock:
        context = {"conversation": _conversation(session_id), "use_llm": False,
                   "builtin_sample_id": body.builtin_sample_id}
        try:
            result = respond(body.query, data.get("profile"), data.get("courses", []), context=context)
        except Exception:
            raise HTTPException(503, "Agent 요청을 처리하지 못했습니다. 로컬 서비스 상태를 확인하세요.") from None
    if result.get("error_code") == "execution_error":
        raise HTTPException(503, "Agent 실행 단계가 실패했습니다. MCP와 근거 자료 상태를 확인하세요.")
    return dict(result, session_id=session_id, transport_mode="http", ai_enabled=False)


@app.delete("/sessions/{session_id}", tags=["Agent"], summary="해당 대화 세션의 임시 기억 삭제")
def reset_session(session_id: SessionId):
    with _session_lock:
        cleared = _sessions.pop(session_id, None) is not None
    return {"session_id": session_id, "cleared": cleared}


@app.get("/samples", response_model=list[SampleSummary], tags=["시작"], summary="가상 학생 사례 11종")
def samples():
    return list_samples()


@app.get("/samples/{sample_id}", response_model=SampleResponse, tags=["시작"],
         summary="사례의 프로필·성적표·기대 정답 받기", responses={404: {"model": ErrorResponse}})
def sample(sample_id: Annotated[str, Path(examples=["on_track"], openapi_examples=SAMPLE_PATH_EXAMPLES,
                                        description="예제에서 on_track 등을 선택하세요. 임의의 string은 등록된 사례가 아닙니다.")]):
    try:
        result = load_sample(sample_id)
    except ValueError:
        raise HTTPException(404, "알 수 없는 더미 사례입니다. GET /samples 목록을 확인하세요.") from None
    return {key: value for key, value in result.items() if key != "csv_bytes"}


@app.post("/diagnosis", response_model=DiagnosisResponse, tags=["학사 계산"], summary="현재 이수 요건 진단",
          responses={200: {"description": "예: on_track → status=미충족, totals.total=90, projection.totals.total=105. fully_met → status=충족, totals.total=123."}})
def diagnosis(body: Annotated[DiagnosisRequest, Body(openapi_examples=STUDENT_EXAMPLES)]):
    """샘플 ID를 판정하는 것이 아니라 요청으로 받은 profile과 courses를 계산합니다."""
    # Omitted facts must remain omitted; schema defaults are not student evidence.
    return invoke("check_requirements", body.model_dump(exclude_unset=True))


@app.post("/plans", response_model=PlanResponse, tags=["학사 계산"], summary="목표 졸업까지 조건부 계획 계산",
          responses={200: {"description": "예: on_track → status=조건부 가능. credit_shortfall → status=위험. assumptions와 warnings에서 합격 가정·실제 개설 미확인을 함께 읽으세요."}})
def plans(body: Annotated[PlanRequest, Body(openapi_examples=STUDENT_EXAMPLES)]):
    """현재 진단을 다시 계산합니다. 미래 개설·합격·비학점 요건은 확정 사실이 아닙니다."""
    return invoke("plan_courses", body.model_dump(exclude_unset=True))


@app.post("/regulations", response_model=RegulationResponse, tags=["학교 자료"], summary="출처를 포함한 규정 답변")
def regulations(body: Annotated[RegulationRequest, Body(openapi_examples=REGULATION_EXAMPLES)]):
    """기존 로컬 RAG를 재사용합니다. 인덱스가 없으면 기존 검색기의 대체 동작과 경고를 그대로 반환합니다."""
    return invoke("answer_regulations", body.model_dump(exclude_unset=True))


@app.get("/events", response_model=EventsResponse, tags=["일정"], summary="근거가 있는 학사일정 미리보기")
def events(query: str = Query(default="성적", max_length=200), year: int = Query(default=2026, ge=2000, le=2099)):
    return invoke("search_academic_events", {"query": query, "year": year})


@app.post("/calendar/ics", response_class=Response, tags=["일정"], summary="미리보기 승인 후 ICS 받기",
          responses={200: {"description": "승인한 일정의 ICS. 외부 캘린더에는 자동 등록하지 않습니다.",
                           "content": {"text/calendar": {"schema": {"type": "string"}}}},
                     403: {"model": ErrorResponse, "description": "명시 승인 없음"}})
def calendar_ics(body: Annotated[CalendarRequest, Body(openapi_examples=CALENDAR_EXAMPLES)]):
    if not body.approved:
        raise HTTPException(403, "일정 미리보기 확인 후 명시적인 승인이 필요합니다.")
    result = invoke("create_calendar_event", body.model_dump())
    return Response(result["content"], media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="sungkyul_academic_dates.ics"'})


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description="졸업로드맵 Swagger·Postman 학습 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    uvicorn.run("jolup.study_api:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
