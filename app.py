"""Local, session-only graduation roadmap demo. Run: streamlit run app.py."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pandas as pd
import streamlit as st

from jolup.chat_client import ChatServiceError, reset_chat_session, respond_chat as respond, transport_mode
from jolup.conversation import ConversationMemory, school_revision
from jolup.evidence import validate_rule_bundle
from jolup.env_export import export_dotenv
from jolup.mcp_client import ServiceError, call_domain, check_requirements, parse_transcript, plan_courses
from jolup.samples import list_samples, load_sample
from jolup.settings import DEFAULT_PROFILE
from jolup.planner import search_courses

from jolup.ui_design import AVATAR, hero_html, panel_title, requirement_cards, steps_html, theme_css
from jolup.ui_calendar import calendar_html, event_rows, month_events, today_seoul

st.set_page_config(page_title="졸업각 · 나 졸업 괜찮아?", page_icon="🐰", layout="wide", initial_sidebar_state="expanded")
st.markdown(theme_css(), unsafe_allow_html=True)


def _reset_conversation():
    session_id = st.session_state.pop("chat_session_id", None)
    if session_id:
        try:
            reset_chat_session(session_id)
        except ChatServiceError:
            st.warning("서버의 이전 대화 삭제를 확인하지 못했습니다. 현재 화면은 새 대화로 시작하며 이전 세션을 다시 사용하지 않습니다.")
    st.session_state["conversation"] = ConversationMemory()
    st.session_state["messages"] = []


def _load(sample_id: str):
    _reset_conversation()
    sample = load_sample(sample_id)
    st.session_state["profile"] = deepcopy(sample["profile"])
    st.session_state["courses"] = deepcopy(sample["courses"])
    st.session_state["csv_bytes"] = sample.get("csv_bytes", b"")
    st.session_state["sample_id"] = sample_id
    st.session_state["study_builtin_sample_id"] = sample_id
    st.session_state["record_label"] = next((x["label"] for x in list_samples() if x["id"] == sample_id), sample_id)
    for key in list(st.session_state):
        if key in ("diagnosis", "plan", "calculation_fingerprint", "messages", "parse_feedback") or key.startswith(("profile_", "approve_", "upload_")):
            del st.session_state[key]
    st.session_state["messages"] = []


samples = list_samples()
if "profile" not in st.session_state:
    initial = next((x["id"] for x in samples if x["id"] == "on_track"), samples[0]["id"])
    _load(initial)


def _df(rows, names=None):
    if not rows:
        st.caption("표시할 항목이 없습니다.")
        return
    df = pd.DataFrame(rows)
    if names:
        df = df[[k for k in names if k in df.columns]].rename(columns=names)
    st.dataframe(df, width="stretch", hide_index=True)


def _diagnosis_table(rows):
    _df(rows, {"area": "영역", "required": "필요", "earned": "이수", "shortage": "부족", "unit": "단위", "status": "상태", "basis": "계산 기준"})


def _source_item(item):
    if not isinstance(item, dict):
        st.write(item)
        return
    label = item.get("title") or item.get("file_name") or item.get("source_id") or "학교 자료"
    page = item.get("page", item.get("pages"))
    st.write(f"{label}" + (f" · PDF {page}쪽" if page else ""))
    details = [item.get("section"), item.get("document_type")]
    if item.get("version_date_hint"):
        details.append("파일명 날짜(참고): " + str(item["version_date_hint"]))
    if any(details):
        st.caption(" · ".join(str(value) for value in details if value))
    excerpt = item.get("text") or item.get("excerpt") or item.get("description")
    if excerpt:
        st.caption(str(excerpt)[:1600])
    if item.get("context_text"):
        with st.expander(f"{label} · 표 머리글·예외를 포함한 원문 문맥"):
            st.text(item["context_text"])


def _source_metadata(meta):
    if isinstance(meta, dict):
        meta = [meta]
    if meta:
        normalized = []
        for item in meta:
            if not isinstance(item, dict):
                continue
            normalized.append({"파일명": item.get("file_name", item.get("filename", item.get("source_id", "미확인"))),
                               "주소": item.get("url") or "제공된 로컬 자료",
                               "등록일": item.get("registered_date") or "미확인",
                               "문서 종류": item.get("document_type") or "미확인",
                               "조문": item.get("section") or "해당 없음",
                               "파일명 날짜(참고)": item.get("version_date_hint") or "미확인"})
        _df(normalized)
    else:
        st.caption("출처 정보 없음 — 등록일은 파일명이나 수정일로 추정하지 않습니다.")


def _sources(result):
    st.markdown("**③ 근거자료**")
    evidence = result.get("evidence", [])
    if evidence:
        for item in evidence:
            _source_item(item)
    else:
        st.caption("이 응답에서 확인된 근거자료가 없습니다.")
    st.markdown("**④ meta · 출처 정보**")
    _source_metadata(result.get("meta", []))


def _render_semester(semester):
    term = semester.get("term", "학기 미확인")
    with st.container(border=True):
        limits = f"신청 기준 {semester['min_credits']}~{semester.get('max_credits', 18)}학점" if "min_credits" in semester else f"상한 {semester.get('max_credits', 18)}학점"
        st.markdown(f"**{term} · 수강 후보 {semester.get('total_credits', 0)}학점 · {limits}**")
        if semester.get("courses"):
            _df(semester["courses"], {"course_name": "과목", "category": "구분", "credits": "학점", "reason": "추천 이유", "offering_status": "개설 확인"})
        else:
            st.caption("확인된 조건으로 배치한 수강 후보가 없습니다. 등록 유지·채플·졸업작품 심사와 아래의 남은 요건을 별도로 확인하세요.")
        if semester.get("projected_rows"):
            with st.expander(f"{term} 학기 말 예상 진단표"):
                for assumption in semester.get("projection_assumptions", []):
                    st.caption(assumption)
                _diagnosis_table(semester["projected_rows"])


def _render_plan(plan):
    st.write(plan.get("answer", "수강 계획을 확인할 자료가 부족해요."))
    for warning in plan.get("warnings", []):
        st.warning(warning)
    for semester in plan.get("semesters", []):
        _render_semester(semester)
    st.caption("교육과정과 모의 개설 자료로 만든 후보입니다. 실제 개설 여부·시간표·수강 여석은 수강신청 전에 확인해야 합니다.")
    milestones = plan.get("milestones", [])
    if milestones:
        st.markdown("**학점 외에 완료할 요건**")
        _df(milestones, {"item": "요건", "remaining": "남은 수", "action": "준비할 일"})
    if plan.get("remaining_shortages"):
        st.markdown("**계획을 모두 이행해도 남는 부족분**")
        _diagnosis_table(plan["remaining_shortages"])
    if plan.get("assumptions"):
        with st.expander("이 계획이 성립하는 조건"):
            for condition in plan["assumptions"]:
                st.write("• " + str(condition))


def _count_value(value):
    """An unknown count stays empty; it must not silently become zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 12 else None


