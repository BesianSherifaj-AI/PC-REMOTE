"""A local Python dashboard for the MAIC touchscreen."""
import argparse
import ctypes
from datetime import datetime, timezone
import io
import ipaddress
import json
import math
import mimetypes
import os
import secrets
import select
from pathlib import Path
import struct
import threading
import time
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlsplit, unquote, parse_qs

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / '.runtime' / 'diagnostics'
STARTED = time.monotonic()


def pc_status():
    memory = None
    if os.name == 'nt':
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                        ('total', ctypes.c_ulonglong), ('available', ctypes.c_ulonglong),
                        ('page_total', ctypes.c_ulonglong), ('page_available', ctypes.c_ulonglong),
                        ('virtual_total', ctypes.c_ulonglong), ('virtual_available', ctypes.c_ulonglong),
                        ('extended_available', ctypes.c_ulonglong)]
        snapshot = MemoryStatus()
        snapshot.length = ctypes.sizeof(snapshot)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(snapshot)):
            memory = {'totalMB': round(snapshot.total / 1048576),
                      'availableMB': round(snapshot.available / 1048576),
                      'usedPercent': snapshot.load}
    return {'ok': True, 'pcName': os.environ.get('COMPUTERNAME', 'Your PC'),
            'uptimeSeconds': round(time.monotonic() - STARTED), 'memory': memory}


def configured_apps(host):
    """Publish only configured web apps on this PC; never run incoming commands."""
    config = ROOT / 'apps.json'
    if not config.is_file():
        return []
    payload = json.loads(config.read_text(encoding='utf-8-sig'))
    if not isinstance(payload, dict) or not isinstance(payload.get('apps'), list):
        raise ValueError('apps.json must contain an apps list.')
    result, ids = [], set()
    for app in payload['apps']:
        if not isinstance(app, dict):
            raise ValueError('Invalid app entry.')
        identifier, name = app.get('id'), app.get('name')
        port, path = app.get('port'), app.get('path', '/')
        description = app.get('description', '')
        if (not isinstance(identifier, str) or not identifier or len(identifier) > 64
                or identifier in ids or not isinstance(name, str) or not name
                or len(name) > 80 or not isinstance(description, str) or len(description) > 240
                or type(port) is not int or not 1 <= port <= 65535
                or not isinstance(path, str) or not path.startswith('/')
                or path.startswith('//') or len(path) > 2048
                or any(ord(c) < 32 or c in '\\' for c in path)):
            raise ValueError('Invalid app name, port, or path in apps.json.')
        result.append({'id': identifier, 'name': name, 'description': description,
                       'url': f'http://{host}:{port}{path}'})
        ids.add(identifier)
    return result


def saved_websites():
    path = ROOT / '.runtime' / 'websites.json'
    if not path.is_file():
        return []
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, list):
        raise ValueError('Invalid saved websites.')
    return value


def validate_bookmark(payload):
    if not isinstance(payload, dict) or set(payload) != {'name', 'url'}:
        raise ValueError('Enter a website name and address.')
    name, address = payload['name'], payload['url']
    if (not isinstance(name, str) or not name.strip() or len(name) > 80
            or not isinstance(address, str) or len(address) > 2048
            or any(ord(c) < 32 or c == '\\' for c in address)):
        raise ValueError('Enter a short name and a valid website address.')
    parsed = urlsplit(address)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(c.isspace() for c in parsed.netloc)):
        raise ValueError('Use an http:// or https:// website address without login credentials.')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('Invalid website port.')
    return name.strip(), address


def calculate(payload):
    if not isinstance(payload, dict) or set(payload) != {'left', 'right', 'operation'}:
        raise ValueError('Enter two numbers and an operation.')
    left, right, operation = payload['left'], payload['right'], payload['operation']
    for value in (left, right):
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError('Enter finite numbers between -1e100 and 1e100.')
    if operation == 'add':
        result = left + right
    elif operation == 'subtract':
        result = left - right
    elif operation == 'multiply':
        result = left * right
    elif operation == 'divide':
        if right == 0:
            raise ValueError('Cannot divide by zero.')
        result = left / right
    else:
        raise ValueError('Choose add, subtract, multiply, or divide.')
    if not math.isfinite(result):
        raise ValueError('The result is too large.')
    return result


