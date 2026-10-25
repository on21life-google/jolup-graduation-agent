import asyncio
import json
import sys
from datetime import date

import pytest
from icalendar import Calendar

from jolup import calendar


@pytest.fixture
def events(tmp_path, monkeypatch):
    rows = [{"id": "grade-review-2026", "title": "성적 정정 기간", "start_date": "2026-12-29", "end_date": "2026-12-31", "source_id": "guide", "url": "https://www.sungkyul.ac.kr/"},
            {"id": "winter-2026", "title": "겨울 계절학기", "start_date": "2026-12-21", "end_date": "2027-01-12", "source_id": "guide", "url": "https://www.sungkyul.ac.kr/"}]
    (tmp_path / "events.json").write_text(json.dumps({"events": rows}), encoding="utf-8")
    monkeypatch.setattr(calendar, "DATA", tmp_path)
    return rows


def test_no_export_without_explicit_approval(events):
    for value in (False, None, 1, "true"):
        with pytest.raises(PermissionError):
            calendar.create_calendar_event(events, approved=value)


def test_inclusive_final_day_becomes_exclusive_ics_end(events):
    raw = calendar.create_calendar_event([events[0]], approved=True)
    item = Calendar.from_ical(raw).walk("VEVENT")[0]
    assert item.decoded("dtstart") == date(2026, 12, 29)
    assert item.decoded("dtend") == date(2027, 1, 1)
    assert str(item["summary"]) == "성적 정정 기간"


def test_repeat_export_stable_and_no_duplicates(events):
    raw = calendar.create_calendar_event(events + [events[0]], approved=True)
    again = calendar.create_calendar_event(events, approved=True)
    assert raw == again
    assert len(Calendar.from_ical(raw).walk("VEVENT")) == 2


def test_cannot_export_modified_or_invented_dates(events):
    for alteration in ({"start_date": "2028-01-01"}, {"id": "invented"}, {"title": "조작"}):
        with pytest.raises(ValueError):
            calendar.create_calendar_event([dict(events[0], **alteration)], approved=True)


def test_cross_year_and_missing_future(events):
    assert len(calendar.search_events(year=2027)) == 1
    assert calendar.search_events(year=2028) == []
    assert len(calendar.search_events("성적", year=2026)) == 1


def test_preview_flags_unknown_id_and_requires_approval(events):
    result = calendar.preview_events([events[0]["id"], "unknown"])
    assert result["status"] == "확인 필요"
    assert result["unknown_ids"] == ["unknown"]
    assert result["approval_required"] is True
    assert len(result["events"]) == 1


def test_common_calendar_words_find_official_titles():
    assert any("학위수여" in event["title"] for event in calendar.search_events("졸업식", year=2027))
    assert any("학위수여" in event["title"] for event in calendar.search_events("졸업", year=2027))
    assert any("철회" in event["title"] for event in calendar.search_events("수강 철회", year=2026))


def test_real_mcp_stdio_tool_discovery_and_approval_gate():
    """Actual subprocess + MCP handshake; no external services or student data."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "jolup.mcp_server"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {"parse_transcript", "check_requirements", "search_courses", "search_scores", "create_calendar_event", "search_regulations"} <= {x.name for x in tools.tools}
                response = await session.call_tool("create_calendar_event", {"events": [], "approved": False})
                assert response.isError is True
                assert "승인" in response.content[0].text
    asyncio.run(run())


def test_real_mcp_http_handshake_and_tools(monkeypatch):
    """Exercise the HTTP transport with a live local subprocess, not a mock."""
    import os
    import socket
    import subprocess
    import time
    from pathlib import Path
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    process = subprocess.Popen([sys.executable, "-m", "jolup.mcp_server", "--transport", "streamable-http"],
                               env={**os.environ, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port)},
                               cwd=Path(__file__).resolve().parents[1],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.monotonic() + 15
        while True:
            assert process.poll() is None, "MCP HTTP server exited before becoming ready"
            try:
                # An unauthenticated GET can return 400/406; a response proves readiness.
                httpx.get(url, timeout=0.5, trust_env=False)
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    pytest.fail("MCP HTTP server did not become ready")
                time.sleep(0.05)

        async def run():
            async with httpx.AsyncClient(trust_env=False) as client:
                async with streamable_http_client(url, http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        names = {tool.name for tool in (await session.list_tools()).tools}
                        assert {"parse_transcript", "check_requirements", "search_scores", "search_courses", "search_academic_events", "create_calendar_event", "search_regulations"} <= names
                        bad_csv = await session.call_tool("parse_transcript", {"csv_text": "name\nFAKE"})
                        assert not bad_csv.isError
                        value = json.loads(bad_csv.content[0].text)
                        assert value["ok"] is False
                        assert value["courses"] == []
                        denied = await session.call_tool("create_calendar_event", {"events": [], "approved": False})
                        assert denied.isError
                        assert "승인" in denied.content[0].text
        asyncio.run(run())
        # The real app domain facade must use this transport when configured.
        monkeypatch.setenv("MCP_URL", url)
        from jolup.mcp_client import call_domain
        from jolup.samples import load_sample
        sample = load_sample("on_track")
        result = call_domain("check_requirements", {"profile": sample["profile"], "courses": sample["courses"]})
        assert result["totals"]["total"] == 90
        assert result["projection"]["totals"]["total"] == 105
        plan = call_domain("plan_courses", {"profile": sample["profile"], "courses": sample["courses"]})
        assert plan["status"] == "조건부 가능"
        preview = call_domain("search_academic_events", {"query": "성적", "year": 2026})
        assert preview["events"]
        ics = call_domain("create_calendar_event", {"events": preview["events"], "approved": True})
        assert Calendar.from_ical(ics["content"]).walk("VEVENT")
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=45).run()
        assert not app.exception
        assert app.session_state["diagnosis"]["totals"]["total"] == 90
        next(item for item in app.button if item.label == "나 졸업 괜찮아?").click().run()
        assert not app.exception
        assert app.session_state["messages"][-1]["result"]["service_mode"] == "MCP HTTP"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
