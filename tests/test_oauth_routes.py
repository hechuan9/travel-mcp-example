"""Synthetic browser consent -> OAuth tokens -> MCP, through public HTTP routes."""
import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from travel_ai.main import create_app

ORIGIN = 'https://travel.example.test'
RESOURCE = ORIGIN + '/ai/mcp/'
REDIRECT = 'https://synthetic-agent.example/callback'
VERIFIER = 'v' * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b'=').decode()
BUSINESS = 'b' * 48


class SyntheticCodex:
    def __init__(self, *args):
        pass

    async def account(self):
        return {'authenticated': True}

    async def run(self, thread_id, message, on_thread, handler):
        on_thread('synthetic-thread')
        return {'status': 'completed', 'answer': 'Synthetic trip'}

    async def close(self):
        pass


@pytest.fixture
def client(tmp_path):
    household_users = ['owner', 'sibling']
    def upstream(request):
        if request.url.path == '/api/login':
            credentials = json.loads(request.content)
            if credentials == {'email': 'owner@example.test', 'password': 'synthetic-password'}:
                return httpx.Response(200, json={'id': 'owner', 'household_id': 'family'},
                                      headers={'Set-Cookie': 'travel_session=human; Path=/; HttpOnly'})
            return httpx.Response(401, json={'error': 'invalid credentials'})
        if request.url.path == '/api/me':
            auth, cookie = request.headers.get('authorization'), request.headers.get('cookie', '')
            if auth == 'Bearer ' + BUSINESS or (not auth and 'travel_session=human' in cookie):
                owner = 'owner'
            elif not auth and 'travel_session=other' in cookie:
                owner = 'other'
            elif not auth and 'travel_session=sibling' in cookie:
                owner = 'sibling'
            else:
                return httpx.Response(401)
            return httpx.Response(200, json={'id': owner, 'household_id': 'foreign-family' if owner == 'other' else 'family', 'email': owner + '@example.test'})
        if request.url.path == '/api/household/users':
            return httpx.Response(200, json=[{'id': u, 'household_id':'family'} for u in household_users])
        return httpx.Response(200, json={'id': 'synthetic-trip', 'version': 1})

    credentials = tmp_path / 'credentials.json'
    credentials.write_text(json.dumps({'ai_token_sha256': hashlib.sha256(b'l' * 48).hexdigest(),
                                       'business_token': BUSINESS}))
    app = create_app(tmp_path, 'http://business', httpx.MockTransport(upstream), SyntheticCodex, credentials)
    app.state.synthetic_household_users = household_users
    with TestClient(app, base_url=ORIGIN) as browser:
        yield browser


def register(client):
    result = client.post('/register', json={'client_name': 'Synthetic agent', 'redirect_uris': [REDIRECT],
        'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'], 'scope': 'travel:mcp'})
    assert result.status_code == 201, result.text
    return result.json()['client_id']


def begin(client, cid, **overrides):
    params = {'client_id': cid, 'response_type': 'code', 'redirect_uri': REDIRECT, 'state': 'synthetic-state',
              'scope': 'travel:mcp', 'code_challenge': CHALLENGE, 'code_challenge_method': 'S256', 'resource': RESOURCE}
    params.update(overrides)
    return client.get('/authorize', params=params, follow_redirects=False)


def consent_page(client, cid):
    authorization = begin(client, cid)
    assert authorization.status_code in (302, 303), authorization.text
    page = client.get(authorization.headers['location'])
    assert page.status_code == 200, page.text
    # no-referrer makes native browser form POST send Origin: null.
    assert page.headers['referrer-policy'] == 'same-origin'
    assert "form-action 'self' https://synthetic-agent.example;" in page.headers['content-security-policy']
    fields = dict(re.findall(r'name="(request_id|csrf)" value="([^"]+)"', page.text))
    assert set(fields) == {'request_id', 'csrf'}
    return page, fields