def _render_study_steps(trace):
    """Show the execution hierarchy and one already-sanitized step summary."""
    run_id = str(trace.get("run_id", ""))
    by_id = {step["id"]: step for step in trace["steps"]}
    rows = []
    for step in trace["steps"]:
        parent = by_id.get(step.get("parent_id"), {})
        rows.append({"단계": step.get("label", step.get("name", "")),
                     "상위 단계": "전체 응답" if step.get("parent_id") == run_id else parent.get("label", parent.get("name", "—")),
                     "함수·파일": step.get("source_ref", ""),
                     "소요 ms": step.get("duration_ms", 0),
                     "상태": step.get("status", "")})
    _df(rows)
    selected_step = st.selectbox(
        "살펴볼 실행 단계", list(by_id),
        format_func=lambda step_id: by_id[step_id].get("label", by_id[step_id].get("name", step_id)),
        key=f"study_step_{run_id}",
    )
    st.json(by_id[selected_step].get("summary", {}), expanded=True)


def _study_export_config():
    return {"api_key": st.session_state.get("langsmith_api_key", "").strip(),
            "project": st.session_state.get("langsmith_project", "jolup-study").strip() or "jolup-study",
            "region": st.session_state.get("langsmith_region", "US"),
            "workspace_id": st.session_state.get("langsmith_workspace", "").strip()}


def _study_export_status(trace, config):
    """Retain only a fixed status; transport errors can contain credentials."""
    try:
        from jolup.observability import export_study_trace
        status = export_study_trace(trace, config).get("status", "error")
        return status if status in ("sent", "blocked", "disabled", "error") else "error"
    except Exception:
        return "error"


def _render_study_export_feedback(run_id, sent, attempts):
    if run_id in sent:
        st.success(f"전송 요청 수락 · 프로젝트 {sent[run_id]['project']} · 실행 ID {run_id}")
        st.caption("클라우드 저장 결과는 다시 조회하지 않았습니다. LangSmith에서 해당 프로젝트와 실행 ID를 확인하세요.")
        st.caption("같은 실행의 중복 전송을 막았습니다. 다른 사례를 비교하려면 새 질문으로 실행 기록을 만드세요.")
    elif attempts.get(run_id, {}).get("status") == "blocked":
        st.warning("기본 더미 기록과 키·프로젝트·리전·워크스페이스 설정을 확인해 주세요. 기본 더미를 다시 불러와 질문한 뒤 올바른 LangSmith 설정으로 시도하세요.")
    elif attempts.get(run_id, {}).get("status") == "disabled":
        st.info("LangSmith 연결 설정을 확인해 주세요. 입력만으로 전송되지는 않으며, 준비되면 전송 버튼을 다시 누르세요.")
    elif attempts.get(run_id, {}).get("status") == "error":
        st.warning("전송 요청을 완료하지 못했습니다. 일부 단계는 이미 전송됐을 수 있습니다. 키·프로젝트·연결을 확인한 뒤 전송 버튼으로 다시 시도하세요. 자동으로 재전송하지 않습니다.")


