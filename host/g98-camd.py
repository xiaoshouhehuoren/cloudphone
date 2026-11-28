#!/usr/bin/env python3
"""g98-camd - persistent V4L2 frame router for the G98 cloud-phone camera.

One long-lived writer thread per v4l2loopback node keeps the device format
pinned and always produces frames, so the Android camera HAL never sees a
stalled producer. Sources (idle card, image, video, RTSP) are swapped by
starting a new ffmpeg reader; the last frame is kept until the new reader
delivers, which makes switching effectively instantaneous and restart-free.

Control API (JSON, localhost):
  POST /set   {"node":"/dev/video10","kind":"video","path":"/root/camin/x.mp4"}
  POST /idle  {"node":"/dev/video10"}
  GET  /state -> {"nodes":{...}}
"""
import collections
import errno
import faulthandler
import json
import math
import os
import queue
import random
import re
import select
import signal
import stat
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Node/output resolution.  4:3 like the original 640x480 so the app's preview
# stretch factor measured on wx1 (YSTRETCH below) stays valid; 1280x960 is
# 4x the pixels of VGA, which is what actually makes the preview sharp.
W = int(os.environ.get("G98_CAMD_W", "1280"))
H = int(os.environ.get("G98_CAMD_H", "960"))
FRAME_SIZE = W * H * 3 // 2
FPS = int(os.environ.get("G98_CAMD_FPS", "30"))
# A reader that produces nothing within START_TIMEOUT seconds is restarted; a
# running source that goes silent for MAX_SILENCE seconds is restarted too.
START_TIMEOUT = float(os.environ.get("G98_CAMD_START_TIMEOUT", "8"))
# 源静默多久才判定死掉并重连。重连会丢掉 ffmpeg 进程，而在重连的空窗里节点没有新帧；
# 微信走的是旧版 Camera API（API1），它的 Camera2Client 把 HAL 的 RESULT 错误当致命错误，
# 一旦节点断流就弹 "Failed to obtain configurations"。所以这里放宽到 30s：短抖动
# （手机推流暂停/RTSP 卡顿）只让画面停住，不会反复重连。
MAX_SILENCE = float(os.environ.get("G98_CAMD_MAX_SILENCE", "30"))
# 源多久没有新帧就进入 hold（重复最后一帧）。比 MAX_SILENCE 小得多：1s 没新帧就
# 说明推流断了/卡了，画面立即冻结在最后一帧，而不是等 30s 后重连。
HOLD_AFTER = float(os.environ.get("G98_CAMD_HOLD_AFTER", "1.0"))
# A source that has never produced a frame (usually an RTSP path with no
# publisher) gets exponential retry backoff.  The first connection is still
# immediate; only empty attempts are delayed.  Any decoded frame resets this.
RETRY_MIN = float(os.environ.get("G98_CAMD_RETRY_MIN", "2"))
RETRY_MAX = float(os.environ.get("G98_CAMD_RETRY_MAX", "15"))
IDLE_FPS = int(os.environ.get("G98_CAMD_IDLE_FPS", "5"))
# 静态帧（idle/image）重复写的节拍。30fps 时 10 个 1280x960 节点要写约 550MB/s，
# camd 自身 ~30% CPU、每台 provider ~17%；静态画面降频不影响观感，
# 但低于 ~10fps 容易让 HAL 的 capture request 排队超过 5s(看门狗)，默认 12。
STATIC_FPS = int(os.environ.get("G98_CAMD_STATIC_FPS", "12"))
# How a source is fitted into the node's %dx%d frame.  contain = keep the whole
# picture, black bars; cover = fill the node, crop the overflow; stretch = fill
# and distort (only for deliberate testing).  rotate/mirror match what a phone
# camera would do to a portrait recording.
FIT = os.environ.get("G98_CAMD_FIT", "contain")
ROTATE = int(os.environ.get("G98_CAMD_ROTATE", "0"))
MIRROR = os.environ.get("G98_CAMD_MIRROR", "0") == "1"
# 前置节点默认不预翻转（模拟真机）：Android 相机 App / 微信等取景器会把前置画面自己
# 做一次左右镜像，真机上看到的就是"镜子里的自己"。早先默认 hflip 一次会把 App 的镜像
# 抵消掉，画面变成"注入源原样"，反而与真机相反（用户反馈"摄像头左右反了"）。
# 需要"注入什么就显示什么"时设 G98_CAMD_AUTOFRONT_MIRROR=1，
# 或用 POST /api/cam/start 的 mirror 字段 / 控制台「前置镜像」下拉单独覆盖。
AUTOFRONT = os.environ.get("G98_CAMD_AUTOFRONT_MIRROR", "0") == "1"
# 注入流"像真机"：真实相机每帧都有传感器噪声、亮度缓慢呼吸、帧间隔抖动，
# 而解码流/静态图完全没有这些特征（帧间像素完全相同的"贴图流"），人脸活体/
# 反翻拍检测很容易据此判假。下面几项给注入画面补上这些统计特征：
#   GRAIN           动态源（video/rtsp/jpeg）每帧随机噪声强度
#   STATIC_GRAIN    静态源（image/idle）噪声强度，靠预渲染多帧循环实现
#   STATIC_VARIANTS 静态源预渲染帧数（越大颗粒变化越自然，内存 ×1.8MB/帧）
#   FRAME_JITTER    写帧周期的随机抖动比例（0.18 = ±18%）
#   AE_BREATH/AE_PERIOD  整体亮度正弦呼吸幅度与周期（秒）
GRAIN = int(os.environ.get("G98_CAMD_GRAIN", "6"))
STATIC_GRAIN = int(os.environ.get("G98_CAMD_STATIC_GRAIN", "5"))
STATIC_VARIANTS = max(1, int(os.environ.get("G98_CAMD_STATIC_VARIANTS", "6")))
FRAME_JITTER = float(os.environ.get("G98_CAMD_FRAME_JITTER", "0.18"))
AE_BREATH = float(os.environ.get("G98_CAMD_AE_BREATH", "0.02"))
# 手机实时源本身已经有传感器噪声、曝光与帧间隔；再叠一层合成噪声/呼吸/抖动只会
# 让微信预览出现跳变并干扰反光活体。默认实时源走纯净链路，静态贴图仍保留拟真。
LIVE_CLEAN = os.environ.get("G98_CAMD_LIVE_CLEAN", "1") == "1"
AE_PERIOD = float(os.environ.get("G98_CAMD_AE_PERIOD", "19"))


