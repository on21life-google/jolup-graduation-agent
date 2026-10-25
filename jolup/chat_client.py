"""Streamlit chat uses one adapter for local Python or the private Agent API.

HTTP chat sends student records only to this project's loopback/Compose service.
LLM credentials and local memory objects never cross this boundary.
"""
from __future__ import annotations

from contextlib import nullcontext
import os
import re
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError


class ChatServiceError(RuntimeError):
    """A fixed, displayable transport error; never an echoed response body."""


def transport_mode() -> str:
    return 'http' if os.getenv('JOLUP_AGENT_API_URL', '').strip() else 'local'


def _base_url() -> str:
    value = os.getenv('JOLUP_AGENT_API_URL', '').strip().rstrip('/')
    try:
        parsed = urlparse(value)
        valid = (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1', 'api'}
                 and not parsed.username and not parsed.password and not parsed.path
                 and not parsed.query and not parsed.fragment and parsed.port != 0)
    except ValueError:
        valid = False
    if not valid:
        raise ChatServiceError('Agent API 주소는 로컬 서버 또는 Compose api 서비스만 사용할 수 있습니다.')
    return value


def _request(method, path, *, payload=None, client=None):
    url = _base_url() + path
    manager = nullcontext(client) if client is not None else httpx.Client(
        timeout=90, trust_env=False, follow_redirects=False)
    try:
        with manager as connection:
            response = connection.request(method, url, json=payload, timeout=90, follow_redirects=False)
            if response.status_code != 200:
                raise ChatServiceError('Agent API 요청이 실패했습니다. 입력과 API·MCP 서비스 상태를 확인하세요.')
            return response.json()
    except (httpx.HTTPError, ValueError):
        raise ChatServiceError('Agent API에 연결하지 못했거나 응답 형식이 올바르지 않습니다.') from None


def respond_chat(query, profile=None, courses=None, context=None, *, client=None):
    """Preserve the direct function contract; HTTP mode never silently falls back."""
    if transport_mode() == 'local':
        from jolup.agent import respond
        return respond(query, profile, courses, context=context)
    context = context or {}
    payload = {'query': query, 'profile': profile, 'courses': courses or []}
    for key in ('session_id', 'builtin_sample_id'):
        if context.get(key) is not None:
            payload[key] = context[key]
    result = _request('POST', '/chat', payload=payload, client=client)
    from jolup.study_api import ChatResponse
    try:
        return ChatResponse.model_validate(result).model_dump()
    except ValidationError:
        raise ChatServiceError('Agent API가 약속한 채팅 응답 형식을 반환하지 않았습니다.') from None


def reset_chat_session(session_id, *, client=None):
    if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', session_id):
        raise ChatServiceError('초기화할 대화 세션 ID를 확인하세요.')
    if transport_mode() == 'local':
        return {'session_id': session_id, 'cleared': False}
    result = _request('DELETE', '/sessions/' + session_id, client=client)
    if (not isinstance(result, dict) or result.get('session_id') != session_id
            or type(result.get('cleared')) is not bool):
        raise ChatServiceError('Agent API의 세션 초기화 응답을 확인할 수 없습니다.')
    return result
