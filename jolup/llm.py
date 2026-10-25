"""Validated app boundary over official LangChain chat-model integrations.

No credentials, prompts, transcripts, or provider response bodies are logged/saved.
The academic calculation remains authoritative; this module adds explanations only.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from functools import cached_property
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import httpx2
import anthropic
import openai
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_MODELS = {"openai": "gpt-6-luna", "anthropic": "claude-haiku-5-5"}
MODEL_ENDPOINTS = {
    "openai": "https://api.openai.com/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
}
ANTHROPIC_VERSION = "2023-06-01"  # Official SDK's stable API version, also used for catalog GET.
_SECRET_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE_PATTERN = re.compile(r"(?<!\d)(?:01[016789][- ]?\d{3,4}[- ]?\d{4}|\d{6}-[1-4]\d{6})(?!\d)")


class ExplanationOutput(BaseModel):
    """계산 결과를 바꾸지 않는 짧은 한국어 보조 설명."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    explanation: str = Field(min_length=1, max_length=4000,
                             description="입력에 있는 사실만으로 작성한 한국어 설명 3~5문장")


class IntentDecision(BaseModel):
    """질문 신호에서 선택한 하나의 허용된 상담 경로."""

    model_config = ConfigDict(extra="forbid", strict=True)
    intent: Literal["diagnosis", "planning", "regulation", "calendar", "unsupported"]


class ExplanationSelection(BaseModel):
    """모델은 이미 검증된 문장의 ID만 선택하며 새 학사 사실을 쓰지 않는다."""

    model_config = ConfigDict(extra="forbid", strict=True)
    fact_ids: list[Literal["current", "in_progress", "plan", "review", "evidence", "scope"]] = Field(
        min_length=1, max_length=6, description="제공된 facts에서 선택한 중복 없는 ID 목록")


def detect_provider(api_key: str | None) -> str | None:
    """Recognize known key prefixes locally; never probe providers with a key."""
    key = (api_key or "").strip()
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith(("sk-proj-", "sk-svcacct-")):
        return "openai"
    return None


def _configured_key(config: dict, provider: str) -> tuple[str, str, str | None]:
    """Resolve one configured credential without probing a provider."""
    key = str(config.get("api_key") or os.getenv("JOLUP_API_KEY", "")).strip()
    if key:
        return provider, key, None
    env = {p: os.getenv(k, "").strip() for p, k in
           (("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY"))}
    if provider != "auto":
        return provider, env[provider], None
    configured = [(p, value) for p, value in env.items() if value]
    if len(configured) == 1:
        provider, key = configured[0]
    elif len(configured) == 2:
        return provider, "", "환경에 두 공급자의 키가 있습니다. 사용할 공급자를 선택해 주세요."
    return provider, key, None


def _credential_provider(key: str, provider: str) -> tuple[str | None, str | None]:
    if not key:
        return None, "API 키가 없습니다. 로컬 계산과 규정 검색은 계속 사용할 수 있습니다."
    if not key.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in key) or len(key) > 2048:
        return None, "API 키 형식을 확인해 주세요. 키 안에 공백이나 줄바꿈을 넣지 마세요."
    detected = detect_provider(key)
    if provider == "auto":
        provider = detected
    elif detected and provider != detected:
        return None, "선택한 공급자와 키 접두사가 다릅니다. 공급자 또는 키를 확인해 주세요."
    if not provider:
        return None, "이 키 형식은 자동으로 구분할 수 없습니다. 고급 설정에서 공급자를 선택해 주세요."
    return provider, None


def _model_setting(provider: str, config: dict) -> tuple[str | None, str | None]:
    model = str(config.get("model") or os.getenv("JOLUP_LLM_MODEL", "") or DEFAULT_MODELS[provider]).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", model):
        return None, "모델 ID 형식을 확인해 주세요."
    if (provider == "openai" and model.startswith("claude-")) or (
            provider == "anthropic" and model.startswith(("gpt-", "o1", "o3", "o4"))):
        return None, "모델 ID가 선택한 공급자와 다릅니다. 모델 입력을 비우면 기본 모델을 사용합니다."
    return model, None


