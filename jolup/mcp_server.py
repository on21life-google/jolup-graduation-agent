"""Local MCP access to the same deterministic functions used by the app."""
from __future__ import annotations

import argparse
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("졸업로드맵", host=os.getenv("MCP_HOST", "127.0.0.1"),
              port=int(os.getenv("MCP_PORT", "8000")), log_level="WARNING",
              instructions="2024학번 성결대학교 컴퓨터공학과 전공심화 더미 자료용. 계산 결과를 수정하지 말고 현재와 예상을 구분하세요. 일정 생성은 미리보기 후 사용자의 명시 승인이 필요합니다.")


@mcp.tool()
def parse_transcript(csv_text: str) -> dict:
    """Parse a dummy CSV in memory. No student record is written to disk or RAG."""
    from jolup.academic import parse_transcript as parse
    if len(csv_text.encode("utf-8")) > 2_000_000:
        return {"ok": False, "courses": [], "errors": ["CSV는 2MB 이하로 입력하세요."], "warnings": [], "removed_columns": []}
    return parse(csv_text)


@mcp.tool()
def check_requirements(profile: dict, courses: list[dict]) -> dict:
    """Calculate current earned requirements and separately projected progress."""
    from jolup.academic import check_requirements as check
    return check(profile, courses)


@mcp.tool()
def search_courses(query: str = "", term: str | None = None) -> list[dict]:
    """Search curriculum candidates; this does not guarantee actual future offerings."""
    from jolup.planner import search_courses as search
    return search(query, term)


@mcp.tool()
def search_scores(courses: list[dict]) -> dict:
    """Return earned-credit totals and an inspectable inclusion/exclusion trace."""
    from jolup.academic import search_scores as search
    return search(courses)


@mcp.tool()
def create_calendar_event(events: list[dict], approved: bool = False) -> dict:
    """Create local ICS text only after preview and explicit user approval."""
    from jolup.calendar import create_calendar_event as create
    content = create(events, approved=approved)
    return {"file_name": "sungkyul_academic_dates.ics", "mime_type": "text/calendar", "content": content.decode("utf-8")}


@mcp.tool()
def search_regulations(query: str, profile: dict | None = None, k: int = 5) -> dict:
    """Search only curated school sources, with page and source metadata."""
    from jolup.rag import KnowledgeBase
    return {"results": KnowledgeBase().search(query, profile=profile, k=max(1, min(k, 5)))}


@mcp.tool()
def search_academic_events(query: str = "", year: int = 2026) -> dict:
    """Preview verified school dates before requesting user approval for an ICS."""
    from jolup.calendar import preview_events, search_events
    events = search_events(query, year)
    return preview_events([x["id"] for x in events])


@mcp.tool()
def plan_courses(profile: dict, courses: list[dict], preferences: dict | None = None) -> dict:
    """Build conditional future candidates using the same deterministic planner."""
    from jolup.planner import plan_courses as plan
    return plan(profile, courses, preferences=preferences)


@mcp.tool()
def answer_regulations(query: str, profile: dict | None = None) -> dict:
    """Return a source-grounded answer from the local school knowledge base."""
    from jolup.rag import KnowledgeBase
    return KnowledgeBase().answer(query, profile=profile)


def main():
    parser = argparse.ArgumentParser(description="졸업로드맵 MCP 서버")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    args = parser.parse_args()
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
