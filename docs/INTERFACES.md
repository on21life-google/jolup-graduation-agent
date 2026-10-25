# 모듈 연결 계약 v2

2024학번 컴퓨터공학과 전공심화, 공학인증 제외. 프로젝트 루트에서 실행. 아래 팀원 이름은 담당 계획이며 개인별 완료 기여는 팀원 자료 병합 후 확인한다.
업무 결과는 JSON으로 직렬화 가능한 dict/list로 연결한다. CSV 입력의 bytes/str와 ICS 결과의 bytes는 별도 입출력 경계다. 아래는 핵심 필드이며, 통합할 때 반환된 추가 필드를 임의로 버리지 않는다. 학생 업로드는 메모리에만 유지한다.

## 계산 모듈: jolup.academic (윤석현)

- `parse_transcript(data: bytes | str) -> dict`: `{ok, courses, errors, warnings, removed_columns}`. CSV 내용만 입력, 경로 아님. courses는 `row_id,course_id,course_name,term,category,credits,grade,status,retake_of,retake_approved`.
- status: `completed / in_progress / planned / withdrawn`. category: `교필 / 교선 / 전필 / 전선 / 일선`.
- `check_requirements(profile: dict, courses: list[dict]) -> dict`: `{status, answer, rows, totals, missing_required_courses, review_items, trace, evidence, meta, projection}`.
- status: `충족 / 미충족 / 확인 필요`. rows: `{area, required, earned, shortage, unit, status, source_ids}`.
- totals: `{total, general_required, general_elective, major_required, major_total, other, in_progress}`.
- trace: 각 과목 `{row_id, course_name, credits, counted, gpa_counted, reason, category}`. `counted`는 취득 학점 인정, `gpa_counted`는 평점 계산 반영이다. F는 취득 학점 0이어도 평점에 반영될 수 있고, P는 평점에서 제외된다. 미확인 재수강과 승인 대체된 이전 행은 평점에 중복 반영하지 않는다. 표시 사유 문자열을 다시 해석해 평점을 계산하지 않는다. 수치·근거 설명이며 LLM 추론 아님.
- projection: `{status, answer, rows, totals, missing_required_courses, review_items}`. 수강 중 합격을 가정한 예상이며 현재와 분리한다. `review_items`는 전망에서 생긴 재수강 등 확인 필요 사유도 포함한다. 계획 모듈은 이 목록이 있으면 그 전망을 기초로 배치를 확정하지 않고 `확인 필요`로 반환한다.
- `search_scores(courses) -> dict`: `{status, totals, trace, review_items, terms}`와 유효한 입력의 `accepted`. `terms`의 각 행은 `{term, earned_credits, gpa, gpa_credits}`이며 `gpa_counted`인 행만 평점 분모·분자에 사용한다. 평점 대상 학점이 없으면 `gpa=None`이다.

## 수강 계획: jolup.planner (윤석현)

- `search_courses(query='', term=None) -> list[dict]`: data/courses.json 교육과정/모의 개설표 후보.
- `plan_courses(profile, courses, diagnosis=None, preferences=None) -> dict`: `{status, answer, semesters, warnings, remaining_shortages, evidence, meta}`.
- semester: `{term, min_credits, max_credits, total_credits, courses}`; course: `{course_id, course_name, category, credits, reason, offering_status, source_ids}`.
- 각 semester의 `projected_totals`, `projected_rows`, `projected_status`, `projection_assumptions`, `milestone_estimates`는 해당 학기 말 전망이다. 채플·작품의 현재 확인값을 미래 완료로 바꾸지 않으며, 등록 지속 등 가정은 행의 `basis`에 표시한다.
- 향후 학기 실제 개설 미확인 자료는 반드시 `모의 개설`/`교육과정상 후보`로 표기. 수강확정/시간표보장 금지.
- 전공종합설계(2)는 (1) 이수/통과예상을 확인. 계획·수강중 과목을 기취득학점에 포함하지 않음.

## RAG: jolup.rag (김주미)

