#!/usr/bin/env python3
import ctypes, os, subprocess, sys, time
sys.path.insert(0, "/root")
import g98_v4l2 as v

W, H = 640, 480
FS = W * H * 3 // 2

def frame(y, u, vv):
    return bytes([y]) * (W * H) + bytes([u]) * (W * H // 4) + bytes([vv]) * (W * H // 4)

fd = os.open("/dev/video10", os.O_WRONLY | os.O_NONBLOCK)
fmt = v.V4L2Format()
fmt.type = 2
fmt.fmt.pix.width = W
fmt.fmt.pix.height = H
fmt.fmt.pix.pixelformat = v.fourcc("YU12")
fmt.fmt.pix.field = 1
fmt.fmt.pix.bytesperline = W
fmt.fmt.pix.sizeimage = FS
try:
    v._ioctl(fd, v.VIDIOC_S_FMT, fmt)
    print("S_FMT ok w=%d h=%d pf=0x%x size=%d" % (fmt.fmt.pix.width, fmt.fmt.pix.height, fmt.fmt.pix.pixelformat, fmt.fmt.pix.sizeimage))
except OSError as e:
    print("S_FMT fail", e)
print("format3:", open("/sys/devices/virtual/video4linux/video10/format").read().strip())
cons = subprocess.Popen(["/root/ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2",
                         "-i", "/dev/video10", "-frames:v", "8", "-y", "/root/wp-%02d.png"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.6)
ok = 0
t0 = time.time()
while ok < 120 and time.time() - t0 < 8:
    try:
        n = os.write(fd, frame(76, 84, 255))
        if n == FS:
            ok += 1
        else:
            print("short write", n)
            break
    except OSError as e:
        print("write err", e)
        break
    time.sleep(0.02)
print("wrote=%d" % ok)
try:
    cons.wait(timeout=8)
except Exception:
    cons.kill()
print("consumer rc", cons.returncode)
os.close(fd)
