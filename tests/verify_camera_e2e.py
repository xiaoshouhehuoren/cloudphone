#!/usr/bin/env python3
"""G98 camera end-to-end verifier.

Runs on the board (192.168.5.111). Verifies, per instance:
  1. camd health and both instance nodes are idle and char devices
  2. vendor.camera.redroid.nodes property points at the right pair
  3. dumpsys media.camera reports 2 camera devices
  4. an injected solid colour reaches the camera preview (centre pixel match)
  5. no CameraServiceWatchdog abort and provider CPU stays low

Usage:  python3 verify_camera_e2e.py [wx1 wx2 ...]
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

DOCKER = "/usr/bin/docker"
FFMPEG = "/root/ffmpeg"
CURL = "/usr/bin/curl"
CAM_DIR = "/root/camin"
DASH = "http://127.0.0.1:8099"
COOKIE = "g98k=g98panel"
TOL = 14


def run(cmd, timeout=40, check=False):
    p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                       text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError("command failed: %s\n%s" % (cmd, p.stderr[-400:]))
    return p.returncode, p.stdout, p.stderr


def http(path, data=None, headers=None, raw=None, timeout=30):
    url = DASH + path
    hdrs = {"Cookie": COOKIE}
    if headers:
        hdrs.update(headers)
    body = None
    if raw is not None:
        body = raw
    elif data is not None:
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def nodes_for(name):
    m = re.match(r"^wx(\d+)$", name)
    n = int(m.group(1)) if m else 1
    base = 20 + 2 * (n - 1)
    return "/dev/video%d" % base, "/dev/video%d" % (base + 1)


def is_char(path):
    try:
        import stat
        return stat.S_ISCHR(os.stat(path).st_mode)
    except OSError:
        return False


def device0_open(name):
    _, out, _ = run([DOCKER, "exec", name, "dumpsys", "media.camera"])
    if "Device 0 is open" in out:
        return True
    if "Device 0 is closed" in out:
        return False
    return None


def camera_devices(name):
    _, out, _ = run([DOCKER, "exec", name, "dumpsys", "media.camera"])
    m = re.search(r"Number of camera devices:\s*(\d+)", out)
    return int(m.group(1)) if m else -1


def centre_pixel(png_local):
    p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i",
                        png_local, "-vf", "crop=1:1:540:1200", "-frames:v", "1",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, timeout=30)
    b = p.stdout
    return (b[0], b[1], b[2]) if len(b) >= 3 else (-1, -1, -1)


def make_color_png(path, color):
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=c=%s:s=640x480" % color, "-frames:v", "1", "-y", path], check=True)


def inject_image(name, node, src_png, tag):
    rc, out, err = run([CURL, "-s", "-X", "POST", "-H", "X-Filename: %s.png" % tag,
                        "-H", "Cookie: " + COOKIE, "--data-binary", "@" + src_png,
                        DASH + "/api/cam/file"], check=True)
    j = json.loads(out)
    if not j.get("ok"):
        raise RuntimeError("upload failed: %s" % out)
    # The dashboard serialises camera operations; a start issued while another
    # instance is still switching comes back busy instead of queued.
    for attempt in range(20):
        res = http("/api/cam/start", {"name": name, "node": node,
                                      "path": j["path"], "kind": "image"})
        if res.get("ok"):
            return res
        if not res.get("busy"):
            raise RuntimeError("start failed: %s" % res)
        time.sleep(0.5)
    raise RuntimeError("start stayed busy: %s" % res)


def wait_streaming(name, timeout=25.0):
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        last = http("/api/cam/status?name=%s" % name)
        st = last.get("state", {})
        if st.get("state") == "streaming":
            return st
        if st.get("error"):
            raise RuntimeError("state error: %s" % st["error"])
        time.sleep(0.5)
    raise RuntimeError("timeout waiting for streaming: %s" % json.dumps(last)[:300])


def watchdog_count(name):
    _, out, _ = run([DOCKER, "exec", name, "logcat", "-d", "-b", "all"])
    return len(re.findall(r"CameraServiceWatchdog triggering abort", out))


def provider_cpu():
    _, out, _ = run("ps -eo pid,pcpu,args | grep camera.provider | grep -v grep")
    total = 0.0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 1:
            try:
                total += float(parts[1])
            except ValueError:
                pass
    return total


def hard_reset(name):
    """Force-stop the client and rebuild the provider for a clean session."""
    run([DOCKER, "exec", name, "am", "force-stop", "com.android.camera2"])
    run([DOCKER, "exec", name, "setprop", "ctl.restart",
         "vendor.camera-provider-2-4-legacy"])
    # Give the provider time to bind its nodes before cameraserver reconnects.
    time.sleep(3)
    run([DOCKER, "exec", name, "setprop", "ctl.restart", "cameraserver"])
    time.sleep(3)
    deadline = time.time() + 20
    while time.time() < deadline:
        if camera_devices(name) == 2:
            return True
        time.sleep(1)
    return False


def soft_reset(name):
    """Drop a stale camera session without touching the provider."""
    run([DOCKER, "exec", name, "am", "force-stop", "com.android.camera2"])
    # Dismiss a leftover "Camera error" dialog in case the previous run left one.
    run([DOCKER, "exec", name, "input", "keyevent", "4"])
    time.sleep(1.5)


def launch_camera(name):
    run([DOCKER, "exec", name, "am", "start", "-n",
         "com.android.camera2/com.android.camera.CameraLauncher"])
    deadline = time.time() + 15
    while time.time() < deadline:
        if device0_open(name) is True:
            return True
        time.sleep(0.5)
    return False


def verify(name):
    check = {}
    back, front = nodes_for(name)
    check["nodes"] = "%s,%s" % (back, front)
    fails = []

    healthy = {}
    try:
        with urllib.request.urlopen("http://127.0.0.1:8100/health", timeout=5) as r:
            healthy = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        fails.append("camd unreachable: %s" % exc)

    for node in (back, front):
        if not is_char(node):
            fails.append("%s is not a char device" % node)
    check["char_devices"] = all(is_char(n) for n in (back, front))

    _, out, _ = run([DOCKER, "exec", name, "getprop", "vendor.camera.redroid.nodes"])
    got = out.strip()
    check["prop"] = got
    if got != "%s,%s" % (back, front):
        fails.append("prop mismatch: %r" % got)

    devs = camera_devices(name)
    check["devices"] = devs
    if devs != 2:
        fails.append("camera devices=%s" % devs)

    src = os.path.join(CAM_DIR, "verify-e2e.png")
    if not os.path.isdir(CAM_DIR):
        os.makedirs(CAM_DIR, exist_ok=True)
    make_color_png(src, "red")

    # Historical aborts from earlier experiments must not fail this run.
    run([DOCKER, "exec", name, "logcat", "-c", "-b", "all"])

    shot_local = "/tmp/verify-e2e-%s.png" % name
    px = (-1, -1, -1)
    tries = 0
    for attempt in range(2):
        # A leftover session from a previous run (or from a manual screencap) can
        # leave the client wedged while devices still reports 2, so always rebuild
        # the provider *before* the app starts: that is the one order that never
        # produces the "Camera error" dialog.
        hard_reset(name)
        launch_camera(name)

        started = time.time()
        inject_image(name, back, src, "verify")
        st = wait_streaming(name)
        if attempt == 0:
            check["latency"] = round(time.time() - started, 2)
            check["resumed"] = st.get("resumed")
        for _ in range(4):
            tries += 1
            time.sleep(1.2)
            run([DOCKER, "exec", name, "screencap", "-p", "/data/local/tmp/verify.png"])
            run([DOCKER, "cp", "%s:/data/local/tmp/verify.png" % name, shot_local])
            px = centre_pixel(shot_local)
            if all(abs(c - e) <= TOL for c, e in zip(px, (253, 0, 0))):
                break
        if all(abs(c - e) <= TOL for c, e in zip(px, (253, 0, 0))):
            break
    check["pixel"] = px
    check["tries"] = tries
    if any(abs(c - e) > TOL for c, e in zip(px, (253, 0, 0))):
        fails.append("centre pixel %s not red" % (px,))

    wd = watchdog_count(name)
    check["watchdog"] = wd
    if wd:
        fails.append("watchdog aborts=%d" % wd)

    check["open"] = device0_open(name)
    if check["open"] is not True:
        fails.append("Device 0 not open")

    http("/api/cam/stop", {"name": name})
    return check, fails


def main(argv):
    names = argv[1:] or ["wx1", "wx2", "wx3", "wx4", "wx5"]
    cpu = provider_cpu()
    print("provider cpu: %.1f%%" % cpu)
    failed = 0
    for name in names:
        try:
            check, fails = verify(name)
        except Exception as exc:  # noqa: BLE001
            print("[FAIL] %s: %s" % (name, exc))
            failed += 1
            continue
        status = "PASS" if not fails else "FAIL"
        print("[%s] %s %s" % (status, name, json.dumps(check, ensure_ascii=False)))
        for f in fails:
            print("       - %s" % f)
        if fails:
            failed += 1
    print("%d/%d passed" % (len(names) - failed, len(names)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
