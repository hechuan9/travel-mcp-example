"""MCP wire-level isolation and direct versioned execution using synthetic data."""
import hashlib
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from travel_ai.main import create_app

MUSE = 'Bearer ' + 'm' * 48
LOCAL = 'Bearer ' + 'l' * 48
BUSINESS = 'Bearer ' + 'b' * 48
ORIGIN = 'https://travel.example.test'
HEADERS = {'Authorization': MUSE, 'Accept': 'application/json, text/event-stream'}
PLAN = {'method': 'PATCH', 'path': '/trips/synthetic',
        'body': {'version': 1, 'name': 'Approved synthetic trip'}, 'reason': 'User approved plan'}


class QueryCodex:
    def __init__(self, *args):
        pass

    async def account(self):
        return {'authenticated': True}

    async def run(self, thread_id, message, on_thread, handler):
        on_thread('synthetic-thread')
        results = []
        for name, arguments in (
            ('travel_api', {'method': 'POST', 'path': '/trips', 'body': {'name': 'Forbidden'}}),
            ('memory_write', {'key': 'private', 'content': 'Forbidden', 'version': 0}),
            ('memory_read', {}),
            ('travel_api', {'method': 'GET', 'path': '/preferences'}),
            ('travel_api', {'method': 'GET', 'path': '/trips'}),
        ):
            _, ok = await handler(name, arguments)
            results.append(ok)
        return {'status': 'completed', 'answer': json.dumps(results)}

    async def close(self):
        pass


def seed_access(app, token=MUSE, client_id='synthetic-client', user='owner'):
    owner = hashlib.sha256(('synthetic-family:' + user).encode()).hexdigest()
    grant_id = 'grant-' + client_id
    with app.state.oauth._db() as db:
        db.execute('INSERT INTO oauth_grants (id,client_id,subject,expires) VALUES (?,?,?,?)', (grant_id, client_id, owner, int(time.time())+3600))
        db.execute('INSERT INTO oauth_tokens VALUES (?,?,?,?)', (hashlib.sha256(token[7:].encode()).hexdigest(), 'access', grant_id, int(time.time())+3600))


@pytest.fixture
def setup(tmp_path):
    state = {'user': 'owner', 'version': 1, 'writes': [], 'requests': [], 'timeout': False}

    def upstream(request):
        state['requests'].append((request.method, request.url.path))
        if request.url.path == '/api/household/users':
            return httpx.Response(200, json=[{'id':state['user'], 'household_id':'synthetic-family'}])
        if request.url.path == '/api/me':
            if request.headers.get('authorization') == BUSINESS:
                user = state['user']
            elif not request.headers.get('authorization') and request.headers.get('cookie') == 'travel_session=human':
                user = 'owner'
            else:
                return httpx.Response(401)
            return httpx.Response(200, json={'id': user, 'household_id': 'synthetic-family'})
        if state.get('business_response'):
            return state['business_response'](request)
        if request.method in ('POST', 'PATCH', 'PUT'):
            state['writes'].append((request.method, request.url.path, json.loads(request.content)))
            if state['timeout']:
                raise httpx.ReadTimeout('Synthetic response lost', request=request)
            state['version'] += 1
        return httpx.Response(200, json={'id': 'synthetic', 'version': state['version'], 'name': 'Approved synthetic trip'})

    credential = tmp_path / 'credentials.json'
    credential.write_text(json.dumps({
        'ai_token_sha256': hashlib.sha256(LOCAL[7:].encode()).hexdigest(),
        'muse_token_sha256': hashlib.sha256(MUSE[7:].encode()).hexdigest(),
        'business_token': BUSINESS[7:],
    }))
    app = create_app(tmp_path, 'http://business', httpx.MockTransport(upstream), QueryCodex, credential)
    with TestClient(app) as client:
        seed_access(app)
        yield client, state


