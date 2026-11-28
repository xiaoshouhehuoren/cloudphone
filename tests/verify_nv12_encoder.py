#!/usr/bin/env python3
"""Soft AVC encoder must accept WeChat's NV12 byte buffers.

WeChat face verification encodes 448x640 NV12 through OMX.google.h264.encoder.
The stock redroid SoftAVCEnc fails on the first frame (Encode Frame failed 0x19d /
ACodec ERROR 0x80001001), so the recorded fdv_*.mp4 is empty and WeChat shows a
local "Network error". The patched library converts NV12 to I420 internally.
Run on the board as root; uses the same EncProbe as the original diagnosis.
"""
import base64
import os
import re
import subprocess
import sys

C = sys.argv[1] if len(sys.argv) > 1 else "wx1"
DEX = "/data/local/tmp/encprobe.dex"
LOCAL_DEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "encprobe.dex")


def dex(cmd, timeout=120):
    return subprocess.run(["docker", "exec", C, "sh", "-c", cmd],
                          capture_output=True, text=True, timeout=timeout)


def ensure_dex():
    try:
        if "yes" in dex("test -s %s && echo yes" % DEX, 20).stdout:
            return True
    except subprocess.TimeoutExpired:
        pass
    if not os.path.exists(LOCAL_DEX):
        print("[FAIL] %s missing and no local encprobe.dex (build sensors-probe/EncProbe.java)" % DEX)
        return False
    data = base64.b64encode(open(LOCAL_DEX, "rb").read()).decode()
    p = subprocess.run(["docker", "exec", "-i", C, "sh", "-c", "base64 -d > " + DEX],
                       input=data, text=True, capture_output=True, timeout=60)
    return p.returncode == 0


def main():
    if not ensure_dex():
        return 1
    cmd = (
        "export ANDROID_ROOT=/system ANDROID_DATA=/data "
        "ANDROID_ART_ROOT=/apex/com.android.art ANDROID_I18N_ROOT=/apex/com.android.i18n "
        "ANDROID_TZDATA_ROOT=/apex/com.android.tzdata ANDROID_RUNTIME_ROOT=/apex/com.android.runtime; "
        "pid=$(pidof system_server); "
        "export BOOTCLASSPATH=$(cat /proc/$pid/environ | tr '\\0' '\\n' | sed -n 's/^BOOTCLASSPATH=//p'); "
        "timeout 90 /system/bin/app_process -Djava.class.path=%s /system/bin com.g98.probe.EncProbe DEFAULT" % DEX
    )
    try:
        out = dex(cmd, 150).stdout
    except subprocess.TimeoutExpired:
        print("[FAIL] EncProbe timed out")
        return 1
    for line in out.splitlines():
        if line.startswith(("USING", "RESULT", "FORMAT")):
            print(line)
    fails = 0
    for w, h, color in ((448, 640, 21), (640, 480, 19)):
        m = re.search(r"RESULT DEFAULT %dx%d color=%d frames=(\d+) outBytes=(\d+) OK" % (w, h, color), out)
        if not m:
            print("[FAIL] no successful RESULT for %dx%d color=%d" % (w, h, color))
            fails += 1
            continue
        frames, nbytes = int(m.group(1)), int(m.group(2))
        if frames < 30 or nbytes < 10000:
            print("[FAIL] %dx%d color=%d frames=%d outBytes=%d" % (w, h, color, frames, nbytes))
            fails += 1
        else:
            print("[PASS] %dx%d color=%d frames=%d outBytes=%d" % (w, h, color, frames, nbytes))
    print("RESULT " + ("PASS" if fails == 0 else "FAIL"))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
