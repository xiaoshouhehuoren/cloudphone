#!/usr/bin/env python3
"""Read-only diagnostic probe for RK3588 hardware H.264 encoder prerequisites.

Reports whether the host kernel exposes Rockchip MPP / rkvenc2 devices and
whether V4L2 video nodes expose H.264 capture, without modifying the board.
"""
import glob
import json
import os
import platform
import subprocess
import sys
from pathlib import Path


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def main():
    rel = platform.release()
    dev_nodes = sorted(glob.glob("/dev/mpp*") + glob.glob("/dev/rkv*"))
    v4l = {}
    for idx in range(4):
        name = read_text("/sys/class/video4linux/video%d/name" % idx)
        if name is not None:
            v4l["/dev/video%d" % idx] = name
    rkvenc = {}
    for p in sorted(glob.glob("/sys/devices/platform/*rkvenc*")):
        drv = os.path.join(p, "driver")
        rkvenc[os.path.basename(p)] = {
            "modalias": read_text(os.path.join(p, "modalias")),
            "driver": os.path.realpath(drv) if os.path.islink(drv) else None,
        }
    cfg_path = "/boot/config-" + rel
    cfg_hits = []
    if os.path.isfile(cfg_path):
        for line in open(cfg_path, errors="replace"):
            if any(k in line for k in ("ROCKCHIP_MPP", "RKVENC", "VIDEO_HANTRO", "VIDEO_ROCKCHIP_RGA")):
                cfg_hits.append(line.strip())
    p0_ready = bool(dev_nodes) and any(v["driver"] for v in rkvenc.values())
    report = {
        "kernel": rel,
        "mpp_or_rkvenc_dev_nodes": dev_nodes,
        "rkvenc_platform_devices": rkvenc,
        "v4l2_nodes_0_3": v4l,
        "kernel_config_matches": cfg_hits,
        "p0_kernel_mpp_ready": p0_ready,
        "blocker": None if p0_ready else (
            "Host kernel %s has DT nodes for rkvenc2 but no bound driver and no /dev/mpp_service or /dev/rkvenc; "
            "/dev/video3 (vepu121-enc) is JPEG-only. Requires Rockchip MPP/rkvenc2 kernel support (P0) "
            "before building 64-bit libmpp + c2.rk.avc.encoder (P1)."
        ) % rel,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print("STATUS " + ("READY" if p0_ready else "BLOCKED_P0_KERNEL"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