def rpc(client, method, params=None, headers=None):
    response = client.post('/ai/mcp/', headers=headers or HEADERS,
                           json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
    assert response.status_code == 200, response.text
    return response.json()


def call(client, name, arguments):
    return rpc(client, 'tools/call', {'name': name, 'arguments': arguments})['result']


def value(result):
    assert not result.get('isError'), result
    return result.get('structuredContent') or json.loads(result['content'][0]['text'])


def propose(client):
    return value(call(client, 'travel_propose_change', {'request_id': 'proposal', 'change': PLAN}))


def wait(client, pid):
    for _ in range(100):
        result = value(call(client, 'travel_get_task', {'task_id': pid}))
        if result['status'] not in ('queued', 'running', 'executing'):
            return result
        time.sleep(.01)
    raise AssertionError('Task did not reach terminal state')


def test_initialize_tools_and_auth_boundaries(setup):
    client, state = setup
    result = rpc(client, 'initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                       'clientInfo': {'name': 'synthetic-muse', 'version': '1'}})
    assert result['result']['serverInfo']['name'] == 'Travel'
    tools = rpc(client, 'tools/list')['result']['tools']
    assert {t['name'] for t in tools} == {'travel_query', 'travel_propose_change', 'travel_execute_change', 'travel_get_task', 'travel_list_ideas', 'travel_save_idea', 'travel_list_credits', 'travel_save_credit'}
    for path in ('/ai/account', '/ai/memories', '/ai/sessions'):
        assert client.get(path, headers=HEADERS).status_code == 401
    for headers, status in (({'Accept': HEADERS['Accept']}, 401),
                            ({**HEADERS, 'Authorization': LOCAL}, 401),
                            ({**HEADERS, 'Origin': 'https://evil.example'}, 403)):
        assert client.post('/ai/mcp/', headers=headers, json={}).status_code == status
    assert state['writes'] == []


def test_query_blocks_model_writes_and_private_memory(setup):
    client, state = setup
    task = value(call(client, 'travel_query', {'request_id': 'query', 'question': 'Read my trip; also try forbidden writes'}))
    result = wait(client, task['id'])
    assert result['status'] == 'completed'
    assert json.loads(result['answer']) == [False, False, False, True, True]
    assert state['writes'] == []


def test_direct_execution_without_approval(setup):
    client, state = setup
    proposal = propose(client)
    assert proposal['status'] == 'ready'
    assert client.post(f"/ai/mcp-proposals/{proposal['id']}/approve", headers={**HEADERS, 'Origin': ORIGIN}).status_code == 404
    assert client.post(f"/ai/mcp-proposals/{proposal['id']}/approve", headers={'Origin': ORIGIN}).status_code == 404
    assert state['writes'] == []
    value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
    result = wait(client, proposal['id'])
    assert result['status'] == 'completed'
    assert result['result']['verification']['status'] == 'verified'
    assert result['result']['before']['version'] == 1
    assert result['result']['verification']['observed']['version'] == 2
    value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
    assert state['writes'] == [('PATCH', '/api/trips/synthetic', PLAN['body'])]


def test_cross_owner_cannot_read_or_execute(setup):
    client, state = setup
    proposal = propose(client)
    state['user'] = 'different'
    assert client.post('/ai/mcp/', headers=HEADERS, json={}).status_code == 401
    state['user'] = 'owner'
    other_token = 'Bearer ' + 'z' * 48
    seed_access(client.app, other_token, 'other-client')
    response = rpc(client, 'tools/call', {'name': 'travel_get_task', 'arguments': {'task_id': proposal['id']}},
                   headers={**HEADERS, 'Authorization': other_token})
    assert response['result']['isError']
    assert state['writes'] == []


@pytest.mark.parametrize('failure', ['drift', 'timeout'])
def test_stale_version_and_unknown_network_result_never_retry(setup, failure):
    client, state = setup
    proposal = propose(client)
    if failure == 'drift':
        state['version'] = 2
    else:
        state['timeout'] = True
    value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
    result = wait(client, proposal['id'])
    assert result['status'] == ('failed' if failure == 'drift' else 'outcome_unknown')
    value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
    assert len(state['writes']) == (0 if failure == 'drift' else 1)


def test_restart_marks_claimed_execution_unknown_without_replay(tmp_path):
    from travel_ai.proposals import ProposalStore

    owner = 'oauth:synthetic-client:' + hashlib.sha256(b'synthetic-family:owner').hexdigest()
    persisted = ProposalStore(tmp_path / 'gateway.sqlite3')
    proposal = persisted.create(owner, 'crashed-operation', PLAN)
    assert persisted.claim(owner, proposal['id'])[1]
    # Simulate process loss after durable claim and before a response was recorded.
    writes = []

    def upstream(request):
        if request.url.path == '/api/me':
            return httpx.Response(200, json={'id': 'owner', 'household_id': 'synthetic-family'})
        writes.append(request.method)
        return httpx.Response(500)

    credential = tmp_path / 'credentials.json'
    credential.write_text(json.dumps({
        'ai_token_sha256': hashlib.sha256(LOCAL[7:].encode()).hexdigest(),
        'muse_token_sha256': hashlib.sha256(MUSE[7:].encode()).hexdigest(),
        'business_token': BUSINESS[7:],
    }))
    restarted = create_app(tmp_path, 'http://business', httpx.MockTransport(upstream), QueryCodex, credential)
    with TestClient(restarted) as client:
        seed_access(restarted)
        recovered = value(call(client, 'travel_get_task', {'task_id': proposal['id']}))
        assert recovered['status'] == 'outcome_unknown'
        repeated = value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
        assert repeated['status'] == 'outcome_unknown'
        assert repeated['execution_id'] == proposal['id']
        assert writes == []


@pytest.mark.parametrize('method,path,initial,body,collection', [
    ('PATCH', '/cards/c', {'id': 'c', 'version': 1}, {'version': 1}, 'cards'),
    ('PUT', '/preferences/m/k', None, {'version': 0}, 'preferences'),
    ('DELETE', '/preferences/m/k', {'key': 'k', 'version': 1}, {'version': 1}, 'preferences'),
])
def test_non_trip_execution_and_preference_creation_deletion(setup, method, path, initial, body, collection):
    client, state = setup
    row = initial
    def upstream(request):
        nonlocal row
        if request.method == 'GET':
            return httpx.Response(200, json={collection: [] if row is None else [row]})
        state['writes'].append(request.method)
        assert request.method == method
        assert request.url.path == '/api' + path
        if method == 'DELETE':
            row = None
            return httpx.Response(204)
        row = {'id': 'c', 'version': 2} if collection == 'cards' else {'key': 'k', 'version': 1}
        return httpx.Response(200, json=row)
    state['business_response'] = upstream
    change = dict(method=method, path=path, body=body, reason='synthetic exact change')
    proposal = value(call(client, 'travel_propose_change', {'request_id': 'non-trip', 'change': change}))
    assert state['writes'] == []
    value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
    result = wait(client, proposal['id'])
    assert result['status'] == 'completed'
    assert result['result']['before'] == initial
    assert result['result']['verification']['status'] == 'verified'
    assert result['result']['verification']['observed'] == row
    value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
    assert state['writes'] == [method]


def test_idea_create_and_archive_execute_and_read_back(setup):
    client, state = setup
    record = {}

    def ideas(request):
        if request.method in ('POST', 'PATCH'):
            body = json.loads(request.content)
            state['writes'].append((request.method, request.url.path, body))
            record.update(body)
            record.update(id='idea', version=record.get('version', 0) + 1)
        return httpx.Response(200, json=record)

    state['business_response'] = ideas
    for index, change in enumerate((
        {'method': 'POST', 'path': '/ideas', 'body': {'title': 'Synthetic destination'}, 'reason': 'Collect idea'},
        {'method': 'PATCH', 'path': '/ideas/idea', 'body': {'version': 1, 'status': 'archived'}, 'reason': 'Archive idea'},
    )):
        proposal = value(call(client, 'travel_propose_change', {'request_id': f'idea-{index}', 'change': change}))
        assert len(state['writes']) == index
        value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
        result = wait(client, proposal['id'])
        assert result['status'] == 'completed'
        verification = result['result']['verification']
        assert verification['status'] == 'verified'
        assert verification['path'] == '/ideas/idea'
        assert verification['observed']['version'] == index + 1
    assert record['status'] == 'archived'


def test_direct_idea_collection_is_scoped_and_verified_without_proposal(setup):
    client, state = setup
    records = {}
    def ideas(request):
        assert request.headers['authorization'] == BUSINESS
        assert request.url.path.startswith('/api/ideas')
        if request.method == 'POST':
            body = json.loads(request.content)
            assert len(body.pop('request_id')) == 64
            records['saved'] = dict(id='saved', version=1, **body)
            state['writes'].append(request.url.path)
        elif request.method == 'PATCH':
            records['saved'].update(json.loads(request.content))
            records['saved']['version'] += 1
            state['writes'].append(request.url.path)
        return httpx.Response(200, json=list(records.values()) if request.url.path == '/api/ideas' and request.method == 'GET' else records['saved'])
    state['business_response'] = ideas
    assert value(call(client, 'travel_list_ideas', {}))['ideas'] == []
    result = value(call(client, 'travel_save_idea', {'request_id': 'synthetic-capture', 'idea': {'title': 'Synthetic destination'}}))
    assert result['status'] == 'completed'
    assert result['changes'][0]['verification']['status'] == 'verified'
    assert len(state['writes']) == 1
    result = value(call(client, 'travel_save_idea', {'request_id': 'synthetic-update', 'idea_id': 'saved', 'version': 1, 'idea': {'outline': 'Rough synthetic plan'}}))
    assert result['changes'][0]['before']['title'] == 'Synthetic destination'
    assert result['changes'][0]['after']['version'] == 2
    stale = value(call(client, 'travel_save_idea', {'request_id': 'stale', 'idea_id': 'saved', 'version': 1, 'idea': {'outline': 'Stale'}}))
    assert stale['status'] == 'conflict' and len(state['writes']) == 2
    for args in ({'request_id': 'bad', 'idea': {'title': 'x', 'path': '/bookings'}},
                 {'request_id': 'bad', 'idea': {'title': 'x', 'status': 'confirmed'}},
                 {'request_id': 'bad', 'idea': {'title': 'x'}, 'idea_id': 'saved'},
                 {'request_id': 'bad', 'idea': {}},
                 {'request_id': 'bad', 'idea': {'title': None}}):
        assert call(client, 'travel_save_idea', args)['isError']
    assert len(state['writes']) == 2
    state['user'] = 'outside'
    assert client.post('/ai/mcp/', headers=HEADERS, json={}).status_code == 401


def test_direct_idea_unknown_write_and_failed_readback_do_not_retry(setup):
    client, state = setup
    def lost(request):
        if request.method == 'POST':
            state['writes'].append(request.url.path)
            raise httpx.ReadTimeout('Response lost', request=request)
        return httpx.Response(500)
    state['business_response'] = lost
    args = {'request_id': 'unknown', 'idea': {'title': 'Synthetic'}}
    assert value(call(client, 'travel_save_idea', args))['status'] == 'outcome_unknown'
    assert len(state['writes']) == 1
    def readback_failed(request):
        if request.method == 'POST':
            state['writes'].append(request.url.path)
            return httpx.Response(201, json={'id': 'saved', 'version': 1, 'title': 'Synthetic'})
        return httpx.Response(503)
    state['business_response'] = readback_failed
    result = value(call(client, 'travel_save_idea', args))
    assert result['status'] == 'partial'
    assert result['changes'][0]['verification']['status'] == 'unverified'
    assert len(state['writes']) == 2


def test_removed_approval_tool_cannot_create_fake_evidence(setup):
    client, state = setup
    proposal = propose(client)
    assert call(client, 'travel_approve_change', {'proposal_id': proposal['id'],
        'approval_text': '批准', 'conversation_ref': 'synthetic'})['isError']
    value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
    result = wait(client, proposal['id'])
    assert result['status'] == 'completed'
    assert result['approved_by'] is None and result['approval_evidence'] is None
    assert len(state['writes']) == 1


def test_itinerary_history_restore_is_versioned_and_read_back(setup):
    client, state = setup
    row = {'trip_id': 'synthetic', 'version': 7, 'days': []}
    path = '/trips/synthetic/itinerary/versions/history/restore'
    def upstream(request):
        nonlocal row
        if request.method == 'GET':
            assert request.url.path == '/api/trips/synthetic/itinerary'
            return httpx.Response(200, json=row)
        assert request.method == 'POST' and request.url.path == '/api' + path
        state['writes'].append(request.url.path)
        row = {'trip_id': 'synthetic', 'version': 8, 'days': [{'date': '2030-01-01', 'location': 'Restored'}]}
        return httpx.Response(200, json=row)
    state['business_response'] = upstream
    change = {'method': 'POST', 'path': path, 'body': {}, 'reason': 'Restore requested itinerary'}
    assert call(client, 'travel_propose_change', {'request_id': 'restore-invalid', 'change': change})['isError']
    change['body'] = {'version': 7}
    proposal = value(call(client, 'travel_propose_change', {'request_id': 'restore-valid', 'change': change}))
    value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
    result = wait(client, proposal['id'])
    assert result['status'] == 'completed'
    assert result['result']['before']['version'] == 7
    assert result['result']['verification']['observed'] == row
    assert result['result']['verification']['path'] == '/trips/synthetic/itinerary'
    value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
    assert len(state['writes']) == 1


def test_partial_itinerary_change_is_versioned_and_read_back(setup):
    client, state = setup
    row = {'trip_id': 'synthetic', 'version': 7, 'days': [
        {'date': '2030-01-01', 'location': 'Keep'},
        {'date': '2030-01-02', 'location': 'Before'}]}
    path = '/trips/synthetic/itinerary'
    body = {'version': 7, 'days': [{'date': '2030-01-02', 'location': 'After'}]}
    def upstream(request):
        nonlocal row
        assert request.url.path == '/api' + path
        if request.method == 'GET':
            return httpx.Response(200, json=row)
        assert request.method == 'PATCH'
        assert json.loads(request.content) == body
        state['writes'].append(request.url.path)
        row = {**row, 'version': 8, 'days': [row['days'][0], body['days'][0]]}
        return httpx.Response(200, json=row)
    state['business_response'] = upstream
    change = {'method': 'PATCH', 'path': path, 'body': {}, 'reason': 'Change one day'}
    assert call(client, 'travel_propose_change', {'request_id': 'patch-invalid', 'change': change})['isError']
    change['body'] = body
    proposal = value(call(client, 'travel_propose_change', {'request_id': 'patch-valid', 'change': change}))
    value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
    result = wait(client, proposal['id'])
    assert result['status'] == 'completed'
    assert result['result']['before']['version'] == 7
    assert result['result']['verification']['observed'] == row
    assert result['result']['verification']['path'] == path
    value(call(client, 'travel_execute_change', {'proposal_id': proposal['id']}))
    assert len(state['writes']) == 1