def authorize(client, cid):
    client.cookies.set('travel_session', 'human')
    page, fields = consent_page(client, cid)
    assert '授权连接' in page.text
    response = client.post('/ai/oauth/consent', data={**fields, 'decision': 'approve'},
                           headers={'Origin': ORIGIN}, follow_redirects=False)
    assert response.status_code == 303, response.text
    params = parse_qs(urlsplit(response.headers['location']).query)
    assert params['state'] == ['synthetic-state']
    return params['code'][0]


def exchange(client, cid, code, **overrides):
    data = {'grant_type': 'authorization_code', 'client_id': cid, 'code': code, 'redirect_uri': REDIRECT,
            'code_verifier': VERIFIER, 'resource': RESOURCE}
    data.update(overrides)
    return client.post('/token', data=data)


def token(client, cid):
    response = exchange(client, cid, authorize(client, cid))
    assert response.status_code == 200, response.text
    return response.json()


def rpc(client, access, method, params=None):
    return client.post('/ai/mcp/', headers={'Authorization': 'Bearer ' + access,
        'Accept': 'application/json, text/event-stream'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})


def test_discovery_login_consent_mcp_refresh_and_revoke(client):
    metadata = client.get('/.well-known/oauth-authorization-server').json()
    assert metadata['issuer'].rstrip('/') == ORIGIN
    assert 'S256' in metadata['code_challenge_methods_supported']
    assert client.get('/.well-known/oauth-protected-resource').json()['resource'] == RESOURCE
    cid = register(client)
    page, fields = consent_page(client, cid)
    assert 'Travel 密码' in page.text
    login = client.post('/ai/oauth/login', headers={'Origin': ORIGIN}, data={**fields,
        'email': 'owner@example.test', 'password': 'synthetic-password'}, follow_redirects=False)
    assert login.status_code == 303, login.text
    page = client.get(login.headers['location'])
    assert '授权连接' in page.text
    approved = client.post('/ai/oauth/consent', headers={'Origin': ORIGIN},
        data={**fields, 'decision': 'approve'}, follow_redirects=False)
    assert approved.status_code == 303, approved.text
    code = parse_qs(urlsplit(approved.headers['location']).query)['code'][0]
    response = exchange(client, cid, code)
    assert response.status_code == 200, response.text
    tokens = response.json()
    tools = rpc(client, tokens['access_token'], 'tools/list')
    assert tools.status_code == 200, tools.text
    assert {tool['name'] for tool in tools.json()['result']['tools']} == {
        'travel_query', 'travel_propose_change', 'travel_execute_change', 'travel_get_task', 'travel_list_ideas', 'travel_save_idea', 'travel_list_credits', 'travel_save_credit'}
    assert exchange(client, cid, code).status_code == 400
    refresh_data = {'grant_type': 'refresh_token', 'client_id': cid, 'refresh_token': tokens['refresh_token'], 'resource': RESOURCE}
    refreshed = client.post('/token', data=refresh_data)
    assert refreshed.status_code == 200, refreshed.text
    assert rpc(client, tokens['access_token'], 'tools/list').status_code == 401
    new = refreshed.json()
    assert rpc(client, new['access_token'], 'tools/list').status_code == 200
    revoked = client.post('/revoke', data={'client_id': cid, 'token': new['refresh_token'], 'token_type_hint': 'refresh_token'})
    assert revoked.status_code == 200, revoked.text
    assert rpc(client, new['access_token'], 'tools/list').status_code == 401


def test_denial_csrf_machine_and_wrong_owner(client):
    cid = register(client)
    client.cookies.set('travel_session', 'human')
    _, fields = consent_page(client, cid)
    for headers, data in (({'Origin': ORIGIN}, {**fields, 'csrf': 'wrong'}),
                          ({'Origin': 'https://other.example'}, fields),
                          ({'Origin': ORIGIN, 'Authorization': 'Bearer synthetic-machine'}, fields)):
        assert client.post('/ai/oauth/consent', headers=headers, data={**data, 'decision': 'approve'}).status_code == 403
    client.cookies.set('travel_session', 'other')
    assert client.post('/ai/oauth/consent', headers={'Origin': ORIGIN}, data={**fields, 'decision': 'approve'}).status_code == 403
    client.cookies.set('travel_session', 'human')
    denied = client.post('/ai/oauth/consent', headers={'Origin': ORIGIN}, data={**fields, 'decision': 'deny'}, follow_redirects=False)
    assert denied.status_code == 303
    assert parse_qs(urlsplit(denied.headers['location']).query) == {'error': ['access_denied'], 'state': ['synthetic-state']}
    assert client.get('/ai/oauth/consent', params={'request_id': fields['request_id']}).status_code == 400


