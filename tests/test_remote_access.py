"""Remote gateway tests against isolated loopback HTTP and WebSocket services."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import select
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, WSMsgType, web

from remote_access import COOKIE, MAX_PENDING, RemoteGateway


ORIGIN = 'https://test.example.ts.net:8443'
HOST = 'test.example.ts.net:8443'


class StubHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_):
        pass

    def _handle(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.seen.append((self.command, self.path, dict(self.headers), body))
        if self.path == '/api/lm/chat' and body == b'{"block":true}':
            self.server.block_started.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if select.select([self.connection], [], [], 0.05)[0]:
                    if not self.connection.recv(1):
                        self.server.block_disconnected.set()
                        return
            self.server.block_write_attempted.set()
            return
        if self.path == '/api/lm/chat' and body == b'{"stream":true}':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'event: delta\ndata: {"text":"first"}\n\n')
            self.wfile.flush()
            self.server.stream_started.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if select.select([self.connection], [], [], 0.05)[0]:
                    if not self.connection.recv(1):
                        self.server.stream_disconnected.set()
                        return
            return
        if self.path.startswith('/api/comfy/output/'):
            self.send_response(206)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Range', 'bytes 0-2/6')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', '3')
            self.end_headers()
            self.wfile.write(b'abc')
        elif self.path == '/api/lm/chat':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'event: delta\ndata: {"text":"test"}\n\n')
            self.wfile.flush()
        else:
            value = json.dumps({'ok': True, 'source': 'isolated-stub'}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(value)))
            self.send_header('Set-Cookie', 'MAICMedia=test-media; Path=/api/comfy; HttpOnly; SameSite=Strict')
            self.end_headers()
            self.wfile.write(value)

    do_GET = do_POST = _handle


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), StubHandler)
        cls.upstream.seen = []
        cls.upstream.block_started = threading.Event()
        cls.upstream.block_disconnected = threading.Event()
        cls.upstream.block_write_attempted = threading.Event()
        cls.upstream.stream_started = threading.Event()
        cls.upstream.stream_disconnected = threading.Event()
        cls.origin = f'http://127.0.0.1:{cls.upstream.server_port}'
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        cls.ws_ready = threading.Event()
        cls.ws_seen = []
        cls.ws_loop = asyncio.new_event_loop()

        async def websocket(request):
            cls.ws_seen.append(dict(request.headers))
            socket = web.WebSocketResponse(protocols=('binary',))
            await socket.prepare(request)
            async for message in socket:
                if message.type == WSMsgType.BINARY:
                    await socket.send_bytes(message.data)
            return socket

        async def setup():
            app = web.Application()
            app.router.add_get('/desktop/ws', websocket)
            cls.ws_runner = web.AppRunner(app, access_log=None)
            await cls.ws_runner.setup()
            site = web.TCPSite(cls.ws_runner, '127.0.0.1', 0)
            await site.start()
            cls.ws_port = site._server.sockets[0].getsockname()[1]

        def run():
            asyncio.set_event_loop(cls.ws_loop)
            cls.ws_loop.run_until_complete(setup())
            cls.ws_ready.set()
            cls.ws_loop.run_forever()
            cls.ws_loop.run_until_complete(cls.ws_runner.cleanup())
            cls.ws_loop.close()

        cls.ws_thread = threading.Thread(target=run, daemon=True)
        cls.ws_thread.start()
        assert cls.ws_ready.wait(5)

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.upstream_thread.join(3)
        cls.ws_loop.call_soon_threadsafe(cls.ws_loop.stop)
        cls.ws_thread.join(3)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='maic-gateway-test-')
        self.root = Path(self.temporary.name)
        self.gateway = RemoteGateway(self.root, self.origin, desktop_port=self.ws_port, public_origin=ORIGIN, port=0)
        self.gateway.start()
        self.start_seen = len(self.upstream.seen)

    def tearDown(self):
        self.gateway.stop()
        self.temporary.cleanup()

    def request(self, method, path, body=None, *, cookie=None, origin=ORIGIN, host=HOST, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.gateway.port, timeout=3)
        values = {'Host': host}
        if origin is not None:
            values['Origin'] = origin
        if cookie:
            values['Cookie'] = cookie
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            values['Content-Type'] = 'application/json'
        values.update(headers or {})
        connection.request(method, path, body=body, headers=values)
        response = connection.getresponse()
        value = response.read()
        result = response.status, response.getheaders(), value
        connection.close()
        return result

    def pair(self, *, label='Test browser'):
        status, headers, body = self.request('POST', '/_remote/pair', {'label': label})
        self.assertEqual(status, 200)
        value = json.loads(body)
        cookie = SimpleCookie()
        cookie.load(next(value for name, value in headers if name.lower() == 'set-cookie'))
        self.assertTrue(cookie[COOKIE]['secure'])
        self.assertTrue(cookie[COOKIE]['httponly'])
        self.assertEqual(cookie[COOKIE]['samesite'], 'Strict')
        self.assertEqual(cookie[COOKIE]['max-age'], '600')
        self.assertRegex(value['code'], r'^\d{6}$')
        self.assertEqual(set(value), {'ok', 'state', 'code'})
        return f'{COOKIE}={cookie[COOKIE].value}'

    def approved(self):
        cookie = self.pair()
        self.gateway.approve(self.gateway.status()['pending'][0]['id'])
        return cookie

    def test_anonymous_and_pending_cannot_access_dashboard_services_or_websocket(self):
        status, headers, body = self.request('GET', '/', origin=None)
        self.assertEqual(status, 200)
        self.assertIn(b'Request connection', body)
        self.assertIn(f'href="{self.origin}/#access"'.encode(), body)
        self.assertIn(b'Open approval on this PC', body)
        self.assertIn(b'Only your Windows PC can approve the connection.', body)
        self.assertIn(('Content-Security-Policy', "frame-ancestors 'self'"), headers)
        self.assertIn(('Referrer-Policy', 'no-referrer'), headers)
        self.assertIn(('Permissions-Policy', 'microphone=(self), camera=()'), headers)
        for cookie in (None, self.pair()):
            for path in ('/app.js', '/voice.js', '/api/status', '/api/control-session',
                         '/api/comfy/output/test', '/desktop/ws', '/desktop/core/rfb.js'):
                self.assertEqual(self.request('GET', path, cookie=cookie)[0], 403, path)
        self.assertEqual(len(self.upstream.seen), self.start_seen)

    def test_pc_approval_link_is_html_escaped_and_does_not_auto_pair(self):
        original = self.gateway.upstream
        try:
            # Constructor already rejects unsafe origins; rendering escapes even
            # an accidentally altered internal value without creating attributes.
            self.gateway.upstream = original + '/" onclick="alert(1)&<test>'
            status, _, body = self.request('GET', '/', origin=None)
            self.assertEqual(status, 200)
            self.assertIn(b'&quot; onclick=&quot;alert(1)&amp;&lt;test&gt;/#access', body)
            self.assertNotIn(b'" onclick="', body)
            self.assertNotIn(b'__PC_APPROVAL_URL__', body)
            self.assertFalse(self.gateway.status()['pending'])
        finally:
            self.gateway.upstream = original

    def test_pair_requires_exact_host_and_origin_and_safe_payload(self):
        for values in ({'origin': 'https://evil.example'}, {'origin': None}, {'host': 'localhost:8842'}):
            self.assertEqual(self.request('POST', '/_remote/pair', {}, **values)[0], 403)
        self.assertEqual(self.request('POST', '/_remote/pair', {'target': 'http://evil.example'})[0], 400)
        self.assertFalse(self.gateway.status()['pending'])

    def test_approve_upgrades_same_cookie_and_persists_only_credential_hash(self):
        cookie = self.pair()
        self.assertEqual(self.request('GET', '/_remote/session', cookie=cookie)[0], 200)
        pending = self.gateway.status()['pending'][0]
        self.gateway.approve(pending['id'])
        status, headers, body = self.request('GET', '/_remote/session', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['state'], 'approved')
        self.assertTrue(any('Max-Age=25919' in value or 'Max-Age=2592000' in value
                            for name, value in headers if name.lower() == 'set-cookie'))
        token = cookie.split('=', 1)[1]
        self.assertNotIn(token, self.gateway._session_file.read_text())
        saved = json.loads(self.gateway._session_file.read_text())
        self.assertEqual(set(saved[0]), {'hash', 'id', 'label', 'expiresAt'})
        restored = RemoteGateway(self.root, self.origin, public_origin=ORIGIN, port=0)
        self.assertEqual(restored.status()['devices'][0]['id'], pending['id'])
        self.assertEqual(self.request('GET', '/api/pc/apps', cookie=cookie)[0], 200)
        self.gateway.revoke(pending['id'])
        self.assertEqual(self.request('GET', '/api/pc/apps', cookie=cookie)[0], 403)
        self.assertEqual(json.loads(self.gateway._session_file.read_text()), [])

    def test_fixed_upstream_strips_identity_headers_and_remote_cookie_preserves_secure_backend_cookie(self):
        cookie = self.approved() + '; MAICDesktop=backend-ticket'
        status, headers, _ = self.request('GET', '/api/pc/apps', cookie=cookie,
            headers={'X-Forwarded-For': '8.8.8.8', 'Forwarded': 'host=evil.example',
                     'Tailscale-User-Login': 'spoof@example.com', 'X-MAIC-Control': 'internal-test'})
        self.assertEqual(status, 200)
        method, path, forwarded, _ = self.upstream.seen[-1]
        self.assertEqual((method, path), ('GET', '/api/pc/apps'))
        self.assertEqual(forwarded['Host'], self.origin.removeprefix('http://'))
        self.assertEqual(forwarded['Origin'], self.origin)
        self.assertNotIn('X-Forwarded-For', forwarded)
        self.assertNotIn('Forwarded', forwarded)
        self.assertNotIn('Tailscale-User-Login', forwarded)
        self.assertEqual(forwarded['Cookie'], 'MAICDesktop=backend-ticket')
        self.assertEqual(forwarded['X-MAIC-Control'], 'internal-test')
        self.assertTrue(any(value.endswith('; Secure') for name, value in headers if name.lower() == 'set-cookie'))

    def test_saved_approval_reconnects_after_gateway_restart_with_same_cookie(self):
        cookie = self.approved()
        before = self.gateway.status()['devices'][0]
        self.gateway.stop()
        self.gateway = RemoteGateway(self.root, self.origin, desktop_port=self.ws_port, public_origin=ORIGIN, port=0)
        self.gateway.start()
        self.assertEqual(self.gateway.status()['devices'][0], before)
        status, _, value = self.request('GET', '/api/pc/apps', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(value)['source'], 'isolated-stub')

    def test_cookie_refresh_preserves_hard_expiry_and_expired_api_prompts_pairing(self):
        cookie = self.approved()
        with self.gateway._lock:
            for item in self.gateway._records.values():
                item['expiresAt'] -= 3600
            expiry = next(iter(self.gateway._records.values()))['expiresAt']
        status, headers, value = self.request('GET', '/_remote/session', cookie=cookie)
        self.assertEqual(status, 200)
        parsed = SimpleCookie()
        parsed.load(next(value for name, value in headers if name.lower() == 'set-cookie'))
        self.assertLessEqual(int(parsed[COOKIE]['max-age']), 30 * 86400 - 3600)
        self.assertEqual(next(iter(self.gateway._records.values()))['expiresAt'], expiry)
        with self.gateway._lock:
            for item in self.gateway._records.values():
                item['expiresAt'] = time.time() - 1
        status, _, value = self.request('GET', '/api/control-session', cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(value)['code'], 'REMOTE_PAIRING_REQUIRED')

    def test_save_failures_do_not_commit_approval_or_revocation(self):
        cookie = self.pair()
        identity = self.gateway.status()['pending'][0]['id']
        with patch.object(self.gateway, '_save', side_effect=OSError('simulated disk failure')):
            with self.assertRaisesRegex(RuntimeError, 'approval could not be saved'):
                self.gateway.approve(identity)
        self.assertEqual(self.gateway.status()['pending'][0]['id'], identity)
        self.assertEqual(self.request('GET', '/api/pc/apps', cookie=cookie)[0], 403)
        self.gateway.approve(identity)
        with patch.object(self.gateway, '_save', side_effect=OSError('simulated disk failure')):
            with self.assertRaisesRegex(RuntimeError, 'revocation could not be saved'):
                self.gateway.revoke(identity)
        self.assertEqual(self.gateway.status()['devices'][0]['id'], identity)
        self.assertEqual(self.request('GET', '/api/pc/apps', cookie=cookie)[0], 200)
        self.gateway.revoke(identity)
        self.assertFalse(self.gateway.status()['devices'])

    def test_chat_media_and_tts_require_approval_and_keep_route_body_limits(self):
        self.assertEqual(self.request('GET', '/api/tts/status')[0], 403)
        self.assertEqual(self.request('POST', '/api/tts/speak', {'text': 'hello'})[0], 403)
        cookie = self.approved()
        for route in ('/chat-media.js', '/api/tts/status'):
            self.assertEqual(self.request('GET', route, cookie=cookie)[0], 200)
        self.assertEqual(self.request('GET', '/', cookie=cookie)[0], 200)
        self.assertEqual(self.request('POST', '/api/tts/speak', {'text': 'hello'}, cookie=cookie)[0], 200)
        # Encoded image bodies exceed the old global 2MB limit, but only chat may.
        payload = {'image_fixture': 'x' * (2 * 1024 * 1024 + 10)}
        self.assertEqual(self.request('POST', '/api/lm/chat', payload, cookie=cookie)[0], 200)
        self.assertEqual(self.request('POST', '/api/control', payload, cookie=cookie)[0], 413)

    def test_unknown_management_query_and_traversal_paths_never_reach_upstream(self):
        cookie = self.approved()
        count = len(self.upstream.seen)
        for method, path, status in (
            ('GET', '/api/remote/status', 403), ('POST', '/api/remote/approve', 403),
            ('POST', '/api/remote/revoke', 403), ('GET', '/private.py', 404),
            ('POST', '/api/run-command', 404), ('GET', '/api/comfy/output/a/b', 404),
            ('GET', '/desktop/../private.js', 400), ('GET', '/desktop/%2e%2e/private.js', 400),
            ('GET', '/desktop/%252e%252e/private.js', 400), ('GET', '/desktop/core%5crfb.js', 400),
            ('GET', '/api/pc/apps?url=http://evil.example', 400),
            ('GET', '/?workspace=codex', 400),
            ('GET', '/?workspace=codex&url=http://unrelated.invalid', 400),
        ):
            self.assertEqual(self.request(method, path, {} if method == 'POST' else None, cookie=cookie)[0], status, path)
        self.assertEqual(len(self.upstream.seen), count)

    def test_authenticated_range_and_sse_passthrough(self):
        cookie = self.approved()
        status, headers, body = self.request('GET', '/api/comfy/output/safe-id', cookie=cookie,
                                              headers={'Range': 'bytes=0-2'})
        self.assertEqual((status, body), (206, b'abc'))
        self.assertIn(('Content-Range', 'bytes 0-2/6'), headers)
        self.assertEqual(self.upstream.seen[-1][2]['Range'], 'bytes=0-2')
        status, headers, body = self.request('POST', '/api/lm/chat', {}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(('Content-Type', 'text/event-stream'), headers)
        self.assertIn(b'event: delta', body)

    def test_chat_abort_before_headers_immediately_closes_blocked_upstream(self):
        cookie = self.approved()
        started = self.upstream.block_started
        disconnected = self.upstream.block_disconnected
        attempted = self.upstream.block_write_attempted
        started.clear()
        disconnected.clear()
        attempted.clear()
        connection = socket.create_connection(('127.0.0.1', self.gateway.port), timeout=2)
        body = b'{"block":true}'
        request = (f'POST /api/lm/chat HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n'
                   f'Cookie: {cookie}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body
        connection.sendall(request)
        self.assertTrue(started.wait(2))
        before = time.monotonic()
        connection.shutdown(socket.SHUT_RDWR)
        connection.close()
        self.assertTrue(disconnected.wait(0.95), 'Gateway kept the upstream chat request alive after browser disconnect.')
        self.assertLess(time.monotonic() - before, 1)
        self.assertFalse(attempted.is_set())

    def test_revoked_slow_post_cannot_finish_body_and_execute_later(self):
        cookie = self.approved()
        identity = self.gateway.status()['devices'][0]['id']
        count = len(self.upstream.seen)
        connection = socket.create_connection(('127.0.0.1', self.gateway.port), timeout=2)
        self.addCleanup(connection.close)
        body = b'{"action":"speaker-mute","value":true}'
        header = (f'POST /api/pc/action HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n'
                  f'Cookie: {cookie}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode()
        started, finished = threading.Event(), threading.Event()
        proxy_request = self.gateway._proxy_request

        async def observed_request(request, digest):
            started.set()
            try:
                return await proxy_request(request, digest)
            finally:
                finished.set()

        # Observe the real body reader so revocation happens after the approved
        # request is registered, and the no-upstream assertion runs after exit.
        with patch.object(self.gateway, '_proxy_request', side_effect=observed_request):
            connection.sendall(header + body[:2])
            self.assertTrue(started.wait(1), 'The partial upload never reached the body reader.')
            self.gateway.revoke(identity)
            try:
                connection.sendall(body[2:])
                self.assertEqual(connection.recv(1), b'')
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                # Windows may report WSAECONNABORTED instead of EOF or reset.
                pass
            self.assertTrue(finished.wait(1), 'The revoked upload handler kept running.')
            self.assertEqual(len(self.upstream.seen), count)

    def test_pair_rate_limit_and_expiry(self):
        cookies = [self.pair() for _ in range(6)]
        self.assertEqual(self.request('POST', '/_remote/pair', {})[0], 429)
        self.assertEqual(len(self.gateway.status()['pending']), 6)
        with self.gateway._lock:
            for item in self.gateway._records.values():
                item['expiresAt'] = time.time() - 1
        self.assertFalse(self.gateway.status()['pending'])
        self.assertEqual(json.loads(self.request('GET', '/_remote/session', cookie=cookies[0])[2])['state'], 'unpaired')

    def test_revoke_and_expiry_cancel_active_sse_upstream(self):
        for expire in (False, True):
            # Separate rate-limit slots and an approved credential for each stream.
            cookie = self.approved()
            identity = self.gateway.status()['devices'][0]['id']
            started = self.upstream.stream_started
            disconnected = self.upstream.stream_disconnected
            started.clear()
            disconnected.clear()
            connection = http.client.HTTPConnection('127.0.0.1', self.gateway.port, timeout=2)
            connection.request('POST', '/api/lm/chat', body=b'{"stream":true}',
                headers={'Host': HOST, 'Origin': ORIGIN, 'Cookie': cookie, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(1), b'e')
            self.assertTrue(started.wait(1))
            before = time.monotonic()
            if expire:
                with self.gateway._lock:
                    for item in self.gateway._records.values():
                        item['expiresAt'] = time.time() - 1
            else:
                self.gateway.revoke(identity)
            self.assertTrue(disconnected.wait(0.95), 'Stream kept running after access was revoked or expired.')
            self.assertLess(time.monotonic() - before, 1)
            response.close()
            connection.close()

    def test_wav_type_language_and_size_checked_before_upstream(self):
        cookie = self.approved()
        count = len(self.upstream.seen)
        for headers, body in (({'Content-Type': 'text/plain'}, b'RIFF'),
                              ({'Content-Type': 'audio/wav', 'X-MAIC-Language': 'invalid'}, b'RIFF'),
                              ({'Content-Type': 'audio/wav'}, b'x' * 960045)):
            self.assertEqual(self.request('POST', '/api/speech/transcribe', body, cookie=cookie, headers=headers)[0], 400)
        self.assertEqual(len(self.upstream.seen), count)

    def test_websocket_fixed_target_and_revoke_closes_live_connection(self):
        cookie = self.approved() + '; MAICDesktop=isolated-ticket'
        identity = self.gateway.status()['devices'][0]['id']

        async def check():
            async with ClientSession() as client:
                async with client.ws_connect(f'http://127.0.0.1:{self.gateway.port}/desktop/ws',
                    headers={'Host': HOST, 'Origin': ORIGIN, 'Cookie': cookie}, protocols=('binary',)) as socket:
                    await socket.send_bytes(b'isolated-desktop-data')
                    message = await asyncio.wait_for(socket.receive(), 2)
                    self.assertEqual(message.data, b'isolated-desktop-data')
                    self.gateway.revoke(identity)
                    message = await asyncio.wait_for(socket.receive(), 2)
                    self.assertIn(message.type, (WSMsgType.CLOSE, WSMsgType.CLOSED))

        asyncio.run(check())
        forwarded = self.ws_seen[-1]
        self.assertEqual(forwarded['Host'], f'127.0.0.1:{self.ws_port}')
        self.assertEqual(forwarded['Origin'], self.origin)
        self.assertEqual(forwarded['Cookie'], 'MAICDesktop=isolated-ticket')

    def test_absent_config_disabled_and_external_upstream_refused(self):
        disabled = RemoteGateway(self.root, self.origin, port=0)
        disabled.start()
        self.assertFalse(disabled.info()['enabled'])
        self.assertIsNone(disabled._thread)
        for origin in ('http://8.8.8.8:8840', 'http://example.com', 'http://user:secret@127.0.0.1',
                       'http://127.0.0.1/another-path'):
            with self.assertRaises(ValueError):
                RemoteGateway(self.root, origin, public_origin=ORIGIN)


if __name__ == '__main__':
    unittest.main()