def validate_diagnostics(payload):
    """Keep only documented, non-secret browser fields; never collect page URLs."""
    allowed = {'schemaVersion', 'source', 'userAgent', 'platform', 'language', 'screen',
               'viewport', 'capabilities', 'persistence', 'clientTime'}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError('Unexpected diagnostics fields.')
    if payload.get('schemaVersion') != 1 or payload.get('source') not in ('maic-user', 'pc-verification'):
        raise ValueError('Unsupported diagnostics format.')
    for key in ('userAgent', 'platform', 'language', 'clientTime'):
        if not isinstance(payload.get(key), str) or len(payload[key]) > 1024:
            raise ValueError('Invalid browser text field.')
    nested = {
        'screen': {'width', 'height', 'pixelRatio'},
        'viewport': {'width', 'height'},
        'capabilities': {'touchPoints', 'cookiesEnabled', 'localStorage', 'webgl', 'webglRenderer'},
        'persistence': {'available', 'previousMarker', 'currentMarker'},
    }
    for key, keys in nested.items():
        value = payload.get(key)
        if not isinstance(value, dict) or set(value) - keys:
            raise ValueError('Invalid browser capability fields.')
        for item in value.values():
            if not isinstance(item, (str, int, float, bool, type(None))):
                raise ValueError('Invalid capability value.')
            if isinstance(item, str) and len(item) > 512:
                raise ValueError('Capability text is too long.')
            if isinstance(item, (int, float)) and not math.isfinite(item):
                raise ValueError('Invalid capability number.')
    return payload


def make_tone():
    output = io.BytesIO()
    rate = 22050
    duration = 0.4
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        samples = []
        for i in range(int(rate * duration)):
            fade = min(1, i / 220, (rate * duration - i) / 220)
            sample = int(6000 * fade * math.sin(2 * math.pi * 660 * i / rate))
            samples.append(struct.pack('<h', sample))
        wav.writeframes(b''.join(samples))
    return output.getvalue()


TONE = make_tone()