def _resolve(config: dict | None = None, *, for_model_list: bool = False) -> tuple[dict, str]:
    config = config or {}
    provider = str(config.get("provider") or os.getenv("JOLUP_LLM_PROVIDER", "auto")).lower()
    info = {"ok": False, "provider": None, "model": None, "has_key": False, "error": None}
    if provider not in {"auto", "openai", "anthropic"}:
        return dict(info, error="지원하는 공급자는 자동 감지, OpenAI, Anthropic입니다."), ""
    provider, key, error = _configured_key(config, provider)
    if error:
        return dict(info, error=error), ""
    info["has_key"] = bool(key)
    provider, error = _credential_provider(key, provider)
    if error:
        return dict(info, error=error), ""
    info["provider"] = provider
    workspace_id = str(config.get("workspace_id") or os.getenv("ANTHROPIC_WORKSPACE_ID", "")).strip() if provider == "anthropic" else ""
    if workspace_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workspace_id):
        info["error"] = "Claude 워크스페이스 ID 형식을 확인해 주세요."
        return info, ""
    info["workspace_id"] = workspace_id
    model, error = (None, None) if for_model_list else _model_setting(provider, config)
    if error:
        return dict(info, error=error), ""
    try:
        timeout = float(config.get("timeout", 30))
        max_tokens = 1200 if for_model_list else int(config.get("max_output_tokens", 1200))
        if not math.isfinite(timeout) or not 1 <= timeout <= 120 or not 64 <= max_tokens <= 8192:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        info["error"] = "제한 시간은 1~120초, 출력 토큰 한도는 64~8192로 설정해 주세요."
        return info, ""
    info.update(ok=True, model=model, timeout=timeout, max_output_tokens=max_tokens)
    return info, key


def resolve_config(config: dict | None = None) -> dict:
    """Safe UI configuration check. The return value never contains the key."""
    return _resolve(config)[0]


def _result(info: dict, *, text: str = "", error: str | None = None,
            usage: dict | None = None, elapsed_ms: float = 0, error_code: str | None = None) -> dict:
    ok = bool(text) and error is None
    return {"ok": ok, "used": ok, "text": text if ok else "", "provider": info.get("provider"),
            "model": info.get("model"), "error": error, "error_code": error_code,
            "usage": usage or {}, "elapsed_ms": round(elapsed_ms, 1)}


