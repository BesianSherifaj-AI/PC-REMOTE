"""Approved browser sessions to this PC's loopback-only desktop server.

No upstream address is accepted from HTTP input. Tickets and VNC passwords
never appear in URLs, request logs, status responses or command arguments.
"""
import asyncio
import ctypes
from ctypes import wintypes
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import threading
import time
from urllib.parse import urlsplit

COOKIE_NAME = 'MAICDesktop'
COOKIE_PATH = '/desktop'
VNC_HOST = '127.0.0.1'
VNC_PORT = 5900
BRIDGE_PORT = 8841
TICKET_SECONDS = 60


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('Desktop credentials require Windows DPAPI.')
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('Windows could not protect or unlock desktop credentials.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.memset(target.data, 0, target.size)
        kernel.LocalFree(target.data)
        ctypes.memset(buffer, 0, len(data))


def _vnc_des(key, data):
    # VNC reverses the bits of each DES key byte. The existing cryptography
    # runtime handles the standard primitive; a repeated 8-byte TDEA key is DES.
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    try:
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    except ImportError:
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
    reversed_key = bytes(int(f'{byte:08b}'[::-1], 2) for byte in key)
    encryptor = Cipher(TripleDES(reversed_key), modes.ECB()).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def desktop_credentials(root):
    protected = Path(root) / '.runtime' / 'desktop' / 'vnc-password.dpapi'
    if not protected.is_file():
        raise RuntimeError('The desktop server has not been set up.')
    return {'password': _dpapi(protected.read_bytes(), decrypt=True).decode('ascii')}


def configure_vnc(root):
    """Configure only the current user's application-mode TightVNC server."""
    if os.name != 'nt':
        raise RuntimeError('The desktop server requires Windows.')
    import winreg
    root = Path(root)
    runtime = root / '.runtime' / 'desktop'
    runtime.mkdir(parents=True, exist_ok=True)
    protected = runtime / 'vnc-password.dpapi'
    marker = runtime / 'configured.json'
    registry_path = r'Software\TightVNC\Server'
    if not marker.exists():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path):
                raise RuntimeError('Existing user TightVNC settings were found; preserve them before configuring this server.')
        except FileNotFoundError:
            pass
    if not protected.exists():
        password = ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
        protected.write_bytes(_dpapi(password.encode('ascii')))
    password = desktop_credentials(root)['password'].encode('ascii')
    settings = {
        'AcceptRfbConnections': 1, 'RfbPort': VNC_PORT,
        'LoopbackOnly': 1, 'AllowLoopback': 1, 'AcceptHttpConnections': 0,
        'UseVncAuthentication': 1, 'UseControlAuthentication': 0,
        'EnableFileTransfers': 0, 'AlwaysShared': 1, 'NeverShared': 0,
        'DisconnectClients': 0, 'DisconnectAction': 0, 'BlockLocalInput': 0,
        'BlockRemoteInput': 0, 'RemoveWallpaper': 0, 'UseMirrorDriver': 0,
        'LogLevel': 0, 'SaveLogToAllUsersPath': 0, 'RunControlInterface': 0,
    }
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
        for name, value in settings.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
        winreg.SetValueEx(key, 'ExtraPorts', 0, winreg.REG_SZ, '')
        # TightVNC's own registry format is legacy DES obfuscation. The source
        # password remains DPAPI protected, and no password/hash is printed.
        winreg.SetValueEx(key, 'Password', 0, winreg.REG_BINARY,
                          _vnc_des(bytes((23, 82, 107, 6, 35, 78, 88, 7)), password))
    marker.write_text(json.dumps({'version': '2.8.88', 'mode': 'user-application',
                                 'loopbackOnly': True, 'vncAuthentication': True}), encoding='utf-8')
    return {'ok': True, 'mode': 'user-application', 'loopbackOnly': True,
            'vncAuthentication': True, 'credentialsProtected': 'Windows DPAPI'}


def _receive(connection, length):
    result = bytearray()
    while len(result) < length:
        chunk = connection.recv(length - len(result))
        if not chunk:
            raise RuntimeError('The desktop connection ended early.')
        result.extend(chunk)
    return bytes(result)


