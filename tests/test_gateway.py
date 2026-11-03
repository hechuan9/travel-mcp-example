import asyncio
import time
import json
import hashlib
import httpx
from fastapi.testclient import TestClient
from travel_ai.main import create_app
from travel_ai.store import Store
from travel_ai.tools import ToolHandler
TOKEN_A = 'Bearer ' + 'a' * 48
TOKEN_B = 'Bearer ' + 'b' * 48

def upstream(request):
    if request.url.path == '/api/me':
        token = request.headers.get('authorization')
        if token not in (TOKEN_A, TOKEN_B): return httpx.Response(401)
        return httpx.Response(200, json={'id': 'a' if token == TOKEN_A else 'b', 'household_id': 'same-family'})
    return httpx.Response(200, json={'trips': []})

class FakeCodex:
    authenticated = True
    calls = []
    def __init__(self, home, tools, instructions): self.home = home
    async def account(self): return {'authenticated': self.authenticated, 'plan': 'synthetic'}
    async def begin_login(self): return {'verificationUrl': 'https://example.test', 'userCode': 'SYNTHETIC'}
    async def run(self, thread_id, message, on_thread, handler):
        self.calls.append((str(self.home), thread_id))
        on_thread(thread_id or 'synthetic-thread')
        await asyncio.sleep(.02)
        if message == 'fail': raise RuntimeError('private provider detail')
        memories, ok = await handler('memory_read', {})
        return {'status': 'completed', 'answer': str(memories)}
    async def close(self): pass

def credentials(path, inbound=TOKEN_A, outbound=TOKEN_A):
    file = path / 'credentials.json'
    file.write_text(json.dumps({'ai_token_sha256': hashlib.sha256(inbound[7:].encode()).hexdigest(), 'business_token': outbound[7:]}))
    return file

def client(path):
    return TestClient(create_app(path, 'http://business', httpx.MockTransport(upstream), FakeCodex, credentials(path)))

def wait(c, run):
    for _ in range(100):
        result = c.get('/ai/runs/' + run['id']).json()
        if result['status'] not in ('queued', 'running'): return result
        time.sleep(.01)
    raise AssertionError('run did not finish')

def test_identity_isolation_memory_and_restart(tmp_path):
    FakeCodex.calls = []
    with client(tmp_path) as c:
        assert c.get('/ai/sessions').status_code == 401
        c.cookies.set('travel_session', 'not-a-machine-credential')
        c.headers['Authorization'] = 'Bearer ' + 'invalid' * 8
        assert c.get('/ai/sessions').status_code == 401
        c.headers['Authorization'] = TOKEN_A
        session = c.post('/ai/sessions', json={'title':'测试'}).json()
        memory = {'key':'preference','content':'合成用户偏好','version':0}
        assert c.put('/ai/memories/preference', json=memory).status_code == 200
        assert c.put('/ai/memories/preference', json=memory).status_code == 409
        message = {'request_id':'unique-request','message':'读取记忆'}
        run = c.post(f"/ai/sessions/{session['id']}/messages", json=message).json()
        result = wait(c, run)
        assert result['status'] == 'completed' and '合成用户偏好' in result['answer']
        assert 'owner' not in result
        repeated = c.post(f"/ai/sessions/{session['id']}/messages", json=message).json()
        assert repeated['id'] == run['id'] and len(FakeCodex.calls) == 1
        assert c.post(f"/ai/sessions/{session['id']}/messages", json={**message, 'message':'changed'}).status_code == 409
        credentials(tmp_path, TOKEN_B, TOKEN_B)
        c.headers['Authorization'] = TOKEN_B
        assert c.get('/ai/sessions').json() == []
        assert c.get('/ai/memories').json() == []
        assert c.get('/ai/sessions/' + session['id']).status_code == 404
        assert c.get('/ai/runs/' + run['id']).status_code == 404
        assert c.post(f"/ai/sessions/{session['id']}/messages", json=message).status_code == 404
    with client(tmp_path) as c:
        c.headers['Authorization'] = TOKEN_A
        assert len(c.get('/ai/memories').json()) == 1
        assert c.get('/ai/runs/' + run['id']).json()['status'] == 'completed'
        resumed = c.post(f"/ai/sessions/{session['id']}/messages", json={'request_id':'resume','message':'读取'}).json()
        assert wait(c, resumed)['status'] == 'completed'
        assert FakeCodex.calls[-1][1] == 'synthetic-thread'
        assert c.request('DELETE','/ai/memories/preference',json={'version':1}).status_code == 204
        assert c.get('/ai/memories').json() == []

