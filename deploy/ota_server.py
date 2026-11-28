from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import mimetypes
import base64
import json
import secrets
import threading
import time
import hmac
import http.client
import os
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
LEGACY_ROOT = Path('/opt/sales-partner-ota')
APPS_MANIFEST_ROUTE = '/rom-ota/oneplus9/stable/apps.json'
APPS_MANIFEST_PATH = ROOT / 'oneplus9' / 'stable' / 'apps.json'
OTA_MANIFEST_PATH = ROOT / 'oneplus9' / 'stable' / 'manifest.json'
DELTA_MANIFEST_PATH = ROOT / 'oneplus9' / 'stable' / 'manifest-delta.json'
MAX_MANIFEST_BYTES = 256 * 1024
DEFAULT_DEVICE_PROFILE = 'oneplus9-lemonade-v1'
DEFAULT_FINGERPRINT = 'OnePlus/OnePlus9/OnePlus9:13/TP1A.220905.001/R.159cc58-4e9e-182b9f:user/release-keys'

PORTAL_HOST = ('127.0.0.1', 18889)
PORTAL_PATH = Path(__file__).resolve().parent / 'portal.html'
DASH_TOKEN_FILE = '/etc/g98/dash.token'
REMOTE_STATE = os.path.join(str(ROOT), 'state', 'remote_state.json')
REMOTE_TOKEN_TTL = 300
REMOTE_SESSION_TTL = 1800
REMOTE_MAX_SESSIONS = 3
PORTAL_DENY = ('/api/cam/file', '/api/cam/files', '/api/cam/qr')
PORTAL_API_EXACT = ('/api/tap', '/api/swipe', '/api/key', '/api/text',
                    '/api/longpress', '/api/touch', '/api/snapshot',
                    '/api/cam/state')
REMOTE_LOCK = threading.Lock()


def _dash_token():
    try:
        with open(DASH_TOKEN_FILE, 'r', encoding='utf-8') as fh:
            return fh.read().strip()
    except OSError:
        return ''


def _remote_load():
    try:
        with open(REMOTE_STATE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault('tokens', {})
    data.setdefault('sessions', {})
    now = time.time()
    for bucket in ('tokens', 'sessions'):
        for key in [k for k, v in data[bucket].items() if float(v.get('exp', 0)) < now]:
            data[bucket].pop(key, None)
    return data


def _remote_save(data):
    tmp = REMOTE_STATE + '.tmp'
    try:
        os.makedirs(os.path.dirname(REMOTE_STATE), exist_ok=True)
    except OSError:
        pass
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, REMOTE_STATE)


def _portal_html(sess):
    try:
        page = PORTAL_PATH.read_text(encoding='utf-8')
    except OSError:
        return '<!doctype html><meta charset="utf-8"><p>portal.html missing'
    page = page.replace('@@DEV@@', str(sess.get('device') or 'wx1'))
    page = page.replace('@@NODE@@', str(sess.get('node') or '/dev/video20'))
    page = page.replace('@@FACING@@', str(sess.get('facing') or 'environment'))
    return page


class OtaHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _rewrite_path(self) -> bool:
        parsed = urlsplit(self.path)
        path = parsed.path
        self._ota_query = parse_qs(parsed.query)
        self._is_apps_manifest = path == APPS_MANIFEST_ROUTE
        self._is_ota_manifest = path == '/rom-ota/oneplus9/stable/manifest.json'
        qs = ('?' + parsed.query) if parsed.query else ''
        if path.startswith('/r/'):
            self._portal_consume(path[len('/r/'):].strip('/'))
            return False
        if path == '/p':
            self._portal_page()
            return False
        if path.startswith('/api/remote/'):
            self._remote_api(path)
            return False
        if self._portal_session():
            if path == '/cam' or path.startswith('/stream/') or path in PORTAL_API_EXACT \
                    or (path.startswith('/api/cam/') and not path.startswith(PORTAL_DENY)):
                self._proxy(path + qs, PORTAL_HOST, inject_cookie=True)
                return False
        if path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(b'ok\n')
            return False
        if path.startswith('/rom-ota/'):
            self.directory = str(ROOT)
            self.path = path[len('/rom-ota'):] or '/'
            return True
        if path.startswith('/rom/oneplus9/'):
            self.directory = str(LEGACY_ROOT)
            self.path = path[len('/rom'):] or '/'
            return True
        if path == '/rom/health':
            self.path = '/health'
            return False
        if path.startswith('/rom/'):
            self.directory = str(LEGACY_ROOT)
            self.path = path[len('/rom'):] or '/'
            return True
        self.send_error(404)
        return False

    def _serve_ota_manifest(self) -> bool:
        if not getattr(self, '_is_ota_manifest', False):
            return False
        try:
            import json
            requested_sequence = self._ota_query.get('sequence', [''])[0]
            use_delta = False
            if not self._ota_query.get('full') and DELTA_MANIFEST_PATH.is_file():
                try:
                    delta_payload = json.loads(DELTA_MANIFEST_PATH.read_text(encoding='utf-8'))
                    use_delta = str(delta_payload.get('delta_from_sequence')) == requested_sequence
                except Exception:
                    use_delta = False
            manifest_path = DELTA_MANIFEST_PATH if use_delta else OTA_MANIFEST_PATH
            body = manifest_path.read_bytes()
            payload = json.loads(body.decode('utf-8'))
            if payload.get('device') != 'oneplus9':
                raise ValueError('device')
            # Keep the V39 flat contract as the sole production wire format.
            payload.setdefault('schema', 'salespartner.oneplus9/ota/v1')
            payload.setdefault('device_profile', DEFAULT_DEVICE_PROFILE)
            payload.setdefault('target_build_fingerprint', DEFAULT_FINGERPRINT)
            if 'latest_sequence' not in payload:
                payload['latest_sequence'] = payload.get('sequence', payload.get('rollback_index', 0))
            if 'ota_url' not in payload and payload.get('artifact_url'):
                payload['ota_url'] = payload['artifact_url']
            if 'sha256' not in payload and payload.get('artifact_sha256'):
                payload['sha256'] = payload['artifact_sha256']
            if 'size' not in payload and payload.get('artifact_size'):
                payload['size'] = payload['artifact_size']
            if 'rollback_index' not in payload:
                payload['rollback_index'] = payload['latest_sequence']
            body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        except (OSError, ValueError, UnicodeDecodeError, TypeError):
            self.send_error(503, 'OTA manifest invalid')
            return True
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command == 'GET':
            self.wfile.write(body)
        return True

    def _serve_apps_manifest(self) -> bool:
        if not self._is_apps_manifest:
            return False
        if not APPS_MANIFEST_PATH.is_file():
            self.send_error(404, 'app manifest unavailable')
            return True
        try:
            body = APPS_MANIFEST_PATH.read_bytes()
            if len(body) > MAX_MANIFEST_BYTES:
                self.send_error(503, 'app manifest too large')
                return True
            import json
            payload = json.loads(body.decode('utf-8'))
            if payload.get('schema') != 'salespartner.oneplus9/apps/v1' or payload.get('device') != 'oneplus9':
                self.send_error(503, 'app manifest invalid')
                return True
        except (OSError, ValueError, UnicodeDecodeError):
            self.send_error(503, 'app manifest invalid')
            return True
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command == 'GET':
            self.wfile.write(body)
        return True

    def _serve_requested_range(self) -> bool:
        starts = self._ota_query.get('part_start')
        ends = self._ota_query.get('part_end')
        if not starts or not ends:
            return False
        try:
            start = int(starts[0])
            end = int(ends[0])
        except (TypeError, ValueError):
            self.send_error(400, 'invalid OTA chunk')
            return True
        path = Path(self.directory) / self.path.lstrip('/')
        if start < 0 or end < start or not path.is_file():
            self.send_error(416, 'invalid OTA chunk')
            return True
        size = path.stat().st_size
        if start >= size:
            self.send_error(416, 'OTA chunk outside artifact')
            return True
        end = min(end, size - 1)
        length = end - start + 1
        self.send_response(206)
        self.send_header('Content-Type', mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(length))
        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command == 'GET':
            with path.open('rb') as source:
                source.seek(start)
                remaining = length
                while remaining:
                    block = source.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        return True

    def _proxy(self, target, upstream, inject_cookie=False):
        body = None
        method = (self.command or 'GET').upper()
        if method in ('POST', 'PUT', 'PATCH'):
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = 0
            if length > 0:
                body = self.rfile.read(length)
        headers = {'Host': '%s:%d' % upstream, 'Accept': '*/*'}
        ctype = self.headers.get('Content-Type')
        if ctype:
            headers['Content-Type'] = ctype
        if body is not None:
            headers['Content-Length'] = str(len(body))
        if inject_cookie:
            tok = _dash_token()
            if tok:
                headers['Cookie'] = 'g98k=' + tok
        conn = http.client.HTTPConnection(upstream[0], upstream[1], timeout=120)
        try:
            conn.request(method, target, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for name in ('Content-Type', 'Content-Length', 'Last-Modified',
                         'Accept-Ranges', 'Content-Range', 'Location'):
                value = resp.getheader(name)
                if value:
                    self.send_header(name, value)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.close_connection = True
            while True:
                block = resp.read(32768)
                if not block:
                    break
                self.wfile.write(block)
                self.wfile.flush()
        except Exception:
            try:
                self.send_error(502, 'portal upstream unavailable')
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _cookie_map(self):
        raw = self.headers.get('Cookie', '') or ''
        out = {}
        for part in raw.split(';'):
            if '=' in part:
                key, value = part.split('=', 1)
                out[key.strip()] = value.strip()
        return out

    def _portal_session(self):
        sid = self._cookie_map().get('g98r', '')
        if not sid:
            return None
        now = time.time()
        with REMOTE_LOCK:
            data = _remote_load()
            sess = data['sessions'].get(sid)
        if not sess or float(sess.get('exp', 0)) < now:
            return None
        return sess

    def _send_plain(self, code, text):
        body = text.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json_plain(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _portal_consume(self, token):
        now = time.time()
        with REMOTE_LOCK:
            data = _remote_load()
            rec = data['tokens'].pop(token, None)
            sid = ''
            if not rec or float(rec.get('exp', 0)) < now:
                rec = None
                _remote_save(data)
            else:
                while len(data['sessions']) >= REMOTE_MAX_SESSIONS:
                    oldest = min(data['sessions'],
                                 key=lambda k: float(data['sessions'][k].get('exp', 0)))
                    data['sessions'].pop(oldest, None)
                sid = secrets.token_urlsafe(16)
                data['sessions'][sid] = {'exp': now + REMOTE_SESSION_TTL,
                                         'device': rec.get('device') or 'wx1',
                                         'node': rec.get('node') or '/dev/video20',
                                         'facing': rec.get('facing') or 'environment',
                                         'created': now}
                _remote_save(data)
        if not rec:
            return self._send_plain(410, '链接已失效或已被使用，请重新生成二维码。')
        self.send_response(302)
        self.send_header('Location', '/p')
        self.send_header('Set-Cookie', 'g98r=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax'
                         % (sid, REMOTE_SESSION_TTL))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _portal_page(self):
        sess = self._portal_session()
        if not sess:
            return self._send_plain(403, '链接已过期，请重新扫码。')
        body = _portal_html(sess).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _remote_api(self, path):
        tok = self.headers.get('X-G98-Token') or ''
        expected = _dash_token()
        if not expected or not hmac.compare_digest(tok, expected):
            return self._json_plain(401, {'error': 'unauthorized'})
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        payload = {}
        if length > 0:
            try:
                payload = json.loads(self.rfile.read(length).decode('utf-8')) or {}
            except (ValueError, UnicodeDecodeError):
                payload = {}
        if path == '/api/remote/new':
            now = time.time()
            token = secrets.token_urlsafe(24)
            device = str(payload.get('device') or 'wx1')[:16]
            node = str(payload.get('node') or '/dev/video20')[:24]
            facing = 'user' if str(payload.get('facing') or '') == 'user' else 'environment'
            with REMOTE_LOCK:
                data = _remote_load()
                data['tokens'][token] = {'exp': now + REMOTE_TOKEN_TTL, 'device': device,
                                         'node': node, 'facing': facing, 'created': now}
                _remote_save(data)
            host = (self.headers.get('Host') or 'sales-ck.kuaihuoyun.com').split(':')[0]
            return self._json_plain(200, {'ok': True, 'token': token,
                                          'url': 'https://%s/r/%s' % (host, token),
                                          'expires_in': REMOTE_TOKEN_TTL,
                                          'device': device, 'node': node, 'facing': facing})
        if path == '/api/remote/revoke':
            sid = str(payload.get('sid') or '')
            with REMOTE_LOCK:
                data = _remote_load()
                gone = data['sessions'].pop(sid, None) is not None
                _remote_save(data)
            return self._json_plain(200, {'ok': True, 'revoked': gone})
        if path == '/api/remote/list':
            with REMOTE_LOCK:
                data = _remote_load()
            items = [{'sid': k, 'device': v.get('device'), 'node': v.get('node'),
                      'exp': v.get('exp')} for k, v in data['sessions'].items()]
            return self._json_plain(200, {'ok': True, 'sessions': items})
        return self._json_plain(404, {'error': 'unknown remote api'})

    def do_POST(self) -> None:  # noqa: N802
        if not self._rewrite_path():
            return
        self.send_error(405)

    def do_GET(self) -> None:  # noqa: N802
        if self._rewrite_path() and not self._serve_ota_manifest() \
                and not self._serve_apps_manifest() \
                and not self._serve_requested_range():
            super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if self._rewrite_path() and not self._serve_ota_manifest() \
                and not self._serve_apps_manifest() \
                and not self._serve_requested_range():
            super().do_HEAD()

    def log_message(self, format: str, *args) -> None:
        return


class ReusableHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True


if __name__ == '__main__':
    ReusableHttpServer(('0.0.0.0', 3001), OtaHandler).serve_forever()