@pytest.mark.parametrize('override', [{'redirect_uri': 'https://other.example/callback'}, {'code_verifier': 'wrong' * 12},
                                      {'resource': 'https://other.example/mcp'}, {'resource': ''}])
def test_exchange_requires_redirect_pkce_and_resource(client, override):
    cid = register(client)
    code = authorize(client, cid)
    response = exchange(client, cid, code, **override)
    assert response.status_code == 400, response.text
    assert 'access_token' not in response.json()


def test_authorize_rejects_wrong_resource_and_redirect(client):
    cid = register(client)
    for override in ({'resource': 'https://other.example/mcp'}, {'redirect_uri': 'https://other.example/callback'}):
        result = begin(client, cid, **override)
        if result.status_code in (302, 303):
            assert 'error' in parse_qs(urlsplit(result.headers['location']).query)
            assert not result.headers['location'].startswith('https://other.example')
        else:
            assert result.status_code == 400, result.text


def test_clients_cannot_read_or_execute_each_others_tasks(client):
    first, second = register(client), register(client)
    one, two = token(client, first)['access_token'], token(client, second)['access_token']
    proposal = rpc(client, one, 'tools/call', {'name': 'travel_propose_change', 'arguments': {
        'request_id': 'synthetic-proposal', 'change': {'method': 'PATCH', 'path': '/trips/synthetic',
        'body': {'version': 1, 'name': 'Synthetic changed trip'}, 'reason': 'Synthetic test'}}})
    assert proposal.status_code == 200, proposal.text
    result = proposal.json()['result']
    assert not result.get('isError'), result
    value = result.get('structuredContent') or json.loads(result['content'][0]['text'])
    for tool, args in (('travel_get_task', {'task_id': value['id']}), ('travel_execute_change', {'proposal_id': value['id']})):
        response = rpc(client, two, 'tools/call', {'name': tool, 'arguments': args})
        assert response.status_code == 200, response.text
        assert response.json()['result']['isError']
    assert client.post('/ai/mcp-proposals/' + value['id'] + '/approve', headers={
        'Origin': ORIGIN, 'Authorization': 'Bearer ' + one}).status_code == 404


def test_reusing_rotated_refresh_revokes_the_grant(client):
    cid = register(client)
    original = token(client, cid)
    data = {'grant_type': 'refresh_token', 'client_id': cid,
            'refresh_token': original['refresh_token'], 'resource': RESOURCE}
    response = client.post('/token', data=data)
    assert response.status_code == 200, response.text
    newer = response.json()
    assert rpc(client, newer['access_token'], 'tools/list').status_code == 200
    assert client.post('/token', data=data).status_code == 400
    assert rpc(client, newer['access_token'], 'tools/list').status_code == 401
    assert client.post('/token', data={**data, 'refresh_token': newer['refresh_token']}).status_code == 400


def test_query_tasks_and_private_routes_are_client_scoped(client):
    cid, other = register(client), register(client)
    access, other_access = token(client, cid)['access_token'], token(client, other)['access_token']
    query = rpc(client, access, 'tools/call', {'name': 'travel_query', 'arguments': {
        'request_id': 'synthetic-query', 'question': 'Read synthetic trip'}})
    assert query.status_code == 200, query.text
    result = query.json()['result']
    assert not result.get('isError'), result
    task = result.get('structuredContent') or json.loads(result['content'][0]['text'])
    denied = rpc(client, other_access, 'tools/call', {'name': 'travel_get_task', 'arguments': {'task_id': task['id']}})
    assert denied.status_code == 200
    assert denied.json()['result']['isError']
    own = rpc(client, access, 'tools/call', {'name': 'travel_get_task', 'arguments': {'task_id': task['id']}})
    assert not own.json()['result'].get('isError'), own.text
    for route in ('/ai/sessions', '/ai/memories', '/ai/account'):
        assert client.get(route, headers={'Authorization': 'Bearer ' + access}).status_code == 401


