"""Bounded LangGraph orchestration; all academic arithmetic stays in Python."""
from __future__ import annotations

import re
from copy import deepcopy
import time
from functools import lru_cache
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from jolup.conversation import ConversationMemory
from jolup.mcp_client import call_domain, check_requirements, plan_courses, service_mode
from jolup.observability import StudyRecorder, observe_call


class AgentState(TypedDict, total=False):
    query: str
    profile: dict
    courses: list[dict]
    context: dict
    intent: str
    result: dict
    routing: dict
    requested_intents: list[str]
    remaining_intents: list[str]
    results: dict


def detect_intent(query: str) -> str:
    q = re.sub(r"\s+", " ", str(query).strip().lower())
    if not q or len(q) > 4000:
        return "unsupported"
    dangerous = ("비밀번호", "api 키 알려", "api키 알려", "시스템 프롬프트", "시스템프롬프트",
                 "학점 조작", "성적 조작", "성적조작", "증명서 위조", "수강신청 해줘",
                 "포털 로그인", "이전 지시 무시", "ignore previous", "타인 성적", "남의 성적")
    if any(x in q for x in dangerous):
        return "unsupported"
    if any(x in q for x in ("일정", "캘린더", "ics", "언제", "날짜", "마감")):
        return "calendar"
    academic_recommendation = "추천" in q and any(x in q for x in ("과목", "수강", "강의", "전공", "학기"))
    if academic_recommendation or any(x in q for x in ("수강 계획", "수강계획", "들을 과목", "들어야", "다음 학기", "다음학기", "남은 학기")):
        return "planning"
    readiness = "졸업" in q and any(x in q for x in ("괜춘", "할 수", "할수", "될까", "되나", "돼?", "돼요"))
    if readiness or any(x in q for x in ("괜찮", "진단", "내 학점", "나 졸업", "내 졸업", "졸업 가능", "졸업가능", "내 성적", "부족", "충족", "계산해")):
        return "diagnosis"
    if any(x in q for x in ("규정", "학칙", "졸업", "학점", "교필", "교선", "전공", "재수강", "채플", "편입", "필수", "공학인증", "수강", "계절학기")):
        return "regulation"
    return "unsupported"


def _base(answer: str, status: str = "확인 필요") -> dict:
    return {"status": status, "answer": answer, "rows": [], "evidence": [], "meta": []}


def _ai_route(query, config, state, previous, intent, routing):
    from jolup import llm
    classify = getattr(llm, "classify_intent", None)
    if classify is None:
        return intent, routing
    classified = observe_call("classify_intent", classify, query, config,
                              profile=state.get("profile"), previous_intent=previous)
    candidate = classified.get("intent")
    if classified.get("used") and candidate in {"diagnosis", "planning", "regulation", "calendar", "unsupported"}:
        return candidate, {"mode": "AI 의도 분류", "used": True}
    if classified.get("error"):
        routing["error"] = classified["error"]
    return intent, routing


def _route(state: AgentState) -> dict:
    query = state["query"]
    replay = (state.get("context") or {}).get("_replay")
    if replay is not None:
        return {"intent": replay["intent"], "routing": {"mode": "이전 결과 재사용", "used": False},
                "requested_intents": replay.get("requested_intents", [replay["intent"]]),
                "remaining_intents": ["reuse"], "results": {}}
    intent = detect_intent(query)
    context = state.get("context") or {}
    previous = context.get("previous_intent")
    q = re.sub(r"\s+", " ", query.strip())
    followup = any(x in q for x in ("그럼", "그러면", "그건", "이것", "다 채울", "보완", "어떻게 해", "더 알려"))
    if intent == "unsupported" and followup and previous in {"diagnosis", "planning", "regulation", "calendar"}:
        intent = "planning" if previous in {"diagnosis", "planning"} and any(x in q for x in ("채울", "보완", "어떻게")) else previous
    routing = {"mode": "로컬 의도 분류", "used": False}
    config = context.get("llm_config") or {}
    if context.get("use_llm", bool(config.get("api_key"))) and not _prohibited(query):
        intent, routing = _ai_route(query, config, state, previous, intent, routing)
    if _prohibited(query):
        intent = "unsupported"
    requested = _requested_intents(query, intent)
    primary = next((name for name in ("planning", "diagnosis") if name in requested),
                   intent if intent in requested else requested[0])
    return {"intent": primary, "routing": routing, "requested_intents": requested,
            "remaining_intents": requested, "results": {}}


