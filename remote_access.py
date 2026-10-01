"""Loopback gateway for an HTTPS Funnel, with locally approved browser sessions.

No external request can select an upstream host, executable, or filesystem path.
Only hashes of browser credentials are retained. Funnel configuration is separate.
"""
import asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone
import hashlib
import html
import ipaddress
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import unquote, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web
from multidict import CIMultiDict
from ai_services import MAX_CHAT_REQUEST_BYTES


COOKIE = 'MAICRemote'
PENDING_SECONDS = 600
SESSION_SECONDS = 30 * 86400
MAX_PENDING = 32
MAX_DEVICES = 64
MAX_BODY = 2 * 1024 * 1024
PUBLIC_APP_ASSETS = {
    '/manifest.webmanifest': 'application/manifest+json',
    '/icon-192.png': 'image/png',
    '/icon-512.png': 'image/png',
    '/apple-touch-icon.png': 'image/png',
}
GET_PATHS = frozenset((
    '/', '/app.js', '/voice.js', '/chat-media.js', '/style.css', '/desktop-viewer.html', '/tone.wav',
    '/api/health', '/api/status', '/api/apps', '/api/control-session', '/api/audio',
    '/api/pc/apps', '/api/pc/state', '/api/hardware', '/api/lm/models',
    '/api/speech/status', '/api/tts/status', '/api/comfy/status', '/api/desktop/status', '/api/remote/info',
))
POST_PATHS = frozenset((
    '/api/tap', '/api/diagnostics', '/api/calculate', '/api/control', '/api/bookmarks',
    '/api/pc/action', '/api/pc/favourites', '/api/lm/start', '/api/lm/open',
    '/api/lm/load', '/api/lm/chat', '/api/speech/transcribe', '/api/tts/speak', '/api/desktop/session',
))
HOP_HEADERS = frozenset((
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'origin',
))
PAIR_PAGE = b'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Connect to your PC</title>
<meta name="theme-color" content="#f4f5ef"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="PC Remote">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
<style>*{box-sizing:border-box}body{background:#f4f5ef;color:#1d3028;font:16px "Segoe UI",Roboto,Arial,sans-serif;margin:0;padding:32px 20px}
main{max-width:520px;margin:8vh auto;background:#fff;border:1px solid #dde2d7;border-radius:22px;padding:34px;box-shadow:0 12px 40px #243a2308}
h1{font-size:30px;font-weight:600;letter-spacing:-1px;line-height:1.2;margin:0 0 18px}h1:before{content:"PC REMOTE";display:block;color:#647553;font-size:10px;letter-spacing:2px;margin-bottom:22px}
button{min-height:48px;background:#18392f;color:#fff;border:1px solid #18392f;border-radius:11px;padding:13px 20px;font:600 14px "Segoe UI",Roboto,Arial,sans-serif;cursor:pointer;touch-action:manipulation}button:hover{background:#285044}
button:focus,input:focus,a:focus{outline:2px solid #528745;outline-offset:3px}label{display:block;margin-top:24px;font-size:12px;font-weight:600;color:#69766b}
#code{font-size:44px;font-weight:600;letter-spacing:7px;font-variant-numeric:tabular-nums;color:#18392f;background:#edf4e7;border-radius:12px;padding:16px;text-align:center;margin:22px 0}#code:empty{display:none}
p{line-height:1.6;color:#69766b;font-size:14px}input{display:block;background:#fff;color:#1d3028;border:1px solid #d4dccf;border-radius:10px;padding:13px;font:16px "Segoe UI",Roboto,Arial,sans-serif;width:100%;min-height:46px;margin:9px 0 20px}
a{color:#345d37;text-underline-offset:3px;display:inline-block;padding:10px 0;min-height:44px}@media(max-width:400px){body{padding:20px 12px}main{margin:4vh auto;padding:25px 20px;border-radius:17px}h1{font-size:26px}button{width:100%}#code{font-size:36px;letter-spacing:5px}}</style><main>
<h1>Connect to your PC</h1><p id="status">Request access, then approve this browser from PC Remote on your Windows PC.</p>
<label id="label">Browser name<input id="name" maxlength="60"></label>
<p id="code"></p><button id="pair">Request connection</button>
<p>Only your Windows PC can approve the connection. Match the code shown on both screens.</p>
<p><a href="__PC_APPROVAL_URL__">Open approval on this PC</a></p></main><script>
(function(){var button=document.getElementById('pair'),status=document.getElementById('status'),code=document.getElementById('code'),name=document.getElementById('name'),label=document.getElementById('label');
name.value=navigator.userAgent.indexOf('Android')>=0?'MAIC':'My browser';
function request(method,path,done,body){var x=new XMLHttpRequest();x.open(method,path,true);x.setRequestHeader('Content-Type','application/json');x.onload=function(){try{done(JSON.parse(x.responseText));}catch(e){status.textContent='Connection unavailable. Try again.';}};x.onerror=function(){status.textContent='Connection unavailable. Try again.';};x.send(method==='POST'?JSON.stringify(body||{}):null);}
function update(value){if(value.state==='approved'){location.replace('/');return;}if(value.code){code.textContent=value.code;status.textContent='On your PC, open Home > Connect from anywhere. Match this code and approve this browser.';button.style.display='none';label.style.display='none';}else if(value.error){status.textContent=value.error;}else if(value.state==='unpaired'){if(code.textContent){status.textContent='The connection request expired. Request connection again.';}code.textContent='';button.style.display='';label.style.display='';}}
button.onclick=function(){request('POST','/_remote/pair',update,{label:name.value});};
request('GET','/_remote/session',update);setInterval(function(){request('GET','/_remote/session',update);},2500);
})();</script></html>'''


def _origin(value, *, public=False):
    parsed = urlsplit(str(value))
    if (parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query
            or parsed.fragment or not parsed.hostname):
        raise ValueError('Remote origins must be credential-free origins without a path.')
    if public:
        if parsed.scheme != 'https' or parsed.port not in (None, 443, 8443, 10000):
            raise ValueError('Remote access requires an HTTPS origin on a supported Funnel port.')
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', parsed.hostname):
            raise ValueError('Invalid remote hostname.')
    else:
        if parsed.scheme != 'http':
            raise ValueError('The fixed dashboard upstream must use local HTTP.')
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise ValueError('The fixed dashboard upstream must be a local IP address.') from exc
        if not (address.is_loopback or address.is_private) or address.is_unspecified:
            raise ValueError('The fixed dashboard upstream must be a local IP address.')
    return f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')


def _digest(value):
    return hashlib.sha256(value.encode('ascii')).hexdigest()


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


class RemoteGateway:
    def __init__(self, root, upstream, desktop_port=8841, public_origin=None, *, port=8842):
        self.root = Path(root)
        self.upstream = _origin(upstream)
        parsed = urlsplit(self.upstream)
        self.upstream_host = parsed.netloc
        self.desktop_host = f'{parsed.hostname}:{int(desktop_port)}'
        self.desktop_upstream = f'http://{self.desktop_host}/desktop/ws'
        if not 0 < int(desktop_port) < 65536 or not 0 <= int(port) < 65536:
            raise ValueError('Invalid fixed gateway port.')
        if public_origin is None:
            config = self.root / '.runtime' / 'remote' / 'config.json'
            try:
                public_origin = json.loads(config.read_text(encoding='utf-8')).get('publicOrigin')
            except FileNotFoundError:
                pass
        self.public_origin = _origin(public_origin, public=True) if public_origin else None
        self.expected_host = urlsplit(self.public_origin).netloc if self.public_origin else None
        self.port = int(port)
        self._lock = threading.RLock()
        self._records = {}
        self._attempts = defaultdict(deque)
        self._websockets = defaultdict(set)
        self._active_tasks = defaultdict(set)
        self._expiry_task = None
        self._session_file = self.root / '.runtime' / 'remote' / 'sessions.json'
        self._loop = self._thread = self._runner = self._client = None
        self._started = threading.Event()
        self._error = None
        self._load()

    def _load(self):
        try:
            values = json.loads(self._session_file.read_text(encoding='utf-8'))
        except (FileNotFoundError, OSError, ValueError):
            return
        if not isinstance(values, list):
            return
        now = time.time()
        for item in values[:MAX_DEVICES]:
            if not isinstance(item, dict):
                continue
            digest, identity, expiry = item.get('hash', ''), item.get('id', ''), item.get('expiresAt', 0)
            if (isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest)
                    and isinstance(identity, str) and re.fullmatch('device_[0-9a-f]{32}', identity)
                    and isinstance(expiry, (int, float)) and now < expiry <= now + SESSION_SECONDS + 60):
                label = item.get('label', 'Browser')
                self._records[digest] = {'id': identity, 'label': self._label(label),
                                         'state': 'approved', 'expiresAt': expiry}

    @staticmethod
    def _label(value):
        if not isinstance(value, str):
            return 'Browser'
        return ''.join(c for c in value[:60] if c.isprintable()).strip() or 'Browser'

    def _save(self):
        self._session_file.parent.mkdir(parents=True, exist_ok=True)
        values = [dict(hash=digest, id=item['id'], label=item['label'], expiresAt=item['expiresAt'])
                  for digest, item in self._records.items() if item['state'] == 'approved']
        temporary = self._session_file.with_name('sessions.' + secrets.token_hex(8) + '.tmp')
        try:
            temporary.write_text(json.dumps(values, separators=(',', ':')), encoding='utf-8')
            temporary.replace(self._session_file)
        finally:
            temporary.unlink(missing_ok=True)

    def _expire(self):
        now = time.time()
        for digest in [d for d, item in self._records.items() if item['expiresAt'] <= now]:
            self._records.pop(digest, None)
            self._close_sockets(digest)

    def info(self):
        return {'ok': True, 'enabled': bool(self.public_origin), 'url': self.public_origin or '',
                'message': ('Gateway is running. Use the secure address and approve new browsers from this PC.' if self._runner
                            else 'Remote gateway is configured; start the dashboard to connect.'
                            if self.public_origin else 'Remote access is not configured.')}

    def status(self):
        with self._lock:
            self._expire()
            pending = [{'id': item['id'], 'code': item['code'], 'label': item['label']}
                       for item in self._records.values() if item['state'] == 'pending']
            devices = [{'id': item['id'], 'label': item['label'], 'expiresAt': _timestamp(item['expiresAt'])}
                       for item in self._records.values() if item['state'] == 'approved']
        return {'ok': True, 'pending': pending, 'devices': devices}

    def approve(self, identity):
        if not isinstance(identity, str):
            raise ValueError('Choose a pending browser.')
        with self._lock:
            self._expire()
            match = next((item for item in self._records.values() if item['id'] == identity), None)
            if not match or match['state'] != 'pending':
                raise ValueError('This browser request expired or was already approved.')
            if sum(item['state'] == 'approved' for item in self._records.values()) >= MAX_DEVICES:
                raise ValueError('Revoke an old browser before approving another.')
            previous = dict(match)
            match.update(state='approved', expiresAt=time.time() + SESSION_SECONDS)
            match.pop('code', None)
            try:
                self._save()
            except OSError:
                match.clear()
                match.update(previous)
                raise RuntimeError('Browser approval could not be saved. Check free disk space and try again.') from None
        return {'ok': True, 'message': 'Browser approved. It can now connect to this PC.'}

    def revoke(self, identity):
        if not isinstance(identity, str):
            raise ValueError('Choose a browser to revoke.')
        with self._lock:
            match = next((digest for digest, item in self._records.items() if item['id'] == identity), None)
            if not match:
                raise ValueError('This browser is already disconnected.')
            previous = self._records.pop(match)
            try:
                self._save()
            except OSError:
                self._records[match] = previous
                raise RuntimeError('Browser revocation could not be saved. Check free disk space and try again.') from None
            self._close_sockets(match)
        return {'ok': True, 'message': 'Browser disconnected and access revoked.'}

    def _close_sockets(self, digest):
        if self._loop and self._loop.is_running():
            async def close():
                for task in list(self._active_tasks.get(digest, ())):
                    task.cancel()
                for socket in list(self._websockets.get(digest, ())):
                    await socket.close(code=1008, message=b'Access revoked')
            asyncio.run_coroutine_threadsafe(close(), self._loop)

    def _record(self, request):
        token = request.cookies.get(COOKIE, '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return None, None
        digest = _digest(token)
        with self._lock:
            self._expire()
            item = self._records.get(digest)
            return digest, dict(item) if item else None

    def _check_request(self, request):
        if request.host != self.expected_host:
            raise web.HTTPForbidden(text='Invalid remote host.')
        origin = request.headers.get('Origin')
        if origin and origin != self.public_origin:
            raise web.HTTPForbidden(text='Invalid remote origin.')
        if request.method not in ('GET', 'HEAD') and origin != self.public_origin:
            raise web.HTTPForbidden(text='The remote origin is required.')
        if request.query_string:
            raise web.HTTPBadRequest(text='Query parameters are not supported.')
        raw = request.raw_path.split('?', 1)[0]
        decoded = unquote(raw)
        if ('\\' in decoded or '\x00' in decoded or '%' in decoded
                or any(part in ('.', '..') for part in decoded.split('/')) or '//' in decoded):
            raise web.HTTPBadRequest(text='Invalid remote path.')
        limit = MAX_CHAT_REQUEST_BYTES if request.path == '/api/lm/chat' else MAX_BODY
        if request.content_length is not None and request.content_length > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=request.content_length)

    @staticmethod
    def _cookie(response, token, max_age):
        response.set_cookie(COOKIE, token, max_age=max_age, path='/', secure=True,
                            httponly=True, samesite='Strict')

    async def _pair(self, request):
        _, existing = self._record(request)
        if existing:
            response = web.json_response({'ok': True, 'state': existing['state'],
                                          **({'code': existing['code']} if existing['state'] == 'pending' else {})})
            self._cookie(response, request.cookies[COOKIE],
                         int(max(1, existing['expiresAt'] - time.time())))
            return response
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text='Send a JSON browser request.')
        if not isinstance(payload, dict) or set(payload) - {'label'}:
            raise web.HTTPBadRequest(text='Only a browser label is accepted.')
        with self._lock:
            self._expire()
            now = time.monotonic()
            attempts = self._attempts[request.remote or 'unknown']
            while attempts and attempts[0] < now - 60:
                attempts.popleft()
            if len(attempts) >= 6 or sum(item['state'] == 'pending' for item in self._records.values()) >= MAX_PENDING:
                return web.json_response({'ok': False, 'error': 'Too many connection requests. Try again later.'}, status=429)
            attempts.append(now)
            token = secrets.token_urlsafe(32)
            used = {item.get('code') for item in self._records.values()}
            code = f'{secrets.randbelow(1000000):06d}'
            while code in used:
                code = f'{secrets.randbelow(1000000):06d}'
            self._records[_digest(token)] = {'id': 'device_' + secrets.token_hex(16),
                                            'label': self._label(payload.get('label', 'Browser')),
                                            'code': code, 'state': 'pending',
                                            'expiresAt': time.time() + PENDING_SECONDS}
        response = web.json_response({'ok': True, 'state': 'pending', 'code': code})
        self._cookie(response, token, PENDING_SECONDS)
        return response

    async def _session(self, request):
        _, item = self._record(request)
        value = {'ok': True, 'state': item['state'] if item else 'unpaired'}
        if item and item['state'] == 'pending':
            value['code'] = item['code']
        response = web.json_response(value)
        if item:
            self._cookie(response, request.cookies[COOKIE], int(max(1, item['expiresAt'] - time.time())))
        return response

    def _headers(self, request, *, websocket=False):
        headers = CIMultiDict()
        for name, value in request.headers.items():
            lower = name.lower()
            if (lower in HOP_HEADERS or lower == 'forwarded' or lower.startswith(('x-forwarded-', 'tailscale-', 'sec-websocket-'))
                    or lower == 'cookie' or lower == 'accept-encoding'):
                continue
            headers.add(name, value)
        cookies = [(name, value) for name, value in request.cookies.items() if name != COOKIE]
        if cookies:
            headers['Cookie'] = '; '.join(f'{name}={value}' for name, value in cookies)
        headers['Host'] = self.desktop_host if websocket else self.upstream_host
        headers['Origin'] = self.upstream
        headers['Accept-Encoding'] = 'identity'
        return headers

    @staticmethod
    def _response_headers(upstream):
        headers = CIMultiDict()
        for name, value in upstream.headers.items():
            lower = name.lower()
            if lower in HOP_HEADERS or lower.startswith('access-control-') or lower == 'set-cookie':
                continue
            if lower == 'location':
                continue
            headers.add(name, value)
        for cookie in upstream.headers.getall('Set-Cookie', []):
            # Backend cookies are HttpOnly; force Secure for the outer HTTPS origin.
            if not re.search(r'(?:^|;)\s*secure(?:;|$)', cookie, re.I):
                cookie += '; Secure'
            headers.add('Set-Cookie', cookie)
        headers['Cache-Control'] = 'no-store'
        headers['X-Content-Type-Options'] = 'nosniff'
        return headers

    async def _proxy(self, request, digest):
        with self._lock:
            item = self._records.get(digest)
            if not item or item['state'] != 'approved' or item['expiresAt'] <= time.time():
                raise web.HTTPForbidden(text='Browser access expired.')
        task = asyncio.current_task()
        self._active_tasks[digest].add(task)
        try:
            return await self._proxy_request(request, digest)
        finally:
            self._active_tasks[digest].discard(task)
            if not self._active_tasks[digest]:
                self._active_tasks.pop(digest, None)

    async def _proxy_request(self, request, digest):
        limit = MAX_CHAT_REQUEST_BYTES if request.path == '/api/lm/chat' else MAX_BODY
        if request.content_length is not None and request.content_length > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=request.content_length)
        body = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > limit:
                raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(body))
        body = bytes(body)
        with self._lock:
            item = self._records.get(digest)
            if not item or item['state'] != 'approved' or item['expiresAt'] <= time.time():
                raise web.HTTPForbidden(text='Browser access expired before this request was sent.')
        if request.path == '/api/speech/transcribe':
            if len(body) > 960044 or request.content_type != 'audio/wav':
                raise web.HTTPBadRequest(text='Send a supported WAV recording.')
            if request.headers.get('X-MAIC-Language', 'auto') not in ('auto', 'en', 'el', 'sq', 'de', 'it'):
                raise web.HTTPBadRequest(text='Unsupported recording language.')
        try:
            async with self._client.request(request.method, self.upstream + request.path,
                                            headers=self._headers(request), data=body,
                                            allow_redirects=False) as upstream:
                if 300 <= upstream.status < 400:
                    raise web.HTTPBadGateway(text='Unexpected dashboard redirect.')
                response = web.StreamResponse(status=upstream.status, headers=self._response_headers(upstream))
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (ClientError, OSError, asyncio.TimeoutError):
            raise web.HTTPBadGateway(text='The PC dashboard is unavailable.')

    async def _websocket(self, request, digest):
        if request.headers.get('Origin') != self.public_origin:
            raise web.HTTPForbidden(text='The remote origin is required.')
        try:
            upstream = await self._client.ws_connect(self.desktop_upstream, headers=self._headers(request, websocket=True),
                                                     max_msg_size=MAX_BODY, heartbeat=20, protocols=('binary',))
        except Exception as exc:
            # Never include an upstream response or URL in browser errors.
            raise web.HTTPBadGateway(text='Reconnect the desktop from the dashboard.') from exc
        with self._lock:
            item = self._records.get(digest)
            valid = bool(item and item['state'] == 'approved' and item['expiresAt'] > time.time())
        if not valid:
            await upstream.close()
            raise web.HTTPForbidden(text='Browser access expired before the desktop connected.')
        browser = web.WebSocketResponse(max_msg_size=MAX_BODY, heartbeat=20, protocols=('binary',))
        try:
            await browser.prepare(request)
            self._websockets[digest].add(browser)

            async def relay(source, target):
                async for message in source:
                    if message.type != WSMsgType.BINARY:
                        break
                    await target.send_bytes(message.data)

            async def approved():
                while not browser.closed:
                    await asyncio.sleep(0.5)
                    with self._lock:
                        item = self._records.get(digest)
                        if not item or item['state'] != 'approved' or item['expiresAt'] <= time.time():
                            return

            tasks = [asyncio.create_task(job) for job in (relay(browser, upstream), relay(upstream, browser), approved())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._websockets[digest].discard(browser)
            if not self._websockets[digest]:
                self._websockets.pop(digest, None)
            await browser.close()
            await upstream.close()
        return browser

    async def _handle(self, request):
        self._check_request(request)
        path = request.path
        if request.method == 'GET' and path in PUBLIC_APP_ASSETS:
            # Only these non-private install assets are public. Read them from
            # the fixed web root without forwarding cookies or opening APIs.
            web_root = (self.root / 'web').resolve()
            asset = (web_root / path[1:]).resolve()
            if web_root not in asset.parents or not asset.is_file():
                raise web.HTTPNotFound(text='App asset unavailable.')
            return web.Response(body=asset.read_bytes(), content_type=PUBLIC_APP_ASSETS[path],
                                headers={'Cache-Control': 'no-store'})
        if path == '/_remote/health' and request.method == 'GET':
            return web.json_response({'ok': True, 'service': 'MAICRemote', 'enabled': True})
        if path == '/_remote/pair' and request.method == 'POST':
            return await self._pair(request)
        if path == '/_remote/session' and request.method == 'GET':
            return await self._session(request)
        digest, item = self._record(request)
        if not item or item['state'] != 'approved':
            if path == '/' and request.method == 'GET':
                page = PAIR_PAGE.replace(b'__PC_APPROVAL_URL__', html.escape(self.upstream + '/#access', quote=True).encode('utf-8'))
                return web.Response(body=page, content_type='text/html', headers={'Cache-Control': 'no-store'})
            if path.startswith('/api/'):
                return web.json_response({'ok': False, 'code': 'REMOTE_PAIRING_REQUIRED',
                                          'message': 'This browser needs approval. Open the secure dashboard and request connection again.'}, status=403)
            raise web.HTTPForbidden(text='Approve this browser on your PC before connecting.')
        if path.startswith('/api/remote/') and path != '/api/remote/info':
            raise web.HTTPForbidden(text='Browser approval is available only on the PC.')
        if path == '/desktop/ws' and request.method == 'GET':
            return await self._websocket(request, digest)
        valid_asset = (path.startswith('/desktop/') and bool(re.fullmatch(r'/desktop/[A-Za-z0-9_./-]+\.(?:js|css|svg|png|html)', path)))
        valid_output = bool(re.fullmatch(r'/api/comfy/output/[A-Za-z0-9_-]{1,128}', path))
        if ((request.method == 'GET' and (path in GET_PATHS or valid_asset or valid_output))
                or (request.method == 'POST' and path in POST_PATHS)):
            return await self._proxy(request, digest)
        raise web.HTTPNotFound(text='This route is unavailable remotely.')

    async def _initialize(self):
        application = web.Application(client_max_size=MAX_CHAT_REQUEST_BYTES)
        application.router.add_route('*', '/{path:.*}', self._handle)
        async def security_headers(request, response):
            response.headers['Content-Security-Policy'] = "frame-ancestors 'self'"
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['Permissions-Policy'] = 'microphone=(self), camera=()'
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
        application.on_response_prepare.append(security_headers)
        self._client = ClientSession(cookie_jar=DummyCookieJar(), auto_decompress=False,
                                     timeout=ClientTimeout(total=None, connect=5, sock_read=190))
        self._runner = web.AppRunner(application, access_log=None, handler_cancellation=True, shutdown_timeout=2)
        await self._runner.setup()
        site = web.TCPSite(self._runner, '127.0.0.1', self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        async def expiry_watch():
            while True:
                await asyncio.sleep(0.5)
                with self._lock:
                    self._expire()
        self._expiry_task = asyncio.create_task(expiry_watch())

    async def _shutdown(self):
        if self._expiry_task:
            self._expiry_task.cancel()
            await asyncio.gather(self._expiry_task, return_exceptions=True)
            self._expiry_task = None
        for tasks in list(self._active_tasks.values()):
            for task in list(tasks):
                task.cancel()
        for sockets in list(self._websockets.values()):
            for socket in list(sockets):
                await socket.close(code=1001, message=b'PC dashboard stopped')
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._client:
            await self._client.close()
            self._client = None

    def start(self):
        if not self.public_origin or (self._thread and self._thread.is_alive()):
            return
        self._started.clear()
        self._error = None

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._initialize())
            except Exception:
                self._error = 'Remote gateway could not start on its loopback port.'
                self._loop.run_until_complete(self._shutdown())
                self._started.set()
                self._loop.close()
                return
            self._started.set()
            try:
                self._loop.run_forever()
            finally:
                self._loop.run_until_complete(self._shutdown())
                self._loop.close()

        self._thread = threading.Thread(target=run, name='MAICRemote', daemon=True)
        self._thread.start()
        if not self._started.wait(6) or self._error:
            raise RuntimeError(self._error or 'Remote gateway did not become ready.')

    def stop(self):
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(6)


Gateway = RemoteGateway