def test_failed_login_does_not_authorize_and_bearer_cannot_open_consent(client):
    cid = register(client)
    _, fields = consent_page(client, cid)
    response = client.post('/ai/oauth/login', data={**fields, 'email': 'owner@example.test', 'password': 'wrong'},
                           headers={'Origin': ORIGIN})
    assert response.status_code == 200
    assert '邮箱或密码不正确' in response.text
    assert 'travel_session' not in client.cookies
    assert client.post('/ai/oauth/consent', data={**fields, 'decision': 'approve'},
                       headers={'Origin': ORIGIN}).status_code == 401
    assert client.get('/ai/oauth/consent', params={'request_id': fields['request_id']},
                      headers={'Authorization': 'Bearer synthetic-machine'}).status_code == 403


def test_parallel_browser_authorizations_do_not_invalidate_each_other(client):
    cid = register(client)
    _, first = consent_page(client, cid)
    _, second = consent_page(client, cid)
    # Browser/CLI double-open and a second connection attempt share the cookie jar.
    reloaded = client.get('/ai/oauth/consent', params={'request_id': first['request_id']})
    assert reloaded.status_code == 200
    login = client.post('/ai/oauth/login', headers={'Origin': ORIGIN},
        data={**first, 'email': 'owner@example.test', 'password': 'synthetic-password'}, follow_redirects=False)
    assert login.status_code == 303
    for fields in (first, second):
        result = client.post('/ai/oauth/consent', headers={'Origin': ORIGIN},
            data={**fields, 'decision': 'approve'}, follow_redirects=False)
        assert result.status_code == 303, result.text
        assert 'code' in parse_qs(urlsplit(result.headers['location']).query)


def test_oauth_survives_management_token_removal(client, tmp_path):
    path = tmp_path / 'credentials.json'
    value = json.loads(path.read_text())
    del value['ai_token_sha256']
    path.write_text(json.dumps(value))
    assert client.get('/ai/account', headers={'Authorization': 'Bearer ' + 'l' * 48}).status_code == 401
    cid = register(client)
    tokens = token(client, cid)
    assert rpc(client, tokens['access_token'], 'tools/list').status_code == 200


def test_switch_account_requires_csrf_and_preserves_authorization(client):
    cid = register(client)
    client.cookies.set('travel_session', 'human', domain='travel.example.test', path='/')
    page, fields = consent_page(client, cid)
    assert '切换账号' in page.text
    assert client.post('/ai/oauth/switch-account', data=fields, headers={'Origin': 'https://other.example'}).status_code == 403
    assert client.post('/ai/oauth/switch-account', data={**fields, 'csrf':'wrong'}, headers={'Origin': ORIGIN}).status_code == 403
    switched = client.post('/ai/oauth/switch-account', data=fields, headers={'Origin': ORIGIN})
    assert switched.status_code == 200 and 'Travel 邮箱' in switched.text
    assert not client.cookies.get('travel_session')
    assert client.post('/ai/oauth/consent', data={**fields, 'decision':'approve'}, headers={'Origin':ORIGIN}).status_code == 401
    # A newly logged-in, unauthorized identity cannot inherit the prior user's consent.
    client.cookies.set('travel_session','other',domain='travel.example.test',path='/')
    assert client.post('/ai/oauth/consent', data={**fields, 'decision':'approve'}, headers={'Origin':ORIGIN}).status_code == 403
    login = client.post('/ai/oauth/login',data={**fields,'email':'owner@example.test','password':'synthetic-password'},headers={'Origin':ORIGIN},follow_redirects=False)
    assert login.status_code == 303
    consent = client.get(login.headers['location'])
    assert 'owner@example.test' in consent.text and '授权连接' in consent.text
    approved = client.post('/ai/oauth/consent',data={**fields,'decision':'approve'},headers={'Origin':ORIGIN},follow_redirects=False)
    assert approved.status_code == 303 and 'code=' in approved.headers['location']


