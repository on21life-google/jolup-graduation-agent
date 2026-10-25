"""The course HTTP chat boundary preserves domain results and isolated sessions."""
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from jolup import study_api
from jolup.samples import load_sample


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv('MCP_URL', raising=False)
    monkeypatch.setenv('JOLUP_RAG_MODE', 'lexical')
    with TestClient(study_api.app, raise_server_exceptions=False) as client:
        yield client


def request(sample_id='on_track', **extra):
    sample = load_sample(sample_id)
    return dict(query='나 졸업 괜찮아?', profile=sample['profile'], courses=sample['courses'], **extra)


def fake_result():
    return dict(status='확인 필요', answer='추가 자료 확인', rows=[], evidence=[], meta=[], intent='diagnosis')


def test_chat_calls_the_agent_and_preserves_current_vs_future(client):
    response = client.post('/chat', json=request())
    assert response.status_code == 200
    result = response.json()
    assert result['status'] == '미충족'
    assert result['totals']['total'] == 90
    assert result['plan']['status'] == '조건부 가능'
    assert result['rows'] and result['evidence'] and result['meta']
    assert result['transport_mode'] == 'http'
    assert result['ai_enabled'] is False
    UUID(result['session_id'])


def test_sessions_are_isolated_reused_and_reset_without_history_injection(client, monkeypatch):
    seen = []
    def respond(query, profile, courses, context):
        seen.append(context)
        return fake_result()
    monkeypatch.setattr(study_api, 'respond', respond, raising=False)
    a = client.post('/chat', json=request(session_id='student-a')).json()
    b = client.post('/chat', json=request(session_id='student-b')).json()
    assert a['session_id'] == 'student-a' and b['session_id'] == 'student-b'
    client.post('/chat', json=request(session_id='student-a'))
    assert seen[0]['conversation'] is seen[2]['conversation']
    assert seen[0]['conversation'] is not seen[1]['conversation']
    assert all(context['use_llm'] is False for context in seen)
    assert client.delete('/sessions/student-a').json() == {'session_id':'student-a', 'cleared':True}
    assert client.delete('/sessions/student-a').json()['cleared'] is False
    client.post('/chat', json=request(session_id='student-a'))
    assert seen[-1]['conversation'] is not seen[0]['conversation']
    assert client.post('/chat', json=request(history=[{'totals':{'total':999}}])).status_code == 422


@pytest.mark.parametrize('extra', [{'api_key':'PRIVATE_DO_NOT_ECHO'}, {'use_llm':True}, {'session_id':'../other'}, {'session_id':''}])
def test_chat_rejects_undeclared_credentials_and_invalid_session_contract(client, extra):
    response = client.post('/chat', json=request(**extra))
    assert response.status_code == 422
    assert 'PRIVATE_DO_NOT_ECHO' not in response.text


def test_chat_blank_query_and_backend_error_are_actionable(client, monkeypatch):
    body = request()
    body['query'] = '   '
    assert client.post('/chat', json=body).status_code == 422
    def fail(*args, **kwargs):
        raise RuntimeError('PRIVATE_BACKEND_DETAIL')
    monkeypatch.setattr(study_api, 'respond', fail, raising=False)
    response = client.post('/chat', json=request())
    assert response.status_code == 503
    assert 'PRIVATE_BACKEND_DETAIL' not in response.text
    assert response.json()['detail']


def test_chat_allows_regulation_without_student_and_documents_new_routes(client):
    response = client.post('/chat', json={'query':'2024학번 컴퓨터공학과 전공심화 졸업 총학점은?'})
    assert response.status_code == 200
    assert response.json()['intent'] == 'regulation'
    assert response.json()['status'] == '근거 확인'
    spec = client.get('/openapi.json').json()
    assert spec['paths']['/chat']['post']['requestBody']['required']
    assert 'delete' in spec['paths']['/sessions/{session_id}']
    assert 'api_key' not in spec['components']['schemas']['ChatRequest']['properties']


def test_memory_is_bounded_and_repeated_explicit_request_reuses_only_its_session(client):
    first = client.post('/chat', json=request(session_id='bounded')).json()
    assert first['memory']['turn_count'] == 1
    replay = request(session_id='bounded')
    replay['query'] = '아까 진단 다시 보여줘'
    again = client.post('/chat', json=replay).json()
    assert again['memory']['reused'] is True
    assert again['memory']['turn_count'] == 2
    for _ in range(4):
        last = client.post('/chat', json=request(session_id='bounded')).json()
    assert last['memory']['turn_count'] == 4
    assert last['memory']['window_size'] == 4


def test_session_eviction_removes_least_recently_used_memory(client, monkeypatch):
    monkeypatch.setattr(study_api, 'MAX_CHAT_SESSIONS', 2)
    monkeypatch.setattr(study_api, 'respond', lambda *args, **kwargs: fake_result())
    for session in ('a', 'b', 'a', 'c'):
        assert client.post('/chat', json=request(session_id=session)).status_code == 200
    assert client.delete('/sessions/b').json()['cleared'] is False
    assert client.delete('/sessions/a').json()['cleared'] is True
    assert client.delete('/sessions/c').json()['cleared'] is True


def test_domain_execution_error_is_transport_failure_not_successful_diagnosis(client, monkeypatch):
    monkeypatch.setattr(study_api, 'respond', lambda *args, **kwargs: dict(fake_result(), error_code='execution_error'))
    response = client.post('/chat', json=request())
    assert response.status_code == 503
    assert 'MCP' in response.json()['detail']


def test_chat_preserves_absent_student_facts_and_does_not_fill_schema_defaults(client, monkeypatch):
    seen = []
    monkeypatch.setattr(study_api, 'respond', lambda *args, **kwargs: seen.append((args, kwargs)) or fake_result())
    body = request()
    del body['profile']['engineering_accreditation']
    del body['profile']['academic_warning']
    assert client.post('/chat', json=body).status_code == 200
    profile = seen[0][0][1]
    assert 'engineering_accreditation' not in profile and 'academic_warning' not in profile
