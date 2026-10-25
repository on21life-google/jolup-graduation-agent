"""Offline transport and orchestration contracts, including malformed MCP replies."""
import asyncio
from contextlib import asynccontextmanager
import json
import runpy
import sys
from types import SimpleNamespace

import pytest

from jolup import agent, mcp_client, mcp_server
from jolup.samples import load_sample


@pytest.mark.parametrize("url", ["https://localhost:8000/mcp", "http://remote.example/mcp", "http://user:pass@localhost/mcp"])
def test_remote_mcp_rejects_nonlocal_or_credentialed_url(url):
    with pytest.raises(mcp_client.ServiceError, match="로컬 서버"):
        asyncio.run(mcp_client._remote("check_requirements", {}, url))


def install_mcp_response(monkeypatch, response):
    import mcp
    import mcp.client.streamable_http as transport
    seen = []

    class Session:
        def __init__(self, read, write):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            pass
        async def initialize(self):
            seen.append("initialize")
        async def call_tool(self, name, arguments):
            seen.append((name, arguments))
            return response

    @asynccontextmanager
    async def stream(url, http_client):
        assert url == "http://localhost:8000/mcp"
        assert http_client.follow_redirects is False
        yield None, None, None

    monkeypatch.setattr(mcp, "ClientSession", Session)
    monkeypatch.setattr(transport, "streamable_http_client", stream)
    return seen


@pytest.mark.parametrize("structured,content,expected", [
    ({"result": [1, 2]}, [], [1, 2]), ({"status": "미충족"}, [], {"status": "미충족"}),
    (None, [SimpleNamespace(type="image"), SimpleNamespace(type="text", text='{"status":"확인 필요"}')], {"status": "확인 필요"}),
])
def test_remote_mcp_unwraps_only_supported_response_shapes(monkeypatch, structured, content, expected):
    seen = install_mcp_response(monkeypatch, SimpleNamespace(isError=False, structuredContent=structured, content=content))
    monkeypatch.setenv("MCP_URL", "http://localhost:8000/mcp")
    assert mcp_client.call_domain("example", {"value": 3}) == expected
    assert seen == ["initialize", ("example", {"value": 3})]


@pytest.mark.parametrize("response", [
    SimpleNamespace(isError=True),
    SimpleNamespace(isError=False, structuredContent=None, content=[]),
    SimpleNamespace(isError=False, structuredContent=None, content=[SimpleNamespace(type="text", text="raw private data")]),
])
def test_remote_mcp_failure_never_exposes_server_payload(monkeypatch, response):
    install_mcp_response(monkeypatch, response)
    with pytest.raises(mcp_client.ServiceError, match="연결하지 못했거나") as failure:
        asyncio.run(mcp_client._remote("example", {}, "http://localhost:8000/mcp"))
    assert "private" not in str(failure.value)


def test_local_tool_dispatch_and_csv_encodings(monkeypatch):
    monkeypatch.delenv("MCP_URL", raising=False)
    csv = "course_name,term,category,credits,grade,status\n과목,2026-1,전선,3,B0,completed\n"
    assert mcp_client.parse_transcript(csv.encode("cp949"))["ok"]
    assert mcp_client.parse_transcript(csv)["ok"]
    assert not mcp_client.parse_transcript(b"\xff")["ok"]
    with pytest.raises(ValueError, match="지원하지"):
        mcp_client.call_domain("nonexistent", {})
    from jolup import calendar
    events = calendar.search_events("성적")[:1]
    assert "BEGIN:VCALENDAR" in mcp_client.call_domain("create_calendar_event", {"events": events, "approved": True})["content"]


