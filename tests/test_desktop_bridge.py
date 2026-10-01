"""Exercise the actual WebSocket gate and binary TCP relay, not mock policies."""
import asyncio
import hashlib
from pathlib import Path
import socket
import socketserver
import struct
import threading
import time

import aiohttp
import pytest

from desktop_bridge import COOKIE_NAME, DesktopBridge, verify_rfb


class _EchoRfb(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.sendall(b'RFB 003.008\n')
            while True:
                data = self.request.recv(65536)
                if not data:
                    return
                self.request.sendall(data)
        except OSError:
            pass


@pytest.fixture
def prepared_bridge(tmp_path):
    backend = socketserver.ThreadingTCPServer(('127.0.0.1', 0), _EchoRfb)
    backend.daemon_threads = True
    worker = threading.Thread(target=backend.serve_forever, daemon=True)
    worker.start()
    approved = {'127.0.0.1', '192.168.0.180'}
    bridge = DesktopBridge(tmp_path, '127.0.0.1', 'http://127.0.0.1:8840',
                           lambda address: address in approved, port=0)
    bridge._vnc_port = backend.server_address[1]  # Test fixture only, never HTTP input.
    assert bridge.start()['available']
    try:
        yield bridge, approved
    finally:
        bridge.stop()
        backend.shutdown()
        backend.server_close()
        worker.join(timeout=2)


def _headers(bridge, ticket=None):
    headers = {'Origin': bridge.origin}
    if ticket:
        headers['Cookie'] = f'{COOKIE_NAME}={ticket}'
    return headers


async def _denied(bridge, headers, suffix=''):
    async with aiohttp.ClientSession() as client:
        try:
            await client.ws_connect(bridge.status()['websocketUrl'] + suffix,
                                    headers=headers, protocols=('binary',))
        except aiohttp.WSServerHandshakeError as error:
            assert error.status == 403
            return
        raise AssertionError('The protected WebSocket accepted a forbidden request.')


def test_binary_relay_and_single_use_ticket(prepared_bridge):
    bridge, _ = prepared_bridge
    token = bridge.issue_ticket('127.0.0.1')['ticket']

    async def scenario():
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(bridge.status()['websocketUrl'],
                                         headers=_headers(bridge, token), protocols=('binary',)) as websocket:
                initial = await websocket.receive(timeout=2)
                assert initial.type == aiohttp.WSMsgType.BINARY
                assert initial.data == b'RFB 003.008\n'
                await websocket.send_bytes(b'RFB 003.008\n')
                reply = await websocket.receive(timeout=2)
                assert reply.data == b'RFB 003.008\n'
        await _denied(bridge, _headers(bridge, token))
    asyncio.run(scenario())


@pytest.mark.parametrize('violation', ['missing-cookie', 'wrong-origin', 'wrong-host',
                                       'unapproved-ip', 'expired', 'wrong-client-ip',
                                       'arbitrary-upstream'])
def test_access_boundaries(prepared_bridge, violation):
    bridge, approved = prepared_bridge
    issued_ip = '192.168.0.180' if violation == 'wrong-client-ip' else '127.0.0.1'
    token = bridge.issue_ticket(issued_ip)['ticket']
    headers = _headers(bridge, token)
    suffix = ''
    if violation == 'missing-cookie':
        headers.pop('Cookie')
    elif violation == 'wrong-origin':
        headers['Origin'] = 'http://unrelated.example'
    elif violation == 'wrong-host':
        headers['Host'] = f'other.example:{bridge.port}'
    elif violation == 'unapproved-ip':
        approved.remove('127.0.0.1')
    elif violation == 'expired':
        key = hashlib.sha256(token.encode('ascii')).digest()
        with bridge._lock:
            bridge._tickets[key] = (issued_ip, time.monotonic() - 1)
    elif violation == 'arbitrary-upstream':
        suffix = '?target=192.168.0.1:22'
    asyncio.run(_denied(bridge, headers, suffix))


def test_ticket_issuance_denies_unapproved_device(prepared_bridge):
    bridge, _ = prepared_bridge
    with pytest.raises(PermissionError):
        bridge.issue_ticket('192.168.0.99')


def test_active_session_closes_after_approval_revoked(prepared_bridge):
    bridge, approved = prepared_bridge
    token = bridge.issue_ticket('127.0.0.1')['ticket']

    async def scenario():
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(bridge.status()['websocketUrl'],
                                         headers=_headers(bridge, token), protocols=('binary',)) as websocket:
                await websocket.receive(timeout=2)
                approved.remove('127.0.0.1')
                result = await websocket.receive(timeout=3)
                assert result.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    asyncio.run(scenario())


def test_status_has_no_tickets_or_credentials(prepared_bridge):
    bridge, _ = prepared_bridge
    token = bridge.issue_ticket('127.0.0.1')['ticket']
    status = bridge.status()
    assert status['available']
    assert set(status) == {'ok', 'available', 'port', 'websocketUrl', 'message'}
    assert not any(token in str(value) for value in status.values())
    assert '?' not in status['websocketUrl']


def test_real_desktop_auth_and_framebuffer_when_setup_exists():
    root = Path(__file__).resolve().parents[1]
    if not (root / '.runtime' / 'desktop' / 'vnc-password.dpapi').is_file():
        pytest.skip('No local desktop installation was prepared.')
    try:
        with socket.create_connection(('127.0.0.1', 5900), timeout=0.2):
            pass
    except OSError:
        pytest.skip('The local desktop server is stopped.')
    result = verify_rfb(root)
    assert result['authenticated'] and result['framebufferUpdate']
    assert result['width'] > 0 and result['height'] > 0


def test_real_desktop_rejects_wrong_password_when_setup_exists():
    root = Path(__file__).resolve().parents[1]
    if not (root / '.runtime' / 'desktop' / 'vnc-password.dpapi').is_file():
        pytest.skip('No local desktop installation was prepared.')
    try:
        connection = socket.create_connection(('127.0.0.1', 5900), timeout=0.2)
    except OSError:
        pytest.skip('The local desktop server is stopped.')
    from desktop_bridge import _receive
    with connection:
        connection.settimeout(2)
        assert _receive(connection, 12) == b'RFB 003.008\n'
        connection.sendall(b'RFB 003.008\n')
        count = _receive(connection, 1)[0]
        assert 2 in _receive(connection, count)
        connection.sendall(b'\x02')
        _receive(connection, 16)
        connection.sendall(bytes(16))
        assert struct.unpack('>I', _receive(connection, 4))[0] != 0