def node_is_front(node):
    m = re.search(r"(\d+)\s*$", node or "")
    return bool(m) and int(m.group(1)) % 2 == 1
# The stock camera app stretches the 640x480 node buffer onto a portrait preview
# surface (measured on wx1: 640x480 -> 1080x1378, so vertical is 1.70x the
# horizontal scale).  Pre-compensating in the buffer keeps circles round and
# faces undistorted on screen.  Set to 1.0 to disable.
YSTRETCH = float(os.environ.get("G98_CAMD_YSTRETCH", "1.0"))
FFMPEG = "/root/ffmpeg"
KEEPF = "/root/keepf"
STATE_LOCK = threading.Lock()
STATE = {"nodes": {}}


def idle_cmd(node):
    label = "IDLE " + os.path.basename(node)
    return [FFMPEG, "-hide_banner", "-loglevel", "error", "-re",
            "-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=10" % (W, H),
            "-vf", "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
                   "text='%s':fontcolor=white:fontsize=22:box=1:boxcolor=black@0.6:"
                   "x=(w-text_w)/2:y=(h-text_h)/2,format=yuv420p" % label,
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]


_BREATH_TABLE = {}


def breath_table(delta):
    """Y 平面亮度的整数查表：把每像素 +delta 后 clamp 到 0..255。

    bytes.translate 是 C 实现，1.2MB 的 Y 平面一次不到 1ms，比逐像素 Python
    循环快三个数量级；delta 按 1/255 量化后缓存，表最多 512 份。
    """
    step = int(round(delta * 255))
    tab = _BREATH_TABLE.get(step)
    if tab is None:
        tab = bytes(min(255, max(0, v + step)) for v in range(256))
        if len(_BREATH_TABLE) < 600:
            _BREATH_TABLE[step] = tab
    return tab


def breath_step(ts):
    """当前时刻的亮度偏移（单位 1/255 的整数级），用于查表与缓存判定。"""
    if AE_BREATH <= 0 or AE_PERIOD <= 0:
        return 0
    return int(round(AE_BREATH * 255.0 * math.sin(2.0 * math.pi * ts / AE_PERIOD)))


def breathe(frame, ts, step=None):
    """给一帧叠加缓慢的 AE 呼吸（只动 Y 平面，色度不变，肉眼是"曝光在飘"）。"""
    if step is None:
        step = breath_step(ts)
    if step == 0:
        return frame
    return frame[:W * H].translate(breath_table(step / 255.0)) + frame[W * H:]


def render_static(cmd):
    """Render ONE frame to a raw YU12 buffer for the static (idle/image) kinds.

    A 30 fps ffmpeg per node costs up to half a core: for a picture that never
    changes we decode it once and let the writer thread repeat the same 460800
    byte buffer.  The node still gets fed at full rate, which is what the camera
    HAL's 5 second watchdog needs.
    """
    args = list(cmd)
    if args and args[-1] == "-":
        args = args[:-1] + ["-frames:v", "1", "-"]
    else:
        args = args + ["-frames:v", "1"]
    try:
        res = subprocess.run(args, capture_output=True, timeout=25)
    except Exception as exc:  # noqa: BLE001
        print("camd: render_static failed: %s" % exc, flush=True)
        return None
    data = res.stdout
    if len(data) < FRAME_SIZE:
        print("camd: render_static short frame %d/%d" % (len(data), FRAME_SIZE),
              flush=True)
        return None
    return data[:FRAME_SIZE]


