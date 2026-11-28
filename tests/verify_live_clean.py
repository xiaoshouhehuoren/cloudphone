#!/usr/bin/env python3
"""Live phone streams must not get synthetic grain/AE/fps resampling."""
import importlib.util
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/root/g98-camd.py"
spec = importlib.util.spec_from_file_location("camd_under_test", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
fails = 0
geo = {"fit": "cover", "rotate": 270, "ystretch": 1.0, "mirror": False}
cmd = m.source_cmd("rtsp", "rtsp://127.0.0.1:8554/cam-wx1", geo)
vf = cmd[cmd.index("-vf") + 1]
print("rtsp -vf:", vf)
for forbidden in ("noise=", "eq=brightness", "fps="):
    if m.LIVE_CLEAN and forbidden in vf:
        print("[FAIL] live filter contains", forbidden)
        fails += 1
for required in ("transpose=2", "scale=1280:960", "format=yuv420p"):
    if required not in vf:
        print("[FAIL] live filter lost geometry:", required)
        fails += 1
static_vf = m.build_vf(geo, fps=30)
print("file/static -vf:", static_vf)
if "fps=30" not in static_vf:
    print("[FAIL] non-live source lost fps pacing")
    fails += 1
if m.GRAIN > 0 and "noise=" not in static_vf:
    print("[FAIL] non-live source lost configured grain")
    fails += 1
print("RESULT " + ("PASS" if fails == 0 else "FAIL"))
sys.exit(0 if fails == 0 else 1)
