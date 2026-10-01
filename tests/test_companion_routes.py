"""Companion route authorization, filters and media; no live agents or PC actions."""
from http.cookies import SimpleCookie
import json
import unittest
from unittest.mock import Mock, patch

import agent_team
from comfy_library import ComfyLibrary
import maic_server
from tests import test_remote_access as gateway_fixture
from tests import test_upgrade_api as dashboard_fixture


OUTPUT = 'a' * 32
RECEIPT = 'r' * 32
SEND = {'agentId': 'main', 'text': 'An isolated test'}
PRIVATE_GETS = ('/api/agents/status', '/api/comfy/library')
PRIVATE_POSTS = (('/api/agents/send', SEND), ('/api/agents/receipt', {'receiptId': RECEIPT}),
                 ('/api/comfy/library/open', {}))


class CompanionRouteTests(unittest.TestCase):
    # Reuse the existing isolated server lifecycle without inheriting its tests.
    setUpClass = classmethod(dashboard_fixture.UpgradeAPITests.setUpClass.__func__)
    tearDownClass = classmethod(dashboard_fixture.UpgradeAPITests.tearDownClass.__func__)
    request = dashboard_fixture.UpgradeAPITests.request
    authorized = dashboard_fixture.UpgradeAPITests.authorized

    def setUp(self):
        dashboard_fixture.UpgradeAPITests.setUp(self)
        self.team = Mock()
        self.team.status.return_value = {'ok': True, 'available': True, 'agents': []}
        self.team.send.return_value = {'ok': True, 'receiptId': RECEIPT, 'agentId': 'main', 'status': 'queued'}
        self.team.receipt.return_value = {'ok': True, 'receiptId': RECEIPT, 'status': 'completed', 'reply': 'Test reply'}
        self.library = Mock()
        self.library.list_outputs.side_effect = ComfyLibrary(self.root).list_outputs
        self.library.open_folder.return_value = {'ok': True, 'message': 'Test folder request'}
        self.reference = {'filename': 'fixture.mp4', 'subfolder': '', 'type': 'output'}
        self.library.reference.return_value = self.reference
        self.ai.output_reference.return_value = ('video/mp4', b'123', {'status': 206, 'headers': {
            'Content-Range': 'bytes 1-3/6', 'Accept-Ranges': 'bytes'}})
        self.server.agent_team = self.team
        self.server.comfy_library = self.library

    def test_all_companion_routes_require_control_authorization(self):
        for path in PRIVATE_GETS + ('/api/comfy/library/output/' + OUTPUT,):
            self.assertEqual(self.request(path)[0], 403, path)
        for path, body in PRIVATE_POSTS:
            self.assertEqual(self.request(path, body)[0], 403, path)
        self.team.status.assert_not_called()
        self.team.send.assert_not_called()
        self.team.receipt.assert_not_called()
        self.library.list_outputs.assert_not_called()
        self.library.reference.assert_not_called()
        self.library.open_folder.assert_not_called()

    def test_approved_routes_dispatch_exact_payloads(self):
        headers = self.authorized()
        for path in PRIVATE_GETS:
            status, response_headers, body = self.request(path, headers=headers)
            self.assertEqual(status, 200, path)
            self.assertTrue(json.loads(body)['ok'])
            self.assertEqual(response_headers['Cache-Control'], 'no-store')
        for path, payload in PRIVATE_POSTS:
            self.assertEqual(self.request(path, payload, headers)[0], 200, path)
        self.team.status.assert_called_once_with()
        self.team.send.assert_called_once_with(SEND)
        self.team.receipt.assert_called_once_with({'receiptId': RECEIPT})
        self.library.open_folder.assert_called_once_with({})

    def test_wrong_origin_or_unapproved_address_cannot_use_existing_token(self):
        headers = self.authorized()
        for path in PRIVATE_GETS:
            self.assertEqual(self.request(path, headers=dict(headers, Origin='https://other.invalid'))[0], 403)
        for path, payload in PRIVATE_POSTS:
            self.assertEqual(self.request(path, payload, dict(headers, Origin='https://other.invalid'))[0], 403)
        with patch.object(maic_server.Handler, 'control_allowed', return_value=False):
            for path in PRIVATE_GETS:
                self.assertEqual(self.request(path, headers=headers)[0], 403)
            for path, payload in PRIVATE_POSTS:
                self.assertEqual(self.request(path, payload, headers)[0], 403)
        self.team.status.assert_not_called()
        self.team.send.assert_not_called()
        self.team.receipt.assert_not_called()
        self.library.open_folder.assert_not_called()

    def test_library_query_is_decoded_and_bounded(self):
        headers = self.authorized()
        path = '/api/comfy/library?folder=root&search=hello%20world&media=video&offset=24&limit=12'
        status, _, _ = self.request(path, headers=headers)
        self.assertEqual(status, 200)
        self.library.list_outputs.assert_called_once_with(folder='root', search='hello world', media='video', offset=24, limit=12)
        invalid = ('limit=25', 'limit=0', 'offset=-1', 'offset=20001', 'offset=nan',
                   'folder=..%2Fprivate', 'media=all%26private', 'search=' + 'x' * 121,
                   'search=%00', 'limit=1&limit=2', 'path=C%3A%5CUsers%5Cprivate')
        for query in invalid:
            with self.subTest(query=query):
                status, _, body = self.request('/api/comfy/library?' + query, headers=headers)
                self.assertEqual(status, 400)
                self.assertFalse(json.loads(body)['ok'])
                self.assertNotIn(b'C:\\Users', body)

    def test_oversized_or_structurally_invalid_agent_sends_are_rejected(self):
        headers = self.authorized()
        self.assertEqual(self.request('/api/agents/send', {'agentId': 'main', 'text': 'x' * 40001}, headers)[0], 413)
        self.team.send.assert_not_called()
        # Use the real validator, which rejects before touching configuration/network.
        self.server.agent_team = agent_team.AgentTeam(self.root)
        self.server.agent_team._opener = Mock()
        for body in ({'agentId': 'main', 'text': 'x' * 6001}, dict(SEND, notify=True),
                     dict(SEND, sender='hermes'), dict(SEND, path='C:\\private'),
                     {'agentId': 'unknown', 'text': 'Hello'}):
            self.assertEqual(self.request('/api/agents/send', body, headers)[0], 400)
        self.server.agent_team._opener.open.assert_not_called()

    def test_library_media_cookie_is_scoped_and_preserves_ranges(self):
        headers = self.authorized()
        status, response_headers, _ = self.request('/api/comfy/library', headers=headers)
        self.assertEqual(status, 200)
        parsed = SimpleCookie(response_headers['Set-Cookie'])
        media_cookie = parsed['MAICMedia']
        self.assertTrue(media_cookie['httponly'])
        self.assertEqual(media_cookie['samesite'], 'Strict')
        self.assertEqual(media_cookie['path'], '/api/comfy')
        media_headers = {'Cookie': 'MAICMedia=' + media_cookie.value, 'Range': 'bytes=1-3'}
        path = '/api/comfy/library/output/' + OUTPUT
        status, response_headers, body = self.request(path, headers=media_headers)
        self.assertEqual((status, body), (206, b'123'))
        self.assertEqual(response_headers['Content-Range'], 'bytes 1-3/6')
        self.library.reference.assert_called_once_with(OUTPUT)
        self.ai.output_reference.assert_called_once_with(self.reference, 'bytes=1-3')
        # A media cookie never authorizes agent calls or output-folder actions.
        self.assertEqual(self.request('/api/agents/status', headers=media_headers)[0], 403)
        self.assertEqual(self.request('/api/agents/send', SEND, media_headers)[0], 403)
        self.assertEqual(self.request('/api/comfy/library/open', {}, media_headers)[0], 403)
        self.assertEqual(self.request(path, headers=dict(media_headers, Origin='https://other.invalid'))[0], 403)
        self.assertEqual(self.request(path, headers={'Cookie': 'MAICMedia=unknown'})[0], 403)

    def test_missing_media_and_service_errors_do_not_leak_paths(self):
        headers = self.authorized()
        self.library.reference.side_effect = OSError('C:\\private\\token.json secret')
        status, _, body = self.request('/api/comfy/library/output/' + OUTPUT, headers=headers)
        self.assertEqual(status, 404)
        self.assertNotIn(b'private', body)
        self.team.status.side_effect = OSError('C:\\private\\hub.token')
        status, _, body = self.request('/api/agents/status', headers=headers)
        self.assertEqual(status, 503)
        self.assertNotIn(b'hub.token', body)