def _safe_usage(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    fields = ("input_tokens", "output_tokens", "total_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: payload[k] for k in fields if type(payload.get(k)) is int and payload[k] >= 0}


def _http_error(status: int) -> tuple[str, str]:
    if status == 401:
        return "authentication", "키 인증에 실패했습니다. 키와 공급자를 확인해 주세요."
    if status in (403, 404):
        return "model_access", "선택한 모델 또는 API에 접근할 수 없습니다. 계정의 모델 권한과 모델 ID를 확인해 주세요."
    if status == 429:
        return "rate_limit", "API 사용 한도 또는 호출 제한에 도달했습니다. 사용량을 확인한 뒤 다시 시도해 주세요."
    if status >= 500:
        return "provider_unavailable", "AI 공급자가 일시적으로 응답하지 못했습니다. 로컬 계산 결과를 사용해 주세요."
    return "request_rejected", "API가 요청을 받아들이지 못했습니다. 모델 ID와 계정 설정을 확인해 주세요."


def _catalog_result(response, result: dict, key: str) -> dict:
    """Validate a successful model catalog independently of the request lifecycle."""
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        return dict(result, error="모델 목록 응답을 읽을 수 없습니다.", error_code="invalid_response")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return dict(result, error="모델 목록 응답 형식이 예상과 다릅니다.", error_code="invalid_response")
    ids = []
    for row in payload["data"][:1000]:
        model_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", model_id):
            continue
        if key in model_id or _SECRET_PATTERN.search(model_id):
            continue
        if model_id not in ids:
            ids.append(model_id)
    return dict(result, ok=True, models=ids, has_more=payload.get("has_more") is True or len(payload["data"]) > 1000)


def list_available_models(config: dict | None = None, *, client: httpx.Client | None = None) -> dict:
    """Explicit read-only catalog lookup. Never generate or auto-select a model.

    Only model IDs leave this function. Credentials, response metadata and raw
    error bodies are neither returned nor logged. One GET, without retries or
    redirect following. A listed model can still lack Responses/Messages access.
    """
    info, key = _resolve(config, for_model_list=True)
    result = {"ok": False, "provider": info.get("provider"), "models": [],
              "error": None, "error_code": None, "elapsed_ms": 0, "has_more": False}
    if not info["ok"]:
        return dict(result, error=info["error"], error_code="configuration")
    provider = info["provider"]
    headers = {"Accept": "application/json"}
    params = None
    if provider == "anthropic":
        headers["x-api-key"] = key
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if info.get("workspace_id"):
            headers["anthropic-workspace-id"] = info["workspace_id"]
        params = {"limit": 1000}
    else:
        headers["Authorization"] = f"Bearer {key}"
    owned_client = client is None
    client = client or httpx.Client(timeout=info["timeout"], follow_redirects=False, trust_env=False)
    started = time.monotonic()
    try:
        response = client.get(MODEL_ENDPOINTS[provider], headers=headers, params=params,
                              timeout=info["timeout"], follow_redirects=False)
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        if response.status_code != 200:
            code, message = _http_error(response.status_code)
            if response.status_code in (403, 404):
                message = "이 키로 모델 목록을 조회할 수 없습니다. API 키의 조회 권한과 계정 설정을 확인해 주세요."
            return dict(result, error=message, error_code=code, http_status=response.status_code)
        return _catalog_result(response, result, key)
    except httpx.TimeoutException:
        return dict(result, error="모델 목록 조회 제한 시간을 넘었습니다.", error_code="timeout")
    except httpx.HTTPError:
        return dict(result, error="AI 서버의 모델 목록에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.", error_code="connection")
    finally:
        if owned_client:
            client.close()


class _BorrowedTransport(httpx2.BaseTransport):
    """Bridge the app's HTTPX test seam to SDK HTTPX2 without closing the caller."""

    def __init__(self, client: httpx.Client):
        self.client = client

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        outgoing = httpx.Request(request.method, str(request.url), headers=request.headers.raw,
                                 content=request.content, extensions=request.extensions)
        try:
            response = self.client.send(outgoing, follow_redirects=False)
        except httpx.TimeoutException as exc:
            raise httpx2.ReadTimeout("HTTP transport timed out", request=request) from exc
        except httpx.HTTPError as exc:
            raise httpx2.ConnectError("HTTP transport failed", request=request) from exc
        return httpx2.Response(response.status_code, headers=response.headers.raw,
                               content=response.content, request=request)


class _CompletionGuard:
    """Retain only a completion flag that LangChain's normalization omits.

    An OpenAI envelope may say completed while a message says in_progress. The
    normalized AIMessage drops that message status, so reject it at the boundary.
    Provider content and credentials are never retained by this guard.
    """

    incomplete = False

    def inspect(self, response: httpx2.Response):
        if response.status_code != 200:
            return
        response.read()
        try:
            body = response.json()
        except ValueError:
            return
        output = body.get("output") if isinstance(body, dict) else None
        if isinstance(output, list):
            self.incomplete = (body.get("status") != "completed" or bool(body.get("error"))
                               or any(isinstance(item, dict) and item.get("type") == "message"
                                      and item.get("status", "completed") != "completed" for item in output))


class _HttpChatAnthropic(ChatAnthropic):
    """Transport injection only; request conversion and parsing stay in LangChain.

    ChatAnthropic currently exposes no public http_client argument. This narrow
    override is pinned and exercised by real SDK MockTransport regression tests.
    """

    http_client: httpx2.Client = Field(exclude=True, repr=False)

    @cached_property
    def _client(self):
        return anthropic.Anthropic(**self._client_params, http_client=self.http_client)


def _chat_model(info: dict, key: str, client: httpx2.Client):
    common = dict(model=info["model"], api_key=key, timeout=info["timeout"],
                  max_tokens=info["max_output_tokens"], max_retries=0, cache=False)
    if info["provider"] == "openai":
        reasoning = {"effort": "none"} if info["model"] == DEFAULT_MODELS["openai"] else None
        return ChatOpenAI(**common, base_url="https://api.openai.com/v1", http_client=client,
                          use_responses_api=True, output_version="responses/v1",
                          store=False, reasoning=reasoning)
    headers = {}
    if info["workspace_id"]:
        headers["anthropic-workspace-id"] = info["workspace_id"]
    return _HttpChatAnthropic(**common, base_url="https://api.anthropic.com", http_client=client,
                              default_headers=headers, anthropic_proxy=None)


_OUTPUT_ERRORS = {
    "refusal": "모델이 설명 생성을 거절했습니다. 로컬 계산 결과를 사용해 주세요.",
    "incomplete": "AI 응답이 완료되지 않아 표시하지 않았습니다. 로컬 계산 결과는 유지됩니다.",
    "empty_output": "API가 표시할 설명문을 반환하지 않았습니다.",
    "invalid_response": "API 응답 형식을 읽을 수 없습니다.",
    "schema_mismatch": "AI 응답이 지정한 구조와 달라 표시하지 않았습니다. 로컬 계산 결과를 사용해 주세요.",
}


def _output_error(raw) -> str | None:
    metadata = raw.response_metadata
    content = raw.content if isinstance(raw.content, list) else []
    details = metadata.get("stop_details") or {}
    if (metadata.get("stop_reason") == "refusal" or details.get("type") == "refusal"
            or any(block.get("type") == "refusal" for block in content if isinstance(block, dict))):
        return "refusal"
    completed = (metadata.get("status") == "completed" if metadata.get("model_provider") == "openai"
                 else metadata.get("stop_reason") in {"end_turn", "stop_sequence"})
    if not completed:
        return "incomplete"
    if not raw.text.strip():
        return "empty_output"
    return None


def _structured_result(envelope: dict, info: dict, key: str) -> dict:
    raw, parsed = envelope["raw"], envelope["parsed"]
    usage = _safe_usage(raw.usage_metadata)
    code = _output_error(raw)
    if code is None and (envelope["parsing_error"] is not None or not isinstance(parsed, BaseModel)):
        code = "schema_mismatch"
    if code:
        return _result(info, error=_OUTPUT_ERRORS[code], error_code=code, usage=usage)
    structured = parsed.model_dump()
    if isinstance(parsed, ExplanationOutput):
        structured["explanation"] = redact_text(parsed.explanation.replace(key, "[비밀키 삭제]"))
        text = structured["explanation"]
    else:
        text = json.dumps(structured, ensure_ascii=False)
    return dict(_result(info, text=text, usage=usage), structured=structured)


def generate(system: str, user: str, config: dict | None = None, *, client: httpx.Client | None = None,
             response_schema: type[BaseModel] = ExplanationOutput) -> dict:
    """One LangChain model call with a provider-native schema and local validation.

    API errors, refusal, truncation and schema failures return a safe fallback. No
    retry, failover, automatic tracing, student-record transfer or model guessing.
    The injected HTTPX client stays caller-owned, including on failed requests.
    """
    info, key = _resolve(config)
    if not info["ok"]:
        return _result(info, error=info["error"], error_code="configuration")
    if not isinstance(system, str) or not isinstance(user, str) or not user.strip():
        return _result(info, error="설명할 입력이 없습니다.", error_code="input")
    if len(system) + len(user) > 60000:
        return _result(info, error="AI 설명 입력이 너무 큽니다. 집계와 근거 일부만 보내 주세요.", error_code="input")
    started = time.monotonic()
    transport = _BorrowedTransport(client) if client is not None else None
    guard = _CompletionGuard()
    with httpx2.Client(transport=transport, timeout=info["timeout"], follow_redirects=False, trust_env=False,
                      event_hooks={"response": [guard.inspect]}) as http:
        try:
            with tracing_context(enabled=False):
                model = _chat_model(info, key, http)
                options = {"method": "json_schema", "include_raw": True}
                if info["provider"] == "openai":
                    options["strict"] = True
                structured_model = model.with_structured_output(response_schema, **options)
                envelope = structured_model.invoke([("system", system), ("human", user)], config={"callbacks": []})
                result = _structured_result(envelope, info, key)
        except (openai.APITimeoutError, anthropic.APITimeoutError, httpx.TimeoutException):
            result = _result(info, error="AI 응답 제한 시간을 넘었습니다. 로컬 계산 결과를 사용해 주세요.", error_code="timeout")
        except (openai.APIStatusError, anthropic.APIStatusError) as exc:
            code, message = _http_error(exc.status_code)
            result = _result(info, error=message, error_code=code)
        except (openai.APIConnectionError, anthropic.APIConnectionError, httpx.HTTPError):
            result = _result(info, error="AI 서버에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.", error_code="connection")
        except ValidationError:
            result = _result(info, error=_OUTPUT_ERRORS["schema_mismatch"], error_code="schema_mismatch")
        except (ValueError, TypeError, AttributeError, KeyError, openai.APIResponseValidationError,
                anthropic.APIResponseValidationError):
            result = _result(info, error=_OUTPUT_ERRORS["invalid_response"], error_code="invalid_response")
    if guard.incomplete and result["error_code"] != "refusal":
        result = _result(info, error=_OUTPUT_ERRORS["incomplete"], error_code="incomplete")
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def redact_text(value: Any, limit: int = 4000) -> str:
    """Defense in depth for public excerpts; not a replacement for field allowlisting."""
    text = str(value or "")[:limit]
    text = _SECRET_PATTERN.sub("[비밀키 삭제]", text)
    text = _EMAIL_PATTERN.sub("[이메일 삭제]", text)
    text = _PHONE_PATTERN.sub("[연락처 삭제]", text)
    text = re.sub(r"(?<!\d)20\d{6,8}(?!\d)", "[학번 삭제]", text)
    return text


def _numeric(value: Any) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def _safe_rows(rows: Any) -> list[dict]:
    result = []
    for row in rows[:30] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        item = {k: redact_text(row.get(k), 100) for k in ("area", "unit", "status")}
        for key in ("required", "earned", "shortage"):
            value = row.get(key)
            item[key] = _numeric(value)
            if isinstance(value, str) and value in {"충족", "미충족", "확인 필요", "통과", "미완료", "완료", "합격"}:
                item[key] = value
        result.append(item)
    return result


def _safe_plan(plan: dict) -> dict:
    safe = {"status": redact_text(plan.get("status"), 100), "semesters": []}
    semesters = plan.get("semesters", [])
    for semester in semesters[:12] if isinstance(semesters, list) else []:
        if not isinstance(semester, dict) or not re.fullmatch(r"20\d{2}-[12]", str(semester.get("term", ""))):
            continue
        safe["semesters"].append({"term": semester["term"],
            "total_credits": _numeric(semester.get("total_credits")), "max_credits": _numeric(semester.get("max_credits")),
            "offering_status": "교육과정상 후보: 실제 개설과 수강 가능 여부 별도 확인"})
    return safe


def _safe_school_evidence(evidence) -> list[dict]:
    safe = []
    for source in evidence[:5] if isinstance(evidence, list) else []:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "")
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "sungkyul.ac.kr" or host.endswith(".sungkyul.ac.kr")):
            continue
        safe.append({"source_id": redact_text(source.get("source_id"), 80),
            "url": url, "page": _numeric(source.get("page")),
            "excerpt": redact_text(source.get("text") or source.get("excerpt") or source.get("quote"), 2200)})
    return safe