class Handler(BaseHTTPRequestHandler):
    server_version = 'MAICPC/2.0'
    sys_version = ''

    def allowed(self):
        address = ipaddress.ip_address(self.client_address[0])
        return address.is_loopback or address in self.server.allowed_network

    def control_allowed(self):
        if self.headers.get('Host') != self.server.expected_host:
            return False
        origin = self.headers.get('Origin')
        if origin and origin != self.server.origin:
            return False
        client_ip = self.client_address[0]
        if client_ip == self.server.server_address[0]:
            return True
        try:
            config = json.loads((ROOT / '.runtime' / 'control-access.json').read_text(encoding='utf-8'))
            return client_ip in config.get('clients', [])
        except (OSError, ValueError):
            return False

    def control_authenticated(self):
        if not self.control_allowed():
            return False
        with self.server.control_lock:
            expected = self.server.control_sessions.get(self.client_address[0])
        received = self.headers.get('X-MAIC-Control', '')
        return bool(expected and secrets.compare_digest(expected, received))

    def media_cookie(self):
        """A distinct image/video session, kept in an HttpOnly same-site cookie."""
        with self.server.control_lock:
            if not hasattr(self.server, 'media_sessions'):
                self.server.media_sessions = {}
            now = time.monotonic()
            self.server.media_sessions = {address: session for address, session in self.server.media_sessions.items() if session[1] > now}
            previous = self.server.media_sessions.get(self.client_address[0])
            value = previous[0] if previous else secrets.token_urlsafe(32)
            self.server.media_sessions[self.client_address[0]] = (value, now + 3600)
        return f'MAICMedia={value}; Path=/api/comfy; Max-Age=3600; HttpOnly; SameSite=Strict'

    def media_authenticated(self):
        if self.control_authenticated():
            return True
        if not self.control_allowed():
            return False
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            received = cookie['MAICMedia'].value if 'MAICMedia' in cookie else ''
        except (CookieError, ValueError):
            return False
        with self.server.control_lock:
            session = getattr(self.server, 'media_sessions', {}).get(self.client_address[0])
        return bool(session and session[1] > time.monotonic() and received and secrets.compare_digest(session[0], received))

    def read_json(self, limit=4096):
        if self.headers.get_content_type() != 'application/json':
            self.json(415, {'ok': False, 'message': 'Send JSON.'})
            return None
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= limit:
                self.json(413, {'ok': False, 'message': 'Request is too large or empty.'})
                return None
            self.connection.settimeout(30 if limit > 131072 else 5)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete request.')
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError('Use a JSON object.')
            return payload
        except (ValueError, UnicodeError, TimeoutError):
            self.json(400, {'ok': False, 'message': 'Could not read the request.'})
            return None

    def send(self, status, content_type, data, download_name=None, headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', (headers or {}).get('Cache-Control', 'no-store'))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "frame-ancestors 'self'")
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        for key, value in (headers or {}).items():
            if key.lower() == 'cache-control':
                continue
            self.send_header(key, value)
        if download_name:
            self.send_header('Content-Disposition', 'attachment; filename="' + download_name + '"')
        self.end_headers()
        self.wfile.write(data)

    def json(self, status, value, headers=None):
        self.send(status, 'application/json; charset=utf-8', json.dumps(value).encode(), headers=headers)

    def private_get(self, path):
        if not self.control_authenticated():
            return self.json(403, {'ok': False, 'message': 'Refresh to renew PC access.'})
        try:
            if path == '/api/pc/apps':
                value = self.server.app_registry.catalog()
            elif path == '/api/pc/state':
                value = self.server.app_registry.state()
            elif path == '/api/hardware':
                from pc_controls import hardware_status
                value = dict(pc_status(), **hardware_status())
            elif path == '/api/lm/models':
                value = self.server.ai_service.models()
            elif path == '/api/speech/status':
                value = self.server.speech_service.status()
            elif path == '/api/tts/status':
                value = self.server.tts_service.status()
            elif path.startswith('/api/remote/'):
                gateway = getattr(self.server, 'remote_gateway', None)
                if path == '/api/remote/status' and self.client_address[0] != self.server.server_address[0]:
                    return self.json(403, {'ok': False, 'message': 'Approve remote browsers from this PC’s local dashboard.'})
                value = (gateway.status() if path == '/api/remote/status' else gateway.info()) if gateway else {
                    'ok': True, 'enabled': False, 'url': '', 'message': 'Secure remote access is not configured yet.'}
            elif path == '/api/comfy/status':
                value = self.server.ai_service.comfy_status()
            elif path == '/api/agents/status':
                value = self.server.agent_team.status()
            elif path == '/api/comfy/library':
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if set(query) - {'folder', 'search', 'media', 'offset', 'limit'} or any(len(v) != 1 for v in query.values()):
                    raise ValueError('Invalid output library filter.')
                value = self.server.comfy_library.list_outputs(
                    folder=query.get('folder', ['root'])[0], search=query.get('search', [''])[0],
                    media=query.get('media', ['all'])[0], offset=int(query.get('offset', ['0'])[0]),
                    limit=int(query.get('limit', ['24'])[0]))
            else:
                bridge = getattr(self.server, 'desktop_bridge', None)
                value = bridge.status() if bridge else {'ok': True, 'available': False, 'message': 'Desktop viewer is unavailable.'}
            headers = {'Set-Cookie': self.media_cookie()} if path in ('/api/comfy/status', '/api/comfy/library') else None
            return self.json(200, value, headers=headers)
        except ValueError:
            return self.json(400, {'ok': False, 'message': 'Invalid PC service request.'})
        except (RuntimeError, OSError, AttributeError):
            return self.json(503, {'ok': False, 'message': 'This PC service is unavailable.'})

    def static_file(self, relative):
        web = (ROOT / 'web').resolve()
        target = (web / relative).resolve()
        if (web not in target.parents or not target.is_file()
                or target.suffix.lower() not in ('.html', '.js', '.css', '.svg', '.png', '.jpg', '.ico', '.wav', '.ogg', '.woff', '.woff2', '.webmanifest')):
            return self.json(404, {'ok': False, 'message': 'Page not found.'})
        content_type = ({'.js': 'text/javascript', '.webmanifest': 'application/manifest+json'}
                        .get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        cache = {'Cache-Control': 'private, max-age=86400'} if relative.startswith('vendor/novnc/') else None
        self.send(200, content_type, target.read_bytes(), headers=cache)

    def stream_chat(self, payload):
        # Do not print, persist, or interpolate prompts/model responses into logs.
        cancellation = threading.Event()
        monitor_stop = threading.Event()
        cancellable = getattr(type(self.server.ai_service), 'stream_chat_cancellable', None)
        stream = cancellable(self.server.ai_service, payload, cancellation) if callable(cancellable) else self.server.ai_service.stream_chat(payload)
        def watch_connection():
            connection = getattr(self, 'connection', None)
            if connection is None:
                return
            while not monitor_stop.is_set():
                try:
                    readable, _, _ = select.select([connection], [], [], 0.25)
                    if readable:
                        # No more browser request bytes are valid during this response.
                        cancellation.set()
                        return
                except (OSError, ValueError):
                    cancellation.set()
                    return
        monitor = threading.Thread(target=watch_connection, daemon=True)
        monitor.start()
        try:
            first = next(stream)
        except (ValueError, RuntimeError, OSError) as error:
            disconnected = cancellation.is_set()
            cancellation.set()
            monitor_stop.set()
            stream.close()
            monitor.join(timeout=0.5)
            if disconnected:
                return
            return self.json(400, {'ok': False, 'message': str(error)})
        except StopIteration:
            disconnected = cancellation.is_set()
            cancellation.set()
            monitor_stop.set()
            stream.close()
            monitor.join(timeout=0.5)
            if disconnected:
                return
            return self.json(503, {'ok': False, 'message': 'The model returned no response.'})
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(first.encode('utf-8'))
            self.wfile.flush()
            for event in stream:
                self.wfile.write(event.encode('utf-8'))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            cancellation.set()
            monitor_stop.set()
            stream.close()
            monitor.join(timeout=0.5)

    def do_GET(self):
        if not self.allowed():
            return self.json(403, {'ok': False})
        path = urlsplit(self.path).path
        if path == '/':
            self.send(200, 'text/html; charset=utf-8', (ROOT / 'web' / 'index.html').read_bytes())
        elif path in ('/app.js', '/voice.js', '/chat-media.js', '/companion.js', '/style.css', '/agent-avatars.css', '/desktop-viewer.html',
                      '/manifest.webmanifest', '/icon-192.png', '/icon-512.png', '/apple-touch-icon.png'):
            self.static_file(path[1:])
        elif path in ('/agent-avatars/moss.svg', '/agent-avatars/aqua.svg', '/agent-avatars/coral.svg', '/agent-avatars/amber.svg'):
            self.static_file(path[1:])
        elif path.startswith('/desktop/'):
            if not self.control_allowed():
                return self.json(403, {'ok': False})
            self.static_file('vendor/novnc/' + unquote(path[len('/desktop/'):]))
        elif path in ('/api/pc/apps', '/api/pc/state', '/api/hardware', '/api/lm/models', '/api/speech/status', '/api/tts/status', '/api/remote/info', '/api/remote/status', '/api/comfy/status', '/api/desktop/status', '/api/agents/status', '/api/comfy/library'):
            self.private_get(path)
        elif path.startswith('/api/comfy/library/output/'):
            if not self.media_authenticated():
                return self.json(403, {'ok': False})
            try:
                reference = self.server.comfy_library.reference(path.rsplit('/', 1)[1])
                result = self.server.ai_service.output_reference(reference, self.headers.get('Range'))
                content_type, data = result[:2]
                metadata = result[2] if len(result) > 2 else {}
                return self.send(metadata.get('status', 200), content_type, data, headers=metadata.get('headers'))
            except (ValueError, RuntimeError, OSError):
                return self.json(404, {'ok': False, 'message': 'This library preview is unavailable. Keep ComfyUI running and refresh the library.'})
        elif path.startswith('/api/comfy/output/'):
            # Media tags use the private cookie; API clients can use the session header.
            if not self.media_authenticated():
                return self.json(403, {'ok': False})
            try:
                result = self.server.ai_service.output(path.rsplit('/', 1)[1], range_header=self.headers.get('Range'))
                content_type, data = result[:2]
                metadata = result[2] if len(result) > 2 else {}
                return self.send(metadata.get('status', 200), content_type, data, headers=metadata.get('headers'))
            except (ValueError, RuntimeError, OSError):
                return self.json(404, {'ok': False, 'message': 'This preview is unavailable.'})
        elif path == '/api/health':
            with self.server.tap_lock:
                taps = self.server.taps
            self.json(200, {'ok': True, 'message': 'Python is responding from your PC.', 'taps': taps})
        elif path == '/api/status':
            self.json(200, pc_status())
        elif path == '/api/apps':
            try:
                with self.server.control_lock:
                    apps = configured_apps(self.server.server_address[0]) + saved_websites()
            except (ValueError, OSError, UnicodeError):
                return self.json(500, {'ok': False, 'message': 'Check your PC app configuration.'})
            self.json(200, {'ok': True, 'apps': apps})
        elif path == '/api/control-session':
            if not self.control_allowed():
                return self.json(403, {'ok': False, 'message': 'Approve this device from your PC first.'})
            with self.server.control_lock:
                token = self.server.control_sessions.setdefault(self.client_address[0], secrets.token_urlsafe(32))
            self.json(200, {'ok': True, 'token': token,
                            'canManageDevices': self.client_address[0] == self.server.server_address[0]},
                      headers={'Set-Cookie': self.media_cookie()})
        elif path == '/api/audio':
            if not self.control_allowed():
                return self.json(403, {'ok': False, 'message': 'Approve this device from your PC first.'})
            try:
                from pc_controls import get_audio_state
                self.json(200, dict(get_audio_state(), ok=True))
            except (ImportError, RuntimeError, OSError):
                self.json(503, {'ok': False, 'message': 'Windows audio controls are unavailable.'})
        elif path == '/tone.wav':
            self.send(200, 'audio/wav', TONE)
        elif path == '/download/discreet-launcher.apk':
            apk = ROOT / 'downloads' / 'discreet-launcher-7.8.4.apk'
            if not apk.is_file():
                return self.json(404, {'ok': False, 'message': 'Launcher download is not ready.'})
            self.send(200, 'application/vnd.android.package-archive', apk.read_bytes(), 'discreet-launcher-7.8.4.apk')
        elif path == '/download/maic-pc.apk':
            apk = ROOT / 'downloads' / 'MAIC-PC.apk'
            if not apk.is_file():
                return self.json(404, {'ok': False, 'message': 'The PC app installer is not ready yet.'})
            self.send(200, 'application/vnd.android.package-archive', apk.read_bytes(), 'MAIC-PC.apk')
        else:
            self.json(404, {'ok': False, 'message': 'Page not found.'})

    def do_POST(self):
        if not self.allowed():
            return self.json(403, {'ok': False})
        if self.path == '/api/speech/transcribe':
            return self.receive_speech()
        private_paths = ('/api/pc/action', '/api/pc/favourites', '/api/lm/start', '/api/lm/open', '/api/lm/load', '/api/lm/chat', '/api/tts/speak', '/api/remote/approve', '/api/remote/revoke', '/api/desktop/session', '/api/agents/send', '/api/agents/receipt', '/api/comfy/library/open')
        if self.path not in ('/api/tap', '/api/diagnostics', '/api/calculate', '/api/control', '/api/bookmarks') + private_paths:
            return self.json(404, {'ok': False})
        origin = self.headers.get('Origin')
        if origin and origin != self.server.origin:
            return self.json(403, {'ok': False})
        if self.headers.get('Transfer-Encoding'):
            return self.json(400, {'ok': False, 'message': 'Unsupported request format.'})
        if self.path in private_paths:
            if not self.control_authenticated():
                return self.json(403, {'ok': False, 'message': 'Refresh to renew PC access.'})
            from ai_services import MAX_CHAT_REQUEST_BYTES
            limit = MAX_CHAT_REQUEST_BYTES if self.path == '/api/lm/chat' else 40000 if self.path == '/api/agents/send' else 24000 if self.path == '/api/tts/speak' else 4096
            payload = self.read_json(limit)
            if payload is None:
                return
            try:
                if self.path == '/api/agents/send':
                    return self.json(200, self.server.agent_team.send(payload))
                if self.path == '/api/agents/receipt':
                    return self.json(200, self.server.agent_team.receipt(payload))
                if self.path == '/api/comfy/library/open':
                    return self.json(200, self.server.comfy_library.open_folder(payload))
                if self.path == '/api/tts/speak':
                    cancellation = threading.Event()
                    monitor_stop = threading.Event()
                    def watch_speech_connection():
                        connection = getattr(self, 'connection', None)
                        if connection is None:
                            return
                        while not monitor_stop.is_set():
                            try:
                                readable, _, _ = select.select([connection], [], [], 0.25)
                                if readable:
                                    cancellation.set()
                                    return
                            except (OSError, ValueError):
                                cancellation.set()
                                return
                    monitor = threading.Thread(target=watch_speech_connection, daemon=True)
                    monitor.start()
                    try:
                        service = self.server.tts_service
                        cancellable = getattr(type(service), 'synthesize_cancellable', None)
                        audio = cancellable(service, payload, cancellation) if callable(cancellable) else service.synthesize(payload)
                        if not cancellation.is_set():
                            try:
                                return self.send(200, 'audio/wav', audio)
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                cancellation.set()
                                return
                    except (ValueError, RuntimeError, OSError):
                        if cancellation.is_set():
                            return
                        raise
                    finally:
                        monitor_stop.set()
                        monitor.join(timeout=0.5)
                if self.path == '/api/pc/action':
                    return self.json(200, self.server.app_registry.action(payload))
                if self.path == '/api/pc/favourites':
                    return self.json(200, self.server.app_registry.favourites(payload))
                if self.path == '/api/lm/chat':
                    return self.stream_chat(payload)
                if self.path == '/api/lm/load':
                    return self.json(200, self.server.ai_service.load_lm_model(payload))
                if self.path in ('/api/remote/approve', '/api/remote/revoke'):
                    if self.client_address[0] != self.server.server_address[0]:
                        return self.json(403, {'ok': False, 'message': 'Use the local dashboard on this PC to manage access.'})
                    if set(payload) != {'id'} or not isinstance(payload['id'], str) or len(payload['id']) > 128:
                        raise ValueError('Select a device from the list.')
                    gateway = getattr(self.server, 'remote_gateway', None)
                    if not gateway:
                        raise RuntimeError('Remote access is unavailable.')
                    return self.json(200, gateway.approve(payload['id']) if self.path.endswith('/approve') else gateway.revoke(payload['id']))
                if payload:
                    raise ValueError('This action takes no arguments.')
                if self.path == '/api/lm/start':
                    return self.json(200, self.server.ai_service.start_lm_api())
                if self.path == '/api/lm/open':
                    executable = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'LM Studio' / 'LM Studio.exe'
                    if not executable.is_file():
                        raise RuntimeError('LM Studio is not installed.')
                    import subprocess
                    subprocess.Popen([str(executable)], shell=False)
                    return self.json(200, {'ok': True, 'message': 'LM Studio opened on Windows. Choose and load a model there.'})
                bridge = getattr(self.server, 'desktop_bridge', None)
                if not bridge:
                    raise RuntimeError('Desktop viewer is unavailable.')
                ticket = bridge.issue_ticket(self.client_address[0])
                cookie = f"{ticket['cookie_name']}={ticket['ticket']}; Path={ticket['path']}; Max-Age=60; HttpOnly; SameSite=Strict"
                return self.json(200, {'ok': True, 'port': bridge.status()['port'], 'credentials': bridge.desktop_credentials()}, headers={'Set-Cookie': cookie})
            except (ValueError, OverflowError) as error:
                return self.json(400, {'ok': False, 'message': str(error)})
            except (RuntimeError, OSError, AttributeError):
                return self.json(503, {'ok': False, 'message': 'This PC service is unavailable. Check Help on the PC.'})
        if self.path in ('/api/control', '/api/bookmarks'):
            if not self.control_authenticated():
                return self.json(403, {'ok': False, 'message': 'PC control authorization expired. Refresh the dashboard.'})
            payload = self.read_json()
            if payload is None:
                return
            try:
                if self.path == '/api/control':
                    from pc_controls import perform_action
                    return self.json(200, perform_action(payload))
                name, address = validate_bookmark(payload)
                with self.server.control_lock:
                    websites = saved_websites()
                    entry = next((site for site in websites if site.get('url') == address), None)
                    if entry:
                        entry['name'] = name
                    else:
                        if len(websites) >= 64:
                            raise ValueError('Up to 64 saved websites are supported.')
                        websites.append({'id': 'website-' + uuid.uuid4().hex, 'name': name,
                                         'url': address, 'description': 'Saved website'})
                    path = ROOT / '.runtime' / 'websites.json'
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(websites, indent=2), encoding='utf-8')
                    temporary.replace(path)
                return self.json(200, {'ok': True, 'message': 'Website saved.'})
            except (ValueError, OverflowError) as error:
                return self.json(400, {'ok': False, 'message': str(error)})
            except (ImportError, RuntimeError, OSError):
                return self.json(503, {'ok': False, 'message': 'The PC could not complete that action.'})
        if self.path == '/api/diagnostics':
            return self.receive_diagnostics()
        if self.path == '/api/calculate':
            if self.headers.get_content_type() != 'application/json':
                return self.json(415, {'ok': False, 'message': 'Send the numbers as JSON.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    return self.json(413, {'ok': False, 'message': 'Calculation is too large or empty.'})
                self.connection.settimeout(5)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('Incomplete calculation.')
                result = calculate(json.loads(raw))
            except (ValueError, OverflowError, UnicodeError, TimeoutError) as error:
                message = str(error) if isinstance(error, ValueError) else 'Enter valid numbers.'
                return self.json(400, {'ok': False, 'message': message})
            return self.json(200, {'ok': True, 'result': result})
        if self.headers.get('Content-Length', '0') != '0' or self.headers.get('Transfer-Encoding'):
            return self.json(400, {'ok': False, 'message': 'This test accepts an empty request.'})
        with self.server.tap_lock:
            self.server.taps += 1
            taps = self.server.taps
        self.json(200, {'ok': True, 'taps': taps, 'message': 'Your touch reached Python on the PC.'})

    def receive_speech(self):
        if not self.control_authenticated():
            return self.json(403, {'ok': False, 'message': 'Refresh to renew PC access.'})
        if self.headers.get('Transfer-Encoding') or self.headers.get_content_type() != 'audio/wav':
            return self.json(415, {'ok': False, 'message': 'Send a short WAV recording.'})
        try:
            from speech_service import MAX_AUDIO_BYTES
            length = int(self.headers.get('Content-Length', '0'))
            if not 44 <= length <= MAX_AUDIO_BYTES:
                return self.json(413, {'ok': False, 'message': 'Recording must be 30 seconds or less.'})
            self.connection.settimeout(15)
            audio = self.rfile.read(length)
            if len(audio) != length:
                raise ValueError('The recording is incomplete.')
            language = self.headers.get('X-MAIC-Language', 'auto')
            result = self.server.speech_service.transcribe(audio, language)
            return self.json(200, result)
        except (ValueError, TimeoutError) as error:
            return self.json(400, {'ok': False, 'message': str(error)})
        except (RuntimeError, OSError, ImportError):
            return self.json(503, {'ok': False, 'message': 'Local dictation is busy or unavailable. Use typed input or try again shortly.'})

    def receive_diagnostics(self):
        if self.headers.get_content_type() != 'application/json':
            return self.json(415, {'ok': False, 'message': 'Send browser diagnostics as JSON.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                return self.json(413, {'ok': False, 'message': 'Diagnostics report is too large or empty.'})
            self.connection.settimeout(5)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete report.')
            payload = validate_diagnostics(json.loads(raw))
        except (ValueError, UnicodeDecodeError, TimeoutError):
            return self.json(400, {'ok': False, 'message': 'Browser diagnostics could not be read.'})
        now = datetime.now(timezone.utc)
        report_id = 'MAIC-' + now.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
        ua = payload['userAgent']
        report = {
            'reportId': report_id,
            'receivedAtUtc': now.isoformat(),
            'observedClientIp': self.client_address[0],
            'browserContext': 'android' if 'Android' in ua else 'pc' if 'Windows' in ua else 'unknown',
            'browserReported': payload,
            'limits': 'Browser data cannot verify bootloader, root, battery hardware, or Android setup state.',
        }
        try:
            REPORTS.mkdir(parents=True, exist_ok=True)
            (REPORTS / (report_id + '.json')).write_text(json.dumps(report, indent=2), encoding='utf-8')
        except OSError:
            return self.json(500, {'ok': False, 'message': 'The PC could not save this report. Try again.'})
        self.json(200, {'ok': True, 'reportId': report_id,
                        'message': 'Browser report saved on your PC. Tell the assistant it was sent.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='PC LAN address; never binds all interfaces')
    parser.add_argument('--port', type=int, default=8840)
    parser.add_argument('--network', required=True, help='Allowed local subnet, for example 192.168.0.0/24')
    args = parser.parse_args()
    ipaddress.IPv4Address(args.host)
    if args.host == '0.0.0.0':
        parser.error('Choose the PC LAN address, not all interfaces.')
    if not (ROOT / 'web' / 'index.html').is_file():
        parser.error('web/index.html is missing.')
    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        server.allowed_network = ipaddress.ip_network(args.network, strict=False)
        server.origin = f'http://{args.host}:{args.port}'
        server.expected_host = f'{args.host}:{args.port}'
        server.control_lock = threading.RLock()
        server.control_sessions = {}
        server.taps = 0
        server.tap_lock = threading.Lock()
        from pc_apps import AppRegistry
        from ai_services import AIService
        server.app_registry = AppRegistry(ROOT)
        server.ai_service = AIService(ROOT)
        from agent_team import AgentTeam
        from comfy_library import ComfyLibrary
        server.agent_team = AgentTeam(ROOT)
        server.comfy_library = ComfyLibrary(ROOT)
        from speech_service import SpeechService
        server.speech_service = SpeechService()
        from pc_tts import TTSService
        server.tts_service = TTSService(ROOT)
        server.remote_gateway = None
        server.desktop_bridge = None
        try:
            from desktop_bridge import DesktopBridge
            def approved(address):
                if address == args.host:
                    return True
                try:
                    return address in json.loads((ROOT / '.runtime' / 'control-access.json').read_text(encoding='utf-8')).get('clients', [])
                except (OSError, ValueError):
                    return False
            server.desktop_bridge = DesktopBridge(ROOT, host=args.host, origin=server.origin, is_client_approved=approved)
            server.desktop_bridge.start()
        except (ImportError, RuntimeError, OSError):
            print('Desktop gateway unavailable; the dashboard remains operational.', flush=True)
        remote_config = ROOT / '.runtime' / 'remote' / 'config.json'
        if remote_config.is_file():
            try:
                from remote_access import RemoteGateway
                settings = json.loads(remote_config.read_text(encoding='utf-8-sig'))
                server.remote_gateway = RemoteGateway(ROOT, upstream=server.origin, public_origin=settings['publicOrigin'])
                server.remote_gateway.start()
            except (ImportError, RuntimeError, OSError, ValueError, KeyError):
                server.remote_gateway = None
                print('Secure remote gateway unavailable; local dashboard remains operational.', flush=True)
        print(f'MAIC PC dashboard: {server.origin}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.tts_service.close()
            if server.remote_gateway:
                server.remote_gateway.stop()
            if server.desktop_bridge:
                server.desktop_bridge.stop()


if __name__ == '__main__':
    main()