def _render_study_export(trace):
    """Keep click authorization and duplicate protection in one visible place."""
    run_id = str(trace.get("run_id", ""))
    sent = st.session_state.setdefault("study_sent_runs", {})
    attempts = st.session_state.setdefault("study_export_results", {})
    exportable = trace.get("exportable") is True and transport_mode() == "local"
    has_key = bool(st.session_state.get("langsmith_api_key", "").strip())
    if transport_mode() == "http":
        st.info("HTTP 모드에서는 서버 실행 단계를 열람합니다. LangSmith 전송은 로컬 모드에서 지원합니다.")
    elif not exportable:
        st.info("외부 전송은 변경하지 않은 기본 더미 실행만 가능합니다. 업로드 자료·수정한 학생 조건은 로컬에서만 살펴보세요. 이전 세션이라면 기본 더미를 다시 불러온 뒤 질문하세요.")
    elif not has_key:
        st.info("로컬 단계 확인에는 키가 필요하지 않습니다. 전송하려면 사이드바의 ‘LangSmith 학습 기록 · 선택’에 별도의 LangSmith 키를 입력하세요.")
    if st.button("이 더미 실행을 LangSmith로 보내기", key=f"study_export_{run_id}",
                 disabled=not exportable or not has_key or run_id in sent, width="stretch"):
        config = _study_export_config()
        with st.spinner("선택한 더미 실행 기록을 전송하고 있어요…"):
            status = _study_export_status(trace, config)
        attempts[run_id] = {"status": status}
        if status == "sent":
            sent[run_id] = {"project": config["project"], "region": config["region"]}
            st.rerun()
    _render_study_export_feedback(run_id, sent, attempts)


def _render_study_trace(result):
    """Inspect sanitized local steps; export only after this run's button click."""
    trace = result.get("study_trace")
    if not isinstance(trace, dict) or not trace.get("steps"):
        return
    with st.expander("실행 과정 살펴보기"):
        st.caption("실제 호출 단계와 계산 요약입니다. AI의 내부 생각을 기록한 내용은 아닙니다.")
        st.caption("기본 더미 선택 → 질문 → 단계별 요약 확인 → 필요할 때 LangSmith로 전송 순서로 살펴보세요.")
        _render_study_steps(trace)
        st.caption("학습용 기본 더미의 집계·실행 단계만 전송합니다. 질문·성적표 원문, AI 설명 문장과 API 키는 실행 기록에서 제외합니다.")
        _render_study_export(trace)


def _sync_api_model():
    """Keep only provider enums and model choices; never copy a previous key."""
    provider = st.session_state.get("api_provider", "auto")
    if provider not in ("openai", "anthropic"):
        from jolup.llm import resolve_config
        provider = resolve_config({"api_key": st.session_state.get("api_key", ""), "provider": "auto"}).get("provider")
    previous = st.session_state.get("api_model_provider")
    models = st.session_state.setdefault("api_models_by_provider", {})
    current_model = st.session_state.get("api_model", "")
    if provider not in ("openai", "anthropic"):
        return
    if previous in ("openai", "anthropic") and provider != previous:
        models[previous] = current_model
        st.session_state["api_model"] = models.get(provider, "")
    else:
        # Existing live sessions adopt their current selection on first reload.
        models[provider] = current_model
    st.session_state["api_model_provider"] = provider


def _clear_model_list():
    st.session_state.pop("available_models_result", None)
    st.session_state.pop("api_model_list_display", None)
    _sync_api_model()


_sync_api_model()

