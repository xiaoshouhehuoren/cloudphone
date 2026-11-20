import socket, threading, select, sys, gzip, json, urllib.parse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899

def send_json(c, obj, status=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    reason = "OK" if status == 200 else "Error"
    c.sendall(("HTTP/1.1 %d %s\r\n" % (status, reason)).encode())
    c.sendall(b"Content-Type: application/json\r\n")
    c.sendall(b"Content-Length: " + str(len(body)).encode() + b"\r\n")
    c.sendall(b"Connection: close\r\n\r\n")
    c.sendall(body)

def mock_response(path, body):
    try:
        if body:
            decoded = gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
            req = json.loads(decoded.decode("utf-8"))
        else:
            req = {}
    except Exception:
        req = {}
    if path.endswith("/wechat-identity/init"):
        data = {"ok": True, "account_id": req.get("account_id", "")}
    elif path.endswith("/heartbeat"):
        data = {"ok": True, "commands_enabled": False, "automatic_retry": False}
    elif path.endswith("/wechat-contacts/digest"):
        data = {"ok": True, "account_id": req.get("account_id", ""), "changed": False,
                "server_contact_count": req.get("contact_count", 0),
                "server_digest": req.get("contacts_digest", "")}
    elif path.endswith("/wechat-contacts/sync"):
        data = {"ok": True, "account_id": req.get("account_id", ""),
                "contact_count": req.get("contact_count", len(req.get("contacts", []))),
                "contacts_digest": req.get("contacts_digest", "")}
    elif path.endswith("/messages"):
        data = {"ok": True, "event_id": req.get("event_id", ""), "persisted": True,
                "duplicate": False, "content_authorized": True}
    else:
        data = {"ok": True}
    return {"code": 200, "msg": "ok", "data": data}

def copy(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 30)
            if not r:
                break
            for s in r:
                d = s.recv(65536)
                if not d:
                    return
                (b if s is a else a).sendall(d)
    except Exception:
        pass
    finally:
        try: a.close()
        except Exception: pass
        try: b.close()
        except Exception: pass

def handle(c):
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = c.recv(4096)
            if not chunk:
                return
            data += chunk
            if len(data) > 65536:
                break
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        method, target, ver = lines[0].split(b" ")[:3]
        if method.upper() == b"CONNECT":
            host, _, port = target.rpartition(b":")
            up = socket.create_connection((host.decode(), int(port)), 15)
            c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                up.sendall(rest)
            copy(c, up)
            return
        # parse absolute/proxy request
        if target.startswith(b"http://"):
            u = urllib.parse.urlsplit(target.decode("utf-8", "replace"))
            host = u.hostname or ""
            port = u.port or 80
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
        else:
            hostport = b""
            for ln in lines[1:]:
                if ln.lower().startswith(b"host:"):
                    hostport = ln.split(b":", 1)[1].strip()
                    break
            host, _, port = hostport.rpartition(b":")
            if not port:
                host = hostport
                port = b"80"
            host = host.decode()
            port = int(port)
            path = target.decode("utf-8", "replace")
        headers = {}
        for ln in lines[1:]:
            if b":" in ln:
                k, v = ln.split(b":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get(b"content-length", b"0") or b"0")
        body = rest
        while len(body) < length:
            chunk = c.recv(min(65536, length - len(body)))
            if not chunk:
                break
            body += chunk
        if path.startswith("/api/channel/app/"):
            print("mock", path, "len", len(body), "host", host, flush=True)
            send_json(c, mock_response(path, body))
            return
        up = socket.create_connection((host, port), 15)
        newlines = [lines[0].split(b" ")[0] + b" " + path.encode() + b" " + ver]
        for ln in lines[1:]:
            if ln.lower().startswith(b"proxy-connection"):
                continue
            newlines.append(ln)
        up.sendall(b"\r\n".join(newlines) + b"\r\n\r\n" + body)
        copy(c, up)
    except Exception as e:
        print("err", repr(e), flush=True)
        try: c.close()
        except Exception: pass

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", PORT))
s.listen(64)
print("mock proxy on 0.0.0.0:%d" % PORT, flush=True)
while True:
    c, a = s.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()