"""Synthetic MCP conversations preserve threads without crossing authenticated owners."""
import asyncio
import hashlib
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from travel_ai.main import create_app
from test_mcp import BUSINESS, HEADERS, LOCAL, MUSE, rpc, seed_access, value


@pytest.fixture
def conversations(tmp_path):
    calls = []

    class ConversationCodex:
        def __init__(self, *args):
            pass

        async def account(self):
            return {'authenticated': True}

        async def run(self, thread_id, message, on_thread, handler):
            thread = thread_id or f'synthetic-thread-{len(calls) + 1}'
            calls.append({'thread_id': thread_id, 'thread': thread, 'message': message})
            on_thread(thread)
            if 'delayed-marker' in message:
                await asyncio.sleep(0.15)
            return {'status': 'completed', 'answer': json.dumps({
                'status': 'completed', 'summary': 'Synthetic conversation response',
                'unresolved': ['Which synthetic city?'] if 'clarify-marker' in message else [],
                **({'pending_confirmations': [{'question': '批准合成日程调整？', 'proposal_id': None}]}
                   if 'approval-marker' in message else {}),
            })}

        async def close(self):
            pass

    def upstream(request):
        if request.url.path == '/api/me':
            return httpx.Response(200, json={'id': 'owner', 'household_id': 'synthetic-family'})
        if request.url.path == '/api/household/users':
            return httpx.Response(200, json=[{'id': 'owner', 'household_id': 'synthetic-family'}])
        return httpx.Response(200, json=[])

    credential = tmp_path / 'credentials.json'
    credential.write_text(json.dumps({
        'ai_token_sha256': hashlib.sha256(LOCAL[7:].encode()).hexdigest(),
        'muse_token_sha256': hashlib.sha256(MUSE[7:].encode()).hexdigest(),
        'business_token': BUSINESS[7:],
    }))
    app = create_app(tmp_path, 'http://business', httpx.MockTransport(upstream), ConversationCodex, credential)
    with TestClient(app) as client:
        seed_access(app)
        yield client, calls


def query(client, request_id, question='Synthetic question', session_id=None, chat=None, headers=None):
    args = {'request_id': request_id, 'question': question}
    if session_id is not None:
        args['session_id'] = session_id
    params = {'name': 'travel_query', 'arguments': args}
    if chat is not None:
        params['_meta'] = {'openai/session': chat}
    return rpc(client, 'tools/call', params, headers=headers)['result']


def completed(client, task, headers=None):
    for _ in range(100):
        result = value(rpc(client, 'tools/call', {
            'name': 'travel_get_task', 'arguments': {'task_id': task['id']},
        }, headers=headers)['result'])
        if result['status'] not in ('queued', 'running'):
            assert result['status'] == 'completed', result
            return result
        time.sleep(.01)
    pytest.fail('Synthetic conversation did not complete')


def test_explicit_session_preserves_three_turns_and_receipt(conversations):
    client, calls = conversations
    first = value(query(client, 'explicit-1', 'Compare synthetic hotels A and B'))
    first_done = completed(client, first)
    session = first_done['session_id']
    assert first_done['receipt'] == {'status': 'completed', 'summary': 'Synthetic conversation response', 'unresolved': []}
    assert first_done['continuation']['action'] == 'done'
    assert first_done['continuation']['session_id'] == session
    for index, question in enumerate(('Is the second one closer?', 'What did I ask first?'), 2):
        task = value(query(client, f'explicit-{index}', question, session_id=session))
        assert completed(client, task)['session_id'] == session
    assert [call['thread_id'] for call in calls] == [None, calls[0]['thread'], calls[0]['thread']]


def test_openai_session_metadata_automatically_resumes_and_separates_chats(conversations):
    client, calls = conversations
    tasks = []
    for index in range(3):
        task = value(query(client, f'metadata-{index}', f'Follow-up {index}', chat='synthetic-chat-a'))
        tasks.append(completed(client, task))
    assert len({task['session_id'] for task in tasks}) == 1
    assert [call['thread_id'] for call in calls] == [None, calls[0]['thread'], calls[0]['thread']]
    other = completed(client, value(query(client, 'other-chat', chat='synthetic-chat-b')))
    assert other['session_id'] != tasks[0]['session_id']
    assert calls[-1]['thread_id'] is None


