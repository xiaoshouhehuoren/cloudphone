#!/usr/bin/env python3
"""Rotated injection must not inherit the legacy ystretch compensation.

Why: ystretch_filter() pre-stretches the WIDTH axis before rotate. With
rotate=90/270 that pre-stretch becomes a VERTICAL stretch, so the global
G98_CAMD_YSTRETCH default (1.7778 on the board, tuned for the old un-rotated
preview path) visibly elongates faces in WeChat's face-verify circle, which
displays frames proportionally and needs no compensation at all.

Checks on NODE (default /dev/video22, an idle node):
  A. /set rotate=270 without ystretch -> geometry.ystretch == 1.0
  B. /set rotate=90  without ystretch -> geometry.ystretch == 1.0
  C. /set rotate=0   without ystretch -> geometry.ystretch == global default
  D. explicit ystretch still wins over the rotated default
The node's original source and geometry are restored at the end.
"""
import json, subprocess, sys, urllib.request

CAMD = "http://127.0.0.1:8100"
NODE = sys.argv[1] if len(sys.argv) > 1 else "/dev/video22"


def post(path, body):
    req = urllib.request.Request(CAMD + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def state():
    data = json.loads(urllib.request.urlopen(CAMD + "/state", timeout=5).read())
    for n in data["nodes"]:
        if n["node"] == NODE:
            return n
    raise SystemExit("node %s missing from /state" % NODE)


def global_ystretch():
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True,
                         text=True).stdout
    for line in out.splitlines():
        if "g98-camd.py" not in line or "grep" in line:
            continue
        pid = line.split()[0]
        try:
            with open("/proc/%s/environ" % pid, "rb") as fh:
                env = fh.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        for item in env:
            if item.startswith("G98_CAMD_YSTRETCH="):
                return float(item.split("=", 1)[1])
    return 1.0


def set_node(body):
    body = dict(body)
    body["node"] = NODE
    post("/set", body)
    return (state().get("geometry") or {}).get("ystretch")


def main():
    failures = []
    original = state()
    geo = original.get("geometry") or {}
    expected_global = global_ystretch()
    print("global G98_CAMD_YSTRETCH = %s" % expected_global)
    try:
        for label, rot, want in (("A", 270, 1.0), ("B", 90, 1.0),
                                 ("C", 0, expected_global)):
            got = set_node({"kind": "idle", "rotate": rot})
            print("%s. rotate=%d (no ystretch) -> %s (want %s)"
                  % (label, rot, got, want))
            if got is None or abs(got - want) > 1e-6:
                failures.append("rotate=%d default ystretch=%s want %s"
                                % (rot, got, want))
        got = set_node({"kind": "idle", "rotate": 270, "ystretch": 1.5})
        print("D. rotate=270 explicit ystretch=1.5 -> %s" % got)
        if got is None or abs(got - 1.5) > 1e-6:
            failures.append("explicit ystretch ignored (got %s)" % got)
    finally:
        body = {"node": NODE, "kind": original.get("kind") or "idle",
                "fit": geo.get("fit"), "rotate": geo.get("rotate"),
                "mirror": geo.get("mirror"), "ystretch": geo.get("ystretch")}
        body = {k: v for k, v in body.items() if v is not None}
        arg = original.get("arg")
        if arg and body["kind"] in ("rtsp", "image", "video"):
            body["url" if body["kind"] == "rtsp" else "path"] = arg
        post("/set", body)
        print("restored kind=%s" % body["kind"])
    if failures:
        for f in failures:
            print("FAIL: " + f)
        print("RESULT FAIL (%d)" % len(failures))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