def render_static_set(cmd, count):
    """Render `count` consecutive frames with per-frame grain, for cycling.

    静态源只解码一次再重复会得到帧间像素完全相同的"贴图流"。这里一次性渲染
    若干带独立时域噪声的帧，写线程轮流发，肉眼就是"传感器有颗粒在动"。
    count=1 时不加噪声，行为与 render_static 一致。
    """
    if count <= 1:
        one = render_static(cmd)
        return [one] if one is not None else []
    args = list(cmd)
    if STATIC_GRAIN > 0:
        try:
            i = args.index("-vf")
        except ValueError:
            i = -1
        if i >= 0:
            args[i + 1] = args[i + 1] + ",noise=alls=%d:allf=t" % STATIC_GRAIN
    if args and args[-1] == "-":
        args = args[:-1] + ["-frames:v", str(count), "-"]
    else:
        args = args + ["-frames:v", str(count)]
    try:
        res = subprocess.run(args, capture_output=True, timeout=40)
    except Exception as exc:  # noqa: BLE001
        print("camd: render_static_set failed: %s" % exc, flush=True)
        return []
    data = res.stdout
    frames = [data[o:o + FRAME_SIZE]
              for o in range(0, len(data) - FRAME_SIZE + 1, FRAME_SIZE)]
    if not frames:
        print("camd: render_static_set short %d/%d" % (len(data), FRAME_SIZE),
              flush=True)
    return frames


def ystretch_filter(ys):
    """横向预拉伸，让 buffer 内容的宽高比 = 源宽高比 × ys（抵消预览拉伸）。"""
    if abs(ys - 1.0) < 0.001:
        return None
    return "scale=w='trunc(iw*%.6f/2)*2':h=ih" % ys


def jpeg_vf(mirror=False):
    # 尺寸无关：手机推来的 JPEG 可能是任意分辨率，先横向预拉伸再 standard fit。
    parts = []
    if mirror:
        parts.append("hflip")
    ys = ystretch_filter(YSTRETCH)
    if ys:
        parts.append(ys)
    parts.append("scale=%d:%d:force_original_aspect_ratio=decrease" % (W, H))
    parts.append("pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black" % (W, H))
    parts.append("format=yuv420p")
    return ",".join(parts)


def jpeg_one_cmd(path, mirror=False):
    """Decode ONE JPEG file into one YU12 frame (about 44 ms per frame)."""
    return [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "mjpeg",
            "-i", path, "-vf", jpeg_vf(mirror), "-f", "rawvideo",
            "-pix_fmt", "yuv420p", "-"]


def jpeg_cmd():
    """Decode JPEG frames pushed over HTTP (browser fallback when WebRTC fails)."""
    # nobuffer/low_delay: without them the mjpeg demuxer sits on the pipe
    # (never EOF for a live stream) and emits nothing at all.
    # A single long-lived mjpeg decoder on pipe:0 stalls after the first
    # frame here (40 JPEGs in, 1 frame out), so JPEG frames are decoded one
    # at a time by JpegDecoder instead.
    return jpeg_one_cmd("/tmp/camd-unused.jpg")


def probe_media(path):
    """Source geometry straight from ffmpeg's banner (no ffprobe on the board).

    The numbers here are what the UI and the verifier compare against the node
    format and against the camera app's stream configuration.
    """
    info = {"width": None, "height": None, "fps": None, "duration": None,
            "rotation": 0, "codec": None, "pix_fmt": None}
    if not path or not os.path.exists(path):
        return info
    try:
        res = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                             capture_output=True, timeout=8)
        text = res.stderr.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return info
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", text)
    if m:
        info["duration"] = round(int(m.group(1)) * 3600 + int(m.group(2)) * 60
                                 + float(m.group(3)), 3)
    m = re.search(r"Video: ([^\n]+)", text)
    if m:
        line = m.group(1)
        info["codec"] = line.split(",")[0].split()[0]
        mm = re.search(r"(\d{2,5})x(\d{2,5})", line)
        if mm:
            info["width"], info["height"] = int(mm.group(1)), int(mm.group(2))
        mm = re.search(r"([\d.]+) fps", line)
        if mm:
            info["fps"] = float(mm.group(1))
        mm = re.search(r"\b(yuv\w+|nv12|rgb\w*)\b", line)
        if mm:
            info["pix_fmt"] = mm.group(1)
    m = re.search(r"rotate\s*:\s*(-?\d+)", text)
    if m:
        info["rotation"] = int(m.group(1)) % 360
    m = re.search(r"rotation of (-?[\d.]+) degrees", text)
    if m:
        info["rotation"] = int(round(float(m.group(1)))) % 360
    return info


def geometry(src, fit=None, rotate=None, mirror=None, ystretch=None):
    """Expected picture rect inside the node frame, for the given fit mode.

    ystretch = (screen px per buffer row) / (screen px per buffer column).  The
    app does not keep the buffer aspect, so the buffer content is composed for
    the *displayed* aspect: a_source * ystretch.
    """
    fit = (fit or FIT).lower()
    rotate = int(rotate if rotate is not None else ROTATE) % 360
    mirror = MIRROR if mirror is None else bool(mirror)
    ys = YSTRETCH if ystretch is None else float(ystretch)
    sw = int(src.get("width") or W)
    sh = int(src.get("height") or H)
    dw, dh = (sh, sw) if rotate in (90, 270) else (sw, sh)
    src_dar = dw / float(dh)
    want = src_dar * ys          # aspect the buffer content needs to have
    if fit == "stretch":
        cw, ch = W, H
    elif fit == "cover":
        if want >= W / float(H):
            ch, cw = H, round(H * want)
        else:
            cw, ch = W, round(W / want)
        cw, ch = max(W, cw), max(H, ch)
    else:
        if want >= W / float(H):
            cw, ch = W, round(W / want)
        else:
            ch, cw = H, round(H * want)
        cw, ch = min(W, cw), min(H, ch)
    cw, ch = max(2, cw - cw % 2), max(2, ch - ch % 2)
    return {
        "fit": fit,
        # 直播流在 /set 时还没出帧，probe 拿不到真实尺寸；滤镜按输入分辨率动态
        # 计算（force_original_aspect_ratio），这里的 content 只是占位值。
        "dynamic": not src.get("width"),
        "ystretch": round(ys, 4),
        "rotate": rotate,
        "mirror": bool(mirror),
        "src_w": sw, "src_h": sh,
        "disp_w": dw, "disp_h": dh,
        "out_w": W, "out_h": H,
        "content_w": cw, "content_h": ch,
        "content_x": (W - cw) // 2,
        "content_y": (H - ch) // 2,
        "src_dar": round(src_dar, 4),
        "content_dar": round(cw / float(ch), 4),
        "display_dar": round(cw / float(ch) / ys, 4),
    }


