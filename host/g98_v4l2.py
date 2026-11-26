"""Minimal V4L2 output (producer) using mmap streaming - what ffmpeg does.

v4l2loopback accepts plain write(), but a plain write() producer has no backpressure:
it cannot know how many buffers the consumer has queued, so it can overrun the reader
queue and starve/stall the Android camera pipeline (CameraServiceWatchdog abort).
Streaming with QUERYBUF/QBUF/DQBUF gives exact backpressure: we only fill a buffer the
consumer actually handed back.
"""
import ctypes
import ctypes.util
import errno
import mmap
import os
import select
import struct

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
_libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
_libc.ioctl.restype = ctypes.c_int


def _ioctl(fd, request, obj):
    """ioctl through libc: Python fcntl does not accept ctypes structures."""
    if _libc.ioctl(fd, request, ctypes.byref(obj)) < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))

_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_DIRBITS = 2
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE = 1
_IOC_READ = 2


def _IOC(direction, typ, nr, size):
    return ((direction << _IOC_DIRSHIFT) | (ord(typ) << _IOC_TYPESHIFT)
            | (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT))


def _IOWR(typ, nr, size):
    return _IOC(_IOC_READ | _IOC_WRITE, typ, nr, size)


def _IOW(typ, nr, size):
    return _IOC(_IOC_WRITE, typ, nr, size)


class V4L2PixFormat(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pixelformat", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("bytesperline", ctypes.c_uint32),
        ("sizeimage", ctypes.c_uint32),
        ("colorspace", ctypes.c_uint32),
        ("priv", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("ycbcr_enc", ctypes.c_uint32),
        ("quantization", ctypes.c_uint32),
        ("xfer_func", ctypes.c_uint32),
    ]


class _FormatUnion(ctypes.Union):
    _fields_ = [("pix", V4L2PixFormat), ("raw", ctypes.c_byte * 200)]


class V4L2Format(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("_pad", ctypes.c_uint32), ("fmt", _FormatUnion)]


class V4L2RequestBuffers(ctypes.Structure):
    _fields_ = [
        ("count", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 1),
    ]


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _Timecode(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("frames", ctypes.c_ubyte),
        ("seconds", ctypes.c_ubyte),
        ("minutes", ctypes.c_ubyte),
        ("hours", ctypes.c_ubyte),
        ("userbits", ctypes.c_ubyte * 4),
    ]


class V4L2Buffer(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("bytesused", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("timestamp", _Timeval),
        ("timecode", _Timecode),
        ("sequence", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("offset", ctypes.c_uint32),
        ("length", ctypes.c_uint32),
        ("reserved2", ctypes.c_uint32),
        ("request_fd", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]


VIDIOC_S_FMT = _IOWR("V", 5, ctypes.sizeof(V4L2Format))
VIDIOC_REQBUFS = _IOWR("V", 8, ctypes.sizeof(V4L2RequestBuffers))
VIDIOC_QUERYBUF = _IOWR("V", 9, ctypes.sizeof(V4L2Buffer))
VIDIOC_QBUF = _IOWR("V", 15, ctypes.sizeof(V4L2Buffer))
VIDIOC_DQBUF = _IOWR("V", 17, ctypes.sizeof(V4L2Buffer))
VIDIOC_STREAMON = _IOW("V", 18, ctypes.sizeof(ctypes.c_int))
VIDIOC_STREAMOFF = _IOW("V", 19, ctypes.sizeof(ctypes.c_int))

V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_MEMORY_MMAP = 1


def fourcc(code):
    return struct.unpack("<I", code.encode())[0]


class V4L2Output:
    """Producer side of a v4l2loopback node with mmap streaming and real backpressure."""

    def __init__(self, node, width, height, pixfmt="YU12", buffers=4, timeout=0.5):
        self.node = node
        self.width = width
        self.height = height
        self.pixfmt = pixfmt
        self.buffers = buffers
        self.timeout = timeout
        self.frame_size = width * height * 3 // 2
        self.fd = -1
        self.maps = []

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def open(self):
        self.fd = os.open(self.node, os.O_RDWR | os.O_NONBLOCK)
        fmt = V4L2Format()
        fmt.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
        fmt.fmt.pix.width = self.width
        fmt.fmt.pix.height = self.height
        fmt.fmt.pix.pixelformat = fourcc(self.pixfmt)
        fmt.fmt.pix.field = 1  # V4L2_FIELD_NONE
        fmt.fmt.pix.bytesperline = self.width
        fmt.fmt.pix.sizeimage = self.frame_size
        fmt.fmt.pix.colorspace = 8  # V4L2_COLORSPACE_SRGB
        _ioctl(self.fd, VIDIOC_S_FMT, fmt)
        req = V4L2RequestBuffers()
        req.count = self.buffers
        req.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
        req.memory = V4L2_MEMORY_MMAP
        _ioctl(self.fd, VIDIOC_REQBUFS, req)
        self.count = req.count
        for index in range(self.count):
            buf = V4L2Buffer()
            buf.index = index
            buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
            buf.memory = V4L2_MEMORY_MMAP
            _ioctl(self.fd, VIDIOC_QUERYBUF, buf)
            mm = mmap.mmap(self.fd, buf.length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                           offset=buf.offset)
            self.maps.append((mm, buf.length))
        for index in range(self.count):
            buf = V4L2Buffer()
            buf.index = index
            buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
            buf.memory = V4L2_MEMORY_MMAP
            _ioctl(self.fd, VIDIOC_QBUF, buf)
        buf_type = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_OUTPUT)
        _ioctl(self.fd, VIDIOC_STREAMON, buf_type)
        return self

    def write_frame(self, frame):
        """Fill one dequeued buffer. Returns True on success, False if no buffer became free."""
        if self.fd < 0:
            return False
        if len(frame) != self.frame_size:
            return False
        r, _, _ = select.select([], [self.fd], [], self.timeout)
        if not r:
            return False
        buf = V4L2Buffer()
        buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
        buf.memory = V4L2_MEMORY_MMAP
        try:
            _ioctl(self.fd, VIDIOC_DQBUF, buf)
        except OSError:
            return False
        mm, length = self.maps[buf.index]
        n = min(len(frame), length)
        mm.seek(0)
        mm.write(frame[:n])
        buf.bytesused = n
        buf.field = 1
        try:
            _ioctl(self.fd, VIDIOC_QBUF, buf)
        except OSError:
            return False
        return True

    def close(self):
        if self.fd < 0:
            return
        try:
            buf_type = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_OUTPUT)
            _ioctl(self.fd, VIDIOC_STREAMOFF, buf_type)
        except OSError:
            pass
        for mm, _ in self.maps:
            try:
                mm.close()
            except Exception:  # noqa: BLE001
                pass
        self.maps = []
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = -1
