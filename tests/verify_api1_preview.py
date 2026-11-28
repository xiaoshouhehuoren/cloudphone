#!/usr/bin/env python3
"""End-to-end legacy Camera API1 preview test at a SMALL size (640x480).

WeChat face verification uses android.hardware.Camera (API1) and hard-codes
640x480. The v4l2loopback nodes are pinned to 1280x960 (keep_format=1), so the
HAL advertises 640x480/320x240, keeps capturing at the native size and scales
each output buffer. This test drives a real API1 client (app_process probe) and
checks that preview frames actually arrive with the exact NV12 byte count.

Probe sources/assets: tests/api1probe/ (Cam1Small.java, cam1small.dex, runner).
Usage on the board: python3 verify_api1_preview.py [wx1]
"""
import json, re, subprocess, sys, time, urllib.request

DOCKER = "/usr/bin/docker"
CAMD = "http://127.0.0.1:8100"
HERE = "/root/tests"
PROBE = HERE + "/api1probe"
IMAGE = "/root/camin/ind-red.png"
SIZE = (640, 480)

def sh(cmd, timeout=180):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)

def sh_soft(cmd, timeout=60, label=""):
    """Never let one wedged container command turn the whole test into a traceback."""
    try:
        return sh(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        print("[WARN] timed out after %ds: %s" % (timeout, label or cmd))
        return None

def camd_nodes():
    data = json.loads(urllib.request.urlopen(CAMD + "/state", timeout=10).read())
    return {n["node"]: n for n in data.get("nodes", [])}


def restore_sources(saved):
    """Put back whatever was injected before (a phone RTSP stream above all).

    The probe feeds a static image so its pixel check is deterministic; leaving
    that image behind silently replaces the user's live camera with colour bars.
    """
    for node, n in saved.items():
        geo = n.get("geometry") or {}
        kind = n.get("kind") or "idle"
        body = {"node": node, "kind": kind}
        for key in ("fit", "rotate", "mirror", "ystretch"):
            if geo.get(key) is not None:
                body[key] = geo[key]
        arg = n.get("arg")
        if arg and kind in ("rtsp", "image", "video"):
            body["url" if kind == "rtsp" else "path"] = arg
        try:
            req = urllib.request.Request(CAMD + "/set", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            print("restored %s -> %s %s" % (node, kind, arg or ""))
        except Exception as exc:  # noqa: BLE001
            print("[WARN] restore %s failed: %s" % (node, exc))


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "wx1"
    nodes = sh("%s exec %s getprop vendor.camera.redroid.nodes" % (DOCKER, name)).stdout.strip().split(",")
    nodes = [n for n in nodes if n]
    if len(nodes) < 2:
        print("[FAIL] %s: camera nodes not found" % name)
        return 1
    for f in ("cam1small.dex", "run_cam1small.sh"):
        r = sh("%s cp %s/api1probe/%s %s:/data/local/tmp/%s" % (DOCKER, HERE, f, name, f))
        if r.returncode != 0:
            print("[FAIL] push %s: %s" % (f, r.stderr.strip()))
            return 1
    current = camd_nodes()
    saved = {n: current[n] for n in nodes[:2] if n in current}
    fails = 0
    try:
        # Feed a known static image so the probe does not depend on any live stream.
        for node in nodes[:2]:
            body = '{"node":"%s","kind":"image","path":"%s","fit":"contain","ystretch":1.0,"mirror":false}' % (node, IMAGE)
            sh("/usr/bin/curl -s -X POST %s/set -H 'Content-Type: application/json' -d '%s'" % (CAMD, body))
        # A foreground camera app holds the devices and makes Camera.open() fail
        # ("Fail to connect to camera service"); the regression e2e step reopens it.
        sh_soft("%s exec %s am force-stop com.android.camera2" % (DOCKER, name), timeout=60, label="force-stop camera2")
        time.sleep(1.5)
        expected = SIZE[0] * SIZE[1] * 3 // 2
        for cam_id in (1, 0):  # 1 = front (what WeChat face verification opens)
            sh("%s exec %s rm -f /data/local/tmp/cam1small.log" % (DOCKER, name))
            r = sh_soft("%s exec %s sh /data/local/tmp/run_cam1small.sh %d %d %d"
                        % (DOCKER, name, cam_id, SIZE[0], SIZE[1]), timeout=180, label="probe camera %d" % cam_id)
            if r is None:
                fails += 1
                continue
            rc = sh_soft("%s exec %s cat /data/local/tmp/cam1small.log" % (DOCKER, name), timeout=60, label="read probe log")
            log = rc.stdout if rc is not None else ""
            m = re.search(r"RESULT frames=(\d+) bytes=(\d+)", log)
            sizes = re.search(r"supportedPreview=\[([^\]]*)\]", log)
            print("== %s camera %d ==" % (name, cam_id))
            print("  supportedPreview: " + (sizes.group(1) if sizes else "unknown"))
            if not m:
                print("  [FAIL] no RESULT line; output: " + r.stdout.strip()[-400:])
                fails += 1
                continue
            frames, nbytes = int(m.group(1)), int(m.group(2))
            ok = frames > 0 and nbytes == expected
            print("  frames=%d bytes=%d expected=%d -> %s" % (frames, nbytes, expected, "PASS" if ok else "FAIL"))
            if not ok:
                fails += 1
    finally:
        restore_sources(saved)
    print("RESULT %s" % ("PASS" if fails == 0 else "FAIL"))
    return 0 if fails == 0 else 2

if __name__ == "__main__":
    sys.exit(main())
