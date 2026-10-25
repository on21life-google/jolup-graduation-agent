"""Streamlit's shared adapter makes one local or private HTTP chat call."""
import importlib
import json

import httpx
import pytest


def module():
    return importlib.import_module('jolup.chat_client')


def result(**extra):
    return dict(status='확인 필요', answer='자료 확인', evidence=[], meta=[], rows=[],
                intent='diagnosis', session_id='session-a', transport_mode='http', ai_enabled=False, **extra)


def test_local_adapter_preserves_explicit_local_ai_and_memory(monkeypatch):
    chat = module()
    monkeypatch.delenv('JOLUP_AGENT_API_URL', raising=False)
    from jolup import agent
    seen = []
    context = {'use_llm':True,'llm_config':{'api_key':'PRIVATE_KEY'},'conversation':object()}
    monkeypatch.setattr(agent,'respond', lambda *a, **kw: seen.append((a,kw)) or {'answer':'local'})
    assert chat.respond_chat('질문',{},[],context)['answer'] == 'local'
    assert seen == [(('질문',{},[]),{'context':context})]
    assert chat.transport_mode() == 'local'
    assert chat.reset_chat_session('session-a') == {'session_id':'session-a','cleared':False}


def test_http_adapter_sends_only_declared_fields_and_keeps_result_details(monkeypatch):
    chat = module()
    monkeypatch.setenv('JOLUP_AGENT_API_URL','http://api:8001/')
    seen = []
    def handle(req):
        seen.append(req)
        return httpx.Response(200,json=result(plan={'status':'조건부 가능'},memory={'turn_count':2}))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        got=chat.respond_chat('질문',{'is_dummy':True},[],{'session_id':'session-a','builtin_sample_id':'on_track',
             'use_llm':True,'llm_config':{'api_key':'PRIVATE_KEY'},'conversation':object()},client=client)
        assert not client.is_closed
    payload=json.loads(seen[0].content)
    assert payload == {'query':'질문','profile':{'is_dummy':True},'courses':[],
                       'session_id':'session-a','builtin_sample_id':'on_track'}
    assert 'PRIVATE_KEY' not in seen[0].content.decode()
    assert str(seen[0].url) == 'http://api:8001/chat'
    assert got['plan']['status']=='조건부 가능' and got['memory']['turn_count']==2
    assert chat.transport_mode()=='http'


@pytest.mark.parametrize('url',['https://external.example','http://127.0.0.1.evil.test','http://name:pw@localhost:8001',
                               'http://localhost:8001/other','http://localhost:8001?next=x','http://[bad'])
def test_address_outside_the_local_project_is_rejected_before_any_request(monkeypatch,url):
    chat=module();monkeypatch.setenv('JOLUP_AGENT_API_URL',url)
    with httpx.Client(transport=httpx.MockTransport(lambda req: pytest.fail('must not send'))) as client:
        with pytest.raises(chat.ChatServiceError): chat.respond_chat('질문',client=client)


@pytest.mark.parametrize('status,payload',[(503,{'detail':'PRIVATE_BACKEND'}),(302,{}),(200,{'answer':'incomplete'}),(200,['unexpected'])])
def test_bad_http_or_schema_response_becomes_fixed_error(monkeypatch,status,payload):
    chat=module();monkeypatch.setenv('JOLUP_AGENT_API_URL','http://127.0.0.1:8001')
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(status,json=payload))) as client:
        with pytest.raises(chat.ChatServiceError) as error: chat.respond_chat('질문',client=client)
    assert 'PRIVATE_BACKEND' not in str(error.value)


def test_connection_failure_is_fixed_and_never_falls_back_silently(monkeypatch):
    chat=module();monkeypatch.setenv('JOLUP_AGENT_API_URL','http://localhost:8001')
    def fail(req): raise httpx.ConnectError('PRIVATE_CONNECTION',request=req)
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(chat.ChatServiceError) as error: chat.respond_chat('질문',client=client)
    assert 'PRIVATE_CONNECTION' not in str(error.value)


def test_reset_session_uses_delete_and_checks_response(monkeypatch):
    chat=module();monkeypatch.setenv('JOLUP_AGENT_API_URL','http://localhost:8001')
    seen=[]
    def handle(req):
        seen.append(req)
        return httpx.Response(200,json={'session_id':'session-a','cleared':True})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert chat.reset_chat_session('session-a',client=client)['cleared'] is True
    assert seen[0].method=='DELETE' and seen[0].url.path=='/sessions/session-a'
    with pytest.raises(chat.ChatServiceError): chat.reset_chat_session('../bad')


def test_owned_http_client_is_closed_with_no_proxy_or_redirects(monkeypatch):
    chat=module();monkeypatch.setenv('JOLUP_AGENT_API_URL','http://localhost:8001')
    captured = {}
    real_client = httpx.Client
    def create(**kwargs):
        captured['options'] = kwargs
        captured['client'] = real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200,json=result())), **kwargs)
        return captured['client']
    monkeypatch.setattr(chat.httpx, 'Client', create)
    assert chat.respond_chat('질문')['session_id'] == 'session-a'
    assert captured['client'].is_closed
    assert captured['options'] == {'timeout':90,'trust_env':False,'follow_redirects':False}


@pytest.mark.parametrize('payload', [['not-a-result'], {'session_id':'different','cleared':True},
                                    {'session_id':'session-a','cleared':'true'}])
def test_reset_rejects_malformed_or_wrong_session_acknowledgment(monkeypatch,payload):
    chat=module();monkeypatch.setenv('JOLUP_AGENT_API_URL','http://localhost:8001')
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=payload))) as client:
        with pytest.raises(chat.ChatServiceError): chat.reset_chat_session('session-a',client=client)


def test_non_json_http_success_is_rejected(monkeypatch):
    chat=module();monkeypatch.setenv('JOLUP_AGENT_API_URL','http://localhost:8001')
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,text='not json'))) as client:
        with pytest.raises(chat.ChatServiceError): chat.respond_chat('질문',client=client)
