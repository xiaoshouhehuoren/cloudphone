#!/usr/bin/env python3
"""在 OTA 服务器上给 /opt/workphone-ota/server.py 打上带 Basic 鉴权的 G98 相机代理。

背景：公网 sales-ck.kuaihuoyun.com -> Kong -> prod:80(systemd-socket-proxyd) ->
127.0.0.1:3001 的 server.py；该机 nginx 处于 inactive，所以只能在 server.py 里做代理。
板子经 SSH 反向隧道把两个端口映射到 prod 本机（见板子
/etc/systemd/system/g98-cam-tunnel.service）：

  板子 8888 mediamtx HLS  ->  prod 127.0.0.1:18888   前缀 /cam/<path>/index.m3u8
  板子 8099 dashboard     ->  prod 127.0.0.1:18889   /camctl 页面 + /api/cam/*

外网看云手机/相机画面走 /cam/（HLS，延迟 2–6s）；手机在外网把画面推上来走
/camctl + /api/cam/frame（JPEG 降级通道，dashboard 侧 camd kind=jpeg）。
WebRTC/WHEP 不在这里代理：它要 UDP 8189，七层 HTTPS 反代搬不动。

用法（在 prod 上，root）：
    python3 ota_cam_proxy.py <cam-key>
<cam-key> 写入 /etc/g98/cam.key（0600），Basic 用户名固定 g98。
dashboard 的 /api/cam/* 需要 g98k cookie，脚本从 /etc/g98/dash.token 读取并在
代理转发时注入；该 token 需事先写好（0600），内容等于板子 /etc/g98/cloudphone.token。

脚本幂等：已是最新版本输出 ALREADY_PATCHED；v1（只有 HLS）会就地升级到 v2。
"""
import os
import shutil
import sys
import time

P = "/opt/workphone-ota/server.py"

CONSTS_V1 = '''
CAM_PREFIX = '/cam/'
CAM_UPSTREAM = ('127.0.0.1', 18888)
CAM_KEY_FILE = '/etc/g98/cam.key'


def _cam_expected_auth():
    try:
        with open(CAM_KEY_FILE, 'r', encoding='utf-8') as fh:
            key = fh.read().strip()
    except OSError:
        return None
    if not key:
        return None
    return 'Basic ' + base64.b64encode(('g98:' + key).encode()).decode()


class OtaHandler(SimpleHTTPRequestHandler):'''

CONSTS_V2 = '''CAM_PAGE_HOST = ('127.0.0.1', 18889)
CAM_API_ALLOW = ('/api/cam/frame', '/api/cam/start', '/api/cam/stop',
                 '/api/cam/state', '/api/cam/status', '/api/cam/orient',
                 '/api/cam/geom')
DASH_TOKEN_FILE = '/etc/g98/dash.token'


def _dash_token():
    try:
        with open(DASH_TOKEN_FILE, 'r', encoding='utf-8') as fh:
            return fh.read().strip()
    except OSError:
        return ''


def _cam_expected_auth():'''

HOOK_V2 = '''        qs = ('?' + parsed.query) if parsed.query else ''
        if path.startswith(CAM_PREFIX):
            self._proxy_cam(path[len('/cam'):] or '/' + qs)
            return False
        if path == '/camctl' or path.startswith('/camctl/'):
            self._proxy_cam('/cam' + qs, upstream=CAM_PAGE_HOST,
                            inject_cookie=True)
            return False
        if path.startswith(CAM_API_ALLOW):
            self._proxy_cam(path + qs, upstream=CAM_PAGE_HOST,
                            inject_cookie=True)
            return False
'''

METHOD_V2 = '''    def _proxy_cam(self, target, upstream=CAM_UPSTREAM, inject_cookie=False):
        expected = _cam_expected_auth()
        got = self.headers.get('Authorization') or ''
        if not expected or not hmac.compare_digest(expected, got):
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="g98 cam"')
            self.send_header('Content-Length', '0')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
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
        conn = http.client.HTTPConnection(upstream[0], upstream[1], timeout=30)
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
                block = resp.read(65536)
                if not block:
                    break
                self.wfile.write(block)
                self.wfile.flush()
        except Exception:
            try:
                self.send_error(502, 'cam upstream unavailable')
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

'''

DO_POST = '''    def do_POST(self) -> None:  # noqa: N802
        if not self._rewrite_path():
            return
        self.send_error(405)

'''


def patch(src):
    steps = []
    if 'import http.client' not in src:
        src = src.replace(
            'from urllib.parse import parse_qs, urlsplit',
            'import base64\nimport hmac\nimport http.client\n'
            'from urllib.parse import parse_qs, urlsplit',
            1)
        steps.append('imports')
    if 'CAM_PREFIX' not in src:
        anchor = '\n\nclass OtaHandler(SimpleHTTPRequestHandler):'
        if anchor not in src:
            raise SystemExit('anchor class OtaHandler not found')
        src = src.replace(anchor, CONSTS_V1, 1)
        steps.append('v1-consts')
    if 'CAM_PAGE_HOST' not in src:
        src = src.replace('def _cam_expected_auth():', CONSTS_V2, 1)
        steps.append('v2-consts')
    if "'/camctl'" not in src:
        old_hook = ("        if path.startswith(CAM_PREFIX):\n"
                    "            self._proxy_cam(path)\n"
                    "            return False\n")
        if old_hook in src:
            src = src.replace(old_hook, HOOK_V2, 1)
        else:
            src = src.replace("        if path == '/health':",
                              HOOK_V2 + "        if path == '/health':", 1)
        steps.append('v2-hook')
    if 'inject_cookie' not in src:
        start = src.index('    def _proxy_cam(self', src.index('class OtaHandler'))
        end = src.index('    def do_GET(self)', start)
        src = src[:start] + METHOD_V2 + src[end:]
        steps.append('v2-method')
    if 'def do_POST(self)' not in src:
        src = src.replace('    def do_GET(self) -> None:  # noqa: N802',
                          DO_POST + '    def do_GET(self) -> None:  # noqa: N802', 1)
        steps.append('do_POST')
    return src, steps


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else ''
    if not key:
        print('usage: ota_cam_proxy.py <cam-key>')
        return 2
    src = open(P, encoding='utf-8').read()
    new, steps = patch(src)
    if steps:
        shutil.copy2(P, P + '.bak-cam-' + time.strftime('%Y%m%d-%H%M%S'))
        with open(P, 'w', encoding='utf-8') as fh:
            fh.write(new)
        print('PATCHED ' + ','.join(steps))
    else:
        print('ALREADY_PATCHED')
    os.makedirs('/etc/g98', exist_ok=True)
    with open('/etc/g98/cam.key', 'w', encoding='utf-8') as fh:
        fh.write(key.strip())
    os.chmod('/etc/g98/cam.key', 0o600)
    print('KEY_WRITTEN')
    if steps:
        print('now run: systemctl restart workphone-ota')
    return 0


if __name__ == '__main__':
    sys.exit(main())