def safe_explanation_input(result: dict) -> dict:
    """Export only derived aggregates and school-source excerpts, never student records.

    Intentionally excludes query, profile, answer, trace, course rows, and review text:
    those fields can echo CSV values, names, IDs, or user-entered free text.
    """
    allowed_intents = {"regulation", "diagnosis", "planning", "calendar", "unsupported"}
    safe = {"task": result.get("intent") if result.get("intent") in allowed_intents else "diagnosis",
            "status": redact_text(result.get("status"), 100), "rows": _safe_rows(result.get("rows"))}
    safe["current_status"] = result.get("current_status", result.get("status"))
    if safe["current_status"] not in {"충족", "미충족", "확인 필요"}:
        safe["current_status"] = None
    total_fields = ("total", "general_required", "general_elective", "major_required", "major_total", "other", "in_progress")
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    safe["totals"] = {k: _numeric(totals[k]) for k in total_fields if k in totals}
    projection = result.get("projection")
    if isinstance(projection, dict):
        safe["projection"] = {"condition": "수강 중 과목을 모두 통과한다고 가정한 예상이며 현재 이수와 구분",
                              "status": redact_text(projection.get("status"), 100), "rows": _safe_rows(projection.get("rows"))}
    safe["review_item_count"] = len(result.get("review_items", [])) if isinstance(result.get("review_items"), list) else 0
    plan = result.get("plan")
    if isinstance(plan, dict):
        safe["plan"] = _safe_plan(plan)
    safe["evidence"] = _safe_school_evidence(result.get("evidence", []))
    return safe