with st.sidebar:
    st.markdown("### 🐰 졸업각")
    st.caption("성결대학교 · 컴퓨터공학과")
    st.divider()
    st.markdown("**더미 학생 사례**")
    ids = [x["id"] for x in samples]
    labels = {x["id"]: x["label"] for x in samples}
    selected = st.selectbox("시험할 사례", ids, format_func=lambda x: labels[x], key="sample_selector",
                            index=ids.index(st.session_state["sample_id"]) if st.session_state["sample_id"] in ids else 0)
    st.caption(next(x["description"] for x in samples if x["id"] == selected))
    if st.button("사례 불러오기", width="stretch", type="primary"):
        _load(selected)
        st.rerun()
    if st.session_state.get("csv_bytes"):
        st.download_button("현재 사례 CSV 받기", st.session_state["csv_bytes"], "dummy_transcript.csv", "text/csv", width="stretch")
    st.divider()
    with st.expander("CSV 성적표로 시험하기"):
        st.caption("가상 학생 자료만 사용하세요. 파일은 이 대화 세션의 메모리에만 유지됩니다.")
        uploaded = st.file_uploader("더미 성적표 CSV", type=["csv"], key="upload_csv")
        if st.button("CSV 검증 후 적용", disabled=uploaded is None, width="stretch"):
            try:
                parsed = parse_transcript(uploaded.getvalue())
            except ServiceError as exc:
                parsed = {"ok": False, "errors": [str(exc)]}
            st.session_state["parse_feedback"] = parsed
            if parsed.get("ok"):
                st.session_state["courses"] = parsed["courses"]
                st.session_state["study_builtin_sample_id"] = None
                st.session_state["record_label"] = "업로드한 더미 성적표"
                st.session_state["csv_bytes"] = b""
                _reset_conversation()
        feedback = st.session_state.get("parse_feedback", {})
        for msg in feedback.get("errors", []):
            st.error(msg)
        for msg in feedback.get("warnings", []):
            st.warning(msg)
        if feedback.get("ok"):
            st.success(f"{len(feedback.get('courses', []))}개 과목 검증 완료")
        if feedback.get("removed_columns"):
            st.caption("제거한 개인정보 열: " + ", ".join(feedback["removed_columns"]))
    with st.expander("학생 조건 확인·변경"):
        profile = st.session_state["profile"]
        st.caption(f"{profile.get('entry_year', '확인 필요')}학번 · {profile.get('department', '확인 필요')} · {profile.get('track', '확인 필요')} · {profile.get('enrollment_type', '확인 필요')} · 공학인증 제외")
        with st.form("profile_form"):
            st.caption("자료에 없는 학기 수는 빈칸으로 두세요. 0으로 간주하지 않습니다.")
            completed = st.number_input("이수 완료 학기 수", 0, 12, _count_value(profile.get("completed_semesters")), key="profile_completed", placeholder="확인 필요")
            registered = st.number_input("등록 학기 수", 0, 12, _count_value(profile.get("registered_semesters")), key="profile_registered", placeholder="확인 필요")
            chapel = st.number_input("채플 통과 학기 수", 0, 12, _count_value(profile.get("chapel_pass_semesters")), key="profile_chapel", placeholder="확인 필요")
            project_statuses = ["미완료", "통과", "불합격", "확인 필요"]
            project_value = profile.get("graduation_project_status")
            project = st.selectbox("졸업작품 심사", project_statuses, index=project_statuses.index(project_value) if project_value in project_statuses else 3, key="profile_project")
            warning_value = profile.get("academic_warning", False)
            warning_status = st.selectbox("직전 학기 학사경고", ["없음", "있음", "확인 필요"], index=1 if warning_value is True else 0 if warning_value is False else 2, key="profile_academic_warning")
            current = st.text_input("현재 학기", profile.get("current_term", "2026-2"), key="profile_current")
            target = st.text_input("목표 졸업의 마지막 재학 학기", profile.get("target_graduation_term", "2027-2"), key="profile_target")
            st.caption("예: 2027-2까지 이수하면 통상 2028년 2월 졸업 심사 대상입니다. 이 화면은 졸업 승인을 발급하지 않습니다.")
            if st.form_submit_button("학생 조건 적용", width="stretch"):
                profile.update(completed_semesters=completed, registered_semesters=registered,
                               chapel_pass_semesters=chapel, graduation_project_status=project,
                               academic_warning={"없음": False, "있음": True, "확인 필요": None}[warning_status],
                               current_term=current.strip(), target_graduation_term=target.strip())
                _reset_conversation()
    with st.expander("AI 설명 연결 · 선택"):
        if transport_mode() == "http":
            st.info("HTTP 모드의 대화는 FastAPI에서 계산·검색합니다. 이 화면의 API 키는 서버에 보내지 않으며 AI 추가 설명은 로컬 모드에서 지원합니다.")
        st.caption("학점 계산은 항상 동일합니다. AI는 계산 결과에서 만든 문장을 선택하며, 현재 판정과 확인 조건은 유지합니다.")
        api_key = st.text_input("API 키", type="password", key="api_key", on_change=_clear_model_list)
        provider = st.selectbox("서비스", ["auto", "openai", "anthropic"], format_func=lambda x: {"auto":"자동 인식", "openai":"OpenAI", "anthropic":"Claude"}[x], key="api_provider", on_change=_clear_model_list)
        model = st.text_input("모델 · 비우면 기본 모델", key="api_model")
        st.caption("서비스별 모델 선택을 기억합니다. 같은 서비스의 키를 교체하면 현재 모델을 유지합니다.")
        st.text_input("Claude 조직 키용 워크스페이스 ID · 선택", key="api_workspace", help="단일 워크스페이스 키는 비워두세요. 조직 전체 키를 사용하는 경우에만 필요합니다.", on_change=_clear_model_list)
        if st.button("사용 가능한 모델 확인", width="stretch"):
            from jolup.llm import list_available_models
            st.session_state.pop("api_model_list_display", None)
            with st.spinner("이 키로 조회 가능한 모델을 확인하고 있어요…"):
                st.session_state["available_models_result"] = list_available_models({"api_key": api_key, "provider": provider, "workspace_id": st.session_state.get("api_workspace", "").strip() or None})
        available = st.session_state.get("available_models_result")
        if available:
            if available.get("ok"):
                model_ids = available.get("models", [])
                st.success(f"모델 목록 조회 완료 · {len(model_ids)}개")
                st.text_area("조회된 모델 ID", value="\n".join(model_ids), height=180, disabled=True, key="api_model_list_display")
                st.caption("사용할 텍스트 모델 ID를 위의 모델 입력란에 넣으세요. 목록 조회는 답변을 생성하거나 모델을 자동으로 바꾸지 않습니다. 목록에 있어도 이 앱의 API 접근 권한·지원 여부는 실제 질문으로 별도 확인합니다.")
                if available.get("has_more"):
                    st.caption("API 응답에 추가 페이지가 있습니다. 현재 조회한 목록만 표시합니다.")
            else:
                st.warning(available.get("error", "모델 목록을 확인하지 못했습니다."))
        ai_enabled = st.checkbox("대화 답변에 AI 설명 추가", value=False, key="api_enabled", disabled=transport_mode() == "http")
        st.caption("키는 기본적으로 현재 세션에 보관합니다. 의도 분류에는 질문의 유형 신호, 답변 설명에는 집계·학교 근거만 보냅니다. 질문·성적표 원문과 대화 전체는 보내지 않습니다.")
        if api_key:
            from jolup.llm import detect_provider
            detected = detect_provider(api_key) if provider == "auto" else provider
            if detected:
                st.caption(f"선택 서비스: {'Claude' if detected == 'anthropic' else 'OpenAI'}")
            else:
                st.warning("이 키의 서비스는 자동으로 확인할 수 없어요. 서비스를 직접 선택하세요.")
        if st.button("키 포함 .env 다운로드 준비", disabled=not bool(api_key), width="stretch"):
            try:
                env_data = export_dotenv({"api_key": api_key, "provider": provider,
                                          "model": model, "workspace_id": st.session_state.get("api_workspace", "")})
            except ValueError as error:
                st.warning(str(error))
            else:
                st.caption("실제 키가 포함됩니다. 지정한 팀원에게 별도로 전달하세요. Docker Compose는 프로젝트 폴더의 .env를 읽으며, Python 직접 실행은 별도 환경변수 설정이 필요합니다.")
                st.download_button(".env 파일 다운로드", env_data, ".env", "text/plain",
                                   key="download_api_env", on_click="ignore", width="stretch")
    with st.expander("LangSmith 학습 기록 · 선택"):
        st.caption("대화 응답 아래 ‘실행 과정 살펴보기’에서 호출 단계·함수·소요 시간을 확인할 수 있습니다. 로컬 확인에는 키가 필요하지 않습니다.")
        st.text_input("LangSmith API 키", type="password", key="langsmith_api_key",
                      help="답변 생성용 OpenAI·Claude 키와 별개입니다. 현재 세션에만 보관합니다.")
        st.text_input("LangSmith 프로젝트", value="jolup-study", key="langsmith_project")
        st.selectbox("LangSmith 리전", ["US", "EU"], key="langsmith_region")
        st.text_input("LangSmith 워크스페이스 ID · 선택", key="langsmith_workspace")
        st.caption("설정을 입력해도 전송하지 않습니다. 학습 기록에서 ‘이 더미 실행을 LangSmith로 보내기’를 누른 실행만 전송합니다.")
    st.divider()
    if st.button("세션 초기화", width="stretch"):
        _reset_conversation()
        st.session_state.clear()
        st.rerun()
    st.caption("실제 개인정보를 입력하지 마세요. 업로드·대화·API 키의 세션 메모리는 초기화하면 폐기합니다. 직접 내려받은 .env 파일은 별도로 남습니다.")


