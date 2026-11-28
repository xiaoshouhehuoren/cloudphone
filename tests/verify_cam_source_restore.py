#!/usr/bin/env python3
"""前后摄各自独立源 + 停流自动还原 的回归测试。

验证三件事：
1. 页面 JPEG 推流（/api/cam/start kind=webcam）只改目标节点，另一路保持原源；
2. /api/cam/stop 把目标节点还原成切进来之前的源（含 fit/rotate/ystretch）；
3. 还原的是“原来那一路”，不是一律 idle。

用法: python3 verify_cam_source_restore.py [container]   默认 wx1
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

CAMD = "http://127.0.0.1:8100"
DASH = "http://127.0.0.1:8099"
TOKEN_FILE = "/etc/g98/cloudphone.token"
FFMPEG = "/root/ffmpeg" if os.path.exists("/root/ffmpeg") else "ffmpeg"
NAME = sys.argv[1] if len(sys.argv) > 1 else "wx1"
fails = 0


def check(ok, msg):
    global fails
    print(("[PASS] " if ok else "[FAIL] ") + msg)
    if not ok:
        fails += 1
    return ok


def http(url, payload=None, timeout=8.0, headers=None):
    data = None
    hdr = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr,
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def node_state(node):
    for item in http(CAMD + "/state").get("nodes") or []:
        if item.get("node") == node:
            return item
    return {}


def geom_of(snap):
    g = snap.get("geometry") or {}
    return {k: g.get(k) for k in ("fit", "rotate", "mirror", "ystretch")}


def token():
    try:
        with open(TOKEN_FILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def dash_start(node, kind, path=None, geom=None):
    body = {"name": NAME, "node": node, "kind": kind}
    if path:
        body["path"] = path
    for k, v in (geom or {}).items():
        body[k] = v
    return http(DASH + "/api/cam/start", body, timeout=20.0,
                headers={"Cookie": "g98k=" + token()})


def dash_stop(node, restore=True):
    body = {"name": NAME, "node": node, "restore": restore}
    return http(DASH + "/api/cam/stop", body, timeout=20.0,
                headers={"Cookie": "g98k=" + token()})


def wait_kind(node, kind, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if node_state(node).get("kind") == kind:
            return True
        time.sleep(0.4)
    return False


def set_source(node, kind, arg, geom):
    payload = {"node": node, "kind": kind}
    payload["path" if kind in ("image", "video") else "url"] = arg
    for k in ("fit", "rotate", "mirror", "ystretch"):
        if geom.get(k) is not None:
            payload[k] = geom[k]
    return http(CAMD + "/set", payload)


def make_jpeg(path):
    subprocess.run([FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=640x480", "-frames:v", "1", "-q:v", "4", path],
                   timeout=30, stdin=subprocess.DEVNULL)
    return os.path.getsize(path) if os.path.exists(path) else 0


def main():
    nodes = [x.get("node") for x in (http(CAMD + "/state").get("nodes") or [])]
    nodes = [n for n in nodes if n][:2]
    if len(nodes) < 2:
        check(False, "需要两路节点，实际 %s" % nodes)
        print("RESULT FAIL")
        return 1
    front = next((n for n in nodes if int(n.rsplit("video", 1)[-1]) % 2 == 1), nodes[1])
    peer = next((n for n in nodes if n != front), nodes[0])
    print("front=%s peer=%s" % (front, peer))

    judge = "/tmp/g98_src_restore.jpg"
    jpg = make_jpeg(judge)
    check(jpg > 1000, "生成测试 JPEG (%d bytes)" % jpg)

    peer_before = node_state(peer)
    orig_front = node_state(front)
    print("original front: %s %s %s" % (orig_front.get("kind"), str(orig_front.get("arg"))[:50],
                                        geom_of(orig_front)))
    try:
        # 1) 给 front 装一个确定的基线源（与 rtsp 不同，便于证明“还原的是它”）
        set_source(front, "image", judge, {"fit": "contain", "rotate": 90,
                                           "ystretch": 1.0, "mirror": False})
        time.sleep(2)
        base = node_state(front)
        check(base.get("kind") == "image", "基线 front kind=%s" % base.get("kind"))
        base_geom = geom_of(base)
        check(base_geom.get("rotate") == 90 and base_geom.get("fit") == "contain",
              "基线 geometry %s" % base_geom)

        # 2) 页面推流只改目标节点
        dash_start(front, "webcam")
        ok = wait_kind(front, "jpeg")
        check(ok, "推流后 front kind=%s" % node_state(front).get("kind"))
        peer_now = node_state(peer)
        check(peer_now.get("kind") == peer_before.get("kind")
              and peer_now.get("arg") == peer_before.get("arg"),
              "peer 未被改动: %s %s" % (peer_now.get("kind"), str(peer_now.get("arg"))[:40]))

        # 3) 推 2 帧保证 camd 真的在收
        for _ in range(2):
            urllib.request.urlopen(urllib.request.Request(
                CAMD + "/frame?node=" + front, data=open(judge, "rb").read(),
                headers={"Content-Type": "image/jpeg"}, method="POST"), timeout=5).read()
            time.sleep(0.2)
        check((node_state(front).get("source_frames") or 0) >= 2,
              "camd 收到帧 src=%s" % node_state(front).get("source_frames"))

        # 4) 停流还原成基线源（含 geometry）
        dash_stop(front, restore=True)
        ok = wait_kind(front, "image")
        back = node_state(front)
        check(ok, "停流后 front kind=%s" % back.get("kind"))
        bg = geom_of(back)
        check(bg.get("rotate") == 90 and bg.get("fit") == "contain"
              and (bg.get("ystretch") or 0) == 1.0, "还原 geometry %s" % bg)
        check(back.get("arg") == judge, "还原路径 %s" % str(back.get("arg"))[:60])
        peer_after = node_state(peer)
        check(peer_after.get("kind") == peer_before.get("kind"),
              "peer 仍然 %s" % peer_after.get("kind"))
    finally:
        # 还原到测试前的真实源，别把用户/回归的注入留在测试图
        ok = orig_front.get("kind")
        if ok in ("rtsp", "image", "video"):
            arg = orig_front.get("arg")
            try:
                set_source(front, ok, arg, geom_of(orig_front))
                print("[INFO] restored front -> %s %s" % (ok, str(arg)[:50]))
            except Exception as exc:  # noqa: BLE001
                print("[WARN] restore front failed: %s" % str(exc)[:120])
        else:
            try:
                http(CAMD + "/set", {"node": front, "kind": "idle"})
            except Exception:  # noqa: BLE001
                pass

    print("RESULT " + ("PASS" if fails == 0 else "FAIL"))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