def test_not_logged_in_failures_and_recovery(tmp_path, monkeypatch):
    with client(tmp_path) as c:
        c.headers['Authorization'] = TOKEN_A
        session = c.post('/ai/sessions',json={}).json()
        monkeypatch.setattr(FakeCodex,'authenticated',False)
        assert c.post(f"/ai/sessions/{session['id']}/messages", json={'request_id':'1','message':'x'}).status_code == 409
        monkeypatch.setattr(FakeCodex,'authenticated',True)
        run = c.post(f"/ai/sessions/{session['id']}/messages",json={'request_id':'1','message':'fail'}).json()
        result = wait(c,run)
        assert result['status'] == 'failed' and 'private provider' not in str(result)
    store = Store(tmp_path/'gateway.sqlite3')
    with store.connect() as db: owner = db.execute('SELECT owner FROM sessions LIMIT 1').fetchone()['owner']
    pending,_ = store.create_run(owner,session['id'],'unfinished','x')
    store.start_tool(pending['id'],'travel_api',{'method':'POST','path':'/trips'})
    restarted = Store(tmp_path/'gateway.sqlite3')
    result = restarted.run(owner,pending['id'])
    assert result['status'] == 'interrupted' and result['tools'][0]['status'] == 'outcome_unknown'

def test_tool_boundaries_unknown_writes_and_deduplication(tmp_path):
    store = Store(tmp_path/'state.sqlite3')
    session = store.create_session('owner','test')
    run,_ = store.create_run('owner',session['id'],'req','test')
    count = []
    def handler(request):
        count.append(request.method)
        if request.url.path.endswith('/timeout'): raise httpx.ReadTimeout('synthetic')
        return httpx.Response(200,json={'id':'synthetic-record'})
    async def scenario():
        async with httpx.AsyncClient(base_url='http://business',transport=httpx.MockTransport(handler)) as api:
            tools = ToolHandler(store,'owner',run['id'],api)
            for path in ('https://evil.test/api/trips','//evil.test/trips','/trips/../plans/x/decision','/trips/%2e%2e/login','/plans/x/decision','/connections/gmail/authorize'):
                assert not (await tools('travel_api', {'method':'POST','path':path,'body':{}}))[1]
            write = {'method':'POST','path':'/trips','body':{'name':'合成'}}
            assert (await tools('travel_api', write))[1]
            assert (await tools('travel_api', write))[1]
            assert count == ['POST']
            assert not (await tools('travel_api',{'method':'PATCH','path':'/trips/timeout','body':{}}))[1]
            assert not (await tools('travel_api',{'method':'POST','path':'/trips','body':{'name':'other'}}))[1]
            assert (await tools('travel_api',{'method':'GET','path':'/trips'}))[1]
            assert count == ['POST','PATCH','GET']
    asyncio.run(scenario())
    assert store.run('owner',run['id'])['tools'][1]['status'] == 'outcome_unknown'

def test_thread_binding_changed_during_account_check_is_not_lost(tmp_path, monkeypatch):
    app = create_app(tmp_path, 'http://business', httpx.MockTransport(upstream), FakeCodex, credentials(tmp_path))
    with TestClient(app) as c:
        c.headers['Authorization'] = TOKEN_A
        session = c.post('/ai/sessions', json={}).json()
        async def account_with_previous_turn_completion(self):
            with app.state.store.connect() as db:
                owner = db.execute('SELECT owner FROM sessions WHERE id=?',(session['id'],)).fetchone()['owner']
            app.state.store.bind_thread(owner, session['id'], 'thread-bound-during-await')
            return {'authenticated':True}
        monkeypatch.setattr(FakeCodex,'account',account_with_previous_turn_completion)
        run = c.post(f"/ai/sessions/{session['id']}/messages", json={'request_id':'race','message':'继续'}).json()
        assert wait(c,run)['status'] == 'completed'
        assert FakeCodex.calls[-1][1] == 'thread-bound-during-await'

