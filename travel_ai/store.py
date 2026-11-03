"""Private AI conversation state, never a copy of the travel business database."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5


def stamp():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                    thread_id TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id),
                    request_id TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL,
                    answer TEXT NOT NULL DEFAULT '', error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner, request_id));
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_owner
                    ON runs(owner) WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS tools (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    name TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL,
                    http_status INTEGER, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memories (
                    owner TEXT NOT NULL, key TEXT NOT NULL, content TEXT NOT NULL,
                    version INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(owner,key));
            ''')
            db.execute("UPDATE runs SET status='interrupted', error='服务重启；先核对工具记录和业务状态，不自动重试', updated_at=? WHERE status IN ('queued','running')", (stamp(),))
            db.execute("UPDATE tools SET status='outcome_unknown' WHERE status='running'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def session(self, owner, session_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM sessions WHERE owner=? AND id=?', (owner, session_id)).fetchone()
            return dict(row) if row else None

    def sessions(self, owner):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,title,created_at FROM sessions WHERE owner=? ORDER BY created_at DESC LIMIT 100', (owner,))]

    def create_session(self, owner, title):
        item = dict(id=str(uuid4()), owner=owner, title=title, created_at=stamp())
        with self.connect() as db:
            db.execute('INSERT INTO sessions(id,owner,title,created_at) VALUES(:id,:owner,:title,:created_at)', item)
        return item

    @staticmethod
    def conversation_session_id(owner, conversation_id):
        return str(uuid5(NAMESPACE_URL, json.dumps([owner, conversation_id])))

    def conversation_session(self, owner, conversation_id):
        # Host metadata correlates conversations; authenticated owner controls access.
        identifier = self.conversation_session_id(owner, conversation_id)
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO sessions(id,owner,title,created_at) VALUES(?,?,?,?)',
                       (identifier, owner, 'Travel 对话', stamp()))
        return self.session(owner, identifier)

    def bind_thread(self, owner, session_id, thread_id):
        with self.connect() as db:
            db.execute('UPDATE sessions SET thread_id=? WHERE owner=? AND id=?', (thread_id, owner, session_id))

    def run(self, owner, run_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE owner=? AND id=?', (owner, run_id)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['tools'] = [dict(r) for r in db.execute('SELECT id,name,arguments,status,http_status,created_at FROM tools WHERE run_id=? ORDER BY id', (run_id,))]
            return result

    def runs(self, owner, session_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM runs WHERE owner=? AND session_id=? ORDER BY created_at LIMIT 200', (owner, session_id))]

    def create_run(self, owner, session_id, request_id, message):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM runs WHERE owner=? AND request_id=?', (owner, request_id)).fetchone()
            if old:
                if old['session_id'] != session_id or old['message'] != message:
                    raise ValueError('同一 request_id 不能用于不同消息')
                return dict(old), False
            item = dict(id=str(uuid4()), owner=owner, session_id=session_id, request_id=request_id,
                        message=message, status='queued', created_at=stamp(), updated_at=stamp())
            try:
                db.execute('INSERT INTO runs(id,owner,session_id,request_id,message,status,created_at,updated_at) VALUES(:id,:owner,:session_id,:request_id,:message,:status,:created_at,:updated_at)', item)
            except sqlite3.IntegrityError:
                raise ValueError('此用户已有进行中的任务，请先查询其结果') from None
            return item, True

    def update_run(self, run_id, status, answer='', error=None):
        with self.connect() as db:
            db.execute('UPDATE runs SET status=?,answer=?,error=?,updated_at=? WHERE id=?', (status, answer, error, stamp(), run_id))

    def start_tool(self, run_id, name, arguments):
        with self.connect() as db:
            return db.execute('INSERT INTO tools(run_id,name,arguments,status,created_at) VALUES(?,?,?,?,?)',
                              (run_id, name, json.dumps(arguments, ensure_ascii=False), 'running', stamp())).lastrowid

    def finish_tool(self, tool_id, status, http_status=None):
        with self.connect() as db:
            db.execute('UPDATE tools SET status=?,http_status=? WHERE id=?', (status, http_status, tool_id))

    def memories(self, owner):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT key,content,version,updated_at FROM memories WHERE owner=? ORDER BY key', (owner,))]

    def put_memory(self, owner, key, content, version):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT version FROM memories WHERE owner=? AND key=?', (owner, key)).fetchone()
            if (row['version'] if row else 0) != version:
                raise ValueError('记忆版本冲突，请先读取最新内容')
            db.execute('INSERT INTO memories VALUES(?,?,?,?,?) ON CONFLICT(owner,key) DO UPDATE SET content=excluded.content,version=excluded.version,updated_at=excluded.updated_at',
                       (owner, key, content, version+1, stamp()))
        return dict(key=key, content=content, version=version+1)

    def delete_memory(self, owner, key, version):
        with self.connect() as db:
            if db.execute('DELETE FROM memories WHERE owner=? AND key=? AND version=?', (owner, key, version)).rowcount != 1:
                raise ValueError('记忆不存在或版本冲突')
