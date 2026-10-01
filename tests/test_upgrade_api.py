"""Authenticated upgrade routes, isolated services, and no desktop/model actions."""
import http.client
from http.cookies import SimpleCookie
import ipaddress
import json
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import maic_server


APP_ID = 'app_' + 'a' * 24
OUTPUT_ID = 'test-approved-output-id'


class QuietHandler(maic_server.Handler):
    def log_message(self, *_):
        pass


class TrackingStream:
    def __init__(self, events=None, failure=None):
        self.events = iter(events or [])
        self.failure = failure
        self.closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        if self.failure:
            raise self.failure
        return next(self.events)

    def close(self):
        self.closed.set()


class UpgradeAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_root = maic_server.ROOT
        runtime = (cls.original_root / '.runtime').resolve()
        runtime.mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='upgrade-api-test-', dir=runtime)
        cls.root = Path(cls.temporary.name).resolve()
        assert cls.root.parent == runtime
        maic_server.ROOT = cls.root
        web = cls.root / 'web'
        vendor = web / 'vendor' / 'novnc' / 'core'
        vendor.mkdir(parents=True)
        (web / 'index.html').write_text('<!doctype html><title>Test dashboard</title>')
        (web / 'app.js').write_text('window.testDashboard = true;')
        (vendor / 'rfb.js').write_text('export default {};')
        (cls.root / 'private.js').write_text('private-test-marker')
        cls.server = maic_server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        cls.host, cls.port = cls.server.server_address
        cls.server.allowed_network = ipaddress.ip_network('127.0.0.0/8')
        cls.server.origin = f'http://{cls.host}:{cls.port}'
        cls.server.expected_host = f'{cls.host}:{cls.port}'
        cls.server.control_lock = threading.RLock()
        cls.server.control_sessions = {}
        cls.server.tap_lock = threading.Lock()
        cls.server.taps = 0
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=3)
        maic_server.ROOT = cls.original_root
        assert Path(cls.temporary.name).resolve().parent == (cls.original_root / '.runtime').resolve()
        cls.temporary.cleanup()

    def setUp(self):
        with self.server.control_lock:
            self.server.media_sessions = {}
        app = {'id': APP_ID, 'name': 'Test App', 'kind': 'desktop', 'running': True,
               'active': False, 'favourite': False}
        self.apps = Mock()
        self.apps.catalog.return_value = {'ok': True, 'apps': [app], 'count': 1}
        self.apps.state.return_value = self.apps.catalog.return_value

        def action(payload):
            if (set(payload) != {'action', 'id'} or payload.get('id') != APP_ID
                    or payload.get('action') not in ('open', 'activate')):
                raise ValueError('Select a known app ID.')
            return {'ok': True, 'activated': payload['action'] == 'activate'}

        def favourites(payload):
            if set(payload) != {'ids'} or payload['ids'] != [APP_ID]:
                raise ValueError('Select known favourite IDs.')
            return {'ok': True, 'apps': [dict(app, favourite=True)]}

        self.apps.action.side_effect = action
        self.apps.favourites.side_effect = favourites
        self.ai = Mock()
        self.ai.models.return_value = {'ok': True, 'available': True, 'models': [{'id': 'test-model', 'name': 'Test model'}]}
        self.ai.comfy_status.return_value = {'ok': True, 'available': True, 'running': 0, 'pending': 0, 'outputs': []}
        self.ai.start_lm_api.return_value = {'ok': True, 'message': 'Test API started.'}
        self.stream = TrackingStream(['event: token\ndata: {"text":"test"}\n\n',
                                      'event: done\ndata: {}\n\n'])
        self.ai.stream_chat.return_value = self.stream

        def output(identifier, range_header=None):
            if identifier != OUTPUT_ID:
                raise ValueError('Unknown output ID.')
            if range_header == 'bytes=1-3':
                return 'video/mp4', b'123', {'status': 206, 'headers': {
                    'Content-Range': 'bytes 1-3/6', 'Accept-Ranges': 'bytes'}}
            return 'image/png', b'test-image-data', {}

        self.ai.output.side_effect = output
        self.bridge = Mock()
        self.bridge.port = 8841
        self.bridge.status.return_value = {'ok': True, 'available': True, 'port': 8841}
        # This deliberately matches the actual bridge contract: no port key.
        self.bridge.issue_ticket.return_value = {
            'ticket': 'fabricated-test-ticket', 'expires_in': 60,
            'cookie_name': 'MAICDesktop', 'path': '/desktop',
            'websocketUrl': f'ws://{self.host}:8841/desktop/ws'}
        self.bridge.desktop_credentials.return_value = {'password': 'fabricated-test-password'}
        self.server.app_registry = self.apps
        self.server.ai_service = self.ai
        self.server.desktop_bridge = self.bridge

    def request(self, path, payload=None, headers=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        request_headers = dict(headers or {})
        method = 'GET' if payload is None else 'POST'
        body = None if payload is None else json.dumps(payload)
        if payload is not None:
            request_headers.setdefault('Content-Type', 'application/json')
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        status, response_headers, data = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, response_headers, data

    def authorized(self):
        status, _, body = self.request('/api/control-session')
        self.assertEqual(status, 200)
        return {'X-MAIC-Control': json.loads(body)['token']}

    def test_remote_management_is_local_pc_only(self):
        gateway = Mock()
        gateway.approve.return_value = {'ok': True, 'message': 'Approved.'}
        gateway.revoke.return_value = {'ok': True, 'message': 'Revoked.'}
        gateway.status.return_value = {'ok': True, 'pending': [], 'devices': []}
        self.server.remote_gateway = gateway
        headers = self.authorized()
        for route in ('approve', 'revoke'):
            self.assertEqual(self.request('/api/remote/' + route, {'id': 'known-device'}, headers)[0], 200)
        gateway.reset_mock()
        original = self.server.server_address
        try:
            self.server.server_address = ('192.0.2.9', self.port)
            with patch.object(maic_server.Handler, 'control_authenticated', return_value=True):
                for route in ('approve', 'revoke'):
                    self.assertEqual(self.request('/api/remote/' + route, {'id': 'known-device'}, headers)[0], 403)
                self.assertEqual(self.request('/api/remote/status', headers=headers)[0], 403)
            gateway.approve.assert_not_called()
            gateway.revoke.assert_not_called()
        finally:
            self.server.server_address = original
            self.server.remote_gateway = None

    def test_speech_upload_requires_session_origin_and_bounded_wav(self):
        self.server.speech_service = Mock()
        self.server.speech_service.transcribe.return_value = {'ok': True, 'text': 'test'}
        def upload(body, headers):
            connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
            connection.request('POST', '/api/speech/transcribe', body, headers)
            response = connection.getresponse()
            status = response.status
            response.read()
            connection.close()
            return status
        wav = b'0' * 44
        self.assertEqual(upload(wav, {'Content-Type': 'audio/wav'}), 403)
        headers = dict(self.authorized(), **{'Content-Type': 'audio/wav'})
        self.assertEqual(upload(wav, dict(headers, Origin='https://evil.invalid')), 403)
        self.assertEqual(upload(wav, dict(headers, **{'Content-Type': 'application/json'})), 415)
        self.assertEqual(upload(b'', headers), 413)
        self.assertEqual(upload(wav, headers), 200)
        self.server.speech_service.transcribe.assert_called_once_with(wav, 'auto')

    def test_pc_tts_is_authenticated_bounded_and_returns_uncached_audio(self):
        self.server.tts_service = Mock()
        self.server.tts_service.status.return_value = {'ok': True, 'available': True, 'voices': []}
        self.server.tts_service.synthesize.return_value = b'RIFF-test-audio'
        payload = {'text': 'A short test.', 'voice': 'af_heart', 'speed': 1}
        self.assertEqual(self.request('/api/tts/status')[0], 403)
        self.assertEqual(self.request('/api/tts/speak', payload)[0], 403)
        headers = self.authorized()
        self.assertEqual(self.request('/api/tts/status', headers=headers)[0], 200)
        self.assertEqual(self.request('/api/tts/speak', payload, dict(headers, Origin='https://unrelated.invalid'))[0], 403)
        status, response_headers, body = self.request('/api/tts/speak', payload, headers)
        self.assertEqual((status, body), (200, b'RIFF-test-audio'))
        self.assertEqual(response_headers['Content-Type'], 'audio/wav')
        self.assertEqual(response_headers['Cache-Control'], 'no-store')
        self.server.tts_service.synthesize.assert_called_once_with(payload)
        self.server.tts_service.synthesize.reset_mock()
        limit_payload = {'text': 'a' * (24000 - len(json.dumps({'text': ''}).encode('utf-8')))}
        self.assertEqual(len(json.dumps(limit_payload).encode('utf-8')), 24000)
        self.assertEqual(self.request('/api/tts/speak', limit_payload, headers)[0], 200)
        self.server.tts_service.synthesize.assert_called_once_with(limit_payload)
        self.server.tts_service.synthesize.reset_mock()

        # The limit is checked from headers before reading the body. Sending
        # no rejected bytes avoids a Windows reset from unread socket data and
        # also proves that rejection does not wait for an oversized upload.
        connection = http.client.HTTPConnection(self.host, self.port, timeout=2)
        self.addCleanup(connection.close)
        connection.putrequest('POST', '/api/tts/speak')
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.putheader('Content-Type', 'application/json')
        connection.putheader('Content-Length', '24001')
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        self.assertFalse(json.loads(response.read())['ok'])
        self.server.tts_service.synthesize.assert_not_called()

    def test_image_chat_body_fits_route_without_expanding_other_actions(self):
        headers = self.authorized()
        payload = {'model': 'test-model', 'messages': [{'role': 'user', 'content': 'a' * 150000}]}
        self.assertEqual(self.request('/api/lm/chat', payload, headers)[0], 200)
        self.ai.stream_chat.assert_called_once_with(payload)
        self.assertEqual(self.request('/api/pc/action', {'id': 'a' * 5000}, headers)[0], 413)

    def test_private_catalogues_and_actions_require_authentication(self):
        get_paths = ('/api/pc/apps', '/api/pc/state', '/api/hardware', '/api/lm/models',
                     '/api/comfy/status', '/api/desktop/status', '/api/speech/status', '/api/remote/info', '/api/remote/status')
        post_paths = ('/api/pc/action', '/api/pc/favourites', '/api/lm/start', '/api/lm/open',
                      '/api/lm/chat', '/api/lm/load', '/api/remote/approve', '/api/remote/revoke', '/api/desktop/session')
        for path in get_paths:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 403)
        for path in post_paths:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, {})[0], 403)
        self.apps.catalog.assert_not_called()
        self.apps.action.assert_not_called()
        self.ai.models.assert_not_called()
        self.ai.start_lm_api.assert_not_called()
        self.ai.stream_chat.assert_not_called()
        self.bridge.issue_ticket.assert_not_called()
        headers = dict(self.authorized(), Origin='https://unrelated.example')
        self.assertEqual(self.request('/api/pc/apps', headers=headers)[0], 403)
        headers = dict(self.authorized(), Host='unrelated.example')
        self.assertEqual(self.request('/api/pc/apps', headers=headers)[0], 403)

    def test_authenticated_catalogues_project_mock_service_results(self):
        headers = self.authorized()
        with patch('pc_controls.hardware_status', return_value={'ok': True, 'cpu': {'available': False}, 'gpu': {'available': False}}):
            for path in ('/api/pc/apps', '/api/pc/state', '/api/hardware', '/api/lm/models',
                         '/api/comfy/status', '/api/desktop/status'):
                with self.subTest(path=path):
                    status, response_headers, body = self.request(path, headers=headers)
                    self.assertEqual(status, 200)
                    self.assertTrue(json.loads(body)['ok'])
                    self.assertEqual(response_headers['Cache-Control'], 'no-store')
        apps = json.loads(self.request('/api/pc/apps', headers=headers)[2])['apps']
        self.assertEqual(set(apps[0]), {'id', 'name', 'kind', 'running', 'active', 'favourite'})

    def test_known_actions_dispatch_and_raw_paths_commands_unknown_ids_are_rejected(self):
        headers = self.authorized()
        self.assertEqual(self.request('/api/pc/action', {'action': 'open', 'id': APP_ID}, headers)[0], 200)
        self.assertEqual(self.request('/api/pc/favourites', {'ids': [APP_ID]}, headers)[0], 200)
        for payload in ({'action': 'open', 'id': 'C:/Windows/cmd.exe'},
                        {'action': 'open', 'id': 'app_' + '0' * 24},
                        {'action': 'shell', 'id': APP_ID},
                        {'action': 'open', 'id': APP_ID, 'arguments': '& calc'}):
            with self.subTest(payload=payload):
                self.assertEqual(self.request('/api/pc/action', payload, headers)[0], 400)
        self.assertEqual(self.request('/api/desktop/session', {'host': 'unrelated.example'}, headers)[0], 400)
        self.bridge.issue_ticket.assert_not_called()
        self.assertEqual(self.request('/api/lm/start', {'model': 'unapproved-model'}, headers)[0], 400)
        self.ai.start_lm_api.assert_not_called()
        self.assertEqual(self.request('/api/not-a-route', {}, headers)[0], 404)

    def test_streaming_chat_preserves_sse_and_closes_stream(self):
        payload = {'model': 'test-model', 'messages': [{'role': 'user', 'content': 'Test only'}]}
        status, headers, body = self.request('/api/lm/chat', payload, self.authorized())
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/event-stream'))
        self.assertIn(b'event: token\n', body)
        self.assertIn(b'event: done\n', body)
        self.assertTrue(self.stream.closed.wait(1))
        self.ai.stream_chat.assert_called_once_with(payload)

    def test_initial_stream_errors_and_empty_streams_release_resources(self):
        for stream, expected in ((TrackingStream(failure=ValueError('Invalid test model.')), 400),
                                 (TrackingStream(), 503)):
            with self.subTest(status=expected):
                self.ai.stream_chat.return_value = stream
                self.assertEqual(self.request('/api/lm/chat', {}, self.authorized())[0], expected)
                self.assertTrue(stream.closed.wait(1))

    def test_client_disconnect_closes_chat_generator(self):
        stream = TrackingStream(['event: token\ndata: {}\n\n'])
        handler = object.__new__(QuietHandler)
        handler.server = SimpleNamespace(ai_service=Mock(stream_chat=Mock(return_value=stream)))
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.wfile.write.side_effect = BrokenPipeError('Test disconnect')
        handler.stream_chat({})
        self.assertTrue(stream.closed.is_set())

    def test_tcp_abort_before_headers_cancels_and_closes_blocked_chat(self):
        class BlockedService:
            def __init__(self):
                self.entered = threading.Event()
                self.closed = threading.Event()
                self.observed_cancellation = threading.Event()
                self.cancellation = None

            def stream_chat_cancellable(self, payload, cancellation):
                # Declare this method on the real class: the handler must use
                # its cancellable-service branch, not a Mock-created attribute.
                self.cancellation = cancellation

                def blocked_generator():
                    self.entered.set()
                    try:
                        if not cancellation.wait(3):
                            raise RuntimeError('Test cancellation deadline exceeded.')
                        self.observed_cancellation.set()
                        return
                        yield 'unreachable test event'
                    finally:
                        self.closed.set()

                return blocked_generator()

            def stream_chat(self, payload):
                raise AssertionError('The cancellable service method was not used.')

        authorization = self.authorized()
        service = BlockedService()
        self.server.ai_service = service
        connection = socket.create_connection((self.host, self.port), timeout=2)
        self.addCleanup(connection.close)
        body = b'{"model":"test-model"}'
        request = (f'POST /api/lm/chat HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n'
                   f'X-MAIC-Control: {authorization["X-MAIC-Control"]}\r\n'
                   f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode('ascii') + body
        # Inspect real network-response writes rather than suppressing the
        # handler's higher-level JSON/SSE paths. This server is test-only.
        with patch.object(socketserver._SocketWriter, 'write', autospec=True) as write:
            try:
                connection.sendall(request)
                self.assertTrue(service.entered.wait(1), 'The request did not reach the blocked iterator.')
                self.assertIsNotNone(service.cancellation)
                self.assertFalse(service.cancellation.is_set())
                write.assert_not_called()
                started = time.monotonic()
                connection.shutdown(socket.SHUT_RDWR)
                connection.close()
                self.assertTrue(service.closed.wait(1), 'The aborted request retained its blocked generator.')
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(service.cancellation.is_set())
                self.assertTrue(service.observed_cancellation.is_set())
                write.assert_not_called()
            finally:
                connection.close()
                if service.cancellation is not None:
                    service.cancellation.set()

    def test_desktop_ticket_is_httponly_cookie_and_absent_from_json(self):
        status, headers, body = self.request('/api/desktop/session', {}, self.authorized())
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie']
        self.assertIn('MAICDesktop=fabricated-test-ticket;', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertIn('Path=/desktop;', cookie)
        self.assertIn('Max-Age=60;', cookie)
        self.assertNotIn(b'fabricated-test-ticket', body)
        data = json.loads(body)
        self.assertNotIn('ticket', data)
        self.assertEqual(data['port'], self.bridge.port)
        self.bridge.issue_ticket.assert_called_once_with(self.host)

    def test_approved_output_range_is_preserved_and_unapproved_clients_rejected(self):
        authorization = self.authorized()
        self.assertEqual(self.request('/api/comfy/output/' + OUTPUT_ID)[0], 403)
        self.ai.output.assert_not_called()
        status, headers, body = self.request('/api/comfy/output/' + OUTPUT_ID,
                                             headers=dict(authorization, Range='bytes=1-3'))
        self.assertEqual(status, 206)
        self.assertEqual(body, b'123')
        self.assertEqual(headers['Content-Range'], 'bytes 1-3/6')
        self.assertEqual(headers['Accept-Ranges'], 'bytes')
        self.assertEqual(headers['Content-Length'], '3')
        self.assertEqual(headers['Content-Type'], 'video/mp4')
        self.ai.output.assert_called_once_with(OUTPUT_ID, range_header='bytes=1-3')
        self.ai.output.reset_mock()
        with patch.object(QuietHandler, 'control_allowed', return_value=False):
            self.assertEqual(self.request('/api/comfy/output/' + OUTPUT_ID, headers=authorization)[0], 403)
        self.ai.output.assert_not_called()
        for identifier in ('unknown', '..%2Fprivate.js', 'C:%5CWindows%5Cprivate.png'):
            self.assertEqual(self.request('/api/comfy/output/' + identifier, headers=authorization)[0], 404)

    def test_media_cookie_is_private_ip_bound_renewable_and_expiring(self):
        status, headers, body = self.request('/api/control-session')
        self.assertEqual(status, 200)
        cookies = SimpleCookie()
        cookies.load(headers['Set-Cookie'])
        media = cookies['MAICMedia']
        self.assertTrue(media['httponly'])
        self.assertEqual(media['samesite'], 'Strict')
        self.assertEqual(media['path'], '/api/comfy')
        self.assertEqual(media['max-age'], '3600')
        self.assertTrue(media.value not in body.decode('utf-8'), 'The media cookie appeared in JSON.')
        authorization = {'X-MAIC-Control': json.loads(body)['token']}
        cookie_header = {'Cookie': 'MAICMedia=' + media.value}

        status, _, preview = self.request('/api/comfy/output/' + OUTPUT_ID, headers=cookie_header)
        self.assertEqual(status, 200)
        self.assertEqual(preview, b'test-image-data')
        # The cookie authorizes media only; catalogue access still needs the
        # authenticated control header, which also renews the media cookie.
        self.assertEqual(self.request('/api/comfy/status', headers=cookie_header)[0], 403)
        status, renewed_headers, _ = self.request('/api/comfy/status', headers=authorization)
        self.assertEqual(status, 200)
        renewed = SimpleCookie()
        renewed.load(renewed_headers['Set-Cookie'])
        self.assertTrue(renewed['MAICMedia'].value == media.value, 'Renewal changed the media session unexpectedly.')

        handler = object.__new__(QuietHandler)
        handler.server = self.server
        handler.client_address = ('127.0.0.2', 12345)
        handler.headers = cookie_header
        handler.control_allowed = Mock(return_value=True)
        self.assertFalse(handler.media_authenticated(), 'A second approved IP reused another device media cookie.')

        self.ai.output.reset_mock()
        self.assertEqual(self.request('/api/comfy/output/' + OUTPUT_ID,
                                      headers={'Cookie': 'MAICMedia=invalid-test-cookie'})[0], 403)
        with self.server.control_lock:
            token, _ = self.server.media_sessions[self.host]
            self.server.media_sessions[self.host] = (token, time.monotonic() - 1)
        self.assertEqual(self.request('/api/comfy/output/' + OUTPUT_ID, headers=cookie_header)[0], 403)
        self.ai.output.assert_not_called()

    def test_dashboard_refuses_cross_origin_framing_but_allows_own_desktop(self):
        status, headers, _ = self.request('/')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Security-Policy'), "frame-ancestors 'self'")
        self.assertEqual(headers.get('X-Frame-Options'), 'SAMEORIGIN')

    def test_static_vendor_files_cannot_escape_web_root(self):
        status, _, body = self.request('/desktop/core/rfb.js')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'export default {};')
        for path in ('/desktop/../../../private.js',
                     '/desktop/%2e%2e/%2e%2e/%2e%2e/private.js',
                     '/desktop/%2e%2e%5c%2e%2e%5c%2e%2e%5cprivate.js'):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b'private-test-marker', body)


if __name__ == '__main__':
    unittest.main()