def _render_chat_result(result):
    st.caption(result.get("execution_mode", "로컬 계산·검색"))
    st.markdown("**① 답변**")
    st.write(result.get("answer", ""))
    st.markdown("**② reason · 진단·확인 결과**")
    if result.get("rows"):
        _diagnosis_table(result["rows"])
    elif result.get("events"):
        _df(result["events"], {"title": "일정", "start_date": "시작일", "end_date": "마지막 날"})
    else:
        st.caption("개별 학생의 학점 진단이 아닌 응답입니다. 근거자료에서 확인한 범위를 표시합니다.")
    if result.get("plan"):
        with st.expander("목표 졸업까지 수강 계획"):
            _render_plan(result["plan"])
    for section in result.get("sections", []):
        label = {"regulation": "학교 규정", "diagnosis": "이수 진단", "planning": "수강 계획", "calendar": "학사일정"}.get(section.get("intent"), "요청한 내용")
        with st.expander(label, expanded=True):
            st.write(section.get("answer", ""))
            if section.get("events"):
                _df(section["events"], {"title": "일정", "start_date": "시작일", "end_date": "마지막 날"})
            _sources(section)
    if result.get("memory", {}).get("reused"):
        st.caption("동일한 학생 조건·학교 자료에서 검증한 최근 결과를 다시 보여드립니다.")
    if result.get("ai_explanation"):
        with st.container(border=True):
            st.caption("AI 추가 설명 · 위 계산표가 기준입니다")
            st.write(result["ai_explanation"])
    if result.get("llm", {}).get("error"):
        st.warning(result["llm"]["error"])
    if result.get("routing", {}).get("error"):
        st.warning("AI 의도 분류를 사용할 수 없어 로컬 분류로 처리했어요. " + result["routing"]["error"])
    with st.expander("③ 근거자료 · ④ meta"):
        _sources(result)


