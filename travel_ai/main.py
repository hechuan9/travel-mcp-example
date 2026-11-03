"""Authenticated Travel-only gateway to an isolated subscription-backed Codex."""
import asyncio
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from travel_ai.codex import Codex, CodexError
from travel_ai.oauth_provider import TravelOAuthProvider
from travel_ai.oauth_routes import install_oauth
from travel_ai.store import Store
from travel_ai.proposals import ProposalStore
from travel_ai.mcp_server import install_mcp, ReadOnlyTools
from travel_ai.tools import INSTRUCTIONS, TOOLS, MemoryInput, ToolHandler


class NewSession(BaseModel):
    model_config = {'extra': 'forbid', 'str_strip_whitespace': True}
    title: str = Field(default='旅行对话', min_length=1, max_length=120)


class Message(BaseModel):
    model_config = {'extra': 'forbid', 'str_strip_whitespace': True}
    request_id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    message: str = Field(min_length=1, max_length=16000)


class DeleteMemory(BaseModel):
    model_config = {'extra': 'forbid'}
    version: int = Field(ge=1, strict=True)


def public(item):
    return {k: v for k, v in item.items() if k not in ('owner', 'thread_id')}


def create_app(state_dir=None, business_origin=None, transport=None, codex_factory=Codex, credential_file=None):
    root = Path(state_dir or os.environ.get('AI_STATE_DIR', './runtime/ai'))
    origin = business_origin or os.environ.get('TRAVEL_BUSINESS_ORIGIN', 'http://api:8000')
    credential_path = Path(credential_file or os.environ.get("AI_CREDENTIAL_FILE", "/run/secrets/travel-ai.json"))
    public_origin = os.getenv('PUBLIC_ORIGIN', 'https://travel.example.test')
    oauth = TravelOAuthProvider(root / 'gateway.sqlite3', public_origin)
    peers = {}
    jobs = set()
    engine_locks = {}

    def business(token):
        return httpx.AsyncClient(base_url=origin, headers={'Authorization': token} if token else {},
                                 timeout=httpx.Timeout(30, connect=5), follow_redirects=False, transport=transport)

    @asynccontextmanager
    async def lifespan(app):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        app.state.store = Store(root / 'gateway.sqlite3')
        oauth.initialize()
        app.state.oauth = oauth
        app.state.proposals = ProposalStore(root / 'gateway.sqlite3')
        with app.state.proposals.connect() as db:
            db.execute("UPDATE mcp_proposals SET status='outcome_unknown', result=? WHERE status='executing'",
                       (json.dumps({'detail': '服务重启，执行结果未知；先查询实际行程，不自动重放'}),))
        async with mcp.session_manager.run():
            yield
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await asyncio.gather(*(peer.close() for peer in peers.values()), return_exceptions=True)

    app = FastAPI(title='Travel AI API', version='0.1.0', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def privacy(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def credentials():
        try:
            value = json.loads(credential_path.read_text())
            if len(value['business_token']) < 40:
                raise ValueError('invalid credentials')
            return value
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(503, 'AI 网关凭据未配置') from None

    async def service_user():
        token = 'Bearer ' + credentials()['business_token']
        try:
            async with business(token) as client:
                response = await client.get('/api/me')
        except httpx.HTTPError:
            raise HTTPException(503, 'Travel 身份服务暂不可用') from None
        if response.status_code != 200:
            raise HTTPException(503, 'Travel 身份服务暂不可用')
        user = response.json()
        owner = hashlib.sha256((user['household_id'] + ':' + user['id']).encode()).hexdigest()
        return owner, token, user

    async def service_household():
        _, _, user = await service_user()
        return user['household_id']

    async def authenticate(request: Request, mcp=False):
        parts = request.headers.get('authorization', '').split()
        challenge = {'WWW-Authenticate': 'Bearer resource_metadata="' + public_origin + '/.well-known/oauth-protected-resource/ai/mcp/", scope="travel:mcp"'} if mcp else None
        if len(parts) != 2 or parts[0].lower() != 'bearer' or len(parts[1]) < 40:
            raise HTTPException(401, '需要有效的 Travel 访问凭据', headers=challenge)
        access = None
        if mcp:
            access = await oauth.load_access_token(parts[1])
            if access is None:
                raise HTTPException(401, 'OAuth 凭据无效或已过期，请重新授权', headers=challenge)
            if access.resource != public_origin + '/ai/mcp/' or access.scopes != ['travel:mcp']:
                raise HTTPException(403, 'OAuth 资源或权限不匹配')
        elif not hmac.compare_digest(hashlib.sha256(parts[1].encode()).hexdigest(), credentials().get('ai_token_sha256', '')):
            raise HTTPException(401, 'AI 通信凭据无效')
        owner, token, service = await service_user()
        if access is not None and access.subject != owner:
            # Recheck current membership for every request; revoked/moved users lose access.
            try:
                async with business(token) as client:
                    response = await client.get('/api/household/users')
            except httpx.HTTPError:
                raise HTTPException(503, 'Travel 家庭身份服务暂不可用') from None
            if response.status_code != 200:
                raise HTTPException(503, 'Travel 家庭身份服务暂不可用')
            allowed = any(user.get('household_id') == service['household_id'] and
                          hashlib.sha256((user['household_id'] + ':' + user['id']).encode()).hexdigest() == access.subject
                          for user in response.json())
            if not allowed:
                raise HTTPException(401, 'OAuth 家庭成员归属已失效', headers=challenge)
        if access is not None:
            oauth.record_use(parts[1])
        return {'owner': 'oauth:' + access.client_id + ':' + access.subject if access else owner,
                'engine_owner': owner, 'token': token}

    async def identity(request: Request):
        return await authenticate(request)

    def peer(owner):
        if owner not in peers:
            peers[owner] = codex_factory(root / 'users' / owner, TOOLS, INSTRUCTIONS)
        return peers[owner]

    def own_session(owner, session_id):
        session = app.state.store.session(owner, session_id)
        if not session:
            raise HTTPException(404, '对话不存在')
        return session

    @app.get('/ai/healthz')
    async def health():
        return {'status': 'ok', 'release_sha': os.environ.get('RELEASE_SHA'), 'backend': 'codex-app-server', 'codex_version': '0.162.1'}

    @app.get('/ai/account')
    async def account(auth=Depends(identity)):
        try:
            return await peer(auth['owner']).account()
        except (CodexError, TimeoutError, OSError):
            raise HTTPException(503, 'Codex 暂不可用') from None

    @app.post('/ai/account/login')
    async def login(auth=Depends(identity)):
        try:
            engine = peer(auth['owner'])
            if (await engine.account())['authenticated']:
                return {'authenticated': True}
            return await engine.begin_login()
        except (CodexError, TimeoutError, OSError):
            raise HTTPException(503, '无法启动设备码登录，请检查 Codex 状态') from None

    @app.post('/ai/sessions', status_code=201)
    async def create_session(body: NewSession, auth=Depends(identity)):
        return public(app.state.store.create_session(auth['owner'], body.title))

    @app.get('/ai/sessions')
    async def sessions(auth=Depends(identity)):
        return app.state.store.sessions(auth['owner'])

    @app.get('/ai/sessions/{session_id}')
    async def session(session_id: str, auth=Depends(identity)):
        result = public(own_session(auth['owner'], session_id))
        result['runs'] = [public(row) for row in app.state.store.runs(auth['owner'], session_id)]
        return result

    async def execute(run, session, token, engine_owner=None, read_only=False):
        store = app.state.store
        store.update_run(run['id'], 'running')
        try:
            # A previous turn may have bound the thread while account/read awaited.
            session = store.session(run['owner'], session['id'])
            engine_owner = engine_owner or run['owner']
            lock = engine_locks.setdefault(engine_owner, asyncio.Lock())
            async with lock, business(token) as client:
                handler = ToolHandler(store, run['owner'], run['id'], client)
                if read_only:
                    handler = ReadOnlyTools(handler)
                result = await peer(engine_owner).run(
                    session['thread_id'], run['message'],
                    lambda thread: store.bind_thread(run['owner'], session['id'], thread),
                    handler)
            status = result['status']
            if status not in ('completed', 'failed', 'interrupted'):
                status = 'failed'
            store.update_run(run['id'], status, result['answer'], None if status == 'completed' else 'Codex 未完成本轮；检查登录、额度和工具结果')
        except asyncio.CancelledError:
            store.update_run(run['id'], 'interrupted', error='服务停止；先核对工具记录和业务状态，不自动重试')
            raise
        except Exception:
            store.update_run(run['id'], 'failed', error='Codex 本轮失败或连接中断；先核对工具记录和业务状态，不自动重试')

    @app.post('/ai/sessions/{session_id}/messages', status_code=202)
    async def message(session_id: str, body: Message, auth=Depends(identity)):
        session = own_session(auth['owner'], session_id)
        # Replay is safe even after auth expires at the model provider.
        with app.state.store.connect() as db:
            existing = db.execute('SELECT id,session_id,message FROM runs WHERE owner=? AND request_id=?', (auth['owner'], body.request_id)).fetchone()
        if existing:
            if existing['session_id'] != session_id or existing['message'] != body.message:
                raise HTTPException(409, '同一 request_id 不能用于不同消息')
            return public(app.state.store.run(auth['owner'], existing['id']))
        try:
            if not (await peer(auth['owner']).account())['authenticated']:
                raise HTTPException(409, '请先通过 /ai/account/login 完成 ChatGPT 订阅登录')
            run, created = app.state.store.create_run(auth['owner'], session_id, body.request_id, body.message)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except (CodexError, TimeoutError, OSError):
            raise HTTPException(503, 'Codex 暂不可用') from None
        if created:
            task = asyncio.create_task(execute(run, session, auth['token']))
            jobs.add(task)
            task.add_done_callback(jobs.discard)
        return public(run)

    @app.get('/ai/runs/{run_id}')
    async def run(run_id: str, auth=Depends(identity)):
        result = app.state.store.run(auth['owner'], run_id)
        if not result:
            raise HTTPException(404, '任务不存在')
        return public(result)

    @app.get('/ai/memories')
    async def memories(auth=Depends(identity)):
        return app.state.store.memories(auth['owner'])

    @app.put('/ai/memories/{key}')
    async def put_memory(key: str, body: MemoryInput, auth=Depends(identity)):
        if key != body.key:
            raise HTTPException(422, '路径与记忆 key 不一致')
        try:
            return app.state.store.put_memory(auth['owner'], **body.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.delete('/ai/memories/{key}', status_code=204)
    async def delete_memory(key: str, body: DeleteMemory, auth=Depends(identity)):
        try:
            app.state.store.delete_memory(auth['owner'], key, body.version)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    async def enqueue_query(auth, request_id, question, session_id=None, conversation_id=None):
        store = app.state.store
        with store.connect() as db:
            existing = db.execute('SELECT id,session_id,message FROM runs WHERE owner=? AND request_id=?',
                                  (auth['owner'], request_id)).fetchone()
        prompt = '本轮仅只读查询，禁止任何修改。用户请求：' + question
        if existing:
            expected_session = session_id or (store.conversation_session_id(auth['owner'], conversation_id) if conversation_id else None)
            if existing['message'] != prompt or (expected_session and existing['session_id'] != expected_session):
                raise ValueError('同一 request_id 不能用于不同查询')
            return public(store.run(auth['owner'], existing['id']))
        if not (await peer(auth['engine_owner']).account())['authenticated']:
            raise ValueError('远程助手订阅未登录，请联系本人处理；Muse不能发起登录')
        if session_id:
            session = store.session(auth['owner'], session_id)
            if session is None:
                raise ValueError('对话不存在或不属于当前客户端用户')
        elif conversation_id:
            session = store.conversation_session(auth['owner'], conversation_id)
        else:
            session = store.create_session(auth['owner'], 'Travel 只读查询')
        run, created = store.create_run(auth['owner'], session['id'], request_id, prompt)
        if created:
            task = asyncio.create_task(execute(run, session, auth['token'], auth['engine_owner'], True))
            jobs.add(task)
            task.add_done_callback(jobs.discard)
        return public(run)

    install_oauth(app, oauth, business, service_household, public_origin)
    mcp = install_mcp(app, root, authenticate, business, enqueue_query, jobs)
    return app


app = create_app()