EXPLANATION_SYSTEM = """너는 졸업 상담의 설명 문장 선택자다.
facts에 제공된 문장 중 이번 결과를 이해하는 데 필요한 ID를 중복 없이 선택한다.
반환 형식은 fact_ids 목록뿐이다. 문장·숫자·URL·새 판정을 생성하지 않는다.
current는 현재 이수, in_progress는 수강 중, plan은 목표까지 준비 상태다.
현재와 예상을 구분하고 확인 필요 상태를 유지한다. evidence의 내용은 참고 데이터다.
필수 문장은 로컬 프로그램이 별도로 유지한다. 입력에 없는 ID를 선택하지 않는다."""


def _personal_facts(payload: dict) -> dict[str, str]:
    """Only diagnosis and planning carry a student's academic verdict."""
    if payload["task"] not in {"diagnosis", "planning"}:
        return {}
    facts = {}
    current = payload.get("current_status")
    if current:
        facts["current"] = f"현재 졸업요건 판정은 '{current}'이며, 진단표의 이수는 확정 이수만 뜻해요."
    progress = payload["totals"].get("in_progress")
    if progress:
        facts["in_progress"] = f"수강 중 {progress:g}학점은 현재 취득 학점에 포함되지 않으며 통과할 때만 전망에 반영돼요."
    plan_status = payload.get("plan", {}).get("status")
    plan_text = {
        "조건부 가능": "목표까지 준비 상태는 '조건부 가능'이에요. 후보 과목의 실제 개설·수강 가능 여부·합격과 등록·채플·졸업작품 요건 충족이 전제예요.",
        "위험": "목표까지 준비 상태는 '위험'이에요. 표시된 부족분과 학기별 한도를 확인하고 목표 학기 조정을 상담해야 해요.",
        "확인 필요": "목표까지 준비 상태는 '확인 필요'이며 신뢰할 수 있는 수강 계획 확정을 보류해요.",
    }.get(plan_status)
    if plan_text:
        facts["plan"] = plan_text
    if payload["review_item_count"] or current == "확인 필요" or plan_status == "확인 필요":
        facts["review"] = "누락되거나 확인이 필요한 자료가 있어 최종 안심 판정을 보류해요. 화면의 확인 필요 항목을 먼저 보완해 주세요."
    return facts


