#!/usr/bin/env python3
"""Operator CLI for the G98 per-container proxy manager.

The manager listens on 127.0.0.1:18080. Each Android container owns one
listener port and one optional Basic-auth token. This CLI only edits the
manager; the board-side route/global-proxy is applied by
/srv/redroid/container-proxy.sh.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:18080"


def call(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as exc:
        print("control API unreachable: %s" % exc, file=sys.stderr)
        print("start it with: pythonw cloudphone/tools/g98-proxy-supervisor.py",
              file=sys.stderr)
        raise SystemExit(1)


def cmd_status(_args):
    data = call("/status")
    print("proxy pid=%s bind=%s" % (data.get("pid"), data.get("bind_host")))
    for name, spec in sorted((data.get("containers") or {}).items()):
        print("  %-8s port=%-5s listening=%s auth=%s" % (
            name, spec.get("port"), spec.get("listening"), spec.get("auth", False)))


def cmd_add(args):
    payload = {"name": args.name, "port": args.port}
    if args.auth:
        payload["auth"] = args.auth
    print(json.dumps(call("/add", payload), ensure_ascii=False))
    print("board: echo '%s %s' >> /srv/redroid/proxies.conf" % (args.name, args.port))
    print("board: /srv/redroid/container-proxy.sh %s %s 192.168.99.1" % (
        args.name, args.port))


def cmd_remove(args):
    print(json.dumps(call("/remove", {"name": args.name}), ensure_ascii=False))


def cmd_reload(_args):
    print(json.dumps(call("/reload"), ensure_ascii=False))


def cmd_check(args):
    data = call("/status")
    spec = (data.get("containers") or {}).get(args.name)
    if not spec:
        print("unknown container: %s" % args.name, file=sys.stderr)
        raise SystemExit(2)
    proxy = "http://192.168.99.1:%s" % spec["port"]
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        with opener.open("http://www.baidu.com/", timeout=12) as resp:
            body = resp.read(200)
            print("%s via %s -> HTTP %s (%d bytes)" % (
                args.name, proxy, resp.status, len(body)))
    except Exception as exc:  # noqa: BLE001 - operator tool
        print("%s via %s -> FAILED: %s" % (args.name, proxy, exc), file=sys.stderr)
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description="G98 per-container proxy control")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="list configured containers").set_defaults(func=cmd_status)

    add = sub.add_parser("add", help="add or replace a container listener")
    add.add_argument("name")
    add.add_argument("port", type=int)
    add.add_argument("--auth", help="optional user:pass")
    add.set_defaults(func=cmd_add)

    remove = sub.add_parser("remove", help="remove a container listener")
    remove.add_argument("name")
    remove.set_defaults(func=cmd_remove)

    sub.add_parser("reload", help="reload config and restart listeners").set_defaults(func=cmd_reload)

    check = sub.add_parser("check", help="fetch a URL through that container port")
    check.add_argument("name")
    check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
