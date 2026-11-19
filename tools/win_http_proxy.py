import socket, threading, select, sys, time
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899

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
            if not chunk: return
            data += chunk
            if len(data) > 65536: break
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        method, target, ver = lines[0].split(b" ")[:3]
        if method.upper() == b"CONNECT":
            host, _, port = target.rpartition(b":")
            up = socket.create_connection((host.decode(), int(port)), 15)
            c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest: up.sendall(rest)
        else:
            if target.startswith(b"http://"):
                hp = target[7:].split(b"/", 1)
                hostport = hp[0]
                path = b"/" + (hp[1] if len(hp) > 1 else b"")
            else:
                hostport = lines[1].split(b": ", 1)[1] if len(lines) > 1 else b""
                path = target
            host, _, port = hostport.rpartition(b":")
            if not port: port = b"80"
            up = socket.create_connection((host.decode(), int(port)), 15)
            newlines = [lines[0].split(b" ")[0] + b" " + path + b" " + ver]
            skip = False
            for ln in lines[1:]:
                if ln.lower().startswith(b"proxy-connection"): continue
                newlines.append(ln)
            up.sendall(b"\r\n".join(newlines) + b"\r\n\r\n" + rest)
        copy(c, up)
    except Exception as e:
        print("err", e, flush=True)
        try: c.close()
        except Exception: pass

s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", PORT)); s.listen(64)
print("proxy on 0.0.0.0:%d" % PORT, flush=True)
while True:
    c, a = s.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()