"""OAuth discovery and first-party login/consent for Travel MCP."""
import hashlib
import html
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import HTTPException, Request
from mcp.server.auth.routes import build_metadata, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

SCOPE = 'travel:mcp'
def flow_cookie(request_id):
    # Each authorization tab owns its CSRF cookie; parallel logins must not overwrite it.
    return 'travel_oauth_flow_' + hashlib.sha256(request_id.encode()).hexdigest()[:24]


def subject(user):
    return hashlib.sha256((user['household_id'] + ':' + user['id']).encode()).hexdigest()


def install_oauth(app, provider, business, service_household, origin):
    resource = origin + '/ai/mcp/'
    routes = create_auth_routes(provider, AnyHttpUrl(origin),
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
        revocation_options=RevocationOptions(enabled=True))

    class OAuthFormAdapter:
        """SDK validates PKCE/client/redirect; additionally require resource at token exchange."""
        def __init__(self, handler, path):
            self.handler, self.path = handler, path

        async def __call__(self, scope, receive, send):
            if scope['method'] != 'POST':
                return await self.handler(scope, receive, send)
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] != 'http.request':
                    return
                body.extend(message.get('body', b''))
                if len(body) > 65536:
                    return await JSONResponse({'error': 'invalid_request'}, status_code=413)(scope, receive, send)
                if not message.get('more_body'):
                    break
            try:
                values = parse_qs(body.decode(), keep_blank_values=True)
                resources = values.get('resource', [])
                valid = self.path != '/token' or (len(resources) == 1 and resources[0].rstrip('/') == resource.rstrip('/'))
            except (ValueError, UnicodeError):
                valid = False
            if not valid:
                return await JSONResponse({'error': 'invalid_target', 'error_description': 'resource must identify Travel MCP'},
                    status_code=400, headers={'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*'})(scope, receive, send)
            # SDK 1.30 RevocationRequest makes its nullable client_secret field required.
            # Public clients correctly omit it; adapt only that shape, retaining SDK validation.
            if self.path == '/revoke' and 'client_secret' not in values:
                ids = values.get('client_id', [])
                client = await provider.get_client(ids[0]) if len(ids) == 1 else None
                if client and client.token_endpoint_auth_method == 'none':
                    body.extend(b'&client_secret=')
            sent = False
            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()
            await self.handler(scope, replay, send)

    metadata = build_metadata(AnyHttpUrl(origin), None,
        ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]), RevocationOptions(enabled=True))
    metadata.token_endpoint_auth_methods_supported = ['none']
    metadata.revocation_endpoint_auth_methods_supported = ['none']
    async def authorization_metadata(request):
        return JSONResponse(metadata.model_dump(mode='json', exclude_none=True),
                            headers={'Access-Control-Allow-Origin': '*'})
    for index, route in enumerate(routes):
        if route.path in ('/token', '/revoke'):
            route.app = OAuthFormAdapter(route.app, route.path)
        if route.path == '/.well-known/oauth-authorization-server':
            routes[index] = Route(route.path, endpoint=authorization_metadata, methods=['GET', 'OPTIONS'])
    app.router.routes.extend(routes)
    metadata_routes = create_protected_resource_routes(AnyHttpUrl(resource), [AnyHttpUrl(origin)], [SCOPE], resource_name='Travel MCP')
    app.router.routes.extend(metadata_routes)

    # Root fallback is useful for clients using RFC9728 discovery without the challenge URL.
    @app.get('/.well-known/oauth-protected-resource')
    async def root_metadata():
        return {'resource': resource, 'authorization_servers': [origin], 'scopes_supported': [SCOPE],
                'bearer_methods_supported': ['header'], 'resource_name': 'Travel MCP'}

    def pending(request_id):
        result = provider.get_pending(request_id)
        if not result:
            raise HTTPException(400, '授权请求已过期或已处理，请从 Muse 重新连接')
        return result

    async def human(request):
        if request.headers.get('authorization'):
            raise HTTPException(403, 'OAuth 授权需要本人登录，机器凭据不能代替')
        cookie = request.cookies.get('travel_session')
        if not cookie:
            return None
        try:
            async with business('') as client:
                response = await client.get('/api/me', headers={'Cookie': 'travel_session=' + cookie})
            return response.json() if response.status_code == 200 else None
        except httpx.HTTPError:
            raise HTTPException(503, '登录服务暂不可用') from None

    async def account_user(request):
        user = await human(request)
        if not user:
            raise HTTPException(401, '请先登录 Travel')
        if user['household_id'] != await service_household():
            raise HTTPException(403, '当前账号不属于此远程助手服务的家庭')
        return user

    @app.get('/ai/account/mcp-connections')
    async def connections(request: Request):
        user = await account_user(request)
        return JSONResponse({'connections': provider.list_connections(subject(user))},
                            headers={'Cache-Control': 'no-store'})

    @app.post('/ai/account/mcp-connections/{grant_id}/revoke')
    async def revoke_connection(grant_id: str, request: Request):
        if request.headers.get('origin') != origin:
            raise HTTPException(403, '仅允许同源本人操作')
        user = await account_user(request)
        if not provider.revoke_connection(subject(user), grant_id):
            raise HTTPException(404, '连接不存在')
        return JSONResponse({'revoked': True}, headers={'Cache-Control': 'no-store'})

    def page(request_id, data, nonce, user, error=''):
        esc = html.escape
        params = data['params']
        callback = str(params['redirect_uri'])
        client_name = data.get('client_name') or data['client_id']
        hidden = f'<input type="hidden" name="request_id" value="{esc(request_id)}"><input type="hidden" name="csrf" value="{esc(nonce)}">'
        if user:
            body = f'''<p>已登录：{esc(user['email'])}</p><form method="post" action="/ai/oauth/consent">{hidden}
            <button name="decision" value="approve">授权连接</button><button class="secondary" name="decision" value="deny">拒绝</button></form>
            <form method="post" action="/ai/oauth/switch-account" style="margin-top:16px">{hidden}
            <button class="secondary">切换账号</button></form><p>切换账号会退出当前浏览器的 Travel 登录，保留本次连接请求。</p>'''
        else:
            body = f'''<form method="post" action="/ai/oauth/login">{hidden}
            <label>Travel 邮箱<input type="email" name="email" autocomplete="username" required></label>
            <label>Travel 密码<input type="password" name="password" autocomplete="current-password" required></label>
            <button>登录后继续</button></form><p>使用 Travel 网页账号，不能填写 MCP token。登录本身不会批准连接。</p>'''
        response = HTMLResponse(f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>连接 Travel</title><style>body{{font:16px system-ui;background:#f5f7fa;color:#182230;margin:0;padding:32px 18px}}main{{max-width:520px;margin:5vh auto;padding:28px;background:white;border-radius:18px}}h1{{font-size:24px}}p,li{{line-height:1.6}}label{{display:block;margin:18px 0}}input{{display:block;box-sizing:border-box;width:100%;padding:12px;margin-top:8px;border:1px solid #9ca3af;border-radius:8px;font:inherit}}button{{padding:12px 18px;background:#174b9f;color:white;border:0;border-radius:8px;margin-right:12px;font:inherit}}.secondary{{background:#e5e7eb;color:#182230}}.error{{color:#b42318}}small{{overflow-wrap:anywhere}}</style>
        <main><h1>连接 Travel</h1><p>请求应用：<strong>{esc(client_name)}</strong></p><small>授权回调地址：{esc(callback)}</small>
        <p>授权后，这个应用可以：</p><ul><li>读取你的旅行、每日安排、预订和同行成员。</li><li>直接保存和补充聊天中的旅行想法，展示在家庭共享 Idea 库。</li><li>按你的请求维护行程及其他业务记录，无需逐项审批。</li></ul>
        <p>尚未接入供应商，不能购买、取消或改签，也不能访问其他客户端的私人对话。应用可以自动刷新凭据，最长30天后重新授权。</p>
        <p class="error">{esc(error)}</p>{body}</main></html>''')
        # Chromium applies form-action to the 303 redirect as well as the POST.
        # Only the already validated, registered callback origin is additionally allowed.
        redirect = urlsplit(callback)
        callback_origin = redirect.scheme + '://' + redirect.netloc
        response.headers['Content-Security-Policy'] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' "
            + callback_origin + "; frame-ancestors 'none'; base-uri 'none'")
        # Native form POST must retain Origin for the strict same-origin check.
        response.headers['Referrer-Policy'] = 'same-origin'
        response.set_cookie(flow_cookie(request_id), nonce, max_age=600, path='/ai/oauth', secure=True, httponly=True, samesite='lax')
        return response

    async def form(request):
        if request.headers.get('authorization') or request.headers.get('origin') != origin:
            raise HTTPException(403, '仅允许同源本人授权')
        # Credential input is bounded and never logged or persisted.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise HTTPException(413, '请求过大')
        try:
            values = parse_qs(body.decode(), keep_blank_values=True)
        except UnicodeError:
            raise HTTPException(400, '表单格式无效') from None
        if any(len(value) != 1 for value in values.values()):
            raise HTTPException(400, '表单字段重复')
        values = {key: value[0] for key, value in values.items()}
        request_id, nonce = values.get('request_id', ''), values.get('csrf', '')
        if not nonce or not secrets.compare_digest(request.cookies.get(flow_cookie(request_id), ''), nonce) or not provider.check_browser(request_id, nonce):
            raise HTTPException(403, '授权页面已失效，请重新打开连接')
        return values, pending(request_id)

    @app.get('/ai/oauth/consent')
    async def consent(request_id: str, request: Request):
        data = pending(request_id)
        user = await human(request)
        nonce = request.cookies.get(flow_cookie(request_id), '')
        if not nonce or not provider.check_browser(request_id, nonce):
            nonce = secrets.token_urlsafe(32)
            if not provider.bind_browser(request_id, nonce):
                raise HTTPException(400, '授权请求已绑定其他浏览器，请从 Muse 重新连接')
        error = ''
        if user and user['household_id'] != await service_household():
            error = f"当前登录账号 {user['email']} 不属于此远程助手服务的家庭，请使用本家庭账号重新登录。"
            # Do not offer consent for an identity that cannot be served by the current gateway.
            user = None
        return page(request_id, data, nonce, user, error)

    @app.post('/ai/oauth/switch-account')
    async def switch_account(request: Request):
        values, data = await form(request)
        response = page(values['request_id'], data, values['csrf'], None)
        response.delete_cookie('travel_session', path='/', secure=True, httponly=True, samesite='lax')
        return response

    @app.post('/ai/oauth/login')
    async def login(request: Request):
        values, data = await form(request)
        if not values.get('email') or not values.get('password'):
            raise HTTPException(400, '请填写 Travel 邮箱和密码')
        try:
            async with business('') as client:
                result = await client.post('/api/login', headers={'Origin': origin},
                    json={'email': values['email'], 'password': values['password']})
        except httpx.HTTPError:
            raise HTTPException(503, '登录服务暂不可用') from None
        if result.status_code != 200 or not result.cookies.get('travel_session'):
            return page(values['request_id'], data, values['csrf'], None, '邮箱或密码不正确，或登录服务暂不可用。')
        response = RedirectResponse('/ai/oauth/consent?request_id=' + values['request_id'], status_code=303)
        response.set_cookie('travel_session', result.cookies['travel_session'], max_age=604800,
                            secure=True, httponly=True, samesite='lax', path='/')
        return response

    @app.post('/ai/oauth/consent')
    async def decide(request: Request):
        values, _ = await form(request)
        user = await human(request)
        if not user:
            raise HTTPException(401, '请先登录 Travel')
        owner = subject(user)
        if user['household_id'] != await service_household():
            raise HTTPException(403, '当前账号不属于此远程助手服务的家庭')
        if values.get('decision') == 'approve':
            target = provider.complete_consent(values['request_id'], owner)
        elif values.get('decision') == 'deny':
            target = provider.deny_consent(values['request_id'])
        else:
            raise HTTPException(400, '请明确授权或拒绝')
        response = RedirectResponse(target, status_code=303)
        response.delete_cookie(flow_cookie(values['request_id']), path='/ai/oauth', secure=True, httponly=True, samesite='lax')
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response