def test_member_memory_uses_preferences_api_without_copying_legacy_notes(tmp_path):
    store = Store(tmp_path/'memory.sqlite3')
    store.put_memory('owner','legacy','历史个人笔记',0)
    session = store.create_session('owner','test')
    run,_ = store.create_run('owner',session['id'],'member-memory','test')
    requests = []
    def handler(request):
        requests.append((request.method,str(request.url),request.content))
        return httpx.Response(200,json={'member':{'id':'member-a'},'preferences':[]})
    async def scenario():
        async with httpx.AsyncClient(base_url='http://business',transport=httpx.MockTransport(handler)) as api:
            tool = ToolHandler(store,'owner',run['id'],api)
            old,ok = await tool('memory_read',{})
            assert ok and old[0]['content']=='历史个人笔记' and requests==[]
            result,ok = await tool('memory_read',{'member_id':'member-a'})
            assert ok and '/preferences?member_id=member-a' in requests[-1][1]
            payload={'member_id':'member-a','key':'pace','content':'合成偏好','version':0,'category':'travel_style','status':'confirmed','source_note':'合成用户明确确认'}
            assert (await tool('memory_write',payload))[1]
            assert requests[-1][0]=='PUT' and requests[-1][1].endswith('/preferences/member-a/pace')
            assert not (await tool('memory_write',{'key':'guess','content':'不得猜测归属','version':0}))[1]
            assert not (await tool('travel_api',{'method':'POST','path':'/imports/apply','body':{}}))[1]
    asyncio.run(scenario())
    assert len(requests)==2
    assert store.memories('owner')[0]['content']=='历史个人笔记'


def test_ai_token_is_exchanged_never_forwarded_and_no_fallback(tmp_path):
    seen = []
    def server(request):
        seen.append(request.headers.get('authorization'))
        assert seen[-1] == TOKEN_B
        return upstream(request)
    class ProbeCodex(FakeCodex):
        async def run(self, thread_id, message, on_thread, handler):
            on_thread('synthetic-probe')
            data, ok = await handler('travel_api', {'method': 'GET', 'path': '/trips'})
            assert ok
            return {'status': 'completed', 'answer': 'verified'}
    app = create_app(tmp_path, 'http://business', httpx.MockTransport(server), ProbeCodex, credentials(tmp_path, TOKEN_A, TOKEN_B))
    with TestClient(app) as c:
        c.headers['Authorization'] = TOKEN_A
        assert c.get('/ai/sessions').status_code == 200
        assert seen == [TOKEN_B]
        session = c.post('/ai/sessions', json={}).json()
        run = c.post(f"/ai/sessions/{session['id']}/messages", json={'request_id':'probe', 'message':'read'}).json()
        assert wait(c, run)['answer'] == 'verified'
        assert all(value == TOKEN_B for value in seen)
        seen.clear()
        c.headers['Authorization'] = TOKEN_B
        c.cookies.set('travel_session', 'synthetic-human')
        assert c.get('/ai/sessions').status_code == 401
        assert seen == []
        (tmp_path / 'credentials.json').unlink()
        c.headers['Authorization'] = TOKEN_A
        assert c.get('/ai/sessions').status_code == 503
        assert seen == []


def test_preference_delete_is_narrowly_allowed(tmp_path):
    store = Store(tmp_path / 'delete.sqlite')
    session = store.create_session('owner', 'cleanup')
    run, _ = store.create_run('owner', session['id'], 'cleanup', 'cleanup')
    seen = []
    def response(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={'deleted': True})
    async def scenario():
        async with httpx.AsyncClient(base_url='http://business', transport=httpx.MockTransport(response)) as api:
            handler = ToolHandler(store, 'owner', run['id'], api)
            assert not (await handler('travel_api', {'method': 'DELETE', 'path': '/members/person', 'body': {'version': 1}}))[1]
            assert (await handler('travel_api', {'method': 'DELETE', 'path': '/preferences/person/identity_aliases', 'body': {'version': 1}}))[1]
    asyncio.run(scenario())
    assert seen == [('DELETE', '/api/preferences/person/identity_aliases')]
