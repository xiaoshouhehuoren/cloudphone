#!/usr/bin/env python3
import ctypes, fcntl, os, sys, struct
sys.path.insert(0, "/root")
import g98_v4l2 as v

print("sizes fmt=%d req=%d buf=%d" % (ctypes.sizeof(v.V4L2Format), ctypes.sizeof(v.V4L2RequestBuffers), ctypes.sizeof(v.V4L2Buffer)))
print("S_FMT=0x%x REQBUFS=0x%x QBUF=0x%x" % (v.VIDIOC_S_FMT, v.VIDIOC_REQBUFS, v.VIDIOC_QBUF))

fd = os.open("/dev/video10", os.O_RDWR | os.O_NONBLOCK)
fmt = v.V4L2Format()
fmt.type = 2
fmt.fmt.pix.width = 640
fmt.fmt.pix.height = 480
fmt.fmt.pix.pixelformat = v.fourcc("YU12")
fmt.fmt.pix.field = 1
fmt.fmt.pix.bytesperline = 640
fmt.fmt.pix.sizeimage = 460800
try:
    fcntl.ioctl(fd, v.VIDIOC_S_FMT, fmt, True)
    print("S_FMT ok: type=%d w=%d h=%d pf=0x%x bpl=%d size=%d" % (fmt.type, fmt.fmt.pix.width, fmt.fmt.pix.height, fmt.fmt.pix.pixelformat, fmt.fmt.pix.bytesperline, fmt.fmt.pix.sizeimage))
except OSError as e:
    print("S_FMT fail", e)
for btype in (2, 1):
    req = v.V4L2RequestBuffers()
    req.count = 4
    req.type = btype
    req.memory = 1
    try:
        fcntl.ioctl(fd, v.VIDIOC_REQBUFS, req, True)
        print("REQBUFS type=%d ok count=%d" % (btype, req.count))
    except OSError as e:
        print("REQBUFS type=%d fail %s" % (btype, e))
os.close(fd)