def _sync_course_interests(fingerprint):
    """Discard a reading list belonging to changed student data or rules."""
    if st.session_state.get("interest_fingerprint") != fingerprint:
        for key in list(st.session_state):
            if key.startswith("interest_"):
                del st.session_state[key]
        st.session_state["interest_fingerprint"] = fingerprint
        st.session_state["interest_selected_ids"] = []


def _render_course_interests(fingerprint):
    """An explicitly separate reading list; never mutate the verified plan."""
    _sync_course_interests(fingerprint)
    with st.expander("관심 과목 찾고 담기 · 계획과 별도"):
        st.caption("교육과정 후보를 비교하는 관심 목록입니다. 실제 개설·시간표·여석은 미확인이며, 담아도 현재 진단이나 자동 수강 계획에 반영되지 않습니다.")
        query = st.text_input("관심 과목 검색 · 과목명·구분·내부 ID", key="interest_query")
        category = st.selectbox("관심 과목 구분", ["전체", "교필", "교선", "전필", "전선", "일선"], key="interest_category")
        semester = st.selectbox("관심 과목 개설학기 후보", ["전체", "1학기", "2학기"], key="interest_semester")
        catalog = {item["course_id"]: item for item in search_courses()}
        candidates = [item for item in search_courses(query)
                      if (category == "전체" or item["category"] == category)
                      and (semester == "전체" or int(semester[0]) in item["offered_semesters"])]
        chosen = st.multiselect("관심 목록에 추가할 과목", [item["course_id"] for item in candidates],
                                format_func=lambda key: f"{catalog[key]['course_name']} · {catalog[key]['category']} · {catalog[key]['credits']}학점", key="interest_add")
        if st.button("선택한 과목 관심 목록에 담기", disabled=not chosen):
            st.session_state["interest_selected_ids"] = list(dict.fromkeys(st.session_state["interest_selected_ids"] + chosen))
        selected = [catalog[key] for key in st.session_state["interest_selected_ids"]]
        st.write(f"관심 과목 {len(selected)}개 · 합계 {sum(item['credits'] for item in selected):g}학점")
        if selected:
            _df(selected, {"course_name": "과목", "category": "구분", "credits": "학점", "offering_status": "개설 확인"})
            removed = st.multiselect("관심 목록에서 뺄 과목", [item["course_id"] for item in selected],
                                     format_func=lambda key: catalog[key]["course_name"], key="interest_remove")
            if st.button("선택한 과목 관심 목록에서 빼기", disabled=not removed):
                st.session_state["interest_selected_ids"] = [key for key in st.session_state["interest_selected_ids"] if key not in removed]
                st.rerun()
        if not candidates:
            st.info("검색 조건에 맞는 교육과정 후보가 없습니다.")
        st.caption("학생 조건·성적표·학교 자료가 달라지면 관심 목록도 초기화합니다. 과목 ID는 프로젝트 내부 식별자입니다.")