def _requested_intents(query: str, primary: str) -> list[str]:
    """Combine explicit domain requests; a planning node already includes diagnosis."""
    if not query.strip() or len(query) > 4000 or _prohibited(query):
        return ["unsupported"]
    q = re.sub(r"\s+", "", query)
    signals = {
        "regulation": any(word in q for word in ("규정", "학칙")),
        "diagnosis": any(word in q for word in ("괜찮", "진단", "내학점", "나졸업", "내졸업", "내성적", "부족", "충족", "계산해")),
        "planning": any(word in q for word in ("수강계획", "들을과목", "다음학기", "남은학기")),
        "calendar": any(word in q for word in ("일정", "캘린더", "ics", "언제", "날짜", "마감")),
    }
    selected = {name for name, found in signals.items() if found}
    if primary != "unsupported":
        selected.add(primary)
    if "planning" in selected:
        selected.discard("diagnosis")
    return [name for name in signals if name in selected] or ["unsupported"]


def _prohibited(query: str) -> bool:
    q = re.sub(r"\s+", "", str(query).lower())
    return any(x in q for x in ("비밀번호", "api키", "시스템프롬프트", "학점조작", "성적조작", "증명서위조", "수강신청해줘", "포털로그인", "이전지시무시", "ignoreprevious", "타인성적", "남의성적"))


def _need_student(state: AgentState) -> dict | None:
    if not state.get("profile") or not state.get("courses"):
        return _base("졸업 상태를 계산하려면 학생 프로필과 성적표가 필요해요. 왼쪽에서 더미 사례를 불러오거나 CSV 성적표를 올려주세요.")
    return None


def _diagnose(state: AgentState) -> dict:
    result = _need_student(state)
    if result is None:
        result = observe_call("check_requirements", check_requirements, state["profile"], state["courses"])
        # Readiness to graduate by the target term is not the same as today's credits.
        plan = observe_call("plan_courses", plan_courses, state["profile"], state["courses"], diagnosis=result)
        result = dict(result, plan=plan)
        result["current_answer"] = result["answer"]
        if not any(word in state["query"] for word in ("현재", "지금", "당장")):
            if plan.get("status") in {"조건부 가능", "위험"}:
                result["answer"] = plan["answer"] + "\n\n현재 졸업요건: " + result["current_answer"]
    return {"result": result}


def _plan(state: AgentState) -> dict:
    result = _need_student(state)
    if result is None:
        diagnosis = observe_call("check_requirements", check_requirements, state["profile"], state["courses"])
        plan = observe_call("plan_courses", plan_courses, state["profile"], state["courses"], diagnosis=diagnosis)
        result = dict(diagnosis, answer=plan["answer"], status=plan["status"], current_status=diagnosis["status"], current_answer=diagnosis["answer"], plan=plan,
                      evidence=plan.get("evidence", diagnosis.get("evidence", [])),
                      meta=plan.get("meta", diagnosis.get("meta", [])))
    return {"result": result}


def _regulation(state: AgentState) -> dict:
    result = observe_call("answer_regulations", call_domain, "answer_regulations",
                          {"query": state["query"], "profile": state.get("profile") or None})
    return {"result": result}


def _calendar(state: AgentState) -> dict:
    q = state["query"]
    # A mixed question may mention admission year before the requested event year.
    event_query = re.sub(r"(?<!\d)20\d{2}\s*(?:학번|년\s*입학)", "", q)
    year_match = re.search(r"(?<!\d)(20\d{2})(?!\d)", event_query)
    year = int(year_match.group(1)) if year_match else 2026
    keyword = next((x for x in ("수강신청", "졸업유예", "졸업식", "학위수여", "수강철회", "성적", "졸업", "계절", "전과", "개강", "종강") if x in q), "")
    events = observe_call("search_academic_events", call_domain, "search_academic_events",
                          {"query": keyword, "year": year})["events"]
    if events:
        answer = f"{year}년 학교 자료에서 확인한 일정이에요. 아래에서 일정을 선택하고 날짜를 확인한 뒤 ICS 파일로 내려받을 수 있어요."
    else:
        answer = f"제공 자료에서 {year}년의 해당 일정을 확인하지 못했어요. 다른 연도의 날짜를 옮겨 추정하지 않아요. 학교의 해당 연도 공지가 필요해요."
    evidence = [{"source_id": x.get("source_id"), "title": x["title"], "url": x.get("url"), "page": x.get("page")} for x in events]
    meta = _event_meta(events)
    return {"result": dict(_base(answer, "확인" if events else "확인 필요"), events=events, evidence=evidence, meta=meta)}


