#!/usr/bin/env python3
"""Clear the first-run / permission dialogs that block the camera preview.

A fresh redroid instance shows Camera2 onboarding ("Remember photo locations?")
and the camera/location permission prompts. Until those are dismissed the app
never opens the camera, so Device 0 stays closed. This helper dumps the view
hierarchy, taps the affirmative button, and repeats until the camera device is
open (or nothing left to tap).

Usage: python3 camera_first_run_assist.py wx3 [wx4 ...]
"""
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

DOCKER = "/usr/bin/docker"
AFFIRMATIVE = ("next", "allow", "ok", "got it", "continue", "start",
               "allow all", "while using the app", "下一步", "允许", "确定",
               "继续", "知道了", "同意")


def run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def dump(name):
    run([DOCKER, "exec", name, "uiautomator", "dump", "/sdcard/ui.xml"])
    p = run([DOCKER, "exec", name, "cat", "/sdcard/ui.xml"])
    return p.stdout


def device_open(name):
    p = run([DOCKER, "exec", name, "dumpsys", "media.camera"])
    return "Device 0 is open" in p.stdout


def candidates(xml):
    if not xml.strip():
        return []
    try:
        root = ET.fromstring(xml[xml.index("<?xml"):])
    except Exception:  # noqa: BLE001
        return []
    out = []
    for node in root.iter("node"):
        label = (node.get("text") or node.get("content-desc") or "").strip().lower()
        if not label:
            continue
        if not any(label == a or label.startswith(a) for a in AFFIRMATIVE):
            continue
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        out.append(((x1 + x2) // 2, (y1 + y2) // 2, label))
    return out


def assist(name, rounds=8):
    for i in range(rounds):
        if device_open(name):
            print("%s: camera open after %d tap(s)" % (name, i))
            return True
        xml = dump(name)
        taps = candidates(xml)
        if not taps:
            print("%s: no dialog left (open=%s)" % (name, device_open(name)))
            return device_open(name)
        x, y, label = taps[0]
        print("%s: tapping %r at (%d,%d)" % (name, label, x, y))
        run([DOCKER, "exec", name, "input", "tap", str(x), str(y)])
        time.sleep(1.2)
    return device_open(name)


def main(argv):
    names = argv[1:] or ["wx1", "wx2", "wx3", "wx4", "wx5"]
    ok = 0
    for name in names:
        try:
            ok += 1 if assist(name) else 0
        except Exception as exc:  # noqa: BLE001
            print("%s: %s" % (name, exc))
    print("%d/%d open" % (ok, len(names)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