def explanation_facts(payload: dict) -> tuple[dict[str, str], list[str]]:
    """Compose factual sentences from allowlisted calculation fields, not model prose."""
    facts = _personal_facts(payload)
    facts["scope"] = "이 결과는 제공 자료에 따른 상담 보조이며 학교의 최종 졸업 승인이나 미래 수강신청을 보장하지 않아요."
    mandatory = list(facts)
    facts["evidence"] = ("규정 설명은 함께 표시한 학교 근거와 적용 대상·페이지를 확인해 주세요."
                         if payload["evidence"] else "현재 결과에 확인된 학교 근거가 없어 규정 내용을 임의로 보충하지 않아요.")
    return facts, mandatory


def explain_result(query: str, deterministic_result: dict, config: dict | None = None,
                   *, client: httpx.Client | None = None) -> dict:
    """App entry point. ``query`` is deliberately not sent to the provider.

    The model selects fact IDs. Only locally composed sentences are displayed;
    mandatory verdicts, caveats and missing-data warnings cannot be omitted.
    """
    del query
    payload = safe_explanation_input(deterministic_result)
    facts, mandatory = explanation_facts(payload)
    payload["facts"] = facts
    response = generate(EXPLANATION_SYSTEM, json.dumps(payload, ensure_ascii=False, allow_nan=False),
                        config, client=client, response_schema=ExplanationSelection)
    if not response["ok"]:
        return response
    try:
        selected = ExplanationSelection.model_validate(response.get("structured")).fact_ids
        if len(set(selected)) != len(selected) or any(key not in facts for key in selected):
            raise ValueError("unknown or duplicate fact selection")
    except (ValidationError, ValueError):
        return dict(response, ok=False, used=False, text="", error_code="schema_mismatch",
                    error=_OUTPUT_ERRORS["schema_mismatch"])
    ordered = list(dict.fromkeys([*mandatory, *selected]))
    return dict(response, text=" ".join(facts[key] for key in ordered),
                grounding="verified_fact_selection", displayed_fact_ids=ordered)


