"""Opt-in loopback Team Hub adapter; no gateway starts or Telegram delivery."""
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


BASE = 'http://127.0.0.1:18800'
AGENTS = {'hermes': ('Hermes', 'Hermes'), 'main': ('RION', 'OpenClaw'),
          'rion_local': ('RION_LOCAL', 'OpenClaw'), 'rios': ('Rios', 'OpenClaw')}
MAX_STATE_BYTES = 4 * 1024 * 1024
CACHE_SECONDS = 3
RECEIPT_SECONDS = 24 * 60 * 60
MESSAGE_STATES = {'queued', 'running', 'completed', 'failed', 'interrupted', 'recorded'}
SERVICE_STATES = {'active', 'inactive', 'failed', 'activating', 'deactivating', 'unknown'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, 'Team Hub redirects are disabled.', headers, fp)


class TeamUnavailable(Exception):
    def __init__(self, state, message, enabled=True):
        self.state, self.message, self.enabled = state, message, enabled


class AgentTeam:
    def __init__(self, root):
        self.root = Path(root)
        self._opener = build_opener(ProxyHandler({}), NoRedirect())
        self._lock = threading.RLock()
        self._cached = None
        self._cached_at = 0
        self._token = ''
        self._receipts = {}

    def _credentials(self):
        path = self.root / '.runtime' / 'agent-team.json'
        try:
            with path.open('rb') as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            config = json.loads(raw)
            if not isinstance(config, dict) or set(config) != {'enabled', 'tokenFile'}:
                raise ValueError()
            if config['enabled'] is False:
                raise TeamUnavailable('disabled', 'Agent team integration is disabled on this PC.', False)
            if config['enabled'] is not True or not isinstance(config['tokenFile'], str):
                raise ValueError()
            token_file = Path(config['tokenFile'])
            if not token_file.is_absolute():
                raise ValueError()
        except FileNotFoundError:
            raise TeamUnavailable('setup_pending', 'Agent team integration has not been configured on this PC.', False)
        except (OSError, ValueError, TypeError):
            raise TeamUnavailable('configuration_error', 'Check the private agent team configuration on this PC.')
        # Browsing a stopped distribution's UNC share can start WSL implicitly.
        # Probe the already-running hub first; never wake it just to read a token.
        if str(token_file).lower().startswith(('\\\\wsl.localhost\\', '\\\\wsl$\\')):
            try:
                with self._opener.open(Request(BASE + '/health', headers={'Accept': 'application/json'}), timeout=3) as response:
                    raw_health = response.read(1025)
                    if response.getcode() != 200 or len(raw_health) > 1024 or json.loads(raw_health).get('status') != 'ok':
                        raise ValueError()
            except (OSError, URLError, ValueError, TypeError, AttributeError):
                raise TeamUnavailable('offline', 'Team Hub is not reachable on this PC.')
        try:
            with token_file.open('rb') as stream:
                raw_token = stream.read(8193)
            if len(raw_token) > 8192:
                raise ValueError()
            token = raw_token.decode('ascii').strip()
            if not re.fullmatch(r'[A-Za-z0-9._~-]{16,512}', token):
                raise ValueError()
        except (OSError, UnicodeError, ValueError):
            raise TeamUnavailable('setup_pending', 'The private Team Hub credential is not available yet.')
        self._token = token
        return token

    def _request(self, path, payload=None):
        if path not in ('/api/state', '/api/messages') or (path == '/api/messages') != (payload is not None):
            raise ValueError('Unsupported Team Hub operation.')
        token = self._credentials()
        request = Request(BASE + path, data=None if payload is None else json.dumps(payload).encode('utf-8'),
                          headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
                                   'Content-Type': 'application/json'}, method='GET' if payload is None else 'POST')
        try:
            with self._opener.open(request, timeout=5) as response:
                if response.getcode() not in (200, 201):
                    raise ValueError()
                raw = response.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError()
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except HTTPError as error:
            if error.code in (401, 403):
                raise TeamUnavailable('authentication_required', 'Team Hub access needs attention on this PC.')
            raise TeamUnavailable('unavailable', 'Team Hub did not accept the request. Check its local dashboard.')
        except (OSError, URLError):
            raise TeamUnavailable('offline', 'Team Hub is not reachable on this PC.')
        except (ValueError, TypeError):
            raise TeamUnavailable('invalid_response', 'Team Hub returned an unsupported response.')

    def _text(self, value, maximum=160):
        if not isinstance(value, str):
            return ''
        if self._token:
            value = value.replace(self._token, '[private credential]')
        value = re.sub(r'\b\d{6,12}:[A-Za-z0-9_-]{20,}\b', '[private credential]', value)
        value = re.sub(r'(?i)\bbearer\s+[A-Za-z0-9._~-]+', '[private credential]', value)
        value = re.sub(r'(?i)\b(?:api[_ -]?key|token|password|secret)\s*[:=]\s*[^\s,;]+', '[private credential]', value)
        value = re.sub(r'(?i)(?:[A-Z]:[\\/]|\\\\|/(?:home|Users|mnt|tmp|var|etc)/)[^\s\n]+', '[private path]', value)
        return ''.join(c for c in value if ord(c) >= 32 or c in '\n\t').strip()[:maximum]

    def _state(self, fresh=False):
        with self._lock:
            if not fresh and self._cached is not None and time.monotonic() - self._cached_at < CACHE_SECONDS:
                if isinstance(self._cached, TeamUnavailable):
                    raise self._cached
                return self._cached
            try:
                data = self._request('/api/state')
                if not isinstance(data.get('agents'), list) or not isinstance(data.get('messages'), list):
                    raise TeamUnavailable('invalid_response', 'Team Hub returned an unsupported response.')
                if len(data['agents']) > 32 or len(data['messages']) > 500:
                    raise TeamUnavailable('invalid_response', 'Team Hub returned an unsupported response.')
                self._cached = data
            except TeamUnavailable as error:
                self._cached = error
                raise
            finally:
                self._cached_at = time.monotonic()
            return data

    @staticmethod
    def _catalog(data):
        found = {}
        for agent in data['agents']:
            if (isinstance(agent, dict) and isinstance(agent.get('id'), str)
                    and agent['id'] in AGENTS and agent['id'] not in found):
                found[agent['id']] = agent
        return found

    @staticmethod
    def _fallback_agents():
        return [{'id': ident, 'name': name, 'framework': framework, 'role': '', 'status': 'unknown',
                 'activity': 'offline', 'canSend': False, 'model': ''}
                for ident, (name, framework) in AGENTS.items()]

    def status(self):
        try:
            data = self._state()
            live = self._catalog(data)
            model = data.get('model') if isinstance(data.get('model'), dict) else {}
            model_state = model.get('status') if model.get('status') in ('loaded', 'offline') else 'unknown'
            model_id = self._text(model.get('id'))
            agents = self._fallback_agents()
            for entry in agents:
                agent = live.get(entry['id'])
                if agent is None:
                    continue
                service = agent.get('status') if isinstance(agent.get('status'), str) and agent['status'] in SERVICE_STATES else 'unknown'
                activity = 'idle' if service == 'active' else 'offline'
                work = [m for m in data['messages'] if isinstance(m, dict) and m.get('recipient') == entry['id']]
                if service == 'active':
                    if any(m.get('status') == 'running' for m in work):
                        activity = 'working'
                    elif any(m.get('status') == 'queued' for m in work):
                        activity = 'waiting'
                    elif model_state != 'loaded':
                        activity = 'unavailable'
                entry.update(name=self._text(agent.get('name')) or entry['name'], role=self._text(agent.get('role'), 320),
                             status=service, activity=activity, model=model_id,
                             canSend=service == 'active' and model_state == 'loaded' and bool(model_id))
            environment = data.get('environment') if isinstance(data.get('environment'), dict) else {}
            return {'ok': True, 'enabled': True, 'available': True, 'state': 'ready',
                    'message': 'Connected to the local Team Hub.', 'agents': agents,
                    'model': {'id': model_id, 'status': model_state}, 'checkedAt': self._text(environment.get('checked_at'), 40)}
        except TeamUnavailable as error:
            return {'ok': True, 'enabled': error.enabled, 'available': False, 'state': error.state,
                    'message': error.message, 'agents': self._fallback_agents(),
                    'model': {'id': '', 'status': 'unknown'}, 'checkedAt': ''}

    def _prune(self):
        cutoff = time.monotonic() - RECEIPT_SECONDS
        self._receipts = {key: value for key, value in self._receipts.items() if value['created'] > cutoff}

    def send(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'agentId', 'text'}:
            raise ValueError('Choose one agent and provide message text only.')
        ident, text = payload['agentId'], payload['text']
        if not isinstance(ident, str) or ident not in AGENTS:
            raise ValueError('Choose a known agent.')
        if (not isinstance(text, str) or not text.strip() or len(text) > 6000
                or any(ord(c) < 32 and c not in '\n\r\t' for c in text)):
            raise ValueError('The message must contain 1 to 6000 characters.')
        text = text.strip()
        with self._lock:
            self._prune()
            if len(self._receipts) >= 256:
                return {'ok': False, 'status': 'busy', 'message': 'Too many recent requests. Try again later.'}
            try:
                data = self._state(fresh=True)
                agent = self._catalog(data).get(ident)
                model = data.get('model') if isinstance(data.get('model'), dict) else {}
                if (not agent or agent.get('status') != 'active' or model.get('status') != 'loaded'
                        or not self._text(model.get('id'))):
                    return {'ok': False, 'status': 'unavailable', 'message': 'This agent and its configured model must be ready before sending.'}
            except TeamUnavailable as error:
                return {'ok': False, 'status': error.state, 'message': error.message}
            try:
                result = self._request('/api/messages', {'sender': 'besian', 'recipient': ident, 'body': text,
                                                        'dispatch': True, 'notify': False})
                if (type(result.get('id')) is not int or result['id'] <= 0 or type(result.get('task_id')) is not int
                        or result['task_id'] <= 0 or result.get('status') != 'queued'):
                    raise TeamUnavailable('invalid_response', 'Team Hub did not confirm this request.')
            except TeamUnavailable:
                # Never retry a POST: the hub may have queued it before the connection failed.
                return {'ok': False, 'status': 'uncertain',
                        'message': 'No confirmation received. The request may be queued; check Team Hub before sending it again.'}
            receipt_id = secrets.token_urlsafe(24)
            self._receipts[receipt_id] = {'message': result['id'], 'task': result['task_id'], 'agent': ident,
                                         'digest': hashlib.sha256(text.encode('utf-8')).digest(), 'created': time.monotonic()}
            self._cached = None
            return {'ok': True, 'receiptId': receipt_id, 'agentId': ident, 'status': 'queued'}

    @staticmethod
    def _owned_messages(data, owner):
        rows = [m for m in data['messages'] if isinstance(m, dict) and type(m.get('id')) is int]
        root = next((m for m in rows if m['id'] == owner['message']), None)
        if (not root or root.get('sender') != 'besian' or root.get('recipient') != owner['agent']
                or root.get('task_id') != owner['task'] or root.get('root_id') != owner['message']
                or root.get('parent_id') is not None or root.get('kind') != 'request'
                or not isinstance(root.get('body'), str)
                or hashlib.sha256(root['body'].encode('utf-8')).digest() != owner['digest']):
            return []
        found = {root['id']: root}
        for _ in range(8):
            changed = False
            for item in rows:
                parent = found.get(item.get('parent_id')) if type(item.get('parent_id')) is int else None
                if (item['id'] in found or not parent or item.get('root_id') != owner['message']
                        or item.get('task_id') != owner['task'] or item.get('sender') != parent.get('recipient')
                        or not isinstance(item.get('recipient'), str) or item['recipient'] not in AGENTS
                        or item.get('kind') not in ('request', 'result')):
                    continue
                if item['kind'] == 'result' and item['recipient'] != parent.get('sender'):
                    continue
                found[item['id']] = item
                changed = True
            if not changed:
                break
        return sorted(found.values(), key=lambda m: m['id'])

    def receipt(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'receiptId'} or not isinstance(payload['receiptId'], str):
            raise ValueError('Provide a message receipt only.')
        receipt_id = payload['receiptId']
        if not re.fullmatch(r'[A-Za-z0-9_-]{32}', receipt_id):
            raise ValueError('Invalid message receipt.')
        with self._lock:
            self._prune()
            owner = self._receipts.get(receipt_id)
            if owner is None:
                return {'ok': False, 'status': 'unknown', 'message': 'This receipt expired or belongs to another server session.'}
            try:
                data = self._state()
            except TeamUnavailable as error:
                return {'ok': False, 'status': error.state, 'message': error.message, 'receiptId': receipt_id}
            messages = self._owned_messages(data, owner)
            if not messages:
                return {'ok': False, 'status': 'unavailable', 'message': 'This request is no longer in the recent Team Hub history.', 'receiptId': receipt_id}
            root = messages[0]
            pending = [m for m in messages if m.get('status') in ('queued', 'running')]
            failed = any(m.get('status') in ('failed', 'interrupted') for m in messages)
            latest = next((m for m in reversed(messages) if m.get('recipient') == owner['agent'] and m.get('status') == 'completed'), None)
            status = ('running' if any(m.get('status') == 'running' for m in pending) else 'queued') if pending else ('failed' if failed else root.get('status'))
            if not isinstance(status, str) or status not in MESSAGE_STATES:
                status = 'unknown'
            return {'ok': True, 'receiptId': receipt_id, 'agentId': owner['agent'], 'status': status,
                    'complete': not pending and status in ('completed', 'failed', 'interrupted'),
                    'reply': self._text(latest.get('response'), 16000) if latest else '',
                    'message': 'A team task needs attention in the local Team Hub.' if failed else ''}