def _event_meta(events: list[dict]) -> list[dict]:
    from jolup.settings import DATA
    import json
    path = DATA / "sources" / "sources.json"
    if not path.exists():
        path = DATA / "sources.json"
    sources: Any = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if isinstance(sources, dict):
        sources = sources.get("sources", [])
    by_id = {x.get("source_id", x.get("id")): x for x in sources}
    result = []
    for sid in dict.fromkeys(x.get("source_id") for x in events):
        source = by_id.get(sid, {})
        result.append({"source_id": sid, "file_name": source.get("file_name", source.get("filename", sid)),
                       "url": source.get("url"), "registered_date": source.get("registered_date"),
                       "date_note": "등록일 미확인" if not source.get("registered_date") else ""})
    return result


def _unsupported(state: AgentState) -> dict:
    return {"result": _base("이 서비스는 제공된 학교 자료를 바탕으로 졸업요건, 수강 계획, 학사일정을 확인해요. 실제 성적 변경·증명서 발급·포털 수강신청은 할 수 없어요. ‘나 졸업 괜찮아?’ 또는 ‘다음 학기 수강 계획을 알려줘’로 물어봐 주세요.")}


def _explain(state: AgentState) -> dict:
    result = dict(state["result"])
    context = state.get("context") or {}
    config = context.get("llm_config") or {}
    result["execution_mode"] = "AI 의도 분류 + 로컬 계산·검색" if state.get("routing", {}).get("used") else "로컬 계산·검색"
    # Explicit opt-in also permits a key supplied by the local environment.
    # Merely having a key in the server environment must never trigger a call.
    use_llm = context.get("use_llm", bool(config.get("api_key")))
    if context.get("_replay") is not None:
        result["execution_mode"] = "이전 검증 결과 재사용 · 검색·계산·AI 추가 호출 없음"
        return {"result": result}
    if use_llm and state["intent"] != "unsupported":
        from jolup.llm import explain_result
        llm = observe_call("explain_result", explain_result, state["query"],
                           dict(result, intent=state["intent"]), config)
        result["llm"] = {k: v for k, v in llm.items() if k not in ("api_key", "key")}
        if llm.get("used") and llm.get("text"):
            result["ai_explanation"] = llm["text"]
            result["execution_mode"] += " + AI 설명"
    return {"result": result}



def _observed_node(name, function):
    def node(state: AgentState) -> dict:
        return observe_call(name, function, state)
    return node


def _domain_node(name, function):
    def node(state: AgentState) -> dict:
        try:
            result = observe_call(name, function, state)["result"]
        except Exception:
            if len(state["requested_intents"]) == 1:
                raise
            result = dict(_base("이 항목은 처리 중 오류가 발생해 확인하지 못했어요."), error_code="execution_error")
        return {"results": dict(state["results"], **{name: result}),
                "remaining_intents": state["remaining_intents"][1:]}
    return node


def _next_node(state: AgentState) -> str:
    return state["remaining_intents"][0] if state["remaining_intents"] else "compose"


def _reuse(state: AgentState) -> dict:
    return {"result": deepcopy(state["context"]["_replay"])}


def _combine_lists(results: dict, key: str) -> list:
    combined = []
    for result in results.values():
        for item in result.get(key, []):
            if item not in combined:
                combined.append(deepcopy(item))
    return combined


