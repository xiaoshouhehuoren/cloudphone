#!/usr/bin/env python3
"""在 OTA 服务器上给 /opt/workphone-ota/server.py 增加带 Basic 鉴权的 /cam/ 反向代理。

背景：公网 sales-ck.kuaihuoyun.com -> Kong -> prod:80(systemd-socket-proxyd) ->
127.0.0.1:3001 的 server.py；该机 nginx 处于 inactive，所以只能在 server.py 里做代理。
板子 8888（mediamtx HLS）经 SSH 反向隧道映射到 prod 127.0.0.1:18888，见板子上的
/etc/systemd/system/g98-cam-tunnel.service。

用法（在 prod 上，root）：python3 ota_cam_proxy.py <key>
key 会写入 /etc/g98/cam.key（0600）。脚本幂等：已打过补丁会输出 ALREADY_PATCHED。
"""
import base64
import os
import shutil
import sys
import time

P = "/opt/workphone-ota/server.py"


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    if not key:
        print("usage: ota_cam_proxy.py <key>")
        return 2
    src = open(P, encoding="utf-8").read()
    if "CAM_PREFIX" in src:
        print("ALREADY_PATCHED")
    else:
        shutil.copy2(P, P + ".bak-cam-" + time.strftime("%Y%m%d-%H%M%S"))
        src = src.replace(
            "from urllib.parse import parse_qs, urlsplit",
            "import base64\nimport hmac\nimport http.client\n"
            "from urllib.parse import parse_qs, urlsplit",
            1)
        consts = '''
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
        src = src.replace("\n\nclass OtaHandler(SimpleHTTPRequestHandler):", consts, 1)
        hook = """        if path.startswith(CAM_PREFIX):
            self._proxy_cam(path)
            return False
"""
        src = src.replace("        if path == '/health':", hook + "        if path == '/health':", 1)
        method = '''
    def _proxy_cam(self, path):
        expected = _cam_expected_auth()
        got = self.headers.get('Authorization') or ''
        if not expected or not hmac.compare_digest(expected, got):
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="g98 cam"')
            self.send_header('Content-Length', '0')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        target = path[len('/cam'):] or '/'
        if '?' in self.path:
            target += '?' + self.path.split('?', 1)[1]
        conn = http.client.HTTPConnection(CAM_UPSTREAM[0], CAM_UPSTREAM[1], timeout=20)
        try:
            conn.request('GET', target, headers={'Host': '127.0.0.1:18888',
                                                 'Accept': '*/*'})
            resp = conn.getresponse()
            self.send_response(resp.status)
            for name in ('Content-Type', 'Content-Length', 'Last-Modified',
                         'Accept-Ranges', 'Content-Range'):
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

    def do_GET(self) -> None:  # noqa: N802'''
        src = src.replace("\n    def do_GET(self) -> None:  # noqa: N802", method, 1)
        open(P, "w", encoding="utf-8").write(src)
        print("PATCHED")
    os.makedirs("/etc/g98", exist_ok=True)
    with open("/etc/g98/cam.key", "w", encoding="utf-8") as fh:
        fh.write(key)
    os.chmod("/etc/g98/cam.key", 0o600)
    print("KEY_WRITTEN")
    return 0


if __name__ == '__main__':
    sys.exit(main())
