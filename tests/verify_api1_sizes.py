#!/usr/bin/env python3
"""Verify API1 (legacy camera) can build preview/video size lists for every wx instance.

Mirrors Parameters::initialize in
frameworks/av/services/camera/libcameraservice/api1/client2/Parameters.cpp:
  - videoSizeUpperBound = per-dimension max of MediaProfiles enc.vid.width/height.max
    (i.e. <VideoEncoderCap maxFrameWidth/maxFrameHeight> in media_profiles_V1_0.xml)
  - getFilteredSizes() keeps OUTPUT + IMPLEMENTATION_DEFINED sizes whose AREA is in
    [lowerArea, upperArea] and whose minFrameDuration <= 1e9/29.97 ns
  - an empty list => BAD_VALUE(-22) => app sees "Failed to initialize camera"
"""
import os, re, subprocess, sys, tempfile
import xml.etree.ElementTree as ET

DOCKER = "/usr/bin/docker"
MP = "/vendor/etc/media_profiles_V1_0.xml"
MAX_PREVIEW_RECORD_DURATION_NS = int(1e9 / 29.97)  # Parameters.h MAX_PREVIEW_RECORD_DURATION_NS
FMT_IMPL_DEFINED = 34

def sh(cmd, timeout=120):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)

def instances():
    out = sh(DOCKER + " ps --format '{{.Names}}'").stdout.split()
    return [n for n in out if re.fullmatch(r"wx[0-9]+", n)]

def enc_caps(name):
    tmp = tempfile.mktemp(suffix=".xml")
    if sh("%s cp %s:%s %s" % (DOCKER, name, MP, tmp)).returncode != 0:
        return None
    try:
        root = ET.parse(tmp).getroot()
    finally:
        os.unlink(tmp)
    caps = []
    for el in root.iter("VideoEncoderCap"):
        caps.append((el.get("name"),
                     int(el.get("minFrameWidth", "0")), int(el.get("minFrameHeight", "0")),
                     int(el.get("maxFrameWidth", "0")), int(el.get("maxFrameHeight", "0"))))
    if not caps:
        return None
    min_w = min(c[1] for c in caps)
    min_h = min(c[2] for c in caps)
    max_w = max(c[3] for c in caps)
    max_h = max(c[4] for c in caps)
    return caps, (min_w, min_h), (max_w, max_h)

def node_size(name):
    prop = sh("%s exec %s getprop vendor.camera.redroid.nodes" % (DOCKER, name)).stdout.strip()
    nodes = [n for n in prop.split(",") if n]
    if not nodes:
        return None, None
    node = nodes[0]
    sysf = "/sys/devices/virtual/video4linux/%s/format" % os.path.basename(node)
    fmt = ""
    if os.path.exists(sysf):
        fmt = open(sysf).read().strip()
    m = re.search(r":([0-9]+)x([0-9]+)@([0-9]+)", fmt)
    if not m:
        return node, None
    return node, (int(m.group(1)), int(m.group(2)), int(m.group(3)))

def stream_configs(name):
    tmp = tempfile.mktemp(suffix=".txt")
    sh("%s exec %s dumpsys media.camera > %s" % (DOCKER, name, tmp), timeout=180)
    txt = ""
    if os.path.exists(tmp):
        txt = open(tmp, errors="replace").read()
        os.unlink(tmp)
    cfgs = set()
    for m in re.finditer(r"\[\s*%d\s+([0-9]+)\s+([0-9]+)\s+OUTPUT\s*\]" % FMT_IMPL_DEFINED, txt):
        cfgs.add((int(m.group(1)), int(m.group(2))))
    return cfgs

def main():
    names = instances()
    if not names:
        print("no wx containers")
        return 1
    fails = 0
    for name in sorted(names):
        caps = enc_caps(name)
        if not caps:
            print("[FAIL] %s: cannot read %s" % (name, MP)); fails += 1; continue
        caps, lower, upper = caps
        node, nsz = node_size(name)
        cfgs = stream_configs(name)
        print("== %s ==" % name)
        print("  encoder caps: " + ", ".join("%s %dx%d..%dx%d" % c for c in caps))
        print("  bounds: lower=%dx%d upper=%dx%d (area %d)" % (lower[0], lower[1], upper[0], upper[1], upper[0]*upper[1]))
        if not nsz:
            print("  [FAIL] node %s format unknown" % node); fails += 1; continue
        w, h, fps = nsz
        area = w * h
        print("  node %s = %dx%d@%d area=%d" % (node, w, h, fps, area))
        print("  stream configs (fmt %d OUTPUT): %s" % (FMT_IMPL_DEFINED, sorted(cfgs)))
        old_upper = (1280, 720)
        ok_bounds = lower[0]*lower[1] <= area <= upper[0]*upper[1]
        print("  pre-fix bound %dx%d would give empty list: %s" % (old_upper[0], old_upper[1], area > old_upper[0]*old_upper[1]))
        print("  area within current bounds: %s" % ok_bounds)
        minfd = int(1e9 / max(fps, 1))
        ok_fps = minfd <= MAX_PREVIEW_RECORD_DURATION_NS
        print("  minFrameDuration=%dns <= %dns: %s" % (minfd, MAX_PREVIEW_RECORD_DURATION_NS, ok_fps))
        in_cfg = (w, h) in cfgs
        print("  %dx%d present in HAL stream configs: %s" % (w, h, in_cfg))
        # The HAL advertises extra same-aspect small sizes and scales on dequeue,
        # because legacy API1 clients (WeChat face verification) hard-code 640x480.
        small_ok = True
        if w > 640 and h > 480:
            small_ok = {(640, 480), (320, 240)}.issubset(cfgs)
        print("  small sizes 640x480/320x240 advertised: %s" % small_ok)
        if ok_bounds and ok_fps and in_cfg and small_ok:
            print("  [PASS] API1 size list non-empty")
        else:
            print("  [FAIL] API1 size list would be empty"); fails += 1
    print("RESULT %s (%d/%d)" % ("PASS" if fails == 0 else "FAIL", len(names)-fails, len(names)))
    return 0 if fails == 0 else 2

if __name__ == "__main__":
    sys.exit(main())