profile = st.session_state["profile"]
courses = st.session_state["courses"]
fingerprint = hashlib.sha256(json.dumps({
    "profile": profile, "courses": courses, "rule_review": validate_rule_bundle(),
    "school_revision": school_revision(),
}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
if st.session_state.get("calculation_fingerprint") != fingerprint:
    try:
        computed_diagnosis = check_requirements(profile, courses)
        computed_plan = plan_courses(profile, courses, diagnosis=computed_diagnosis)
        # Publish one matching pair only after both operations complete.
        st.session_state.update(diagnosis=computed_diagnosis, plan=computed_plan,
                                calculation_fingerprint=fingerprint)
    except ServiceError as exc:
        st.error(str(exc))
        st.stop()
diagnosis = st.session_state["diagnosis"]
plan = st.session_state["plan"]

st.markdown(hero_html(), unsafe_allow_html=True)
with st.expander("처음 사용 안내 · FAQ"):
    st.markdown("**① 더미 선택·CSV 검증 → ② 현재 졸업요건 확인 → ③ 수강 후보·조건 확인 → ④ 일정 미리보기·승인**")
    st.write("성적표 입력은 현재 CSV를 지원합니다. 수강 중 학점은 이수 완료로 계산하지 않으며, 자료가 없으면 확인 필요로 남깁니다.")
    st.write("결과는 학교 근거와 가상 기록에 따른 준비 안내입니다. 공식 졸업 승인은 학과·학사 시스템에서 확인하세요.")
    st.write("기본 계산·검색은 로컬에서 실행합니다. HTTP 모드는 구성한 프로젝트 서버를 사용하고, 외부 AI는 연결을 켠 경우에만 호출합니다. LangSmith는 기본 더미 기록을 선택해 전송할 때만 사용합니다.")
    st.write("ICS는 날짜를 확인하고 승인한 뒤 내려받아 캘린더에서 직접 가져옵니다. 외부 캘린더에 자동 등록하지 않습니다.")
st.markdown(steps_html(bool(courses)), unsafe_allow_html=True)
st.caption(f"{profile.get('display_name', '가상 학생')} · {st.session_state['record_label']} · 현재 {profile.get('current_term', '확인 필요')} · 목표 마지막 학기 {profile.get('target_graduation_term', '확인 필요')}")
totals = diagnosis.get("totals", {})
col1, col2, col3 = st.columns(3)
col1.metric("현재 확정 이수", f"{totals.get('total', 0)} 학점", "졸업 기준 123학점", delta_color="off")
col2.metric("현재 수강 중", f"{totals.get('in_progress', 0)} 학점", "아직 이수 학점에 미포함", delta_color="off")
col3.metric("전공 이수", f"{totals.get('major_total', 0)} / 63", "주전공 + 전공심화", delta_color="off")
st.progress(min(max(float(totals.get("total", 0)) / 123, 0), 1), text="전체 학점 진행률 · 다른 영역과 비학점 요건도 각각 충족해야 합니다.")

diagnosis_tab, chat_tab, calendar_tab, details_tab = st.tabs(["진단·수강 계획", "대화로 확인", "학사일정", "계산 과정·출처"])
with diagnosis_tab:
    st.subheader("현재 상태와 목표까지의 준비")
    left, right = st.columns(2, gap="large")
    with left:
        with st.container(border=True):
            st.caption("① 결론 · 현재 졸업요건")
            st.markdown(f"### {diagnosis.get('status', '확인 필요')}")
            st.write(diagnosis.get("answer", ""))
    with right:
        with st.container(border=True):
            st.caption("목표 졸업까지 · 수강 계획 기준")
            st.markdown(f"### {plan.get('status', '확인 필요')}")
            st.write(plan.get("answer", ""))
    st.markdown("**② reason · 졸업요건 상태 진단표**")
    st.markdown(requirement_cards(diagnosis.get("rows", [])), unsafe_allow_html=True)
    _diagnosis_table(diagnosis.get("rows", []))
    for item in diagnosis.get("review_items", []):
        st.warning(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
    with st.expander("수강 중 과목을 모두 통과한다면?"):
        projection = diagnosis.get("projection", {})
        st.info("수강 중 과목 합격을 가정한 예상입니다. 현재 이수 완료 학점과 구별해서 확인하세요.")
        st.write(projection.get("answer", ""))
        _diagnosis_table(projection.get("rows", []))
        for item in projection.get("review_items", []):
            st.warning(item)
    st.subheader("남은 학기의 수강 후보")
    _render_plan(plan)
    _render_course_interests(fingerprint)

with chat_tab:
    chat_left, chat_right = st.columns([5, 7], gap="large")
    with chat_left:
        st.markdown(panel_title("💬 AI 에이전트 채팅"), unsafe_allow_html=True)
        st.subheader("학교 근거와 함께 답해드려요")
        st.caption("FastAPI 서버의 계산·검색으로 답합니다. 최근 4회 결과는 서버 세션에 보관합니다." if transport_mode() == "http" else "로컬 계산·검색으로 답합니다. AI 설명 연결을 켠 경우에만 선택한 API를 호출합니다.")
        st.caption("최근 4회 결과를 이어서 확인할 수 있습니다. ‘아까 규정 다시 보여줘’처럼 요청하세요. 학생 조건이나 학교 자료가 달라지면 이전 결과를 재사용하지 않습니다.")
        quick = st.columns(3)
        selected_query = None
        for col, label in zip(quick, ["나 졸업 괜찮아?", "다음 학기 수강 계획을 알려줘", "2026년 졸업 관련 일정 알려줘"]):
            if col.button(label, width="stretch"):
                selected_query = label
        query = st.chat_input("졸업요건, 수강 계획, 학사일정을 물어보세요", max_chars=4000)
        if selected_query or query:
            text = selected_query or query
            config = {"api_key": st.session_state.get("api_key", ""), "provider": st.session_state.get("api_provider", "auto"), "model": st.session_state.get("api_model", "").strip() or None, "workspace_id": st.session_state.get("api_workspace", "").strip() or None}
            previous_intent = next((m["result"]["intent"] for m in reversed(st.session_state.get("messages", [])) if m["result"].get("intent") in {"diagnosis", "planning", "regulation", "calendar"}), None)
            with st.spinner("자료와 계산 결과를 확인하고 있어요…"):
                try:
                    conversation = st.session_state.setdefault("conversation", ConversationMemory())
                    result = respond(text, profile, courses, context={"conversation": conversation, "session_id": st.session_state.get("chat_session_id"), "previous_intent": previous_intent, "use_llm": bool(st.session_state.get("api_enabled")), "llm_config": config if st.session_state.get("api_enabled") else {}, "builtin_sample_id": st.session_state.get("study_builtin_sample_id")})
                    if result.get("session_id"):
                        st.session_state["chat_session_id"] = result["session_id"]
                except Exception:
                    result = {"status": "확인 필요", "answer": "응답을 만드는 중 오류가 발생했어요. 계산·출처 탭의 자료를 확인하고 다시 시도해 주세요.", "rows": [], "evidence": [], "meta": [], "execution_mode": "처리 오류"}
            st.session_state.setdefault("messages", []).append({"query": text, "result": result})
        messages = st.session_state.get("messages", [])
        for index, message in enumerate(messages):
            with st.chat_message("user"):
                st.write(message["query"])
            with st.chat_message("assistant", avatar=str(AVATAR)):
                st.write(message["result"].get("answer", ""))
                if index < len(messages) - 1:
                    with st.expander(f"이전 답변 {index + 1} · 진단표와 근거 다시 보기"):
                        _render_chat_result(message["result"])
        if not st.session_state.get("messages"):
            st.info("위 질문 버튼을 눌러 정상·부족 사례의 답변을 확인해 보세요.")
    with chat_right:
        st.markdown(panel_title("📊 대화에 따라 바뀌는 대시보드"), unsafe_allow_html=True)
        if st.session_state.get("messages"):
            latest = st.session_state["messages"][-1]["result"]
            _render_chat_result(latest)
            _render_study_trace(latest)
        else:
            st.caption("현재 선택한 학생 · 질문을 하면 해당 진단·계획·일정이 이곳에 표시됩니다.")
            st.write(diagnosis.get("answer", ""))
            st.markdown(requirement_cards(diagnosis.get("rows", [])), unsafe_allow_html=True)
            st.caption("현재 확정 이수 기준입니다. 전체 진단표와 앞으로의 계획은 ‘진단·수강 계획’에서 확인하세요.")


with calendar_tab:
    st.subheader("학사일정 미리보기")
    st.caption("학교 자료에서 확인된 날짜만 제공합니다. 미리보기 → 명시 승인 → ICS 다운로드 순서로 진행합니다.")
    year = st.selectbox("조회 연도", [2026, 2027, 2028], key="calendar_year")
    try:
        events = call_domain("search_academic_events", {"year": year})["events"]
    except ServiceError as exc:
        st.error(str(exc))
        events = []
    if not events:
        st.info("이 연도에 확인된 학교 일정이 없습니다. 공식 공지가 제공되면 추가할 수 있습니다.")
    else:
        today = today_seoul()
        default_month = today.month if today.year == year else int(events[0]["start_date"][5:7])
        month = st.selectbox("달력 월", list(range(1, 13)), index=default_month - 1,
                             format_func=lambda value: f"{value}월", key=f"calendar_month_{year}")
        st.markdown(calendar_html(events, year, month, today), unsafe_allow_html=True)
        shown_events = month_events(events, year, month)
        st.caption(f"{today.isoformat()} · 한국 시간 기준 · 출처에 있는 일정만 표시합니다. 날짜를 누르는 것만으로 등록되지 않습니다.")
        if shown_events:
            _df(event_rows(shown_events, today))
        else:
            st.info("이 달에 확인된 학교 일정이 없습니다. 다른 달 또는 아래 전체 일정 목록을 확인하세요.")
        by_id = {x["id"]: x for x in events}
        selected_ids = st.multiselect("파일에 넣을 일정", list(by_id), format_func=lambda x: f"{by_id[x]['start_date']} · {by_id[x]['title']}", key=f"calendar_selection_{year}")
        preview = {"events": [by_id[event_id] for event_id in selected_ids]}
        if preview["events"]:
            _df(preview["events"], {"title": "일정", "start_date": "시작일", "end_date": "마지막 날", "url": "근거 주소"})
            signature = hashlib.sha256(json.dumps(preview["events"], sort_keys=True).encode()).hexdigest()[:16]
            approval = st.checkbox("위 일정의 이름과 날짜를 확인했고, 이 내용으로 ICS 파일 만들기를 승인합니다.", key=f"approve_{signature}")
            try:
                ics = call_domain("create_calendar_event", {"events": preview["events"], "approved": True})["content"].encode("utf-8") if approval else b""
            except ServiceError as exc:
                st.error(str(exc))
                ics = b""
                approval = False
            st.download_button("승인한 일정 ICS 받기", ics, "sungkyul_academic_dates.ics", "text/calendar", disabled=not approval, type="primary")
            st.caption("다운로드한 파일을 사용 중인 캘린더에서 직접 가져오세요. 반복 다운로드의 일정 ID는 동일합니다.")

with details_tab:
    st.subheader("어떤 학점이 인정됐나요?")
    st.caption("계산 코드가 남긴 인정·제외 내역입니다. 취득 학점과 평점 반영은 별개이며, F는 학점 없이 평점에만 반영될 수 있습니다.")
    _df(diagnosis.get("trace", []), {"course_name": "과목", "category": "구분", "credits": "학점", "counted": "학점 인정", "gpa_counted": "평점 반영", "reason": "계산 근거"})
    with st.expander("입력 성적표 보기"):
        _df(courses, {"course_id": "과목 코드", "course_name": "과목", "term": "학기", "category": "구분", "credits": "학점", "grade": "성적", "status": "상태", "retake_of": "재수강 대상", "retake_approved": "재수강 승인"})
    st.subheader("학교 자료와 적용 근거")
    _sources(diagnosis)

st.markdown('<div class="j-footer">졸업각 · UniPath AI 디자인: 우수진 · 학교 근거와 검증된 계산을 연결한 팀 통합본</div>', unsafe_allow_html=True)
