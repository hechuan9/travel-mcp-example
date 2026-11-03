"""Pinned Codex App Server stdio client. Never expose arbitrary RPC to callers."""
import asyncio
import json
import os
from pathlib import Path


class CodexError(Exception):
    pass


class Codex:
    def __init__(self, home: Path, tools, instructions):
        self.home = home
        self.tools = tools
        self.instructions = instructions
        self.process = None
        self.pending = {}
        self.sequence = 0
        self.turn_future = None
        self.tool_handler = None
        self.answer = ''
        self.login = None
        self.reader = None
        self.server_tasks = set()
        self.lock = asyncio.Lock()

    async def start(self):
        async with self.lock:
            if self.process and self.process.returncode is None:
                return
            self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            workspace = self.home / 'workspace'
            workspace.mkdir(exist_ok=True, mode=0o700)
            env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.home),
                   'CODEX_HOME': str(self.home), 'TMPDIR': '/tmp'}
            self.process = await asyncio.create_subprocess_exec(
                os.environ.get('CODEX_BIN', 'codex'), 'app-server', '--listen', 'stdio://',
                '-c', 'features.shell_tool=false', '-c', 'web_search="disabled"',
                '-c', 'cli_auth_credentials_store="file"',
                cwd=workspace, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024)
            self.reader = asyncio.create_task(self.read())
            await self.rpc('initialize', {'clientInfo': {'name': 'travel-ai', 'version': '0.1.0'},
                                          'capabilities': {'experimentalApi': True}})
            await self.send({'method': 'initialized', 'params': {}})

    async def send(self, message):
        if not self.process or self.process.returncode is not None:
            raise CodexError('Codex 进程未运行')
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode())
        await self.process.stdin.drain()

    async def rpc(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({'id': request_id, 'method': method, 'params': params})
            return await asyncio.wait_for(future, timeout=45)
        finally:
            self.pending.pop(request_id, None)

    async def server_request(self, message):
        try:
            if message['method'] == 'item/tool/call' and self.tool_handler:
                params = message['params']
                value, success = await self.tool_handler(params['tool'], params['arguments'])
                result = {'contentItems': [{'type': 'inputText', 'text': json.dumps(value, ensure_ascii=False)}], 'success': success}
                await self.send({'id': message['id'], 'result': result})
            else:
                # No host execution, approval granting, or arbitrary client RPC.
                await self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Travel does not support this request'}})
        except Exception:
            await self.send({'id': message['id'], 'error': {'code': -32603, 'message': 'Tool request failed; verify state before retrying'}})

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if 'id' in message and 'method' not in message:
                    future = self.pending.get(message['id'])
                    if future and not future.done():
                        if 'error' in message:
                            future.set_exception(CodexError('Codex 请求失败，请检查登录或模型额度'))
                        else:
                            future.set_result(message.get('result', {}))
                elif 'id' in message:
                    task = asyncio.create_task(self.server_request(message))
                    self.server_tasks.add(task)
                    task.add_done_callback(self.server_tasks.discard)
                else:
                    params = message.get('params', {})
                    if message.get('method') == 'account/login/completed':
                        self.login = None
                    elif message.get('method') == 'item/completed':
                        item = params.get('item', {})
                        if item.get('type') == 'agentMessage' and self.turn_future:
                            self.answer = item.get('text', '')
                    elif message.get('method') == 'turn/completed' and self.turn_future and not self.turn_future.done():
                        self.turn_future.set_result({'status': params['turn']['status'], 'answer': self.answer})
        except (ValueError, OSError, asyncio.LimitOverrunError):
            pass
        finally:
            for future in list(self.pending.values()) + ([self.turn_future] if self.turn_future else []):
                if not future.done():
                    future.set_exception(CodexError('Codex 连接中断；先查询业务状态'))

    async def account(self):
        await self.start()
        result = await self.rpc('account/read', {'refreshToken': False})
        account = result.get('account') or {}
        return {'authenticated': account.get('type') == 'chatgpt',
                'plan': account.get('planType'), 'login_pending': self.login is not None}

    async def begin_login(self):
        await self.start()
        if not self.login:
            result = await self.rpc('account/login/start', {'type': 'chatgptDeviceCode'})
            self.login = {k: result[k] for k in ('loginId', 'verificationUrl', 'userCode')}
        return self.login

    async def run(self, thread_id, message, on_thread, handler):
        await self.start()
        self.tool_handler = handler
        self.answer = ''
        try:
            if thread_id:
                await self.rpc('thread/resume', {'threadId': thread_id, 'approvalPolicy': 'never', 'sandbox': 'read-only', 'excludeTurns': True, 'developerInstructions': self.instructions})
            else:
                result = await self.rpc('thread/start', {
                    'cwd': str(self.home / 'workspace'), 'approvalPolicy': 'never', 'sandbox': 'read-only',
                    'environments': [], 'developerInstructions': self.instructions, 'dynamicTools': self.tools})
                thread_id = result['thread']['id']
                on_thread(thread_id)
            self.turn_future = asyncio.get_running_loop().create_future()
            await self.rpc('turn/start', {'threadId': thread_id, 'environments': [],
                                         'input': [{'type': 'text', 'text': message}]})
            return await asyncio.wait_for(self.turn_future, timeout=600)
        except (TimeoutError, CodexError):
            # Kill on uncertain completion. Never leave an unobserved turn mutating business state.
            await self.close()
            raise
        finally:
            self.tool_handler = None
            self.turn_future = None

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.server_tasks:
            await asyncio.gather(*self.server_tasks, return_exceptions=True)
        self.process = None
        self.login = None
