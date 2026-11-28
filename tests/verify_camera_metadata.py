#!/usr/bin/env python3
"""Verify Camera2 static metadata matches the real V4L2 node geometry.

Checks every running wx instance (or explicit args):
  - pixelArraySize == V4L2 node native WxH (no fabricated 8000x6000 / 5184x3888)
  - activeArraySize == (0, 0, W, H)
  - sensor.orientation == 90
  - flash.info.available == FALSE
  - 640x480 and native WxH present in availableStreamConfigurations (fmt 34 OUTPUT)
"""
import os
import re
import subprocess
import sys
import tempfile

DOCKER = "/usr/bin/docker"


def sh(cmd, timeout=120):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def instances():
    if len(sys.argv) > 1:
        return sys.argv[1:]
    out = sh(DOCKER + " ps --format '{{.Names}}'").stdout.split()
    return sorted(n for n in out if re.fullmatch(r"wx[0-9]+", n))


def node_size(name):
    prop = sh("%s exec %s getprop vendor.camera.redroid.nodes" % (DOCKER, name)).stdout.strip()
    nodes = [n for n in prop.split(",") if n]
    if not nodes:
        return None
    sysf = "/sys/devices/virtual/video4linux/%s/format" % os.path.basename(nodes[0])
    if not os.path.exists(sysf):
        return None
    m = re.search(r":([0-9]+)x([0-9]+)@([0-9]+)", open(sysf).read().strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_cameras(dump_text):
    # Split on "== Camera device 0 static metadata:" or similar section markers
    sections = re.split(r"(?===\s*Camera\s+HAL\s+device\s+\S+\s+\(v[0-9.]+\)\s+static\s+information:)", dump_text)
    cams = []
    for sec in sections:
        m_px = re.search(r"android\.sensor\.info\.pixelArraySize.*?\n\s*\[\s*([0-9]+)\s+([0-9]+)\s*\]", sec)
        if not m_px:
            continue
        m_act = re.search(r"android\.sensor\.info\.activeArraySize.*?\n\s*\[\s*([0-9]+)\s+([0-9]+)\s+([0-9]+)\s+([0-9]+)\s*\]", sec)
        m_ori = re.search(r"android\.sensor\.orientation.*?\n\s*\[\s*([0-9]+)\s*\]", sec)
        m_fl = re.search(r"android\.flash\.info\.available.*?\n\s*\[\s*([A-Z]+)\s*\]", sec)
        cfgs = set()
        for m in re.finditer(r"\[\s*34\s+([0-9]+)\s+([0-9]+)\s+OUTPUT\s*\]", sec):
            cfgs.add((int(m.group(1)), int(m.group(2))))
        cams.append({
            "pixel_array": (int(m_px.group(1)), int(m_px.group(2))),
            "active_array": tuple(int(m_act.group(i)) for i in range(1, 5)) if m_act else None,
            "orientation": int(m_ori.group(1)) if m_ori else None,
            "flash": m_fl.group(1) if m_fl else None,
            "cfgs": cfgs,
        })
    return cams


def main():
    names = instances()
    if not names:
        print("no wx containers")
        return 1
    fails = 0
    for name in names:
        nsz = node_size(name)
        tmp = tempfile.mktemp(suffix=".txt")
        sh("%s exec %s dumpsys media.camera > %s" % (DOCKER, name, tmp), timeout=180)
        txt = open(tmp, errors="replace").read() if os.path.exists(tmp) else ""
        if os.path.exists(tmp):
            os.unlink(tmp)
        cams = parse_cameras(txt)
        print("== %s (node=%s, cameras=%d) ==" % (name, nsz, len(cams)))
        if not nsz or len(cams) < 2:
            print("  [FAIL] missing node size or fewer than 2 cameras")
            fails += 1
            continue
        ok = True
        for idx, c in enumerate(cams[:2]):
            print("  cam%d pixelArray=%s activeArray=%s orient=%s flash=%s cfgs=%s" % (
                idx, c["pixel_array"], c["active_array"], c["orientation"], c["flash"], sorted(c["cfgs"])))
            if c["pixel_array"] != nsz:
                print("  [FAIL] cam%d pixelArray %s != node %s" % (idx, c["pixel_array"], nsz)); ok = False
            if c["active_array"] != (0, 0, nsz[0], nsz[1]):
                print("  [FAIL] cam%d activeArray %s != (0,0,%d,%d)" % (idx, c["active_array"], nsz[0], nsz[1])); ok = False
            if c["orientation"] != 90:
                print("  [FAIL] cam%d orientation %s != 90" % (idx, c["orientation"])); ok = False
            if c["flash"] != "FALSE":
                print("  [FAIL] cam%d flash %s != FALSE" % (idx, c["flash"])); ok = False
            if not {(640, 480), nsz}.issubset(c["cfgs"]):
                print("  [FAIL] cam%d missing 640x480 or native %s in stream configs" % (idx, nsz)); ok = False
        if ok:
            print("  [PASS]")
        else:
            fails += 1
    print("RESULT %s (%d/%d)" % ("PASS" if fails == 0 else "FAIL", len(names) - fails, len(names)))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