def build_vf(geo, fps=None, live=False):
    chain = []
    rot = geo["rotate"]
    if rot == 90:
        chain.append("transpose=1")
    elif rot == 180:
        chain.append("transpose=2,transpose=2")
    elif rot == 270:
        chain.append("transpose=2")
    if geo["mirror"]:
        chain.append("hflip")
    # 预拉伸之后用 ffmpeg 自己的 fit 函数，整套链路对输入分辨率无关：
    # 手机直播流的实际尺寸往往在 /set 时还没就绪（probe 拿不到），
    # 不能把 probe 出来的 1280x960 当成内容尺寸写死，否则画面被压扁/拉长。
    pre = ystretch_filter(geo["ystretch"])
    if pre:
        chain.append(pre)
    if geo["fit"] == "stretch":
        chain.append("scale=%d:%d" % (W, H))
    elif geo["fit"] == "cover":
        # Cover scales past the frame and crops the overflow: the rawvideo
        # stream must stay exactly WxH or the writer reads misaligned frames.
        chain.append("scale=%d:%d:force_original_aspect_ratio=increase"
                     % (W, H))
        chain.append("crop=%d:%d:(iw-ow)/2:(ih-oh)/2" % (W, H))
    else:
        chain.append("scale=%d:%d:force_original_aspect_ratio=decrease"
                     % (W, H))
        chain.append("pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black" % (W, H))
    # 实时流不经过 fps 重采样：源有抖动时 fps 滤镜会主动重复/丢帧，写线程本来
    # 就只取最新帧，再叠这层会变成可见的画面跳动。
    if fps and not (live and LIVE_CLEAN):
        chain.append("fps=%d" % fps)
    # 真机传感器的时域噪声与曝光呼吸只补给静态/文件源；实时手机源保持原样。
    if not (live and LIVE_CLEAN):
        if GRAIN > 0:
            chain.append("noise=alls=%d:allf=t+u" % GRAIN)
        if AE_BREATH > 0 and AE_PERIOD > 0:
            chain.append("eq=brightness=%f*sin(2*PI*t/%f)" % (AE_BREATH, AE_PERIOD))
    chain.append("format=yuv420p")
    return ",".join(chain)


