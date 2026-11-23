#!/usr/bin/env python3
"""Send one command to a wx touch bridge."""
import argparse
import json
import socket
import sys

SOCK_DIR = "/run/g98"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("n", type=int)
    p.add_argument("action", choices=("tap", "down", "move", "up", "key", "raw"))
    p.add_argument("args", nargs="*")
    a = p.parse_args()
    if a.action in ("tap", "down", "move", "up"):
        if len(a.args) != 2:
            p.error("tap/down/move/up need FX FY")
        req = {"a": a.action, "fx": float(a.args[0]), "fy": float(a.args[1])}
    elif a.action == "key":
        if len(a.args) != 1:
            p.error("key needs CODE")
        req = {"a": "key", "code": int(a.args[0])}
    else:
        if len(a.args) != 1:
            p.error("raw needs a command string")
        req = {"a": "raw", "line": a.args[0]}
    path = "%s/wx-touch-%d.sock" % (SOCK_DIR, a.n)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
        s.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    text = data.decode("utf-8", "replace").strip()
    print(text)
    try:
        return 0 if json.loads(text).get("ok") else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
