#!/usr/bin/env python3
"""G98 per-container HTTP/HTTPS proxy manager.

Each Android container gets its own listen port on the Windows LAN address.
The container is identified by that port (and by an optional Basic-auth token).
Traffic is logged per container so a container cannot hide behind another's proxy.

Usage:
    pythonw g98-proxy.py
    python g98-proxy.py --foreground

Control API (localhost only):
    GET  http://127.0.0.1:18080/status
    POST http://127.0.0.1:18080/add    {"name":"m8","port":18083}
    POST http://127.0.0.1:18080/remove {"name":"m8"}
    POST http://127.0.0.1:18080/reload
"""

import argparse
import base64
import hmac
import json
import os
import re
import select
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "g98-proxy.json"
DEFAULT_LOG = HERE / "g98-proxy.log"
BIND_HOST = "192.168.99.1"
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 18080
MAX_HEAD = 64 * 1024
BUFSIZE = 64 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Log:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, **fields):
        fields.setdefault("ts", now())
        line = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


LOG = Log(DEFAULT_LOG)


def parse_authority(value):
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else 443
        return host, port
    if ":" in value:
        host, port = value.rsplit(":", 1)
        return host, int(port)
    return value, 443


def recv_head(sock):
    data = b""
    while b"\r\n\r\n" not in data and b"\n\n" not in data:
        chunk = sock.recv(BUFSIZE)
        if not chunk:
            return data, b""
        data += chunk
        if len(data) > MAX_HEAD:
            raise ValueError("header too large")
    idx = data.find(b"\r\n\r\n")
    sep = 4
    if idx < 0:
        idx = data.find(b"\n\n")
        sep = 2
    return data[:idx], data[idx + sep:]


def parse_head(head):
    text = head.decode("latin-1")
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or not lines[0].strip():
        raise ValueError("empty request")
    parts = lines[0].split()
    if len(parts) < 2:
        raise ValueError("bad request line")
    method, target = parts[0], parts[1]
    headers = []
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers.append((name.strip(), value.strip()))
    return method, target, headers


def header_get(headers, name):
    name = name.lower()
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


def filter_headers(headers):
    drop = {
        "proxy-connection", "proxy-authorization", "connection", "keep-alive",
        "te", "trailer", "upgrade",
    }
    return [(k, v) for k, v in headers if k.lower() not in drop]


def relay(a, b, stats):
    readers = {a, b}
    try:
        while readers:
            r, _, x = select.select(list(readers), [], list(readers), 120)
            if x:
                break
            if not r:
                break
            for src in r:
                data = src.recv(BUFSIZE)
                if not data:
                    readers.discard(src)
                    dst = b if src is a else a
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                dst = b if src is a else a
                dst.sendall(data)
                if src is a:
                    stats["up"] += len(data)
                else:
                    stats["down"] += len(data)
    except (OSError, ValueError):
        return


