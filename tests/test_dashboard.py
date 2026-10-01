"""Network contract checks; isolated app data and no desktop launches."""
import http.client
import ipaddress
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import maic_server
import pc_controls


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_root = maic_server.ROOT
        runtime = (cls.original_root / '.runtime').resolve()
        runtime.mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='dashboard-test-', dir=runtime)
        cls.test_root = Path(cls.temporary.name).resolve()
        assert cls.test_root.parent == runtime
        maic_server.ROOT = cls.test_root
        (cls.test_root / 'apps.json').write_text('{"apps": []}', encoding='utf-8')
        cls.server = maic_server.ThreadingHTTPServer(('127.0.0.1', 0), maic_server.Handler)
        cls.host, cls.port = cls.server.server_address
        cls.server.allowed_network = ipaddress.ip_network('127.0.0.0/8')
        cls.server.origin = f'http://{cls.host}:{cls.port}'
        cls.server.expected_host = f'{cls.host}:{cls.port}'
        cls.server.tap_lock = threading.Lock()
        cls.server.taps = 0
        cls.server.control_lock = threading.RLock()
        cls.server.control_sessions = {}
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=3)
        maic_server.ROOT = cls.original_root
        # Verify the recursive cleanup remains inside this workspace's runtime.
        assert Path(cls.temporary.name).resolve().parent == (cls.original_root / '.runtime').resolve()
        cls.temporary.cleanup()

    def request(self, path, payload=None, headers=None, raw=None):
        request_headers = dict(headers or {})
        method, body = 'GET', None
        if payload is not None or raw is not None:
            method = 'POST'
            body = raw if raw is not None else json.dumps(payload)
            request_headers.setdefault('Content-Type', 'application/json')
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        status, data = response.status, json.loads(response.read())
        connection.close()
        return status, data

    def authorization(self):
        status, data = self.request('/api/control-session')
        self.assertEqual(status, 200)
        self.assertIsInstance(data.get('token'), str)
        return {'X-MAIC-Control': data['token']}

    def test_health_and_status(self):
        self.assertTrue(self.request('/api/health')[1]['ok'])
        status, data = self.request('/api/status')
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data['uptimeSeconds'], 0)
        if data['memory']:
            self.assertGreater(data['memory']['totalMB'], 0)

    def test_calculator_contract(self):
        for operation, expected in [('add', 15), ('subtract', 9), ('multiply', 36), ('divide', 4)]:
            status, data = self.request('/api/calculate', {'left': 12, 'right': 3, 'operation': operation})
            self.assertEqual(status, 200)
            self.assertEqual(data['result'], expected)
        self.assertEqual(self.request('/api/calculate', {'left': 12, 'right': 0, 'operation': 'divide'})[0], 400)
        self.assertEqual(self.request('/api/calculate', {'left': True, 'right': 3, 'operation': 'add'})[0], 400)
        self.assertEqual(self.request('/api/calculate', {'left': 1e101, 'right': 3, 'operation': 'add'})[0], 400)

    def test_control_requires_session_and_same_origin(self):
        action = {'action': 'open-app', 'value': 'calculator'}
        with patch.object(pc_controls.subprocess, 'Popen') as launch:
            self.assertEqual(self.request('/api/control', action)[0], 403)
            self.assertEqual(self.request('/api/control', action, {'X-MAIC-Control': 'wrong'})[0], 403)
            headers = dict(self.authorization(), Origin='https://unrelated.example')
            self.assertEqual(self.request('/api/control', action, headers)[0], 403)
            launch.assert_not_called()
        self.assertEqual(self.request('/api/control-session', headers={'Host': 'unrelated.example'})[0], 403)
        self.assertEqual(self.request('/api/control-session', headers={'Origin': 'https://unrelated.example'})[0], 403)
        with patch.object(maic_server.Handler, 'control_allowed', return_value=False):
            self.assertEqual(self.request('/api/control-session')[0], 403)

    def test_only_fixed_desktop_apps_are_allowed(self):
        headers = self.authorization()
        with patch.object(pc_controls.subprocess, 'Popen') as launch:
            self.assertEqual(self.request('/api/control', {'action': 'open-app', 'value': 'calculator'}, headers)[0], 200)
            arguments = launch.call_args.args[0]
            self.assertEqual(Path(arguments[0]).name, 'calc.exe')
            self.assertFalse(launch.call_args.kwargs['shell'])
            launch.reset_mock()
            self.assertEqual(self.request('/api/control', {'action': 'open-app', 'value': 'powershell.exe'}, headers)[0], 400)
            launch.assert_not_called()

    def test_null_and_malformed_json_receive_response(self):
        headers = self.authorization()
        for raw in ('null', '[]', '{broken'):
            self.assertEqual(self.request('/api/control', headers=headers, raw=raw)[0], 400)
        self.assertEqual(self.request('/api/control', headers=headers, raw='x' * 4097)[0], 413)

    def test_audio_read_and_unchanged_volume_write(self):
        status, state = self.request('/api/audio')
        self.assertEqual(status, 200)
        if state['speaker']['available']:
            original = state['speaker']['volume']
            status, data = self.request('/api/control', {'action': 'speaker-volume', 'value': original}, self.authorization())
            self.assertEqual(status, 200)
            self.assertAlmostEqual(data['state']['speaker']['volume'], original, places=4)

    def test_bookmarks_are_saved_without_executable_schemes(self):
        headers = self.authorization()
        self.assertEqual(self.request('/api/bookmarks', {'name': 'Reference', 'url': 'https://example.com/'}, headers)[0], 200)
        self.assertEqual(self.request('/api/bookmarks', {'name': 'Updated reference', 'url': 'https://example.com/'}, headers)[0], 200)
        for address in ('javascript:alert(1)', 'file:///C:/Windows', 'https://user:password@example.com', 'http://example.com:99999'):
            self.assertEqual(self.request('/api/bookmarks', {'name': 'Rejected', 'url': address}, headers)[0], 400)
        status, data = self.request('/api/apps')
        self.assertEqual(status, 200)
        self.assertEqual(len(data['apps']), 1)
        self.assertEqual(data['apps'][0]['name'], 'Updated reference')


if __name__ == '__main__':
    unittest.main()