def test_mcp_tools_share_catalog_scores_and_rag_contracts(monkeypatch):
    from jolup import rag
    captured = []
    class KnowledgeBase:
        def search(self, query, profile=None, k=5):
            captured.append(k)
            return [{"query": query}]
        def answer(self, query, profile=None):
            return {"answer": query, "profile": profile}
    monkeypatch.setattr(rag, "KnowledgeBase", KnowledgeBase)
    sample = load_sample("on_track")
    assert mcp_server.search_courses(term="2027-S") == []
    assert mcp_server.search_scores(sample["courses"])["totals"]["total"] == 90
    assert mcp_server.search_regulations("전공", k=30)["results"] == [{"query": "전공"}]
    assert captured == [5]
    assert mcp_server.answer_regulations("채플")["answer"] == "채플"
    assert mcp_client.call_domain("answer_regulations", {"query": "학점"})["answer"] == "학점"
    assert mcp_server.parse_transcript("한" * 666667)["ok"] is False


def test_mcp_module_command_selects_requested_transport(monkeypatch):
    from mcp.server.fastmcp import FastMCP
    seen = []
    monkeypatch.setattr(FastMCP, "run", lambda self, transport: seen.append(transport))
    monkeypatch.setattr(sys, "argv", ["mcp_server", "--transport", "streamable-http"])
    runpy.run_path(mcp_server.__file__, run_name="__main__")
    assert seen == ["streamable-http"]


@pytest.mark.parametrize("response", [{"used": False, "error": "분류 연결 오류"}, {"used": False}])
def test_classifier_failure_preserves_local_route(monkeypatch, response):
    import jolup
    monkeypatch.setattr(jolup, "llm", SimpleNamespace(classify_intent=lambda *a, **kw: response), raising=False)
    result = agent._route({"query": "졸업학점 규정", "context": {"use_llm": True}})
    assert result["intent"] == "regulation" and result["routing"]["used"] is False
    assert result["routing"].get("error") == response.get("error")


def test_missing_classifier_can_still_use_local_route(monkeypatch):
    import jolup
    monkeypatch.setattr(jolup, "llm", SimpleNamespace(), raising=False)
    assert agent._route({"query": "채플 규정", "context": {"use_llm": True}})["intent"] == "regulation"


def test_regulation_calendar_and_missing_plan_execute_their_graph_branch(monkeypatch):
    from jolup import rag
    monkeypatch.delenv("MCP_URL", raising=False)
    monkeypatch.setattr(rag.KnowledgeBase, "answer", lambda self, **kwargs: {"status": "근거 확인", "answer": "근거 설명"})
    assert agent.respond("채플 규정")["answer"] == "근거 설명"
    answer = agent.respond("2026년 성적 일정")
    assert answer["status"] == "확인" and answer["events"] and answer["meta"]
    assert "성적표" in agent.respond("수강 계획")["answer"]


def test_calendar_metadata_supports_nested_and_absent_manifest(monkeypatch, tmp_path):
    from jolup import settings
    monkeypatch.setattr(settings, "DATA", tmp_path)
    assert agent._event_meta([{"source_id": "unknown"}])[0]["file_name"] == "unknown"
    nested = tmp_path / "sources"
    nested.mkdir()
    (nested / "sources.json").write_text(json.dumps([{"id": "known", "filename": "dates.txt", "registered_date": "2026-01-01"}]))
    result = agent._event_meta([{"source_id": "known"}])
    assert result[0]["file_name"] == "dates.txt" and result[0]["date_note"] == ""


def test_mcp_public_wrappers_preserve_academic_results_and_calendar_approval(monkeypatch):
    monkeypatch.delenv("MCP_URL", raising=False)
    sample = load_sample("on_track")
    assert mcp_server.parse_transcript(sample["csv_bytes"].decode("utf-8-sig"))["ok"]
    assert mcp_server.check_requirements(sample["profile"], sample["courses"])["totals"]["total"] == 90
    assert mcp_client.check_requirements(sample["profile"], sample["courses"])["totals"]["total"] == 90
    for plan in (mcp_server.plan_courses, mcp_client.plan_courses):
        assert plan(sample["profile"], sample["courses"])["current_totals"]["total"] == 90
    events = mcp_server.search_academic_events("성적")["events"]
    assert events
    with pytest.raises(PermissionError):
        mcp_server.create_calendar_event(events)
    exported = mcp_server.create_calendar_event(events, approved=True)
    assert exported["mime_type"] == "text/calendar" and "BEGIN:VCALENDAR" in exported["content"]