def _compose(state: AgentState) -> dict:
    results = state["results"]
    result = deepcopy(results[state["intent"]])
    if len(results) == 1:
        return {"result": result}
    labels = {"regulation": "규정 근거", "diagnosis": "현재 진단과 목표 전망",
              "planning": "현재 진단과 수강 계획", "calendar": "학사일정"}
    sections = [dict(value, intent=name) for name, value in results.items()]
    answers = []
    for section in sections:
        text = section["answer"]
        if section["intent"] == "planning":
            text += "\n현재 졸업요건: " + section.get("current_answer", "확인 필요")
        answers.append(labels[section["intent"]] + "\n" + text)
    failed = any(section.get("error_code") for section in sections)
    result.update(answer=("일부 항목을 확인하지 못했어요. 아래 항목별 결과를 확인해 주세요.\n\n" if failed else "") + "\n\n".join(answers),
                  sections=sections, partial_failure=failed,
                  evidence=_combine_lists(results, "evidence"), meta=_combine_lists(results, "meta"))
    if "calendar" in results:
        result["events"] = deepcopy(results["calendar"].get("events", []))
    return {"result": result}


@lru_cache(maxsize=1)
def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("route", _observed_node("route", _route))
    branches = {"diagnosis": _diagnose, "planning": _plan, "regulation": _regulation,
                "calendar": _calendar, "unsupported": _unsupported}
    successors = {"regulation": ["diagnosis", "planning", "calendar", "compose"],
                  "diagnosis": ["calendar", "compose"], "planning": ["calendar", "compose"],
                  "calendar": ["compose"], "unsupported": ["compose"]}
    for name, fn in branches.items():
        graph.add_node(name, _domain_node(name, fn))
        graph.add_conditional_edges(name, _next_node, successors[name])
    graph.add_node("compose", _observed_node("compose", _compose))
    graph.add_node("reuse", _observed_node("reuse", _reuse))
    graph.add_node("explain", _observed_node("explain", _explain))
    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", _next_node, [*branches, "reuse"])
    graph.add_edge("compose", "explain")
    graph.add_edge("reuse", "explain")
    graph.add_edge("explain", END)
    return graph.compile()


def _conversation_context(query, profile, courses, context):
    context = dict(context or {})
    context.pop("_replay", None)  # Only this process's memory may supply cached facts.
    memory = context.pop("conversation", None)
    invalidated = False
    if isinstance(memory, ConversationMemory):
        invalidated = memory.synchronize(profile, courses)
        context["previous_intent"] = memory.previous_intent()
        if not _prohibited(query):
            replay = memory.replay(query)
            if replay is not None:
                context["_replay"] = replay
    return context, memory, invalidated


def respond(query: str, profile: dict | None = None, courses: list[dict] | None = None,
            context: dict | None = None) -> dict:
    """Bounded ordered domains, optional local replay, one explanation; no autonomous loop."""
    start = time.perf_counter()
    context, memory, invalidated = _conversation_context(query, profile or {}, courses or [], context)
    recorder = StudyRecorder(profile or {}, courses or [],
                             builtin_sample_id=context.get("builtin_sample_id"))
    # Raw graph state contains student records and credentials. Automatic
    # tracing stays off; only the separately allowlisted local study trace can
    # be exported later by the user's explicit button click.
    with tracing_context(enabled=False), recorder.activate():
        try:
            state = build_graph().invoke({"query": query, "profile": profile or {}, "courses": courses or [],
                                          "context": context}, config={"recursion_limit": 8, "callbacks": []}, version="v2").value
        except Exception:
            # Preserve the completed failure span for study without exposing
            # exception text, tracebacks, request headers, or partial verdicts.
            failed = _base("처리 중 오류가 발생해 판정을 완료하지 못했어요. 실행 과정에서 실패한 단계를 확인해 주세요.")
            failed.update(execution_mode="처리 오류", error_code="execution_error")
            state = {"result": failed, "intent": detect_intent(query), "routing": {}}
    result = dict(state["result"])
    result.setdefault("rows", [])
    result.setdefault("evidence", [])
    result.setdefault("meta", [])
    result["intent"] = state["intent"]
    result["routing"] = state.get("routing", {})
    result["requested_intents"] = state.get("requested_intents", [state["intent"]])
    result["service_mode"] = service_mode()
    result["timings"] = {"total_seconds": round(time.perf_counter() - start, 3)}
    result["study_trace"] = recorder.finish(result)
    if isinstance(memory, ConversationMemory):
        memory.remember(query, result)
        result["memory"] = {"turn_count": len(memory), "window_size": memory.window_size,
                            "reused": context.get("_replay") is not None, "invalidated": invalidated}
    return result
