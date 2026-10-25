"""One domain interface: local functions or the private Compose MCP service."""
from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlparse


class ServiceError(RuntimeError):
    pass


def service_mode() -> str:
    return "MCP HTTP" if os.getenv("MCP_URL", "").strip() else "로컬 함수"


async def _remote(name: str, arguments: dict, url: str):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    parsed = urlparse(url)
    # Uploaded records are allowed only over a local/private project transport.
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1", "mcp"} or parsed.username or parsed.password:
        raise ServiceError("MCP 주소는 이 프로젝트의 로컬 서버 또는 Compose mcp 서비스만 사용할 수 있습니다.")
    try:
        async with httpx.AsyncClient(timeout=45, trust_env=False, follow_redirects=False) as client:
            async with streamable_http_client(url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    response = await session.call_tool(name, arguments)
                    if response.isError:
                        raise ServiceError("MCP 도구가 요청을 처리하지 못했습니다. 입력 자료와 서비스 상태를 확인해 주세요.")
                    structured = response.structuredContent
                    if structured is not None:
                        return structured["result"] if set(structured) == {"result"} else structured
                    texts = [item.text for item in response.content if getattr(item, "type", None) == "text"]
                    if len(texts) == 1:
                        return json.loads(texts[0])
                    raise ServiceError("MCP 응답 형식을 확인할 수 없습니다.")
    except Exception as exc:
        # Provider or transport messages can echo arguments. Do not log/return them.
        raise ServiceError("로컬 MCP 서비스에 연결하지 못했거나 응답이 올바르지 않습니다. 서버 실행 상태를 확인해 주세요.") from None


def call_domain(name: str, arguments: dict):
    url = os.getenv("MCP_URL", "").strip()
    if url:
        return asyncio.run(_remote(name, arguments, url))
    if name == "check_requirements":
        from jolup.academic import check_requirements
        return check_requirements(**arguments)
    if name == "plan_courses":
        from jolup.planner import plan_courses
        return plan_courses(**arguments)
    if name == "answer_regulations":
        from jolup.rag import KnowledgeBase
        return KnowledgeBase().answer(**arguments)
    if name == "search_academic_events":
        from jolup.calendar import preview_events, search_events
        return preview_events([event["id"] for event in search_events(**arguments)])
    if name == "parse_transcript":
        from jolup.academic import parse_transcript
        return parse_transcript(arguments["csv_text"])
    if name == "create_calendar_event":
        from jolup.calendar import create_calendar_event
        return {"content": create_calendar_event(**arguments).decode("utf-8")}
    raise ValueError("지원하지 않는 졸업로드맵 도구입니다.")


def check_requirements(profile: dict, courses: list[dict]) -> dict:
    return call_domain("check_requirements", {"profile": profile, "courses": courses})


def plan_courses(profile: dict, courses: list[dict], diagnosis=None, preferences=None) -> dict:
    # The server recalculates from the records; do not transmit a stale result.
    return call_domain("plan_courses", {"profile": profile, "courses": courses, "preferences": preferences})


def parse_transcript(data: bytes | str) -> dict:
    if isinstance(data, bytes):
        for encoding in ("utf-8-sig", "cp949"):
            try:
                data = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return {"ok": False, "courses": [], "errors": ["UTF-8 또는 CP949 인코딩 CSV가 필요합니다."], "warnings": [], "removed_columns": []}
    return call_domain("parse_transcript", {"csv_text": data})