def test_same_family_member_authorizes_with_separate_identity(client):
    cid = register(client)
    client.cookies.set('travel_session','sibling')
    page, fields = consent_page(client, cid)
    assert 'sibling@example.test' in page.text and '授权连接' in page.text
    result = client.post('/ai/oauth/consent',data={**fields,'decision':'approve'},headers={'Origin':ORIGIN},follow_redirects=False)
    assert result.status_code == 303
    code = parse_qs(urlsplit(result.headers['location']).query)['code'][0]
    t = exchange(client,cid,code).json()
    assert rpc(client,t['access_token'],'tools/list').status_code == 200
    access = client.portal.call(client.app.state.oauth.load_access_token,t['access_token'])
    assert access.subject == hashlib.sha256(b'family:sibling').hexdigest()
    # Original subscriber cannot see the sibling's private MCP tasks through their token.
    result=rpc(client,t['access_token'],'tools/call',{'name':'travel_propose_change','arguments':{'request_id':'sibling-plan','change':{'method':'POST','path':'/trips','body':{'name':'Synthetic'},'reason':'Test'}}})
    assert result.status_code == 200
    proposal=json.loads(result.json()['result']['content'][0]['text'])
    client.cookies.set('travel_session','human')
    assert client.post('/ai/mcp-proposals/'+proposal['id']+'/approve',headers={'Origin':ORIGIN}).status_code == 404
    client.cookies.set('travel_session','sibling')
    assert client.post('/ai/mcp-proposals/'+proposal['id']+'/approve',headers={'Origin':ORIGIN}).status_code == 404

    # Removing a member invalidates their already-issued token on the next MCP call.
    client.app.state.synthetic_household_users.remove('sibling')
    assert rpc(client,t['access_token'],'tools/list').status_code == 401


def test_account_connections_are_human_subject_scoped_and_revocable(client):
    path = '/ai/account/mcp-connections'
    assert client.get(path).status_code == 401
    cid = register(client)
    tokens = token(client, cid)
    result = client.get(path)
    assert result.status_code == 200
    assert result.headers['cache-control'] == 'no-store'
    connections = result.json()['connections']
    assert len(connections) == 1
    connection = connections[0]
    assert set(connection) == {'id', 'client_name', 'created_at', 'expires_at', 'status', 'last_used_at'}
    assert connection['client_name'] == 'Synthetic agent'
    assert connection['created_at'] < connection['expires_at']
    assert connection['status'] == 'active'
    assert connection['last_used_at'] is None
    assert tokens['access_token'] not in result.text
    revoke = path + '/' + connection['id'] + '/revoke'
    assert client.get(path, headers={'Authorization': 'Bearer ' + tokens['access_token']}).status_code == 403
    assert client.post(revoke).status_code == 403
    assert client.post(revoke, headers={'Origin': 'https://foreign.example'}).status_code == 403
    client.cookies.set('travel_session', 'sibling')
    assert client.get(path).json() == {'connections': []}
    assert client.post(revoke, headers={'Origin': ORIGIN}).status_code == 404
    client.cookies.set('travel_session', 'other')
    assert client.get(path).status_code == 403
    assert client.post(revoke, headers={'Origin': ORIGIN}).status_code == 403
    client.cookies.set('travel_session', 'human')
    assert rpc(client, tokens['access_token'], 'tools/list').status_code == 200
    assert client.get(path).json()['connections'][0]['last_used_at'] is not None
    assert client.post(revoke, headers={'Origin': ORIGIN}).json() == {'revoked': True}
    assert client.get(path).json() == {'connections': []}
    assert rpc(client, tokens['access_token'], 'tools/list').status_code == 401
    assert client.post('/token', data={'grant_type': 'refresh_token', 'client_id': cid,
        'refresh_token': tokens['refresh_token'], 'resource': RESOURCE}).status_code == 400