def verify_rfb(root):
    """Read-only framebuffer handshake and a small pixel-update request."""
    with socket.create_connection((VNC_HOST, VNC_PORT), timeout=3) as connection:
        if _receive(connection, 12) != b'RFB 003.008\n':
            raise RuntimeError('Unexpected desktop protocol version.')
        connection.sendall(b'RFB 003.008\n')
        count = _receive(connection, 1)[0]
        if not 0 < count <= 64 or 2 not in _receive(connection, count):
            raise RuntimeError('Private VNC authentication was not offered.')
        connection.sendall(b'\x02')
        challenge = _receive(connection, 16)
        password = desktop_credentials(root)['password'].encode('ascii')
        connection.sendall(_vnc_des(password, challenge))
        if _receive(connection, 4) != b'\x00\x00\x00\x00':
            raise RuntimeError('The protected desktop credentials were rejected.')
        connection.sendall(b'\x01')  # Share the existing interactive desktop.
        header = _receive(connection, 24)
        width, height = struct.unpack('>HH', header[:4])
        name_length = struct.unpack('>I', header[20:24])[0]
        if not width or not height or name_length > 16384:
            raise RuntimeError('Invalid desktop dimensions.')
        _receive(connection, name_length)
        connection.sendall(b'\x03\x00' + struct.pack('>HHHH', 0, 0, min(width, 32), min(height, 32)))
        update = _receive(connection, 4)
        if update[0] != 0 or struct.unpack('>H', update[2:])[0] == 0:
            raise RuntimeError('The desktop did not return a pixel update.')
    return {'ok': True, 'authenticated': True, 'framebufferUpdate': True,
            'width': width, 'height': height}


