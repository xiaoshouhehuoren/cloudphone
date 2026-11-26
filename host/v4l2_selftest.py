#!/usr/bin/env python3
import os
import subprocess
import sys
import time

sys.path.insert(0, "/root")
from g98_v4l2 import V4L2Output

W, H = 640, 480
FS = W * H * 3 // 2


def frame(y, u, v):
    return bytes([y]) * (W * H) + bytes([u]) * (W * H // 4) + bytes([v]) * (W * H // 4)


colors = [(76, 84, 255), (149, 43, 21), (29, 255, 107), (226, 149, 1)]
out = V4L2Output("/dev/video10", W, H)
out.open()
print("open ok buffers=%d" % out.count, flush=True)
cons = subprocess.Popen(["/root/ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2",
                         "-i", "/dev/video10", "-frames:v", "12", "-y", "/root/vt-%02d.png"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.5)
sent = 0
t0 = time.time()
while sent < 240 and time.time() - t0 < 12:
    if out.write_frame(frame(*colors[(sent // 30) % 4])):
        sent += 1
    else:
        time.sleep(0.01)
print("sent=%d in %.1fs" % (sent, time.time() - t0), flush=True)
cons.wait(timeout=10)
out.close()
print("done", flush=True)
