"""Bounded live HTTPS check using one automatically cleaned-up QA browser.

Run on the owner PC. Does not send desktop input, run apps, call models, change
Tailscale, or print/persist session credentials, device codes, or private data.
"""
import asyncio
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from remote_access import _origin


class PublicResolver(AbstractResolver):
    def __init__(self, hostname, addresses):
        self.hostname, self.addresses = hostname, addresses

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host != self.hostname:
            raise OSError('Only the configured HTTPS origin is allowed.')
        return [{'hostname': host, 'host': address, 'port': port, 'family': socket.AF_INET,
                 'proto': 0, 'flags': socket.AI_NUMERICHOST} for address in self.addresses]

    async def close(self):
        pass


def serve_config():
    executable = Path(__import__('os').environ['ProgramFiles']) / 'Tailscale' / 'tailscale.exe'
    value = subprocess.run([str(executable), 'serve', 'status', '--json'],
                           capture_output=True, timeout=5, check=True, text=True)
    return json.loads(value.stdout)


async def main():
    before = serve_config()
    public = _origin(json.loads((ROOT / '.runtime/remote/config.json').read_text())['publicOrigin'], public=True)
    local = _origin((ROOT / '.runtime/server.url').read_text().strip())
    host = urlsplit(public).hostname
    label = 'QA protocol check ' + secrets.token_hex(6)
    report = {'ok': False, 'checks': {}, 'cleanup': False}
    identity = None
    stage = 'public-dns'
    started = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=8)
    local_headers = {'Origin': local}
    remote_headers = {'Origin': public}
    jar = aiohttp.CookieJar()

    async def request(client, origin, path, expected=200, payload=None, headers=None, *, json_response=True):
        async with client.request('POST' if payload is not None else 'GET', origin + path,
                                  json=payload, headers=headers, allow_redirects=False) as response:
            if response.status != expected:
                raise RuntimeError('Unexpected test route status.')
            value = await response.json() if json_response else await response.read()
            return value

    try:
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as dns:
            async with dns.get('https://dns.google/resolve', params={'name': host, 'type': 'A'}) as response:
                data = await response.json()
        addresses = [item['data'] for item in data.get('Answer', []) if item.get('type') == 1
                     and ipaddress.ip_address(item['data']).is_global]
        if not addresses:
            raise RuntimeError('No public relay address was found.')
        report['checks']['publicDnsRelay'] = True
        stage = 'owner-control-session'
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as pc:
            owner = await request(pc, local, '/api/control-session', headers=local_headers)
            if not owner.get('canManageDevices'):
                raise RuntimeError('This process is not using the owner PC dashboard address.')
            local_headers['X-MAIC-Control'] = owner['token']
            resolver = PublicResolver(host, addresses)
            connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
            async with aiohttp.ClientSession(timeout=timeout, cookie_jar=jar, connector=connector) as remote:
                stage = 'public-pairing-page'
                page = await request(remote, public, '/', headers=remote_headers, json_response=False)
                if b'Request connection' not in page:
                    raise RuntimeError('Anonymous request did not reach the pairing screen.')
                await request(remote, public, '/api/control-session', 403, headers=remote_headers, json_response=False)
                report['checks']['anonymousDenied'] = True
                stage = 'own-pairing-request'
                pair = await request(remote, public, '/_remote/pair', payload={'label': label}, headers=remote_headers)
                state = await request(pc, local, '/api/remote/status', headers=local_headers)
                match = [item for item in state.get('pending', []) if item.get('label') == label and item.get('code') == pair.get('code')]
                if len(match) != 1:
                    raise RuntimeError('The unique QA request could not be identified.')
                identity = match[0]['id']
                await request(pc, local, '/api/remote/approve', payload={'id': identity}, headers=local_headers)
                stage = 'approval-cookie'
                state = await request(remote, public, '/_remote/session', headers=remote_headers)
                if state.get('state') != 'approved':
                    raise RuntimeError('The QA browser was not approved.')
                report['checks']['pairApprove'] = True
                stage = 'authenticated-dashboard-routes'
                await request(remote, public, '/', headers=remote_headers, json_response=False)
                for path in ('/app.js', '/voice.js', '/chat-media.js', '/?workspace=codex',
                             '/desktop-viewer.html', '/desktop/core/rfb.js'):
                    await request(remote, public, path, headers=remote_headers, json_response=False)
                control = await request(remote, public, '/api/control-session', headers=remote_headers)
                remote_headers['X-MAIC-Control'] = control['token']
                for path in ('/api/pc/apps', '/api/pc/state', '/api/hardware', '/api/lm/models',
                             '/api/speech/status', '/api/tts/status', '/api/desktop/status'):
                    await request(remote, public, path, headers=remote_headers)
                await request(remote, public, '/api/remote/status', 403, headers=remote_headers, json_response=False)
                report['checks']['dashboardRoutes'] = True
                report['checks']['remoteCannotManageDevices'] = True
            stage = 'new-https-connection-reconnect'
            connector = aiohttp.TCPConnector(resolver=PublicResolver(host, addresses), use_dns_cache=False)
            async with aiohttp.ClientSession(timeout=timeout, cookie_jar=jar, connector=connector) as remote:
                await request(remote, public, '/api/control-session', headers=remote_headers)
                await request(remote, public, '/api/pc/apps', headers=remote_headers)
                report['checks']['reconnectWithSameCookie'] = True
                stage = 'desktop-websocket'
                await request(remote, public, '/api/desktop/session', payload={}, headers=remote_headers)
                async with remote.ws_connect(public + '/desktop/ws', headers=remote_headers,
                                              protocols=('binary',), timeout=5, max_msg_size=65536) as desktop:
                    message = await asyncio.wait_for(desktop.receive(), 5)
                    if message.type != aiohttp.WSMsgType.BINARY or not message.data.startswith(b'RFB '):
                        raise RuntimeError('The desktop protocol banner did not arrive.')
                    report['checks']['desktopRfbHandshake'] = True
                    stage = 'revoke-own-session'
                    await request(pc, local, '/api/remote/revoke', payload={'id': identity}, headers=local_headers)
                    identity = None
                    report['cleanup'] = True
                    deadline = time.monotonic() + 3
                    closed = False
                    while time.monotonic() < deadline:
                        message = await asyncio.wait_for(desktop.receive(), max(0.01, deadline - time.monotonic()))
                        if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                            closed = True
                            break
                    if not closed:
                        raise RuntimeError('Revocation did not close the desktop.')
                await request(remote, public, '/api/pc/apps', 403, headers=remote_headers, json_response=False)
                report['checks']['revokeClosesDesktopAndDeniesApi'] = True
            report['checks']['tailscaleConfigurationUnchanged'] = serve_config() == before
            if not report['checks']['tailscaleConfigurationUnchanged']:
                raise RuntimeError('Existing Tailscale configuration changed during the check.')
            report['ok'] = True
    except asyncio.CancelledError:
        report['failedStage'] = stage
        report['errorType'] = 'OverallTimeout'
    except Exception as error:
        report['failedStage'] = stage
        report['errorType'] = type(error).__name__
    finally:
        if identity:
            try:
                async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as pc:
                    await request(pc, local, '/api/remote/revoke', payload={'id': identity}, headers=local_headers)
                report['cleanup'] = True
            except Exception:
                report['cleanup'] = False
        report['elapsedSeconds'] = round(time.monotonic() - started, 2)
    print(json.dumps(report, sort_keys=True))
    return 0 if report['ok'] else 1


async def bounded():
    try:
        return await asyncio.wait_for(main(), timeout=90)
    except Exception as error:
        print(json.dumps({'ok': False, 'failedStage': 'setup', 'errorType': type(error).__name__, 'cleanup': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(bounded()))
