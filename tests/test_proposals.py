from concurrent.futures import ThreadPoolExecutor

import pytest

from travel_ai.proposals import ProposalStore


@pytest.fixture
def store(tmp_path):
    return ProposalStore(tmp_path / 'gateway.sqlite3')


PLAN = {'method': 'PATCH', 'path': '/api/trips/example', 'body': {'name': 'Synthetic'}, 'reason': 'Test'}


def test_create_is_idempotent_and_plan_immutable(store):
    proposal = store.create('owner', 'request', PLAN)
    assert store.create('owner', 'request', dict(reversed(list(PLAN.items())))) == proposal
    with pytest.raises(ValueError, match='不同方案'):
        store.create('owner', 'request', {**PLAN, 'reason': 'different'})
    assert store.get('owner', proposal['id'])['plan'] == PLAN
    assert store.create('other', 'request', PLAN)['id'] != proposal['id']


def test_owner_isolation_and_direct_execution(store):
    item = store.create('owner', 'request', PLAN)
    assert store.get('other', item['id']) is None
    for operation in (
        lambda: store.claim('other', item['id']),
        lambda: store.finish('other', item['id'], 'completed', {}),
    ):
        with pytest.raises(ValueError, match='不存在'):
            operation()
    claimed, won = store.claim('owner', item['id'])
    assert won and claimed['status'] == 'executing'
    assert claimed['approved_by'] is None


def test_concurrent_claim_has_one_winner_and_restart_never_replays(store):
    item = store.create('owner', 'request', PLAN)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: store.claim('owner', item['id']), range(8)))
    assert sum(claimed for _, claimed in claims) == 1
    assert {p['execution_id'] for p, _ in claims} == {item['id']}
    restarted = ProposalStore(store.path)
    result, claimed = restarted.claim('owner', item['id'])
    assert not claimed
    assert result['status'] == 'executing'


@pytest.mark.parametrize('status', ['completed', 'failed', 'outcome_unknown'])
def test_terminal_outcomes_are_durable_and_not_reexecuted(store, status):
    item = store.create('owner', 'request', PLAN)
    with pytest.raises(ValueError, match='未在执行'):
        store.finish('owner', item['id'], status, {})
    store.claim('owner', item['id'])
    result = {'status_code': 200}
    finished = store.finish('owner', item['id'], status, result)
    assert store.finish('owner', item['id'], status, result) == finished
    terminal, claimed = ProposalStore(store.path).claim('owner', item['id'])
    assert terminal == finished
    assert not claimed
    with pytest.raises(ValueError, match='已结束'):
        store.finish('owner', item['id'], status, {'different': True})


def test_legacy_pending_proposal_can_execute_without_inventing_approval(tmp_path):
    import json
    import sqlite3
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE mcp_proposals (id TEXT PRIMARY KEY, owner TEXT NOT NULL,
            request_id TEXT NOT NULL, plan TEXT NOT NULL, status TEXT NOT NULL,
            approved_by TEXT, approved_at TEXT, execution_id TEXT, result TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(owner, request_id))''')
        db.execute("INSERT INTO mcp_proposals (id,owner,request_id,plan,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   ('legacy', 'owner', 'request', json.dumps(PLAN), 'pending', 'before', 'before'))
    store = ProposalStore(path)
    assert store.get('owner', 'legacy')['approval_evidence'] is None
    claimed, won = store.claim('owner', 'legacy')
    assert won and claimed['plan'] == PLAN
    assert claimed['approved_by'] is None
    assert ProposalStore(path).get('owner', 'legacy') == claimed


def test_legacy_approval_evidence_is_retained_on_execution(store):
    import json
    item = store.create('owner', 'legacy-approved', PLAN)
    evidence = {'channel': 'chat', 'approval_text': 'historical synthetic approval'}
    with store.connect() as db:
        db.execute("UPDATE mcp_proposals SET status='approved', approved_by=?, approval_evidence=? WHERE id=?",
                   ('historical-user', json.dumps(evidence), item['id']))
    claimed, won = store.claim('owner', item['id'])
    assert won
    assert claimed['approved_by'] == 'historical-user'
    assert claimed['approval_evidence'] == evidence
