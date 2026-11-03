"""Durable immutable MCP proposals; authentication belongs to the HTTP boundary."""
import json
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from travel_ai.store import stamp


class ProposalStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS mcp_proposals (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    request_id TEXT NOT NULL, plan TEXT NOT NULL,
                    status TEXT NOT NULL, approved_by TEXT, approved_at TEXT,
                    execution_id TEXT, result TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner, request_id));
            ''')
            if 'approval_evidence' not in {row[1] for row in db.execute('PRAGMA table_info(mcp_proposals)')}:
                db.execute('ALTER TABLE mcp_proposals ADD COLUMN approval_evidence TEXT')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        item = dict(row)
        item['plan'] = json.loads(item['plan'])
        item['result'] = json.loads(item['result']) if item['result'] is not None else None
        item['approval_evidence'] = json.loads(item['approval_evidence']) if item.get('approval_evidence') else None
        return item

    def get(self, owner, proposal_id):
        with self.connect() as db:
            return self.decode(db.execute(
                'SELECT * FROM mcp_proposals WHERE owner=? AND id=?',
                (owner, proposal_id)).fetchone())

    def create(self, owner, request_id, plan):
        encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM mcp_proposals WHERE owner=? AND request_id=?',
                             (owner, request_id)).fetchone()
            if old:
                if old['plan'] != encoded:
                    raise ValueError('同一 request_id 不能用于不同方案')
                return self.decode(old)
            proposal_id, now = str(uuid4()), stamp()
            db.execute('''INSERT INTO mcp_proposals
                (id,owner,request_id,plan,status,created_at,updated_at)
                VALUES(?,?,?,?,'ready',?,?)''',
                (proposal_id, owner, request_id, encoded, now, now))
            return self.decode(db.execute('SELECT * FROM mcp_proposals WHERE id=?',
                                          (proposal_id,)).fetchone())

    def claim(self, owner, proposal_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._owned(db, owner, proposal_id)
            claimed = row['status'] in ('pending', 'ready', 'approved')
            if claimed:
                db.execute('''UPDATE mcp_proposals SET status='executing',execution_id=?,
                    updated_at=? WHERE id=?''', (proposal_id, stamp(), proposal_id))
            return self.decode(self._owned(db, owner, proposal_id)), claimed

    def finish(self, owner, proposal_id, status, result):
        if status not in ('completed', 'failed', 'outcome_unknown'):
            raise ValueError('无效的方案执行终态')
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._owned(db, owner, proposal_id)
            if row['status'] == status and row['result'] == encoded:
                return self.decode(row)
            if row['status'] != 'executing':
                raise ValueError('方案未在执行或已结束；不得重新执行')
            db.execute('UPDATE mcp_proposals SET status=?,result=?,updated_at=? WHERE id=?',
                       (status, encoded, stamp(), proposal_id))
            return self.decode(self._owned(db, owner, proposal_id))

    @staticmethod
    def _owned(db, owner, proposal_id):
        row = db.execute('SELECT * FROM mcp_proposals WHERE owner=? AND id=?',
                         (owner, proposal_id)).fetchone()
        if row is None:
            raise ValueError('方案不存在')
        return row