def test_absent_session_metadata_keeps_independent_queries(conversations):
    client, calls = conversations
    first = completed(client, value(query(client, 'independent-1')))
    second = completed(client, value(query(client, 'independent-2')))
    assert first['session_id'] != second['session_id']
    assert [call['thread_id'] for call in calls] == [None, None]


def test_same_chat_metadata_does_not_cross_oauth_owners(conversations):
    client, calls = conversations
    first = completed(client, value(query(client, 'same-request', chat='shared-chat-label')))
    other_token = 'Bearer ' + 'z' * 48
    seed_access(client.app, other_token, 'other-client')
    other_headers = {**HEADERS, 'Authorization': other_token}
    other = completed(client, value(query(client, 'same-request', chat='shared-chat-label', headers=other_headers)), other_headers)
    assert first['session_id'] != other['session_id']
    assert [call['thread_id'] for call in calls] == [None, None]
    denied = query(client, 'foreign-session', session_id=first['session_id'], headers=other_headers)
    assert denied['isError']
    assert len(calls) == 2


def test_replay_is_idempotent_and_cannot_move_between_sessions(conversations):
    client, calls = conversations
    first = completed(client, value(query(client, 'replay', chat='chat-a')))
    replay = value(query(client, 'replay', chat='chat-a'))
    explicit_replay = value(query(client, 'replay', session_id=first['session_id']))
    assert first['id'] == replay['id'] == explicit_replay['id']
    assert len(calls) == 1
    second = completed(client, value(query(client, 'second', chat='chat-b')))
    assert query(client, 'replay', session_id=second['session_id'])['isError']
    assert query(client, 'replay', chat='chat-b')['isError']
    assert query(client, 'replay', 'Different question', session_id=first['session_id'])['isError']
    assert len(calls) == 2


def test_clarification_receipt_preserves_continuation(conversations):
    client, calls = conversations
    first = value(query(client, 'clarify', 'clarify-marker'))
    assert first['continuation']['action'] == 'poll'
    assert first['continuation']['task_id'] == first['id']
    result = completed(client, first)
    assert result['continuation']['action'] == 'continue'
    assert result['receipt']['unresolved'] == ['Which synthetic city?']
    answer = completed(client, value(query(client, 'clarification-answer', 'Synthetic City',
                                          session_id=result['continuation']['session_id'])))
    assert answer['continuation']['action'] == 'done'
    assert calls[1]['thread_id'] == calls[0]['thread']


@pytest.mark.parametrize(('status', 'answer', 'action'), [
    ('completed', '{"status":"completed","unresolved":[]}', 'done'),
    ('completed', '{"status":"completed","unresolved":["Missing dates"]}', 'continue'),
    ('completed', '{"status":"needs_input"}', 'continue'),
    ('completed', 'Plain text answer', 'inspect'),
    ('completed', '["unexpected receipt"]', 'inspect'),
    ('failed', '{"status":"completed","unresolved":[]}', 'inspect'),
    ('interrupted', '{"status":"completed","unresolved":[]}', 'inspect'),
    ('queued', '', 'poll'),
    ('running', '', 'poll'),
])
def test_result_contract_does_not_mistake_execution_failure_for_completion(status, answer, action):
    from travel_ai.mcp_server import query_result

    result = query_result({'id': 'synthetic-run', 'session_id': 'synthetic-session',
                           'owner': 'private-owner', 'thread_id': 'private-thread',
                           'status': status, 'answer': answer})
    assert result['continuation']['action'] == action
    assert result['continuation']['session_id'] == 'synthetic-session'
    assert 'owner' not in result and 'thread_id' not in result


def test_conversation_mapping_and_thread_survive_store_restart(tmp_path):
    from travel_ai.store import Store

    path = tmp_path / 'conversations.sqlite3'
    original = Store(path)
    session = original.conversation_session('synthetic-owner', 'synthetic-chat')
    original.bind_thread('synthetic-owner', session['id'], 'synthetic-persistent-thread')
    reopened = Store(path)
    resumed = reopened.conversation_session('synthetic-owner', 'synthetic-chat')
    assert resumed['id'] == session['id']
    assert resumed['thread_id'] == 'synthetic-persistent-thread'
    assert len(reopened.sessions('synthetic-owner')) == 1
    other = reopened.conversation_session('other-owner', 'synthetic-chat')
    assert other['id'] != session['id']
    assert other['thread_id'] is None
    assert reopened.session('other-owner', session['id']) is None