class CompanionGatewayTests(unittest.TestCase):
    setUpClass = classmethod(gateway_fixture.GatewayTests.setUpClass.__func__)
    tearDownClass = classmethod(gateway_fixture.GatewayTests.tearDownClass.__func__)
    setUp = gateway_fixture.GatewayTests.setUp
    tearDown = gateway_fixture.GatewayTests.tearDown
    request = gateway_fixture.GatewayTests.request
    pair = gateway_fixture.GatewayTests.pair
    approved = gateway_fixture.GatewayTests.approved

    def test_remote_anonymous_and_pending_browsers_cannot_access_companion(self):
        for cookie in (None, self.pair()):
            for path in PRIVATE_GETS + ('/api/comfy/library/output/' + OUTPUT,):
                self.assertEqual(self.request('GET', path, cookie=cookie)[0], 403, path)
            for path, payload in PRIVATE_POSTS:
                self.assertEqual(self.request('POST', path, payload, cookie=cookie)[0], 403, path)
        self.assertEqual(len(self.upstream.seen), self.start_seen)

    def test_remote_approved_routes_keep_query_payload_and_range(self):
        cookie = self.approved()
        query = '/api/comfy/library?folder=root&search=hello%20world&media=video&offset=24&limit=12'
        for path in ('/api/agents/status', query, '/api/comfy/library/output/' + OUTPUT):
            self.assertEqual(self.request('GET', path, cookie=cookie, headers={'Range': 'bytes=1-3'})[0], 200)
            self.assertEqual(self.upstream.seen[-1][1], path)
            self.assertEqual(self.upstream.seen[-1][2]['Range'], 'bytes=1-3')
        for path, payload in PRIVATE_POSTS:
            self.assertEqual(self.request('POST', path, payload, cookie=cookie)[0], 200)
            self.assertEqual(self.upstream.seen[-1][1], path)
            self.assertEqual(json.loads(self.upstream.seen[-1][3]), payload)
        _, headers, _ = self.request('GET', '/api/comfy/library', cookie=cookie)
        self.assertIn('Secure', next(value for key, value in headers if key.lower() == 'set-cookie'))

    def test_remote_wrong_origin_and_unlisted_routes_remain_blocked(self):
        cookie = self.approved()
        start = len(self.upstream.seen)
        for path in PRIVATE_GETS:
            self.assertEqual(self.request('GET', path, cookie=cookie, origin='https://other.invalid')[0], 403)
        for path, payload in PRIVATE_POSTS:
            self.assertEqual(self.request('POST', path, payload, cookie=cookie, origin='https://other.invalid')[0], 403)
        for path, expected in (('/api/agents/raw', 404), ('/api/comfy/library/output/../private', 400),
                               ('/api/comfy/library/open/anything', 404)):
            self.assertEqual(self.request('GET', path, cookie=cookie)[0], expected)
        self.assertEqual(len(self.upstream.seen), start)

    def test_remote_library_query_exception_stays_narrow_and_requests_bounded(self):
        cookie = self.approved()
        start = len(self.upstream.seen)
        invalid = ('/api/agents/status?search=test', '/api/comfy/library?path=private',
                   '/api/comfy/library?limit=1&limit=2', '/api/comfy/library?search=' + 'x' * 129,
                   '/api/comfy/library?search=%00', '/api/comfy/library?limit=1&limit=')
        for path in invalid:
            self.assertEqual(self.request('GET', path, cookie=cookie)[0], 400, path)
        self.assertEqual(self.request('POST', '/api/comfy/library?search=test', {}, cookie=cookie)[0], 400)
        self.assertEqual(self.request('POST', '/api/agents/send', b'', cookie=cookie,
                                     headers={'Content-Type': 'application/json', 'Content-Length': str(2 * 1024 * 1024 + 1)})[0], 413)
        self.assertEqual(len(self.upstream.seen), start)


if __name__ == '__main__':
    unittest.main()