def http_error(sock, code, reason):
    body = (reason + "\n").encode()
    try:
        sock.sendall(
            ("HTTP/1.1 %d %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (code, reason, len(body))).encode()
            + body
        )
    except OSError:
        pass


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(30)
        client = "%s:%s" % self.client_address[:2]
        server = self.server
        container = server.container_name
        stats = {"up": 0, "down": 0}
        method = target = ""
        status = 0
        err = ""
        upstream = None
        try:
            head, rest = recv_head(sock)
            if not head:
                return
            method, target, headers = parse_head(head)
            if server.auth:
                supplied = header_get(headers, "Proxy-Authorization") or ""
                expected = "Basic " + server.auth
                if not hmac.compare_digest(supplied.strip(), expected):
                    status = 407
                    sock.sendall(
                        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                        b"Proxy-Authenticate: Basic realm=\"g98\"\r\n"
                        b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    return
            if method.upper() == "CONNECT":
                host, port = parse_authority(target)
                upstream = socket.create_connection((host, port), timeout=30)
                sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
                    stats["up"] += len(rest)
                status = 200
                relay(sock, upstream, stats)
            else:
                parsed = urllib.parse.urlsplit(target)
                if parsed.scheme and parsed.scheme.lower() != "http":
                    status = 400
                    http_error(sock, 400, "unsupported scheme")
                    return
                if parsed.scheme:
                    host = parsed.hostname
                    port = parsed.port or 80
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query
                else:
                    hostport = header_get(headers, "Host")
                    if not hostport:
                        status = 400
                        http_error(sock, 400, "missing Host")
                        return
                    host, port = parse_authority(hostport)
                    path = target
                if not host:
                    status = 400
                    http_error(sock, 400, "missing host")
                    return
                length = int(header_get(headers, "Content-Length") or 0)
                if header_get(headers, "Transfer-Encoding"):
                    status = 501
                    http_error(sock, 501, "chunked request not supported")
                    return
                body = rest
                while len(body) < length:
                    chunk = sock.recv(min(BUFSIZE, length - len(body)))
                    if not chunk:
                        break
                    body += chunk
                out = [("%s %s HTTP/1.1" % (method, path)).encode("latin-1")]
                for k, v in filter_headers(headers):
                    out.append(("%s: %s" % (k, v)).encode("latin-1"))
                out.append(b"Connection: close")
                out.append(b"")
                out.append(body[:length])
                upstream = socket.create_connection((host, port), timeout=30)
                upstream.sendall(b"\r\n".join(out))
                status = 200
                relay(upstream, sock, stats)
        except Exception as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
            if not status:
                status = 502
                http_error(sock, 502, "bad gateway")
        finally:
            for s in (upstream, sock):
                try:
                    if s is not None:
                        s.close()
                except OSError:
                    pass
            LOG.write(container=container, client=client, method=method, target=target,
                      status=status, up=stats["up"], down=stats["down"], error=err)


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, auth=None, container_name="unknown"):
        self.auth = auth
        self.container_name = container_name
        super().__init__(addr, ProxyHandler)


class Manager:
    def __init__(self, config_path, log_path=None):
        self.config_path = Path(config_path)
        self.lock = threading.RLock()
        self.servers = {}
        self.config = {}
        self.started = time.time()
        if log_path:
            global LOG
            LOG = Log(log_path)

    def load(self):
        if self.config_path.exists():
            self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        else:
            self.config = {"bind_host": BIND_HOST, "control_host": CONTROL_HOST,
                           "control_port": CONTROL_PORT, "containers": {}}
            self.save()

    def save(self):
        tmp = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.config_path)

    def bind_host(self):
        return self.config.get("bind_host", BIND_HOST)

    def start_listener(self, name, spec):
        with self.lock:
            if name in self.servers:
                return
            port = int(spec["port"])
            auth = spec.get("auth")
            if auth:
                auth = base64.b64encode(str(auth).encode()).decode()
            try:
                srv = ProxyServer((self.bind_host(), port), auth=auth, container_name=name)
            except OSError as exc:
                LOG.write(container=name, event="listen_failed", port=port, error=str(exc))
                return
            th = threading.Thread(target=srv.serve_forever, name="proxy-" + name, daemon=True)
            th.start()
            self.servers[name] = (srv, th)
            LOG.write(container=name, event="listening", bind=self.bind_host(), port=port,
                      auth=bool(auth))

    def stop_listener(self, name):
        with self.lock:
            item = self.servers.pop(name, None)
        if not item:
            return
        srv, th = item
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        LOG.write(container=name, event="stopped")

    def start_all(self):
        for name, spec in (self.config.get("containers") or {}).items():
            if spec.get("enabled", True):
                self.start_listener(name, spec)

    def add(self, name, port, auth=None):
        name = str(name)
        if not NAME_RE.match(name):
            raise ValueError("invalid container name")
        port = int(port)
        if port < 1024 or port > 65535:
            raise ValueError("port out of range")
        with self.lock:
            for other, spec in (self.config.get("containers") or {}).items():
                if int(spec.get("port", -1)) == port and other != name:
                    raise ValueError("port already assigned to " + other)
            self.config.setdefault("containers", {})[name] = {"port": port, "enabled": True}
            if auth:
                self.config["containers"][name]["auth"] = auth
            self.save()
            self.start_listener(name, self.config["containers"][name])

    def remove(self, name):
        with self.lock:
            self.config.get("containers", {}).pop(name, None)
            self.save()
            self.stop_listener(name)

    def status(self):
        with self.lock:
            return {
                "ok": True,
                "pid": os.getpid(),
                "started": self.started,
                "bind_host": self.bind_host(),
                "containers": {
                    name: {
                        "port": int(spec.get("port")),
                        "enabled": bool(spec.get("enabled", True)),
                        "listening": name in self.servers,
                        "auth": bool(spec.get("auth")),
                    }
                    for name, spec in (self.config.get("containers") or {}).items()
                },
            }


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline().decode("latin-1").strip()
            if not line:
                return
            parts = line.split()
            method, path = parts[0].upper(), parts[1] if len(parts) > 1 else "/"
            headers = {}
            while True:
                h = self.rfile.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                text = h.decode("latin-1")
                if ":" in text:
                    k, v = text.split(":", 1)
                    headers[k.lower()] = v.strip()
            body = b""
            length = int(headers.get("content-length", "0") or 0)
            if length:
                body = self.rfile.read(length)
            mgr = self.server.manager
            if method == "GET" and path == "/status":
                self._json(200, mgr.status())
                return
            if method == "POST" and path in ("/add", "/remove", "/reload"):
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except Exception:
                    payload = {}
                if path == "/add":
                    mgr.add(payload.get("name"), payload.get("port"), payload.get("auth"))
                elif path == "/remove":
                    mgr.remove(str(payload.get("name", "")))
                else:
                    with mgr.lock:
                        for name in list(mgr.servers):
                            mgr.stop_listener(name)
                        mgr.load()
                        mgr.start_all()
                self._json(200, mgr.status())
                return
            self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            try:
                self._json(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
            except Exception:
                pass

    def _json(self, code, obj):
        body = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        reason = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}.get(code, "OK")
        self.wfile.write(("HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (code, reason, len(body))).encode() + body)


class ControlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, manager):
        self.manager = manager
        super().__init__(addr, ControlHandler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--foreground", action="store_true")
    args = parser.parse_args()
    mgr = Manager(args.config, args.log)
    mgr.load()
    mgr.start_all()
    control = None
    if mgr.config.get("control_port", CONTROL_PORT):
        try:
            control = ControlServer((mgr.config.get("control_host", CONTROL_HOST),
                                     int(mgr.config.get("control_port", CONTROL_PORT))), mgr)
            threading.Thread(target=control.serve_forever, name="control", daemon=True).start()
            LOG.write(event="control_listening", host=mgr.config.get("control_host", CONTROL_HOST),
                      port=mgr.config.get("control_port", CONTROL_PORT))
        except OSError as exc:
            LOG.write(event="control_failed", error=str(exc))
    LOG.write(event="manager_started", pid=os.getpid(), config=str(args.config))
    print("g98-proxy listening; status on http://%s:%s/status" % (
        mgr.config.get("control_host", CONTROL_HOST), mgr.config.get("control_port", CONTROL_PORT)), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for name in list(mgr.servers):
            mgr.stop_listener(name)
        if control:
            control.shutdown()
            control.server_close()


if __name__ == "__main__":
    main()
