"""Team Hub isolation and receipt ownership; never dispatches live agent work."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import agent_team as team


class Response:
    def __init__(self, value, status=200):
        self.raw = io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.status = status

    def getcode(self):
        return self.status

    def read(self, size):
        return self.raw.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.raw.close()


def state(messages=None):
    return {'agents': [{'id': ident, 'name': name, 'framework': framework, 'role': 'Configured role',
                        'status': 'active', 'workspace': '/home/private', 'teamctl': 'private command'}
                       for ident, (name, framework) in team.AGENTS.items()],
            'model': {'id': 'selected-model', 'status': 'loaded', 'context_length': 33024},
            'messages': messages or [], 'environment': {'checked_at': '2026-10-01T01:00:00Z', 'private': 'data'},
            'facts': [{'value': 'SECRET FACT'}], 'events': [{'text': 'SECRET EVENT'}]}


def message(ident=1, **fields):
    return dict({'id': ident, 'task_id': 10, 'root_id': 1, 'parent_id': None,
                 'sender': 'besian', 'recipient': 'main', 'kind': 'request',
                 'body': 'Hello', 'status': 'completed', 'response': 'Hello Besian',
                 'error': 'private error path C:\\secret\\data'}, **fields)


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '.runtime').mkdir()
        self.token = 'private_team_credential_1234567890'
        self.token_path = self.root / 'hub.token'
        self.token_path.write_text(self.token)
        self.configure()
        self.client = team.AgentTeam(self.root)
        self.client._opener = Mock()

    def configure(self, **override):
        config = {'enabled': True, 'tokenFile': str(self.token_path)}
        config.update(override)
        (self.root / '.runtime' / 'agent-team.json').write_text(json.dumps(config))

    def respond(self, data):
        self.client._opener.open.return_value = Response(data)

    def own_receipt(self):
        self.client._opener.open.side_effect = [Response(state()), Response({'id': 1, 'task_id': 10, 'status': 'queued'}, 201)]
        receipt = self.client.send({'agentId': 'main', 'text': 'Hello'})
        self.client._opener.open.side_effect = None
        return receipt['receiptId']

    def test_status_projects_only_safe_fields_and_caches(self):
        data = state([message(sender='rios', recipient='main', status='running', body='PRIVATE BODY')])
        self.respond(data)
        first = self.client.status()
        second = self.client.status()
        self.assertEqual(first, second)
        self.assertEqual(self.client._opener.open.call_count, 1)
        self.assertEqual(first['agents'][1]['activity'], 'working')
        self.assertTrue(first['agents'][1]['canSend'])
        encoded = json.dumps(first)
        for private in ('SECRET', 'PRIVATE BODY', 'workspace', 'teamctl', '/home/private', self.token):
            self.assertNotIn(private, encoded)
        request = self.client._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, team.BASE + '/api/state')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.token)
        self.assertEqual(self.client._opener.open.call_args.kwargs['timeout'], 5)

    def test_cache_expires(self):
        self.client._opener.open.side_effect = [Response(state()), Response(state())]
        with patch.object(team.time, 'monotonic', side_effect=[100, 101, 104, 104]):
            self.client.status()
            self.client.status()
            self.client.status()
        self.assertEqual(self.client._opener.open.call_count, 2)

    def test_setup_pending_and_disabled_do_not_connect(self):
        for mode in ('missing', 'disabled', 'missing_token', 'relative', 'invalid_json', 'extra_url'):
            with self.subTest(mode=mode):
                self.configure()
                config = self.root / '.runtime' / 'agent-team.json'
                if mode == 'missing': config.unlink()
                elif mode == 'disabled': self.configure(enabled=False)
                elif mode == 'missing_token': self.configure(tokenFile=str(self.root / 'missing'))
                elif mode == 'relative': self.configure(tokenFile='relative.token')
                elif mode == 'invalid_json': config.write_text('broken')
                elif mode == 'extra_url': self.configure(url='https://external.invalid/')
                client = team.AgentTeam(self.root)
                client._opener = Mock()
                result = client.status()
                self.assertFalse(result['available'])
                self.assertEqual(len(result['agents']), 4)
                self.assertFalse(any(a['canSend'] for a in result['agents']))
                client._opener.open.assert_not_called()
                self.assertNotIn(str(self.root), json.dumps(result))

    def test_offline_and_authentication_have_truthful_generic_messages(self):
        for error, expected in [(URLError('private.host'), 'offline'),
                                (HTTPError(team.BASE, 401, self.token, {}, None), 'authentication_required')]:
            self.client._cached = None
            self.client._opener.open.side_effect = error
            result = self.client.status()
            self.assertEqual(result['state'], expected)
            self.assertNotIn(self.token, json.dumps(result))
            self.assertNotIn('private.host', json.dumps(result))

    @unittest.skipUnless(os.name == 'nt', 'WSL UNC paths are Windows-specific')
    def test_offline_wsl_is_not_started_by_reading_its_token(self):
        self.configure(tokenFile=r'\\wsl.localhost\NoSuchDistribution\home\private\hub.token')
        self.client._opener.open.side_effect = URLError('offline')
        result = self.client.status()
        self.assertEqual(result['state'], 'offline')
        request = self.client._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, team.BASE + '/health')
        self.assertIsNone(request.get_header('Authorization'))

    def test_response_size_and_shape_are_bounded(self):
        for raw in (b'x' * (team.MAX_STATE_BYTES + 1), b'[]', b'{"agents":[],"messages":"bad"}', b'not json'):
            self.client._cached = None
            self.respond(raw)
            self.assertEqual(self.client.status()['state'], 'invalid_response')

    def test_no_model_or_gateway_means_no_send(self):
        for model_state, service in [('offline', 'active'), ('loaded', 'inactive')]:
            data = state()
            data['model']['status'] = model_state
            data['agents'][1]['status'] = service
            self.respond(data)
            result = self.client.send({'agentId': 'main', 'text': 'Hello'})
            self.assertFalse(result['ok'])
        self.assertEqual(self.client._opener.open.call_count, 2)

    def test_send_strict_fields_and_live_catalog(self):
        invalid = [None, {'agentId': 'main', 'text': 'Hello', 'notify': True},
                   {'agentId': 'main', 'text': 'Hello', 'sender': 'hermes'},
                   {'agentId': 'main', 'text': 'Hello', 'parent_id': 1},
                   {'agentId': 'external', 'text': 'Hello'}, {'agentId': [], 'text': 'Hello'},
                   {'agentId': 'main', 'text': ''}, {'agentId': 'main', 'text': 'x' * 6001},
                   {'agentId': 'main', 'text': 'control\x00'}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError): self.client.send(payload)
        self.client._opener.open.assert_not_called()
        data = state()
        data['agents'] = [data['agents'][0]]
        self.respond(data)
        self.assertFalse(self.client.send({'agentId': 'main', 'text': 'Hello'})['ok'])

    def test_send_forces_sender_and_disables_telegram(self):
        receipt = self.own_receipt()
        self.assertEqual(len(receipt), 32)
        request = self.client._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, team.BASE + '/api/messages')
        self.assertEqual(json.loads(request.data), {'sender': 'besian', 'recipient': 'main',
                                                  'body': 'Hello', 'dispatch': True, 'notify': False})
        self.assertEqual(len(self.client._receipts), 1)
        self.assertNotIn('Hello', repr(self.client._receipts))

    def test_uncertain_post_is_never_retried(self):
        self.client._opener.open.side_effect = [Response(state()), URLError('timeout')]
        result = self.client.send({'agentId': 'main', 'text': 'Hello'})
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(self.client._opener.open.call_count, 2)
        self.assertEqual(self.client._receipts, {})

    def test_receipt_never_exposes_unrelated_history_or_errors(self):
        receipt_id = self.own_receipt()
        self.respond(state([message(), message(99, body='Unrelated', response='PRIVATE CHAT', root_id=99)]))
        result = self.client.receipt({'receiptId': receipt_id})
        self.assertTrue(result['complete'])
        self.assertEqual(result['reply'], 'Hello Besian')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('secret', json.dumps(result))

    def test_receipt_owns_entire_validated_handoff_but_not_injected_links(self):
        receipt_id = self.own_receipt()
        child = message(2, sender='main', recipient='hermes', parent_id=1, body='Delegated', response='Specialist')
        reply = message(3, sender='hermes', recipient='main', parent_id=2, kind='result', response='Final combined answer')
        injected = message(4, sender='rios', recipient='main', parent_id=3, kind='result', response='PRIVATE INJECTION')
        self.respond(state([injected, reply, child, message()]))
        result = self.client.receipt({'receiptId': receipt_id})
        self.assertEqual(result['reply'], 'Final combined answer')
        self.assertTrue(result['complete'])

    def test_pending_child_keeps_receipt_incomplete(self):
        receipt_id = self.own_receipt()
        child = message(2, sender='main', recipient='hermes', parent_id=1, body='Delegated', status='queued')
        self.respond(state([message(), child]))
        result = self.client.receipt({'receiptId': receipt_id})
        self.assertEqual(result['status'], 'queued')
        self.assertFalse(result['complete'])

    def test_receipt_collision_or_forgery_rejected(self):
        receipt_id = self.own_receipt()
        for change in ({'body': 'Other private conversation'}, {'sender': 'hermes'},
                       {'recipient': 'rios'}, {'task_id': 23}, {'root_id': 55}, {'parent_id': 9}):
            self.client._cached = None
            self.respond(state([message(**change)]))
            result = self.client.receipt({'receiptId': receipt_id})
            self.assertFalse(result['ok'])
            self.assertNotIn('reply', result)
        result = self.client.receipt({'receiptId': 'x' * 32})
        self.assertEqual(result['status'], 'unknown')
        with self.assertRaises(ValueError): self.client.receipt({'receiptId': receipt_id, 'id': 99})

    def test_receipts_expire_and_are_not_persisted(self):
        receipt_id = self.own_receipt()
        self.client._receipts[receipt_id]['created'] -= team.RECEIPT_SECONDS + 1
        self.assertEqual(self.client.receipt({'receiptId': receipt_id})['status'], 'unknown')
        self.assertEqual(list((self.root / '.runtime').iterdir()), [self.root / '.runtime' / 'agent-team.json'])

    def test_reply_scrubs_credentials_and_local_paths(self):
        receipt_id = self.own_receipt()
        reply = 'Done ' + self.token + ' Bearer abcdefghijklmnop C:\\Users\\private\\token.txt /home/private/key api_key=secret123'
        self.respond(state([message(response=reply)]))
        result = self.client.receipt({'receiptId': receipt_id})
        for private in (self.token, 'abcdefghijklmnop', 'C:\\Users', '/home/private', 'secret123'):
            self.assertNotIn(private, result['reply'])

    def test_redirects_and_alternate_routes_are_rejected(self):
        with self.assertRaises(HTTPError):
            team.NoRedirect().redirect_request(Mock(full_url=team.BASE), None, 302, '', {}, 'https://outside.invalid')
        with self.assertRaises(ValueError): self.client._request('/api/facts', {})
        self.client._opener.open.assert_not_called()

    def test_malformed_optional_fields_do_not_escape(self):
        data = state()
        data['agents'].extend([{'id': []}, None])
        data['agents'][0]['status'] = []
        data['model']['status'] = {}
        self.respond(data)
        result = self.client.status()
        self.assertTrue(result['available'])
        self.assertEqual(result['agents'][0]['status'], 'unknown')
        self.assertFalse(result['agents'][0]['canSend'])


if __name__ == '__main__':
    unittest.main()