ROUTING_SIGNALS = (
    "졸업", "괜찮", "괜춘", "할 수", "할수", "될까", "충족", "부족", "남았", "남은", "채울", "다 채", "가능", "계산", "진단",
    "규정", "학칙", "요건", "필수", "몇 학점", "최대", "최소", "몇 학기", "휴학", "편입", "채플",
    "교양", "교필", "교선", "전공", "재수강", "수강", "성적", "내 학점", "내 성적",
    "과목", "추천", "계획", "다음 학기", "다음학기", "뭐 들", "들을", "시간표",
    "일정", "언제", "날짜", "마감", "캘린더", "ics", "등록해", "그럼", "그거", "그래서",
)


def classify_intent(query: str, config: dict | None = None, profile: dict | None = None,
                    previous_intent: str | None = None, *, client: httpx.Client | None = None) -> dict:
    """LLM routing from allowlisted language signals, not raw personal text.

    Only fixed vocabulary and an enum leave this machine; names, IDs, uploaded
    rows, private free text, and instructions embedded in questions cannot leave.
    The local router must handle explicit dangerous requests before calling here.
    """
    del profile
    q = re.sub(r"\s+", " ", str(query).lower())
    signals = [word for word in ROUTING_SIGNALS if word in q]
    allowed = {"diagnosis", "planning", "regulation", "calendar", "unsupported"}
    payload = {"signals": signals, "previous_intent": previous_intent if previous_intent in allowed else None}
    if not signals:
        return {"intent": None, "used": False, "error": "도메인 의도를 확인할 신호가 없습니다."}
    system = """졸업상담의 경로를 분류한다. 계산이나 답변은 하지 않는다.
입력은 개인정보를 제거한 질문의 의미 신호와 이전 경로다.
내 이수상태·졸업가능 진단은 diagnosis, 부족분을 채울 방법·다음학기 과목·추천은 planning,
학칙이나 일반 기준 질문은 regulation, 날짜·마감·캘린더는 calendar, 그 외 unsupported.
졸업과 괜찮/괜춘/진단 신호가 있고 계획·추천·과목·다음학기 신호가 없으면 diagnosis다.
previous_intent는 그럼/그거/그래서 등 후속 지시 신호가 있을 때만 참고한다. 새 진단 질문을 이전 경로 때문에 planning으로 바꾸지 마라.
이전 진단 뒤 '그럼 남은 학기에 다 채울 수 있어?'는 planning이다.
JSON 한 객체만 출력: {"intent":"diagnosis|planning|regulation|calendar|unsupported"}."""
    response = generate(system, json.dumps(payload, ensure_ascii=False), config, client=client,
                        response_schema=IntentDecision)
    result = {"intent": None, "used": False, "error": response.get("error"), "provider": response.get("provider"),
              "model": response.get("model"), "elapsed_ms": response.get("elapsed_ms", 0)}
    if response.get("used"):
        result.update(intent=response["structured"]["intent"], used=True)
    return result