class DesktopBridge:
    def __init__(self, root, host, origin, is_client_approved, *, port=BRIDGE_PORT):
        self.root = Path(root)
        self.host = str(ipaddress.ip_address(host))
        parsed = urlsplit(origin)
        if parsed.scheme not in ('http', 'https') or parsed.hostname != self.host or parsed.username or parsed.password:
            raise ValueError('Desktop origin must be this PC dashboard address.')
        self.origin = origin.rstrip('/')
        self.port = port
        self.is_client_approved = is_client_approved
        self._vnc_port = VNC_PORT  # Fixed in production; no HTTP parameter changes it.
        self._tickets = {}
        self._lock = threading.Lock()
        self._thread = self._loop = self._runner = None
        self._ready = threading.Event()
        self._running = False
        self._message = 'Desktop bridge has not started.'
        self._sessions = set()

    def _approved(self, address):
        try:
            return bool(self.is_client_approved(address))
        except Exception:
            return False

    def _probe(self):
        try:
            with socket.create_connection((VNC_HOST, self._vnc_port), timeout=0.35) as connection:
                return _receive(connection, 12).startswith(b'RFB ')
        except (OSError, RuntimeError):
            return False

    def status(self):
        available = self._running and self._probe()
        return {'ok': True, 'available': bool(available), 'port': self.port,
                'websocketUrl': f'ws://{self.host}:{self.port}/desktop/ws',
                'message': 'Windows desktop is ready.' if available else self._message}

    def desktop_credentials(self):
        return desktop_credentials(self.root)

    def issue_ticket(self, client_ip):
        if not self._approved(client_ip):
            raise PermissionError('This device is not approved for desktop control.')
        if not self._running or not self._probe():
            raise RuntimeError('The local desktop server is unavailable.')
        ticket = secrets.token_urlsafe(32)
        digest = hashlib.sha256(ticket.encode('ascii')).digest()
        now = time.monotonic()
        with self._lock:
            self._tickets = {key: value for key, value in self._tickets.items() if value[1] > now}
            if len(self._tickets) >= 128:
                raise RuntimeError('Too many pending desktop sessions; try again shortly.')
            self._tickets[digest] = (client_ip, now + TICKET_SECONDS)
        return {'ticket': ticket, 'expires_in': TICKET_SECONDS,
                'cookie_name': COOKIE_NAME, 'path': COOKIE_PATH,
                'websocketUrl': f'ws://{self.host}:{self.port}/desktop/ws'}

    def _consume(self, ticket, address):
        if not ticket or len(ticket) > 128:
            return False
        try:
            digest = hashlib.sha256(ticket.encode('ascii')).digest()
        except UnicodeEncodeError:
            return False
        with self._lock:
            entry = self._tickets.get(digest)
            if entry is None or entry[0] != address or entry[1] <= time.monotonic():
                return False
            del self._tickets[digest]
            return True

    async def _websocket(self, request):
        from aiohttp import WSMsgType, web
        if (request.host != f'{self.host}:{self.port}' or request.headers.get('Origin') != self.origin
                or request.query_string or not self._approved(request.remote)):
            raise web.HTTPForbidden(text='Desktop access denied.')
        websocket = web.WebSocketResponse(protocols=('binary',), heartbeat=20, timeout=2,
                                          max_msg_size=1024 * 1024, compress=False)
        if not websocket.can_prepare(request).ok:
            raise web.HTTPBadRequest(text='A WebSocket connection is required.')
        if not self._consume(request.cookies.get(COOKIE_NAME), request.remote):
            raise web.HTTPForbidden(text='Desktop session expired; reconnect from the dashboard.')
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(VNC_HOST, self._vnc_port), 3)
        except (OSError, asyncio.TimeoutError):
            raise web.HTTPServiceUnavailable(text='The local desktop server is unavailable.')
        try:
            await websocket.prepare(request)
        except Exception:
            writer.close()
            await writer.wait_closed()
            raise
        self._sessions.add(websocket)

        async def browser_to_vnc():
            async for message in websocket:
                if not self._approved(request.remote):
                    break
                if message.type != WSMsgType.BINARY:
                    break
                writer.write(message.data)
                await writer.drain()

        async def vnc_to_browser():
            while not websocket.closed:
                data = await reader.read(65536)
                if not data:
                    break
                await websocket.send_bytes(data)

        async def approval_watch():
            deadline = time.monotonic() + 3600
            while self._approved(request.remote) and time.monotonic() < deadline:
                await asyncio.sleep(1)

        tasks = [asyncio.create_task(job()) for job in (browser_to_vnc, vnc_to_browser, approval_watch)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            await websocket.close()
            self._sessions.discard(websocket)
        return websocket

    async def _launch(self):
        from aiohttp import web
        application = web.Application()
        application.router.add_get('/desktop/ws', self._websocket)
        self._runner = web.AppRunner(application, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self._running = True
        self._message = 'Start the local desktop server to connect.'

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._launch())
        except Exception:
            self._message = 'Desktop bridge could not start; check its port and Python dependencies.'
            self._ready.set()
            if self._runner:
                self._loop.run_until_complete(self._runner.cleanup())
            self._loop.close()
            return
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    def start(self):
        if self._thread and self._thread.is_alive():
            return self.status()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name='MAIC Desktop Bridge', daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self.status()

    async def _shutdown(self):
        await asyncio.gather(*(session.close() for session in list(self._sessions)), return_exceptions=True)
        if self._runner:
            await self._runner.cleanup()

    def stop(self):
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=4)
            except (TimeoutError, RuntimeError):
                pass
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=2)
        self._running = False
        with self._lock:
            self._tickets.clear()
        self._message = 'Desktop bridge is stopped.'


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--configure-vnc', action='store_true')
    parser.add_argument('--verify-rfb', action='store_true')
    options = parser.parse_args()
    workspace = Path(__file__).resolve().parent
    try:
        result = configure_vnc(workspace) if options.configure_vnc else verify_rfb(workspace) if options.verify_rfb else {'ok': False, 'message': 'Choose a setup or verification command.'}
        print(json.dumps(result))
    except Exception:
        # Do not print exception arguments from credential-handling operations.
        print(json.dumps({'ok': False, 'message': 'Desktop setup or verification failed; check the local server configuration.'}))
        raise SystemExit(1)