- `KnowledgeBase()`; `.build_index(force=False)->dict`; `.search(query, profile=None, k=5)->list[dict]`; `.answer(query, profile=None)->dict`.
- `.search()`의 결과에는 `{source_id, chunk_id, context_id, text, file_name, url, registered_date, page, score, retrieval_backend}`가 포함된다. `text`는 실제 모델 토큰 한도에 맞춘 작은 검색 청크이며 `context_id`는 같은 원문 페이지를 연결하는 키다. 최대 5개 페이지에서 강한 청크 하나씩 선택한다. 알 수 없는 등록일은 `None`이다.
- `.answer()`는 `{status, answer, evidence, meta, retrieval, scope, warnings, answer_mode}`를 반환하며 일부 규정을 확정하지 못하면 `unverified_topics`도 포함할 수 있다. `evidence`에는 검색 청크 필드와 함께 전체 원문 페이지인 `context_text`를 붙여 표 머리글·예외 조건을 확인할 수 있게 한다. `meta`는 파일명·URL·등록일·페이지·청크 ID·검색 방식 등 출처 식별 정보이며 원문 문맥을 대신하지 않는다. `retrieval`은 색인·검색 상태 요약, `warnings`는 대체 검색 등의 경고다. 원문 확인으로 보완한 인용의 `citation_origin="source_context"`와 점수는 순수 상위 검색 결과와 구분한다. 증거 없는 질문은 확인 필요로 남기고 자료 속 지시문은 실행하지 않는다.
- sources.json은 자료 메타데이터. ingestion은 sources만 처리하고 사용자 CSV는 색인 금지.
- 벡터DB Chroma + 로컬 다국어 임베딩 FastEmbed를 사용한다. `chunking.TokenChunker`가 실제 토크나이저와 특수 토큰을 포함한 한도를 확인하며, 검색 조각과 전체 페이지 문맥을 분리한다. 모델 다운로드나 벡터 질의 실패에 따른 대체 어휘 검색은 `retrieval_backend`·`warnings`로 명시한다. Chroma의 내부 SQLite 저장과 학생 기록용 별도 SQL DB는 구분하며, MongoDB·Redis는 연결하지 않았다.

## UI / Agent / 일정: 우수진

- `jolup.calendar.search_events(query='', year=2026)->list[dict]`, `preview_events(event_ids)->dict`, `create_calendar_event(events, approved=False)->bytes`.
- event: `{id,title,start_date,end_date,source_id,url}`; end_date는 사용자에게 보이는 마지막 날짜, ICS DTEND는 다음날. 종일일정, UID 안정적, 명시 승인 필수.
- `jolup.agent.respond(query, profile=None, courses=None, context=None)->dict`: LangGraph 라우터, intents `regulation/diagnosis/planning/calendar/unsupported`; `{intent,requested_intents,sections?,answer,status,rows,plan?,events?,evidence,meta,timings,study_trace,memory?,ai_explanation?,llm?}`.
- 목표 준비 상태를 주로 반환하는 계획 응답에서는 `current_status`에 현재 졸업요건 판정을 따로 유지한다. 선택적 AI 설명은 `ExplanationSelection.fact_ids`를 검증한 후 로컬 문장으로 조립하며 `answer/status/rows`를 바꾸지 않는다. 자세한 모델 계약은 [API.md](API.md)를 따른다.
- app.py: Streamlit 샘플/CSV 업로드, 프로필, 채팅, 진단표/계산 trace/계획/출처/일정 미리보기·승인·다운로드. 공통 응답4요소 유지.
- 로컬 `context.conversation`은 `ConversationMemory` 객체다. 최근4턴, 입력·학교자료 내용 fingerprint 변경 시 무효화, 명시적 결과 재표시만 재사용한다. 새로운 후속 질문은 이전 intent를 참고하되 다시 계산한다. 혼합 질문의 `sections`는 영역별 판정·근거를 보존한다.
- 로컬 메모리는 Streamlit session_state, HTTP 메모리는 API 프로세스의 session_id별 객체에 둔다. 초기화 시 학생자료·진단·기억·일정승인을 폐기한다. HTTP의 최대64세션 LRU 제거·프로세스 재시작으로 기억은 사라질 수 있다. 영구 학생DB를 의미하지 않는다.
- `jolup.mcp_server`는 `parse_transcript`, `check_requirements`, `search_courses`, `search_scores`, `create_calendar_event`, `search_regulations`, `search_academic_events`, `plan_courses`, `answer_regulations`의 9개 도구를 노출한다. MCP stdio와 streamable-http 지원.

## 채팅 전송: jolup.chat_client / jolup.study_api

- `respond_chat(query, profile=None, courses=None, context=None, *, client=None)`: `JOLUP_AGENT_API_URL` 미설정이면 기존 `respond()`를 호출한다. 설정하면 `/chat`에 POST하고 실패를 로컬 성공으로 숨기지 않는다. 허용 주소는 http의 loopback 또는 Compose `api`뿐이다.
- `/chat` 요청: `{query, profile?, courses?, session_id?, builtin_sample_id?}`. 추가 필드를 거부하고 `history/result`, API 키, `use_llm`을 받지 않는다. 매 요청에 학생 자료를 다시 전달하며, 생략한 학적 사실을 스키마 기본값으로 채워 계산하지 않는다.
- `/chat` 응답은 기존 Agent 결과를 보존하고 `{session_id, transport_mode:"http", ai_enabled:false}`를 더한다. 서버가 ID를 발급하거나 요청의 유효한 ID를 사용한다. `memory={turn_count,window_size:4,reused,invalidated}`는 집계이고 전체 과거 대화가 아니다.
- `DELETE /sessions/{session_id}` / `reset_chat_session(session_id)`는 `{session_id,cleared:bool}`을 반환한다. 없어진 세션이면 false이며, UI는 삭제 실패도 표시하고 이전 ID를 버린다.
- 오류: 입력 형식은 HTTP422, 실제 Agent/통신 실행 실패는503 또는 `ChatServiceError`다. 학사 자료 부족·미충족은 HTTP200의 도메인 status로 표현한다. API `/health`는 프로세스 응답이며 모든 외부 의존성 준비를 보증하지 않는다.
- HTTP 채팅은 `use_llm=False`; 생성·LangSmith 키를 UI에서 서버로 보내지 않는다. 해당 기능은 로컬 모드에서만 제공한다. Compose 기본4서비스는 app→api→mcp→chroma다. 일부 별도 UI의 도메인 직접 호출은 유지한다.
- 교수 예제의 lifespan MCP 연결 재사용과 달리 기존 `mcp_client.call_domain`은 호출별 연결·해제를 유지한다. API lifespan은 임시 세션 정리를 맡는다. 자유 ReAct 도구 반복이나 영구 사용자 저장소를 추가한 것으로 설명하지 않는다.