def test_get_task_waits_for_delayed_completion_on_wire(conversations):
    client, _ = conversations
    task = value(query(client, 'delayed-query', 'delayed-marker'))
    with client.app.state.store.connect() as db:
        pending = db.execute('SELECT status FROM runs WHERE id=?', (task['id'],)).fetchone()
    assert pending['status'] in ('queued', 'running')
    result = value(rpc(client, 'tools/call', {
        'name': 'travel_get_task', 'arguments': {'task_id': task['id']},
    })['result'])
    assert result['status'] == 'completed'
    assert result['continuation']['action'] == 'done'
    assert result['session_id'] == task['session_id']


def test_get_task_wait_is_bounded_and_returns_poll_when_still_running(conversations, monkeypatch):
    from types import SimpleNamespace
    from travel_ai import mcp_server

    client, _ = conversations
    previous = completed(client, value(query(client, 'before-pending')))
    store = client.app.state.store
    with store.connect() as db:
        owner = db.execute('SELECT owner FROM runs WHERE id=?', (previous['id'],)).fetchone()['owner']
    pending, _ = store.create_run(owner, previous['session_id'], 'synthetic-pending', 'Still processing')
    store.update_run(pending['id'], 'running')
    sleeps = []

    async def fast_sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(mcp_server, 'asyncio', SimpleNamespace(sleep=fast_sleep))
    result = value(rpc(client, 'tools/call', {
        'name': 'travel_get_task', 'arguments': {'task_id': pending['id']},
    })['result'])
    assert sleeps == [1] * 20
    assert result['status'] == 'running'
    assert result['continuation']['action'] == 'poll'
    assert result['continuation']['task_id'] == pending['id']


@pytest.mark.parametrize('business_status', ['completed', 'partial'])
def test_pending_confirmation_survives_completed_query(business_status):
    from travel_ai.mcp_server import query_result

    pending = [{'question': '将合成晚餐接入时间轴并移除冲突建议，是否确认？',
                'proposal_id': 'synthetic-proposal'}]
    receipt = {'status': business_status, 'unresolved': [],
               'pending_confirmations': pending, 'changes': []}
    result = query_result({'id': 'synthetic-run', 'session_id': 'synthetic-session',
                           'status': 'completed', 'answer': json.dumps(receipt)})
    assert result['continuation']['action'] == 'ask_user'
    assert result['continuation']['pending_confirmations'] == pending
    assert result['receipt'] == receipt


@pytest.mark.parametrize(('run_status', 'action'), [('running', 'poll'), ('failed', 'inspect')])
def test_pending_confirmation_does_not_hide_running_or_failed_query(run_status, action):
    from travel_ai.mcp_server import query_result

    result = query_result({'id': 'synthetic-run', 'session_id': 'synthetic-session',
                           'status': run_status, 'answer': json.dumps({
                               'status': 'completed', 'pending_confirmations': [
                                   {'question': '批准合成方案？', 'proposal_id': None}]})})
    assert result['continuation']['action'] == action


def test_empty_pending_confirmations_allows_verified_completion():
    from travel_ai.mcp_server import query_result

    result = query_result({'id': 'synthetic-run', 'session_id': 'synthetic-session',
                           'status': 'completed', 'answer': json.dumps({
                               'status': 'completed', 'unresolved': [], 'pending_confirmations': []})})
    assert result['continuation']['action'] == 'done'


def test_confirmation_question_reaches_mcp_client_and_survives_readback(conversations):
    client, _ = conversations
    task = value(query(client, 'synthetic-approval', 'approval-marker'))
    first = completed(client, task)
    again = completed(client, task)
    assert first['continuation']['action'] == 'ask_user'
    assert first['continuation']['pending_confirmations'][0]['question'] == '批准合成日程调整？'
    assert again['continuation'] == first['continuation']