def source_cmd(kind, arg, geo, fps=None):
    if kind == "jpeg":
        return jpeg_one_cmd("/tmp/camd-unused.jpg")
    if kind == "image":
        return [FFMPEG, "-hide_banner", "-loglevel", "error", "-re",
                "-loop", "1", "-i", arg, "-vf", build_vf(geo),
                "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
    if kind == "video":
        return [FFMPEG, "-hide_banner", "-loglevel", "error", "-re",
                "-stream_loop", "-1", "-i", arg, "-vf", build_vf(geo, fps or FPS),
                "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
    if kind == "rtsp":
        # Low-latency pull: the phone encoder and mediamtx already add delay, so
        # do not let ffmpeg's demuxer/decoder add its own analysis + reorder
        # buffering on top (that alone was worth ~1s of lag behind the phone).
        return [FFMPEG, "-hide_banner", "-loglevel", "error",
                "-fflags", "nobuffer", "-flags", "low_delay",
                "-analyzeduration", "0", "-probesize", "32",
                "-max_delay", "0", "-reorder_queue_size", "0",
                "-rtsp_transport", "tcp",
                "-i", arg, "-vf", build_vf(geo, fps or FPS, live=True),
                "-flush_packets", "1",
                "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
    raise ValueError("bad kind " + kind)


class Reader(threading.Thread):
    """Runs one ffmpeg source and publishes complete frames."""

    def __init__(self, node, kind, arg, on_frame, geo=None):
        super().__init__(daemon=True)
        self.node = node
        self.kind = kind
        self.arg = arg
        self.geo = geo or geometry({})
        self.on_frame = on_frame
        self.proc = None
        self.stop = threading.Event()
        self.frames = 0
        self.restarts = 0
        self.retry_delay = 0.0
        self.stdin_lock = threading.RLock()
        self.dropped = 0
        self.fed_frames = 0
        # Frames that arrive before the decoder exists.  The mjpeg demuxer gets
        # stuck if it is started against an empty pipe, so we spawn it lazily
        # once the first frame is actually here.
        self.pending = collections.deque(maxlen=6)

    def feed(self, data):
        """Push one encoded JPEG into the decoder; drops frames instead of blocking."""
        proc = self.proc
        with self.stdin_lock:
            if proc is None or proc.stdin is None:
                self.pending.append(data)
                return True
            view = memoryview(data)
            try:
                # A pipe write may be partial: send the whole JPEG or nothing,
                # otherwise the mjpeg demuxer sees a truncated image and drops it.
                while view:
                    n = proc.stdin.write(view)
                    if not n:
                        self.dropped += 1
                        return False
                    view = view[n:]
                proc.stdin.flush()
                self.fed_frames += 1
                if self.fed_frames <= 3 or self.fed_frames % 100 == 0:
                    print("camd: jpeg fed #%d %dB (dropped %d)" %
                          (self.fed_frames, len(data), self.dropped), flush=True)
                return True
            except (BlockingIOError, OSError, ValueError):
                self.dropped += 1
                return False

    def run(self):
        # A source can die or not exist yet (e.g. an RTSP publisher that comes up
        # after us). Keep retrying instead of leaving the node on its last frame.
        while not self.stop.is_set():
            frames_before = self.frames
            self._run_once()
            if self.stop.is_set():
                break
            self.restarts += 1
            if self.frames > frames_before:
                # A source that worked gets a prompt reconnect after it ends.
                self.retry_delay = 0.0
                delay = 1.0
            else:
                # Do not relaunch ffmpeg in a tight loop while nobody publishes.
                # 2, 4, 8, 15, 15... seconds; reset above/on the first real frame.
                self.retry_delay = (RETRY_MIN if self.retry_delay <= 0
                                    else min(self.retry_delay * 2, RETRY_MAX))
                delay = self.retry_delay
            print("camd: source %s %s ended, restart #%d in %.0fs (%d new frames)" %
                  (self.kind, self.arg, self.restarts, delay,
                   self.frames - frames_before), flush=True)
            # Event.wait, unlike sleep, lets set_source() cancel the backoff.
            self.stop.wait(delay)

    def _run_once(self):
        if self.kind == "jpeg":
            # Wait for real data before spawning: starting ffmpeg on an empty
            # pipe makes its mjpeg demuxer block and never recover.
            deadline = time.time() + 60
            while not self.stop.is_set() and time.time() < deadline:
                with self.stdin_lock:
                    if self.pending:
                        break
                time.sleep(0.05)
            if self.stop.is_set():
                return
        cmd = idle_cmd(self.node) if self.kind == "idle" else source_cmd(self.kind, self.arg, self.geo)
        print("camd: reader spawn %s %s" % (self.node, " ".join(cmd[-6:])), flush=True)
        try:
            errsink = open("/tmp/camd-jpeg.err", "ab") if self.kind == "jpeg" else subprocess.DEVNULL
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE,
                                         stderr=errsink, bufsize=0)
        except Exception as exc:  # noqa: BLE001
            print("camd: spawn failed %s: %s" % (self.kind, exc), flush=True)
            return

        if self.kind == "jpeg":
            with self.stdin_lock:
                pre = list(self.pending)
                self.pending.clear()
            print("camd: jpeg decoder up pid=%d pre=%d" % (self.proc.pid, len(pre)), flush=True)
            for d in pre:
                self.feed(d)
            print("camd: jpeg pre-fed ok=%d" % len(pre), flush=True)
        buf = bytearray()
        fd = self.proc.stdout
        # Non-blocking reads: select() can report readiness for a partially
        # filled pipe or a stream that stalls mid-frame, and a blocking read
        # would then hang this thread forever with the node frozen.
        try:
            os.set_blocking(fd.fileno(), False)
        except OSError:
            pass
        started = last = time.time()
        while not self.stop.is_set():
            # Timeouts are enforced here, not only when select() reports idle: a
            # source that is connected but never sends (RTSP path with no
            # publisher) can keep the pipe "ready" with zero bytes forever.
            now = time.time()
            if now - last > MAX_SILENCE or (self.frames == 0 and now - started > START_TIMEOUT):
                break
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            try:
                # A non-blocking FileIO read returns None when the kernel has
                # nothing for us, and b"" only at EOF.
                chunk = fd.read(FRAME_SIZE * 4)
            except BlockingIOError:
                continue
            if chunk is None:
                continue
            if not chunk:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.005)
                continue
            last = time.time()
            buf += chunk
            while len(buf) >= FRAME_SIZE:
                frame = bytes(buf[:FRAME_SIZE])
                del buf[:FRAME_SIZE]
                self.frames += 1
                self.retry_delay = 0.0
                self.on_frame(frame)
        if self.kind == "jpeg":
            print("camd: jpeg decoder exit frames=%d poll=%s" % (self.frames, self.proc.poll()), flush=True)
        # Do NOT set self.stop here: that belongs to set_source(). Leaving it set
        # made a dead source permanent, so run() never got to restart it.
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # ffmpeg blocked writing into a full stdout pipe ignores SIGTERM
                # for a moment; SIGKILL it instead of leaking the reader thread.
                self.proc.kill()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        try:
            if self.proc.stdout is not None:
                self.proc.stdout.close()
        except OSError:
            pass


class JpegDecoder(threading.Thread):
    """Decode pushed JPEG frames into YU12 frames, one ffmpeg per frame."""

    def __init__(self, node, on_frame, mirror=False):
        super().__init__(daemon=True)
        self.node = node
        self.on_frame = on_frame
        self.mirror = bool(mirror)
        self.q = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.frames = 0
        self.dropped = 0
        self.errors = 0
        self.restarts = 0
        self.proc = None
        self.tmp = "/dev/shm/camd-%s.jpg" % node.rsplit("/", 1)[-1]

    def push(self, data):
        """Queue one JPEG; a full queue drops the stale frame instead of lagging."""
        try:
            self.q.put_nowait(data)
            return True
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            self.dropped += 1
            try:
                self.q.put_nowait(data)
            except queue.Full:
                pass
            return True

    def run(self):
        while not self.stop.is_set():
            try:
                data = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with open(self.tmp, "wb") as fh:
                    fh.write(data)
                res = subprocess.run(jpeg_one_cmd(self.tmp, self.mirror),
                                     capture_output=True, timeout=5)
                if len(res.stdout) == FRAME_SIZE:
                    self.frames += 1
                    self.on_frame(res.stdout)
                else:
                    self.errors += 1
                    if self.errors <= 3 or self.errors % 200 == 0:
                        print("camd: jpeg decode %s short %dB rc=%d" %
                              (self.node, len(res.stdout), res.returncode), flush=True)
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                if self.errors <= 3 or self.errors % 200 == 0:
                    print("camd: jpeg decode %s failed: %s" % (self.node, exc), flush=True)
        try:
            os.unlink(self.tmp)
        except OSError:
            pass


class NodeRouter:
    def __init__(self, node):
        self.node = node
        self.lock = threading.Lock()
        self.frame = None
        self.static = None
        self.seq = 0
        self.kind = None
        self.arg = None
        self.since = 0.0
        self.reader = None
        self.decoder = None
        self.fd = None
        self.written = 0
        self.errors = 0
        self.consecutive_errors = 0
        # Source geometry from ffmpeg plus the rect the picture occupies inside
        # the node frame; the dashboard shows it and tests compare it with the
        # node format and the camera app's stream size.
        self.src = {}
        self.fit = FIT
        self.rotate = ROTATE
        self.mirror = MIRROR
        self.ystretch = YSTRETCH
        self.geo = geometry({}, self.fit, self.rotate, self.mirror, self.ystretch)
        # 最后一次拿到“新”源帧的时间；超过 HOLD_AFTER 秒即认为源静默，写线程改为
        # 原样重发最后一帧（hold），保证节点始终有新帧可 dequeue，HAL 不会超时。
        self.last_frame_ts = 0.0
        self.holding = False
        # 静态源的预渲染帧序列（每帧带独立噪声），以及循环下标。
        self.static_set = []
        self.static_idx = 0
        # 呼吸合成很贵（1.8MB 拼接 ≈3.7ms/帧），按"亮度档"整组缓存：档位每 ~2s
        # 才变一次，变时把全部变体重算一遍（6 帧 ≈ 22ms），之后每帧零成本。
        self.breath_step = None
        self.breath_frames = []
        self.stop = threading.Event()
        self.writer = threading.Thread(target=self._write_loop, daemon=True)
        self.writer.start()
        self.set_source("idle", None)

    def _reopen(self):
        if self.fd is not None:
            try:
                self.fd.close()
            except Exception:  # noqa: BLE001
                pass
        self.fd = open(self.node, "wb", buffering=0)
        return self.fd

    def _write_loop(self):
        while not self.stop.is_set():
            t0 = time.time()
            with self.lock:
                static = self.static
                frame = static or self.frame
                kind = self.kind
                last_ts = self.last_frame_ts
                holding = self.holding
            # 源静默（推流断了/手机没推）时写线程仍旧按 30fps 重发最后一帧：
            # 节点上永远有“新”buffer，HAL 的 capture request 不会 5s 超时，
            # 微信这类 API1 客户端也就不会把会话判死。完全没有帧时补黑帧。
            now = time.time()
            silent = (static is None and frame is not None and last_ts > 0
                      and now - last_ts > HOLD_AFTER)
            if silent and not holding:
                with self.lock:
                    self.holding = True
                print("camd: %s source silent %.1fs, holding last frame" %
                      (self.node, now - last_ts), flush=True)
            elif holding and not silent:
                with self.lock:
                    self.holding = False
                print("camd: %s source resumed" % self.node, flush=True)
            if frame is None and kind not in (None, "idle"):
                frame = blank_frame()
            # 静态源：轮流发预渲染的带噪帧，再叠一层缓慢 AE 呼吸，避免"贴图流".
            if static is not None and self.static_set:
                with self.lock:
                    self.static_idx = (self.static_idx + 1) % len(self.static_set)
                    frame = self.static_set[self.static_idx]
                # idle 只是占位彩条，不是"模拟摄像头"，不值得为它付呼吸缓存的内存。
                step = 0 if kind == "idle" else breath_step(now)
                if step != self.breath_step:
                    self.breath_frames = [breathe(f, now, step) for f in self.static_set]
                    self.breath_step = step
                if self.breath_frames and len(self.breath_frames) == len(self.static_set):
                    frame = self.breath_frames[self.static_idx]
            if frame is not None:
                try:
                    # Blocking write: v4l2loopback only accepts one frame when a
                    # reader buffer is free, so this paces us to the consumer.
                    view = memoryview(frame)
                    off = 0
                    while off < len(frame):
                        n = self.fd.write(view[off:])
                        if not n:
                            raise OSError("short write")
                        off += n
                    self.written += 1
                    self.consecutive_errors = 0
                except BlockingIOError:
                    # Consumer has no free buffer (it is not dequeueing right
                    # now). That is backpressure, not a broken fd: wait and retry
                    # instead of reopening the node and spamming the journal.
                    time.sleep(0.05)
                except OSError as exc:
                    if exc.errno == errno.EBUSY:
                        time.sleep(0.05)
                        continue
                    self.errors += 1
                    self.consecutive_errors += 1
                    if self.consecutive_errors <= 3 or self.consecutive_errors % 200 == 0:
                        print("camd: write %s failed (#%d): %s" %
                              (self.node, self.consecutive_errors, exc), flush=True)
                    # A renegotiated/closed loopback node leaves the old fd
                    # unusable; reopen rather than writing into a dead handle.
                    try:
                        self._reopen()
                    except Exception as rexc:  # noqa: BLE001
                        print("camd: reopen %s failed: %s" % (self.node, rexc), flush=True)
                        time.sleep(0.5)
            # An idle card only has to keep the node's format alive; pushing it at
            # the full source rate burns a core per node for nothing.
            # 静态帧（idle/image）重复写很便宜，按满速喂节点；只有视频/RTSP 用
            # IDLE_FPS 降频是没有意义的，所以这里只对非静态源保留原逻辑。
            with self.lock:
                is_static = self.static is not None
            period = 1.0 / (STATIC_FPS if is_static
                            else (FPS if kind != "idle" else IDLE_FPS))
            # 真机的帧间隔不是恒定值（曝光/传输抖动），固定节拍本身也是"合成流"特征。
            if FRAME_JITTER > 0 and not (LIVE_CLEAN and kind in ("rtsp", "jpeg")):
                period *= 1.0 + random.uniform(-FRAME_JITTER, FRAME_JITTER)
            dt = period - (time.time() - t0)
            if dt > 0:
                time.sleep(dt)

    def _on_frame(self, frame):
        with self.lock:
            self.frame = frame
            self.last_frame_ts = time.time()
            self.seq += 1

    def set_source(self, kind, arg, fit=None, rotate=None, mirror=None,
                   ystretch=None):
        src = probe_media(arg) if kind in ("image", "video") else {}
        fit = (fit or self.fit or FIT)
        rotate = self.rotate if rotate is None else int(rotate)
        if mirror is None:
            # 默认不预翻转：相机 App 自己镜像前置取景，和真机自拍一致。
            # AUTOFRONT=1 才会抵消 App 镜像，让前置也按注入源原方向显示。
            mirror = bool(AUTOFRONT and node_is_front(self.node))
        else:
            mirror = bool(mirror)
        if ystretch is None:
            # 预拉伸发生在 rotate 之前的宽轴上；rotate=90/270 会把它转成纵向拉伸。
            # 旋转链路服务的是微信这类按 sensor.orientation 自行转正、且等比显示的
            # 客户端，本不需要补偿：缺省必须落回 1.0，否则脸被纵向拉长（1.7778 时
            # 尤其明显）。只有非旋转的老预览路径才继承全局 YSTRETCH 补偿。
            ystretch = 1.0 if rotate in (90, 270) else YSTRETCH
        self.ystretch = float(ystretch)
        geo = geometry(src, fit, rotate, mirror, self.ystretch)
        with self.lock:
            old, old_dec = self.reader, self.decoder
            self.kind, self.arg, self.since = kind, arg, time.time()
            self.src, self.geo, self.fit, self.rotate, self.mirror = src, geo, fit, rotate, mirror
        for obj in (old, old_dec):
            if obj is not None:
                obj.stop.set()
                if getattr(obj, "proc", None) is not None and obj.proc.poll() is None:
                    obj.proc.terminate()
        if kind in ("idle", "image"):
            # 静态源：只解码一帧，写线程原样重复，省掉每节点一个常驻 ffmpeg。
            cmd = idle_cmd(self.node) if kind == "idle" else source_cmd(
                "image", arg, self.geo)
            # idle 彩条只是占位：2 帧 + 轻噪声即可，没必要占 6×1.8MB 内存。
            nvar = (max(1, min(2, STATIC_VARIANTS)) if kind == "idle"
                    else (STATIC_VARIANTS if STATIC_GRAIN > 0 else 1))
            frames = render_static_set(cmd, nvar)
            with self.lock:
                if frames:
                    self.static_set = frames
                    self.static = frames[0]
                    self.static_idx = 0
                    self.breath_step = None
                    self.breath_frames = []
                    self.frame = frames[0]
                    self.seq += 1
            return None
        with self.lock:
            self.static = None
        if kind == "jpeg":
            dec = JpegDecoder(self.node, self._on_frame, self.mirror)
            with self.lock:
                self.reader, self.decoder = None, dec
            dec.start()
            return dec
        reader = Reader(self.node, kind, arg, self._on_frame, self.geo)
        with self.lock:
            self.reader, self.decoder = reader, None
        reader.start()
        return reader

    def feed(self, data):
        dec = self.decoder
        if dec is None:
            return False
        return dec.push(data)

    def snapshot(self):
        with self.lock:
            return {
                "node": self.node,
                "kind": self.kind,
                "arg": self.arg,
                "src": self.src,
                "geometry": self.geo,
                "frames": self.seq,
                "written": self.written,
                "errors": self.errors,
                "since": self.since,
                "live_clean": bool(LIVE_CLEAN and self.kind in ("rtsp", "jpeg")),
                "source_frames": (self.decoder or self.reader).frames
                                 if (self.decoder or self.reader) else 0,
                "source_restarts": (self.decoder or self.reader).restarts
                                   if (self.decoder or self.reader) else 0,
                "retry_delay": getattr(self.decoder or self.reader, "retry_delay", 0.0),
                "source_dropped": (self.decoder or self.reader).dropped
                                  if (self.decoder or self.reader) else 0,
                # 源静默时写线程重复最后一帧，written 继续增长而 source_frames 不动。
                "holding": self.holding,
                "last_frame_age": (round(time.time() - self.last_frame_ts, 2)
                                   if self.last_frame_ts else None),
            }


_BLANK = None


def blank_frame():
    """一片纯黑的 YUV420p 帧（Y=16, UV=128）。

    源从没产出过帧时（对象存储/推流还没起来）写线程没有可重复的“最后一帧”，
    节点就会彻底断流，旧版相机 API 的客户端随即报错。用一帧黑顶上，保持节点
    在 30fps 有 buffer 可 dequeue，等真帧到了再被覆盖。
    """
    global _BLANK
    if _BLANK is None:
        _BLANK = bytes([16]) * (W * H) + bytes([128]) * (W * H // 2)
    return _BLANK


def set_keepf(node, val):
    """Keep the loopback format pinned so the consumer never re-negotiates."""
    try:
        subprocess.run([KEEPF, node, str(val)], timeout=3,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


class Handler(BaseHTTPRequestHandler):
    routers = {}

    def log_message(self, *args):  # silence
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/state"):
            self._send({"ok": True, "nodes": [r.snapshot() for r in self.routers.values()]})
        elif self.path.startswith("/health"):
            self._send({"ok": True})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.startswith("/frame"):
            # Raw JPEG body from the phone's browser fallback (WebRTC unusable).
            # localhost only, so a size cap is enough protection.
            node = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("node", [""])[0]
            router = self.routers.get(node)
            if router is None:
                self._send({"error": "unknown node " + str(node)}, 404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 8 * 1024 * 1024:
                self._send({"error": "bad length"}, 400)
                return
            data = self.rfile.read(length)
            ok = router.feed(data)
            self._send({"ok": ok, "bytes": len(data)})
            return
        if not self.path.startswith("/set"):
            self._send({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            self._send({"error": "bad json"}, 400)
            return
        node = body.get("node") or "/dev/video10"
        kind = body.get("kind") or "idle"
        arg = body.get("path") or body.get("url")
        router = self.routers.get(node)
        if router is None:
            self._send({"error": "unknown node " + str(node)}, 404)
            return
        if kind in ("image", "video") and not (arg and os.path.exists(arg)):
            self._send({"error": "path not found"}, 400)
            return
        if kind == "rtsp" and not arg:
            self._send({"error": "url required"}, 400)
            return
        if kind == "jpeg" and arg and not str(arg).startswith("webcam"):
            self._send({"error": "bad jpeg tag"}, 400)
            return
        try:
            router.set_source(kind, arg, fit=body.get("fit"),
                              rotate=body.get("rotate"), mirror=body.get("mirror"),
                              ystretch=body.get("ystretch"))
        except Exception as exc:  # noqa: BLE001
            self._send({"error": str(exc)}, 400)
            return
        self._send({"ok": True, "state": router.snapshot()})


def main():
    nodes = os.environ.get("G98_CAMD_NODES", "/dev/video10,/dev/video11").split(",")
    nodes = [n.strip() for n in nodes if n.strip()]
    routers = {}
    for node in nodes:
        # Refuse anything that is not a real v4l2 char device.  A stale regular
        # file at the same path (left behind by a writer that ran while the
        # module was unloaded) silently swallows frames and balloons /dev.
        try:
            if not stat.S_ISCHR(os.stat(node).st_mode):
                print("camd: %s is not a char device, refusing" % node, flush=True)
                continue
        except OSError as exc:
            print("camd: cannot stat %s: %s" % (node, exc), flush=True)
            continue
        try:
            fd = open(node, "wb", buffering=0)
        except OSError as exc:
            print("camd: cannot open %s: %s" % (node, exc), flush=True)
            continue
        r = NodeRouter(node)
        r.fd = fd
        routers[node] = r
        print("camd: node %s ready" % node, flush=True)
    if not routers:
        raise SystemExit("camd: no nodes available")
    Handler.routers = routers
    port = int(os.environ.get("G98_CAMD_PORT", "8100"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("camd: listening on 127.0.0.1:%d" % port, flush=True)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    for r in routers.values():
        r.stop.set()


if __name__ == "__main__":
    # kill -USR1 <pid> dumps every reader/writer stack; invaluable when a source
    # silently stops delivering frames.
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except Exception:
        pass
    main()