[Swagger·Postman 안내](API_EXPLORER.md)의 실제 예제를 사용한다. 최신 로컬 HTTP 실측은 [Postman 21요청·60검증 결과](../reports/postman_validation_20261026.json)다.

## 학습 관찰: jolup.observability

- `StudyRecorder(profile, courses, *, builtin_sample_id=None)`: 입력의 내장 더미 일치 여부를 확인하고 세션에서 사용할 로컬 실행 기록을 만든다. 사례 ID만으로 외부 전송을 허용하지 않는다.
- `recorder.activate()`: 현재 실행 범위의 관찰 컨텍스트. 원본 LangGraph 상태의 자동 외부 추적은 `respond()`의 `tracing_context(enabled=False)`로 계속 차단한다.
- `observe_call(name, function, *args, **kwargs)`: 지정된 함수의 단계·부모 관계·시간·허용 요약을 기록하고 원래 반환값을 유지한다. 인수와 원문 출력 전체는 기록하지 않는다.
- `recorder.finish(result)->dict`: `{schema_version, run_id, case_id, exportable, start_time, end_time, duration_ms, status, steps, summary, truncated, integrity}`. `status`는 실행 성공/오류이며 학업의 충족/미충족과 다르다.
- `steps`: 각 단계의 `id, parent_id, name, label, source_ref, start_time, end_time, duration_ms, status, summary`. `source_ref`는 미리 정한 함수 참조이며 `summary`는 허용된 상태·숫자 집계·공개 출처 ID 등으로 제한한다.
- `export_study_trace(trace, config, *, client=None)->dict`: 명시적 버튼에서만 호출. `config={api_key, project, region, workspace_id}`이며 LangSmith 전용 키를 받는다. 프로젝트 기본값은 `jolup-study`, 리전은 `US/EU`. 변조 여부·현재 프로세스 기록·더미 일치를 확인한 완료 기록만 공식 REST `POST /runs`로 보낸다.
- 전송 결과 `status`: `sent / blocked / disabled / error`. `sent`는 서버의 전송 요청 수락이며 클라우드 저장 결과 재조회는 아니다. 오류에는 일부 단계가 이미 수락된 경우가 포함될 수 있다. 자동 재시도하지 않는다.
- 앱은 내장 더미 선택 출처를 `context.builtin_sample_id`로 전달한다. 업로드·수정 프로필·미확인 출처는 외부 전송 금지. 성공한 `run_id`는 세션에서 중복 전송 차단. 기존 계산·판정·계획을 변경하거나 새 LLM 호출을 만들지 않는다.
- 질문·대화·성적표·프로필·AI 설명·예외 원문과 API 키는 로컬 실행 기록 및 전송 본문에서 제외한다. LangSmith 키는 인증 헤더에만 사용한다. 설정 입력만으로 네트워크 호출을 하지 않는다.
- HTTP 모드는 API 프로세스의 단계 기록 열람만 지원한다. 다른 프로세스의 기록을 로컬 서명 검증으로 승인하지 않으며 LangSmith 키를 API에 전달하지 않는다.
- `app.py._render_study_trace()`가 마지막 대화 응답의 접힌 단계 표·요약·전송 상태를 보여 준다. 상세 사용법은 [LANGSMITH.md](LANGSMITH.md)를 따른다.

## 규칙과 자료

- data/rules/sungkyul_cs_2024.json, data/courses.json, data/events.json은 root 담당.
- `jolup.samples.list_samples()->list[dict]` 각 `{id,label,description}`.
- `jolup.samples.load_sample(id)->dict` 각 `{profile,courses,csv_bytes,expected}`. 11종.
- 키가 없는 경우 로컬 검색/계산 답변임을 명시. 실제 LLM API 호출 결과와 혼동 금지. LLM은 수치를 변경할 권한 없음.
