#!/usr/bin/env python3
"""Copy a known-good Camera2 preference file into other redroid instances.

A freshly created instance shows the Camera2 first-run dialog and permission
prompts. Until those are answered the app never opens the camera, so the
injected stream cannot be seen. Instead of tapping dialogs blindly (uiautomator
is not available on this image) we copy the preference file from an instance
that already finished onboarding ("pref_has_seen_permissions_dialogs=1").

Usage: python3 seed_camera_prefs.py <source:wx1> <target...>
"""
import os
import subprocess
import sys
import time

DOCKER = "/usr/bin/docker"
PKG = "com.android.camera2"
PREFS = "/data/data/%s/shared_prefs/%s_preferences.xml" % (PKG, PKG)


def run(cmd, timeout=60, binary=False):
    return subprocess.run(cmd, capture_output=True, text=not binary, timeout=timeout)


def read_prefs(name):
    p = run([DOCKER, "exec", name, "cat", PREFS])
    if p.returncode != 0 or len(p.stdout) < 50:
        raise SystemExit("cannot read prefs from %s: %s" % (name, p.stderr[:200]))
    return p.stdout.encode()


def owner(name):
    p = run([DOCKER, "exec", name, "sh", "-c", "stat -c '%U %G' " + PREFS])
    parts = p.stdout.split()
    if len(parts) == 2:
        return parts[0], parts[1]
    p = run([DOCKER, "exec", name, "sh", "-c",
             "dumpsys package %s | grep -m1 userId=" % PKG])
    uid = p.stdout.strip().split("userId=")[-1]
    return "u0_a%s" % uid if uid.isdigit() else "root", "u0_a%s" % uid if uid.isdigit() else "root"


def seed(source, target):
    blob = read_prefs(source)
    run([DOCKER, "exec", target, "am", "force-stop", PKG])
    user, group = owner(target)
    tmp = "/data/local/tmp/cam_prefs.xml"
    p = run([DOCKER, "exec", "-i", target, "sh", "-c", "cat > " + tmp], binary=True)
    p = subprocess.run([DOCKER, "exec", "-i", target, "sh", "-c", "cat > " + tmp],
                       input=blob, capture_output=True, timeout=60)
    if p.returncode != 0:
        raise SystemExit("%s: write failed: %s" % (target, p.stderr[:200]))
    cp = run([DOCKER, "exec", target, "sh", "-c",
              "mkdir -p /data/data/%s/shared_prefs && cp %s %s && chown %s:%s %s && chmod 660 %s"
              % (PKG, tmp, PREFS, user, group, PREFS, PREFS)])
    if cp.returncode != 0:
        raise SystemExit("%s: install failed: %s" % (target, cp.stderr[:200]))
    run([DOCKER, "exec", target, "am", "start", "-n",
         PKG + "/com.android.camera.CameraLauncher"])
    for _ in range(12):
        time.sleep(0.8)
        d = run([DOCKER, "exec", target, "dumpsys", "media.camera"])
        if "Device 0 is open" in d.stdout:
            print("%s: camera open (prefs from %s)" % (target, source))
            return True
    print("%s: still closed" % target)
    return False


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    source = argv[1].split(":")[-1]
    ok = sum(1 for t in argv[2:] if seed(source, t))
    print("%d/%d seeded" % (ok, len(argv) - 2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
