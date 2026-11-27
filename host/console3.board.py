#!/usr/bin/env python3
# g98 云手机控制台 v3 —— 屏幕实时查看 + 点击/滑动/按键/文字输入 + 重新设计的实例列表
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import faulthandler
import hashlib
import hmac
import math
import signal
import socket
import ssl
import struct
import select
import urllib.request
import urllib.error
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote, urlencode

sys.path.insert(0, "/root")
from cloudphone_api import agent_state, api_token, container_info, list_containers, wechat_state  # noqa: E402

PORT = int(os.environ.get("G98_DASH_PORT", "8099"))
TLS_PORT = int(os.environ.get("G98_TLS_PORT", "8443"))
TLS_CERT = os.environ.get("G98_TLS_CERT", "/root/g98-tls.crt")
TLS_KEY = os.environ.get("G98_TLS_KEY", "/root/g98-tls.key")
TLS_IP = os.environ.get("G98_TLS_IP", "192.168.5.111")
BUILD = "v3.%s" % os.environ.get("G98_DASH_BUILD", "touch2")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TOKEN_FILE = "/etc/g98/cloudphone.token"
SNAP = {"ts": 0, "rows": [], "board": {}}
SNAP_LOCK = threading.Lock()
REFRESH_SEC = float(os.environ.get("G98_REFRESH_SEC", "20"))
SIZE_CACHE = {}

def _alog(msg):
    try:
        with open("/var/log/g98-actions.log", "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:  # noqa: BLE001
        pass


# ---------------- 实时画面（MJPEG 推流） ----------------
# 设备端没有可用编码器（screenrecord -h264 报 Encoder failed -38），但
# screenrecord --output-format=raw-frames 可用（原生分辨率 RGBA，不编码）。
# 链路：容器 raw-frames（docker exec 直取，359MB/s）→ 板子 ffmpeg（缩放到 720x1280 + JPEG）
#       → multipart/x-mixed-replace。走 adb 时被 33MB/s 卡死在 9fps，故不用 adb。
STREAMS = {}
STREAMS_LOCK = threading.Lock()
ADB_SERIALS = {}
STREAM_FPS = 15
# 480x854 在 1080x2400 的屏幕上等于把画面砍掉一半像素，字都在糊；720x1280 是
# 同样的宽高比、2.25 倍像素，JPEG q=4 后每帧约 100-150KB，局域网完全吃得下。
STREAM_OUT_W, STREAM_OUT_H = 720, 1280
STREAM_JPEG_Q = 4
FFMPEG_STREAM = "/root/ffmpeg" if os.path.exists("/root/ffmpeg") else "ffmpeg"


class Stream(object):
    """一个容器的 MJPEG 管道；所有观看者共享同一条编码流。"""

    def __init__(self, name):
        self.name = name
        self.cond = threading.Condition()
        self.jpeg = None
        self.seq = 0
        self.clients = 0
        self.last_frame = 0.0
        self.last_src = 0.0
        self.last_use = time.time()
        self._cap_started = 0.0
        self._pipeline = ""
        self.started = time.time()
        self.cap = None
        self.enc = None
        self.fills = []
        self.frame_bytes = 0
        self.error = ""
        self.stopped = False
        self._fps = 0.0
        self._cnt = 0
        self._t0 = time.time()

    def start(self):
        w, h = screen_size(self.name)
        if w <= 0 or h <= 0:
            raise RuntimeError("取不到屏幕尺寸")
        # 实测：screenrecord raw-frames 输出 24 位 RGB（720x1280 = 2764800 字节/帧），
        # 不是文档里常见的 RGBA；按 4 字节解会得到错位重影。
        self.w, self.h = w, h
        # 实测：screenrecord 以原生 1080x2400 抓帧只有 16-20fps（读回带宽限制），
        # 加 --size 让设备侧先缩放反而能跑到 60fps+，且管道数据量降 2.2 倍。
        # 目标：抓帧像素 ≈ 115 万（1080x2400 → 720x1600），再缩到 720x1280。
        k = min(1.0, (720.0 * 1600.0 / float(w * h)) ** 0.5)
        self.cw = max(2, int(round(w * k / 2.0)) * 2)
        self.ch = max(2, int(round(h * k / 2.0)) * 2)
        self.frame_bytes = self.cw * self.ch * 3
        self._last_fill = 0.0
        # 一条 bash 管道：容器 screenrecord raw-frames → dd 按帧对齐 → ffmpeg 缩放+JPEG。
        # dd 的 iflag=fullblock 是关键：rawvideo demuxer 不会按帧补齐短读，之前直连
        # 会报 Invalid buffer size（packet 40960 < expected 7776000）。
        # python 只读 JPEG（约 30 KB/帧），不再搬运 7.7 MB/帧的原始 RGB。
        pipeline = (
            "docker exec %s screenrecord --output-format=raw-frames --size %dx%d - 2>/dev/null "
            "| dd bs=%d iflag=fullblock 2>/dev/null "
            "| %s -hide_banner -loglevel error -threads 4 -f rawvideo -pix_fmt rgb24 "
            "-s %dx%d -i - -vf scale=%d:-2:flags=fast_bilinear,fps=%d -q:v %d -f image2pipe -c:v mjpeg -"
        ) % (self.name, self.cw, self.ch, self.frame_bytes, FFMPEG_STREAM, self.cw, self.ch,
             STREAM_OUT_W, STREAM_FPS, STREAM_JPEG_Q)
        self.enc = None
        self._pipeline = pipeline
        self._spawn_cap()
        # 静态补帧：screenrecord 画面不动时不出帧。旧实现每帧 docker exec
        # screencap + 启一次 ffmpeg，只有 ~1.7fps；改成容器内 screencap 循环 +
        # 常驻 ffmpeg，静态画面实测 ~4fps，CPU 约 13%。
        fill_pipeline = (
            "docker exec %s sh -c 'while true; do screencap; sleep 0.03; done' 2>/dev/null "
            "| %s -hide_banner -loglevel error -threads 2 -f rawvideo -pix_fmt rgba "
            "-s %dx%d -i - -vf scale=%d:-2:flags=fast_bilinear -q:v %d "
            "-f image2pipe -c:v mjpeg -"
        ) % (self.name, FFMPEG_STREAM, self.w, self.h, STREAM_OUT_W, STREAM_JPEG_Q)
        self.fills = []
        for _ in range(1):
            try:
                fill = subprocess.Popen(["bash", "-c", fill_pipeline], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        bufsize=0, start_new_session=True)
                self.fills.append(fill)
                threading.Thread(target=self._read_fill, args=(fill,), daemon=True).start()
            except Exception as exc:  # noqa: BLE001
                self.error = "fill: " + str(exc)[:160]
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._idle, daemon=True).start()

    def _spawn_cap(self):
        proc = subprocess.Popen(["bash", "-c", self._pipeline], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                bufsize=0, start_new_session=True)
        self.cap = proc
        self._cap_started = time.time()
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()

    def _watchdog(self):
        # screenrecord 默认 180 秒后退出；到点前主动换一条管道，避免页面
        # 长时间只靠 screencap 补帧（表现为静止 4fps、输入不跟手）。
        while not self.stopped:
            time.sleep(5)
            proc = self.cap
            if proc is None or self.stopped:
                continue
            if time.time() - self._cap_started <= 150:
                continue
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._spawn_cap()
            except Exception as exc:  # noqa: BLE001
                self._kill("restart: " + str(exc))
                return

    def healthy(self):
        if self.stopped or self.error:
            return False
        # 画面静止时 screenrecord 不会出新帧，不能用「最近有没有帧」判活，
        # 否则每 6 秒就被判死一次、反复重启（表现为黑屏）。
        if self.cap is not None and self.cap.poll() is not None:
            return False
        if self.last_frame and time.time() - self.last_frame > 120:
            return False
        return True

    def stop(self):
        self.stopped = True
        proc = self.cap
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        # 杀掉 docker exec 只断管道，容器里的 screenrecord/screencap 循环还在跑；
        # 必须进去收尸，否则残留进程会越积越多并抢 CPU。
        for fill in self.fills:
            try:
                os.killpg(os.getpgid(fill.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                try:
                    fill.kill()
                except Exception:  # noqa: BLE001
                    pass
        self.fills = []
        try:
            subprocess.run(["docker", "exec", self.name, "pkill", "-f",
                            "screenrecord --output-format"], timeout=10,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "exec", self.name, "pkill", "-f",
                            "while true; do screencap"], timeout=10,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "exec", self.name, "pkill", "-x", "screencap"],
                           timeout=10, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass
        with self.cond:
            self.cond.notify_all()

    def _kill(self, why=""):
        if why and not self.error:
            self.error = why[:200]
        self.stop()

    def _publish(self, jpg, src=False):
        now = time.time()
        with self.cond:
            self.jpeg = jpg
            self.seq += 1
            self.last_frame = now
            if src:
                self.last_src = now
            self.cond.notify_all()
        self._cnt += 1
        now = time.time()
        if now - self._t0 >= 1.0:
            self._fps = self._cnt / (now - self._t0)
            self._cnt = 0
            self._t0 = now

    def _idle_frame(self):
        """画面静止时 screenrecord 不出帧；用 screencap 原始 RGBA 补帧。
        原始帧比 PNG 快约 2 倍，静态画面可维持 2fps 左右，输入反馈不再等 1 秒。"""
        now = time.time()
        if now - self._last_fill < 0.1:
            return
        self._last_fill = now
        p = subprocess.run(["docker", "exec", self.name, "screencap"],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        raw = p.stdout
        if len(raw) < 1000:
            return
        r = subprocess.run(
            [FFMPEG_STREAM, "-hide_banner", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "%dx%d" % (self.w, self.h), "-i", "-",
             "-vf", "scale=%d:-2:flags=fast_bilinear" % STREAM_OUT_W,
             "-q:v", str(STREAM_JPEG_Q), "-f", "image2pipe", "-c:v", "mjpeg", "-"],
            input=raw, capture_output=True, timeout=15)
        if r.stdout.startswith(b"\xff\xd8"):
            self._publish(r.stdout)

    def _fill_loop(self):
        while not self.stopped:
            time.sleep(0.1)
            if time.time() - self.last_frame > 0.2:
                try:
                    self._idle_frame()
                except Exception:  # noqa: BLE001
                    pass

    def _read(self, proc):
        buf = b""
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                buf += chunk
                while True:
                    i = buf.find(b"\xff\xd8")
                    j = buf.find(b"\xff\xd9", i + 2) if i >= 0 else -1
                    if i < 0 or j < 0:
                        if len(buf) > (4 << 20):
                            buf = buf[-(1 << 20):]
                        break
                    jpg = buf[i:j + 2]
                    buf = buf[j + 2:]
                    self._publish(jpg, src=True)
        except Exception as exc:  # noqa: BLE001
            if self.cap is proc:
                self._kill("read: " + str(exc))
        finally:
            # 旧管道被 watchdog 换掉时，这一行不能再把整个流判死。
            if not self.stopped and self.cap is proc:
                self._kill("encoder 已退出")

    def _read_fill(self, fill):
        """读取常驻 screencap 补帧管道；它退出不影响 screenrecord 主链路。"""
        buf = b""
        try:
            while not self.stopped:
                chunk = fill.stdout.read(8192)
                if not chunk:
                    break
                buf += chunk
                while True:
                    i = buf.find(b"\xff\xd8")
                    j = buf.find(b"\xff\xd9", i + 2) if i >= 0 else -1
                    if i < 0 or j < 0:
                        if len(buf) > (4 << 20):
                            buf = buf[-(1 << 20):]
                        break
                    jpg = buf[i:j + 2]
                    buf = buf[j + 2:]
                    self._publish(jpg)
        except Exception:  # noqa: BLE001
            pass

    def _idle(self):
        while not self.stopped:
            time.sleep(5)
            idle = time.time() - self.last_use
            if self.clients <= 0 and idle > 30:
                drop_stream(self.name)
                return
            if self.clients > 0 and idle > 120:
                # 客户端仍挂着但长时间写不出去（僵尸 MJPEG 连接），主动回收编码器
                drop_stream(self.name)
                return

    def wait(self, seq, timeout=5.0):
        with self.cond:
            if self.seq == seq:
                self.cond.wait(timeout)
            return self.seq, self.jpeg

    def fps(self):
        return self._fps



class H264Parser(object):
    """把 Annex-B H.264 拆成 AVCC 配置和可解码的 NAL。"""

    def __init__(self, on_config, on_nal):
        self.buf = b""
        self.sps = None
        self.pps = None
        self.config_key = None
        self.au = []
        self.on_config = on_config
        self.on_nal = on_nal

    @staticmethod
    def _find_start(buf, from_idx=0):
        i = buf.find(b"\x00\x00\x01", from_idx)
        if i < 0:
            return None
        if i > 0 and buf[i - 1] == 0:
            return i - 1, 4
        return i, 3

    def _emit_config(self):
        if not self.sps or not self.pps or len(self.sps) < 4 or len(self.pps) < 1:
            return
        key = bytes(self.sps) + b"|" + bytes(self.pps)
        if key == self.config_key:
            return
        self.config_key = key
        sps, pps = bytes(self.sps), bytes(self.pps)
        avcc = (bytes([1, sps[1], sps[2], sps[3], 0xff, 0xe1]) +
                struct.pack(">H", len(sps)) + sps +
                bytes([1]) + struct.pack(">H", len(pps)) + pps)
        codec = "avc1.%02x%02x%02x" % (sps[1], sps[2], sps[3])
        self.on_config({"type": "config", "codec": codec,
                        "description": base64.b64encode(avcc).decode("ascii")})

    def feed(self, data):
        self.buf += data
        while True:
            start = self._find_start(self.buf, 0)
            if start is None:
                if len(self.buf) > 4:
                    self.buf = self.buf[-4:]
                return
            idx, sc = start
            if idx > 0:
                self.buf = self.buf[idx:]
            nxt = self._find_start(self.buf, sc)
            if nxt is None:
                return
            nidx, _ = nxt
            nal = self.buf[sc:nidx]
            self.buf = self.buf[nidx:]
            if not nal:
                continue
            t = nal[0] & 0x1f
            if t == 7:
                self.sps = nal
                self._emit_config()
            elif t == 8:
                self.pps = nal
                self._emit_config()
            elif t == 9:
                self._emit_au()
            elif t in (1, 5):
                if self.sps and self.pps:
                    self._emit_config()
                self.au.append(nal)

    def _emit_au(self):
        if not self.au:
            return
        au = self.au
        self.au = []
        payload = b"".join(struct.pack(">I", len(n)) + n for n in au)
        key = any((n[0] & 0x1f) == 5 for n in au if n)
        self.on_nal(payload, key)

    def flush_idle(self):
        # 等一个完整访问单元（AUD 分隔）再发，避免把一帧的多个 slice 当多帧。
        if self.au and not self.buf:
            self._emit_au()


class H264Session(object):
    """单设备 H.264 软编流；浏览器用 WebCodecs 解码。"""

    def __init__(self, name):
        self.name = name
        self.stop_event = threading.Event()
        self.cond = threading.Condition()
        self.config = None
        self.frames = deque(maxlen=8)
        self.error = ""
        self.proc = None
        self._t0 = time.monotonic()
        self._pipeline = ""

    def start(self):
        w, h = screen_size(self.name)
        if w <= 0 or h <= 0:
            raise RuntimeError("取不到屏幕尺寸")
        k = min(1.0, (540.0 * 1200.0 / float(w * h)) ** 0.5)
        self.cw = max(2, int(round(w * k / 2.0)) * 2)
        self.ch = max(2, int(round(h * k / 2.0)) * 2)
        self.frame_bytes = self.cw * self.ch * 3
        self._pipeline = (
            "docker exec %s screenrecord --output-format=raw-frames --size %dx%d - 2>/dev/null "
            "| dd bs=%d iflag=fullblock 2>/dev/null "
            "| %s -hide_banner -loglevel error -threads 2 -f rawvideo -pix_fmt rgb24 "
            "-s %dx%d -i - -flags +low_delay -vf scale=%d:-2:flags=fast_bilinear "
            "-c:v libx264 -preset ultrafast -tune zerolatency -profile:v baseline -level 3.1 "
            "-x264-params aud=1:repeat-headers=1 "
            "-g 30 -keyint_min 30 -sc_threshold 0 -crf 26 -pix_fmt yuv420p -flush_packets 1 -f h264 -"
        ) % (self.name, self.cw, self.ch, self.frame_bytes, FFMPEG_STREAM,
             self.cw, self.ch, STREAM_OUT_W)
        threading.Thread(target=self._reader, daemon=True).start()

    def wait_config(self, timeout=7.0):
        end = time.time() + timeout
        with self.cond:
            while self.config is None and not self.error and time.time() < end:
                self.cond.wait(min(0.25, max(0.0, end - time.time())))
            return self.config

    def next_frame(self, timeout=2.0):
        with self.cond:
            if not self.frames and not self.error:
                self.cond.wait(timeout)
            return self.frames.popleft() if self.frames else None

    def _on_config(self, cfg):
        with self.cond:
            if self.config is None:
                self.config = cfg
            self.cond.notify_all()

    def _on_nal(self, nal, key):
        with self.cond:
            self.frames.append((key, int((time.monotonic() - self._t0) * 1000000), nal))
            self.cond.notify_all()

    def _reader(self):
        while not self.stop_event.is_set():
            try:
                self.proc = subprocess.Popen(["bash", "-c", self._pipeline],
                                             stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL,
                                             bufsize=0, start_new_session=True)
            except Exception as exc:  # noqa: BLE001
                with self.cond:
                    self.error = "h264 start: " + str(exc)[:180]
                    self.cond.notify_all()
                return
            parser = H264Parser(self._on_config, self._on_nal)
            try:
                while not self.stop_event.is_set():
                    ready, _, _ = select.select([self.proc.stdout], [], [], 0.25)
                    if not ready:
                        parser.flush_idle()
                        continue
                    chunk = self.proc.stdout.read(65536)
                    if not chunk:
                        break
                    parser.feed(chunk)
                parser.flush_idle()
            except Exception:  # noqa: BLE001
                pass
            if self.stop_event.is_set():
                break
            time.sleep(0.4)
        with self.cond:
            self.cond.notify_all()

    def stop(self):
        self.stop_event.set()
        proc = self.proc
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            subprocess.run(["docker", "exec", self.name, "pkill", "-f",
                            "screenrecord --output-format=raw-frames --size"],
                           timeout=10, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass
        with self.cond:
            self.cond.notify_all()


class FMP4Mux(object):
    """把 ffmpeg 的 fMP4 字节流按完整 box 切开：先发 init(ftyp+moov)，之后每个 moof+mdat 作为一段。"""

    def __init__(self, on_chunk):
        self.buf = b""
        self.init = b""
        self.inited = False
        self.moof = None
        self.on_chunk = on_chunk

    def feed(self, data):
        self.buf += data
        while True:
            if len(self.buf) < 8:
                return
            size = struct.unpack(">I", self.buf[:4])[0]
            typ = self.buf[4:8]
            hdr = 8
            if size == 1:
                if len(self.buf) < 16:
                    return
                size = struct.unpack(">Q", self.buf[8:16])[0]
                hdr = 16
            elif size == 0:
                return
            if size < hdr or len(self.buf) < size:
                return
            box = self.buf[:size]
            self.buf = self.buf[size:]
            self._box(typ, box)

    def _box(self, typ, box):
        if not self.inited:
            self.init += box
            if typ == b"moov":
                self.inited = True
                self.on_chunk(self.init)
                self.init = b""
            return
        if typ == b"moof":
            self.moof = box if self.moof is None else self.moof + box
            return
        if typ == b"mdat" and self.moof is not None:
            self.on_chunk(self.moof + box)
            self.moof = None
            return
        if self.moof is not None:
            self.moof += box
            return
        self.on_chunk(box)


class FMP4Session(object):
    """单设备 H.264 软编 fMP4 流；浏览器用 MediaSource 播放，HTTP 页面也能用。"""

    def _kill_capture(self):
        try:
            subprocess.run(["docker", "exec", self.name, "pkill", "-9", "-f",
                            "screenrecord --output-format=raw-frames --size"],
                           timeout=8, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass

    def __init__(self, name):
        self.name = name
        self.stop_event = threading.Event()
        self.cond = threading.Condition()
        self.chunks = deque()
        self.first = False
        self.error = ""
        self.proc = None
        self._pipeline = ""

    def start(self):
        if rtc_active():
            raise RuntimeError("WebRTC 正在播放，旧版流已停用；请刷新控制台")
        kill_wx_pipeline(self.name)
        self._kill_capture()
        w, h = screen_size(self.name)
        if w <= 0 or h <= 0:
            raise RuntimeError("取不到屏幕尺寸")
        k = min(1.0, (540.0 * 1200.0 / float(w * h)) ** 0.5)
        self.cw = max(2, int(round(w * k / 2.0)) * 2)
        self.ch = max(2, int(round(h * k / 2.0)) * 2)
        self.frame_bytes = self.cw * self.ch * 3
        self._pipeline = (
            "docker exec %s screenrecord --output-format=raw-frames --size %dx%d - 2>/dev/null "
            "| /root/frame_pump.py %d %d 30 "
            "| %s -hide_banner -loglevel error -threads 2 -f rawvideo -pix_fmt rgb24 "
            "-s %dx%d -framerate 30 -i - -flags +low_delay -vf scale=%d:-2:flags=fast_bilinear "
            "-c:v libx264 -preset ultrafast -tune zerolatency -profile:v baseline -level 3.1 "
            "-x264-params aud=1:repeat-headers=1 "
            "-g 1 -keyint_min 1 -sc_threshold 0 -crf 26 -pix_fmt yuv420p "
            "-flush_packets 1 -movflags +frag_keyframe+empty_moov+default_base_moof -f mp4 -"
        ) % (self.name, self.cw, self.ch, self.cw, self.ch, FFMPEG_STREAM,
             self.cw, self.ch, STREAM_OUT_W)
        threading.Thread(target=self._reader, daemon=True).start()

    def wait_first(self, timeout=8.0):
        end = time.time() + timeout
        with self.cond:
            while not self.first and not self.error and not self.stop_event.is_set() and time.time() < end:
                self.cond.wait(min(0.25, max(0.0, end - time.time())))
            return self.first and not self.stop_event.is_set()

    def next_chunk(self, timeout=4.0):
        with self.cond:
            if not self.chunks and not self.error and not self.stop_event.is_set():
                self.cond.wait(timeout)
            if self.chunks:
                return self.chunks.popleft()
            return None

    def _push(self, chunk):
        with self.cond:
            self.chunks.append(chunk)
            self.first = True
            self.cond.notify_all()

    def _reader(self):
        while not self.stop_event.is_set():
            try:
                self.proc = subprocess.Popen(["bash", "-c", self._pipeline],
                                             stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL,
                                             bufsize=0, start_new_session=True)
            except Exception as exc:  # noqa: BLE001
                with self.cond:
                    self.error = "fmp4 start: " + str(exc)[:180]
                    self.cond.notify_all()
                return
            mux = FMP4Mux(self._push)
            try:
                while not self.stop_event.is_set():
                    ready, _, _ = select.select([self.proc.stdout], [], [], 0.5)
                    if not ready:
                        continue
                    chunk = self.proc.stdout.read(65536)
                    if not chunk:
                        break
                    mux.feed(chunk)
            except Exception:  # noqa: BLE001
                pass
            if self.stop_event.is_set():
                break
            with self.cond:
                self.error = "encoder exited"
                self.cond.notify_all()
            break
        with self.cond:
            self.cond.notify_all()

    def stop(self):
        self.stop_event.set()
        proc = self.proc
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            subprocess.run(["docker", "exec", self.name, "pkill", "-f",
                            "screenrecord --output-format=raw-frames --size"],
                           timeout=10, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass
        with self.cond:
            self.cond.notify_all()


FMP4_SESSIONS = {}
FMP4_LOCK = threading.Lock()


def adb_serial(name, force=False):
    ser = ADB_SERIALS.get(name)
    if ser and not force:
        rc, out, _ = sh(["adb", "devices"], timeout=10)
        for line in out.splitlines():
            if line.startswith(ser) and line.rstrip().endswith("device"):
                return ser
    rc, out, _ = sh(["docker", "inspect", "-f",
                     "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name], timeout=10)
    ip = out.strip()
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip or ""):
        return ""
    dexec(name, ["sh", "-c", "setprop service.adb.tcp.port 5555; setprop ctl.start adbd"], timeout=15)
    ser = ip + ":5555"
    for _ in range(8):
        sh(["adb", "connect", ser], timeout=15)
        rc, out, _ = sh(["adb", "devices"], timeout=10)
        for line in out.splitlines():
            if line.startswith(ser) and line.rstrip().endswith("device"):
                ADB_SERIALS[name] = ser
                return ser
        time.sleep(0.4)
    return ""


def drop_stream(name):
    with STREAMS_LOCK:
        st = STREAMS.pop(name, None)
    if st:
        st.stop()


def get_stream(name):
    with STREAMS_LOCK:
        st = STREAMS.get(name)
        if st and st.healthy():
            st.last_use = time.time()
            return st
        if st:
            st.stop()
            STREAMS.pop(name, None)
        st = Stream(name)
        st.start()
        STREAMS[name] = st
        return st


RTC_SESSIONS = {}
RTC_LOCK = threading.Lock()
RTC_OP_LOCK = threading.Lock()
# 无观看端的死循环保护：某些页面/脚本会不停 POST /api/rtc/start（WHEP 一直失败），
# 每轮都起一条 screenrecord+ffmpeg（约 55% CPU）。60s 内超过 RTC_START_LIMIT 次
# 就拒绝，等页面自己放弃。
RTC_STARTS = {}
RTC_START_LIMIT = int(os.environ.get("G98_RTC_START_LIMIT", "5"))


def rtc_active():
    with RTC_LOCK:
        return any(st.alive() for st in RTC_SESSIONS.values())


RTC_IDLE_TIMEOUT = float(os.environ.get("G98_RTC_IDLE_TIMEOUT", "45"))
# 起流后一直没人 WHEP 进来（页面在瞎重试）时，等待上限要比正常心跳超时短。
RTC_FIRST_VIEW = float(os.environ.get("G98_RTC_FIRST_VIEW", "15"))


def rtc_reap_idle(now=None):
    """停掉没有观看端心跳的 RTC 会话，返回被停掉的实例名列表。"""
    now = time.time() if now is None else now
    victims = []
    with RTC_LOCK:
        for name, st in list(RTC_SESSIONS.items()):
            ref = max(getattr(st, "last_view", 0.0), getattr(st, "started_at", 0.0))
            # 从来没被观看过（WHEP 客户端一次都没成功）的会话只给 RTC_FIRST_VIEW
            # 秒：有的页面会不停重试 start，若按 45s 回收就会在后台一直编码。
            grace = RTC_FIRST_VIEW if not getattr(st, "viewed", False) else RTC_IDLE_TIMEOUT
            if now - ref > grace or not st.alive():
                RTC_SESSIONS.pop(name, None)
                victims.append((name, st, round(now - ref, 1)))
    for name, st, age in victims:
        try:
            st.stop()
        except Exception:  # noqa: BLE001
            pass
        _alog("rtc reap idle %s after %.1fs without viewer" % (name, age))
    return [v[0] for v in victims]


def rtc_janitor():
    while True:
        time.sleep(10)
        try:
            rtc_reap_idle()
        except Exception:  # noqa: BLE001
            pass

RTC_CAP_W = 480
RTC_CAP_H = 1066
RTC_FPS = max(10, min(60, int(os.environ.get("G98_RTC_FPS", "30"))))


def kill_wx_pipeline(name):
    """Kill host-side capture loops and publishers for one wx container."""
    for pat in ("docker exec %s screenrecor[d]" % name,
                "rtsp://127.0.0.1:8554/%s[%s]" % (name[:-1], name[-1:]),
                "frame_pump.py .*rtc-seed-%s[%s]" % (name[:-1], name[-1:])):
        try:
            subprocess.run(["pkill", "-9", "-f", pat], timeout=6,
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass
RTC_RTSP = "rtsp://127.0.0.1:8554/%s"


def capture_seed(name, w, h):
    """Grab one still frame first: screenrecord emits nothing on a fully idle
    display, so frame_pump needs a seed before the first damage frame arrives."""
    path = "/tmp/rtc-seed-%s.raw" % name
    try:
        os.unlink(path)
    except OSError:
        pass
    cmd = ("docker exec %s screencap -p | %s -hide_banner -loglevel error "
           "-f image2pipe -vcodec png -i - -vf scale=%d:%d:flags=fast_bilinear "
           "-frames:v 1 -pix_fmt rgb24 -f rawvideo %s"
           % (name, FFMPEG_STREAM, w, h, path))
    try:
        subprocess.run(["bash", "-c", cmd], timeout=20,
                       stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return None
    try:
        if os.path.getsize(path) == w * h * 3:
            return path
    except OSError:
        pass
    return None


class RTCSession(object):
    """把当前云手机画面软编成 H.264，推进本机 MediaMTX，浏览器通过 WHEP 收流。"""

    def __init__(self, name):
        self.name = name
        self.proc = None
        self.log = "/tmp/rtc-%s.log" % name
        self._pipeline = ""
        self._logf = None
        # 观看端心跳：页面每 15s 打一次 /api/rtc/ping。没有心跳的会话在
        # RTC_IDLE_TIMEOUT 秒后由 rtc_janitor 停掉，否则关掉标签页后
        # screenrecord+ffmpeg 会一直编码（实测残留 20 分钟、55% CPU）。
        self.started_at = time.time()
        self.last_view = self.started_at

    def _kill_capture(self):
        try:
            subprocess.run(["docker", "exec", self.name, "pkill", "-9", "-f",
                            "screenrecord --output-format=raw-frames --size"],
                           timeout=8, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass

    def start(self):
        kill_wx_pipeline(self.name)
        self._kill_capture()
        w, h = screen_size(self.name)
        if w <= 0 or h <= 0:
            raise RuntimeError("取不到屏幕尺寸")
        k = min(1.0, (float(RTC_CAP_W * RTC_CAP_H) / float(w * h)) ** 0.5)
        cw = max(2, int(round(w * k / 2.0)) * 2)
        ch = max(2, int(round(h * k / 2.0)) * 2)
        seed = capture_seed(self.name, cw, ch)
        pump = "/root/frame_pump.py %d %d %d" % (cw, ch, RTC_FPS)
        if seed:
            pump += " " + seed
        self._pipeline = (
            "while true; do docker exec %s screenrecord --output-format=raw-frames "
            "--size %dx%d -; sleep 0.1; done 2>/dev/null "
            "| %s "
            "| %s -hide_banner -loglevel warning -threads 1 -f rawvideo -pix_fmt rgb24 "
            "-s %dx%d -framerate %d -i - -flags +low_delay "
            "-an -c:v libx264 -preset ultrafast -tune zerolatency -profile:v baseline -level 4.0 "
            "-x264-params aud=1:repeat-headers=1:sync-lookahead=0:rc-lookahead=0:bframes=0 -g 30 -keyint_min 30 -sc_threshold 0 "
            "-b:v 1000k -maxrate 1200k -bufsize 2400k -pix_fmt yuv420p -flush_packets 1 "
            "-max_delay 0 -muxdelay 0 -muxpreload 0 -f rtsp -rtsp_transport tcp %s"
        ) % (self.name, cw, ch, pump, FFMPEG_STREAM, cw, ch, RTC_FPS, RTC_RTSP % self.name)
        self._logf = open(self.log, "wb")
        self.proc = subprocess.Popen(["bash", "-c", self._pipeline],
                                     stdin=subprocess.DEVNULL,
                                     stdout=self._logf, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        time.sleep(1.2)
        if self.proc.poll() is not None:
            raise RuntimeError("Mediamtx 推流进程退出：" + self.tail(260))
        return self

    def alive(self):
        if not (self.proc and self.proc.poll() is None):
            return False
        # The bash supervisor can outlive a failed ffmpeg child (e.g. MediaMTX
        # rejected the publisher).  Treat a logged output-open failure as dead
        # so the next rtc_start replaces the broken pipeline instead of reusing it.
        log = self.tail(700)
        return not ("Error opening output file" in log or
                    "Output file does not contain any stream" in log)

    def tail(self, n=300):
        try:
            with open(self.log, "rb") as fh:
                return fh.read()[-n * 4:].decode("utf-8", "replace").strip()[-n:]
        except OSError:
            return ""

    def stop(self):
        proc = self.proc
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.wait(timeout=4)
            except Exception:  # noqa: BLE001
                pass
        kill_wx_pipeline(self.name)
        self._kill_capture()
        if self._logf is not None:
            try:
                self._logf.close()
            except Exception:  # noqa: BLE001
                pass
            self._logf = None


def rtc_stop(name=None):
    with RTC_OP_LOCK:
        with RTC_LOCK:
            if name:
                st = RTC_SESSIONS.pop(name, None)
                victims = [st] if st is not None else []
            else:
                victims = list(RTC_SESSIONS.values())
                RTC_SESSIONS.clear()
        for st in victims:
            try:
                st.stop()
            except Exception:  # noqa: BLE001
                pass
        return len(victims)


def rtc_start(name, spawn=True):
    # One board should only run one x264 pipeline at a time.  Opening a second
    # console tab used to leave every selected device encoding forever.  The
    # whole replacement is serialized, otherwise two concurrent starts can
    # each stop the other's map entry and leave two orphan encoders running.
    with RTC_OP_LOCK:
        now = time.time()
        hist = [t for t in RTC_STARTS.get(name, []) if now - t < 60.0]
        RTC_STARTS[name] = hist
        if len(hist) >= RTC_START_LIMIT:
            _alog("rtc throttled %s (%d starts in 60s, no viewer)" % (name, len(hist)))
            return {"ok": False, "error": "重连过于频繁（没有观看端），已暂停 60s"}
        with RTC_LOCK:
            live = RTC_SESSIONS.get(name)
            stale = live is None or not live.alive()
            victims = []
            for n, st in list(RTC_SESSIONS.items()):
                if n != name or stale:
                    victims.append(st)
                    RTC_SESSIONS.pop(n, None)
        for st in victims:
            try:
                st.stop()
            except Exception:  # noqa: BLE001
                pass
        if live is not None and not stale:
            return {"ok": True, "name": name, "url": "/whep/" + name,
                    "reused": True, "pid": live.proc.pid}
        drop_stream(name)
        # Also stop the MJPEG Stream objects: a fallback tab can leave a
        # 540x1200 dd|ffmpeg|screenrecord pipeline competing with WebRTC.
        with STREAMS_LOCK:
            streams = list(STREAMS.values())
            STREAMS.clear()
        for legacy in streams:
            try:
                legacy.stop()
            except Exception:  # noqa: BLE001
                pass
        # WebRTC is now the only live viewer.  Stop every legacy fMP4/MJPEG
        # encoder, not just the same device: an old tab left on wx2 used to
        # keep a second screenrecord+x264 pipeline alive and starve the RTC one.
        with FMP4_LOCK:
            olds = list(FMP4_SESSIONS.values())
            FMP4_SESSIONS.clear()
        for old in olds:
            try:
                old.stop()
            except Exception:  # noqa: BLE001
                pass
        if not spawn:
            # 懒启动：/api/rtc/start 只做清理和登记，真正的 screenrecord+ffmpeg
            # 由 /whep 在浏览器真正订阅时拉起（没人看 = 0% 编码 CPU）。
            return {"ok": True, "name": name, "url": "/whep/" + name,
                    "pending": True}
        st = RTCSession(name)
        try:
            st.start()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(str(exc)[:240])
        with RTC_LOCK:
            RTC_SESSIONS[name] = st
            RTC_STARTS.setdefault(name, []).append(time.time())
        return {"ok": True, "name": name, "url": "/whep/" + name,
                "log": st.tail(220), "pid": st.proc.pid}


# WHEP 到达时编码器刚起，mediamtx 还没注册 publisher 会回 404。
# 服务端在这里最多等这么久，把 404 吃掉，浏览器只会觉得首帧慢一点。
WHEP_READY_TIMEOUT = float(os.environ.get("G98_WHEP_READY_TIMEOUT", "8"))


def whep_post(name, sdp):
    """把 WHEP offer 转发给 mediamtx，返回 (status, body, content_type)。"""
    req = urllib.request.Request(
        "http://127.0.0.1:8889/%s/whep" % quote(name),
        data=sdp, method="POST",
        headers={"Content-Type": "application/sdp", "Accept": "application/sdp"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read(), (resp.headers.get("Content-Type") or "application/sdp")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), (exc.headers.get("Content-Type") or "text/plain; charset=utf-8")
    except Exception as exc:  # noqa: BLE001
        return 502, ("MediaMTX WHEP: " + str(exc)).encode("utf-8"), "text/plain; charset=utf-8"


def rtc_ensure(name):
    """WHEP 到达时确保编码器在跑；复用存活会话，否则现拉一条。"""
    with RTC_LOCK:
        live = RTC_SESSIONS.get(name)
    if live is not None and live.alive():
        return live
    info = rtc_start(name, spawn=True)
    if not info.get("ok"):
        raise RuntimeError(info.get("error") or "rtc start failed")
    with RTC_LOCK:
        st = RTC_SESSIONS.get(name)
    if st is None:
        raise RuntimeError("rtc session missing after start")
    _alog("rtc encoder up %s pid=%s for incoming whep" % (name, st.proc.pid))
    return st


LOGIN = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>G98 云手机 · 登录</title>
<style>
:root{--bg:#07090d;--panel:#0e141c;--line:rgba(255,255,255,.08);--txt:#f2f6fb;--mut:#8e9bae;--dim:#5f6b7c;--acc:#7aa7ff}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;display:grid;place-items:center;padding:24px;
background:radial-gradient(900px 560px at 18% -10%,rgba(122,167,255,.16),transparent 62%),
radial-gradient(760px 520px at 100% 0%,rgba(195,155,255,.11),transparent 60%),var(--bg);
color:var(--txt);font:13px/1.5 "Inter","SF Pro Display",-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
-webkit-font-smoothing:antialiased}
.frame{width:min(92vw,392px)}
.brand{display:flex;align-items:center;gap:10px;margin:0 2px 16px}
.logo{width:32px;height:32px;border-radius:10px;display:grid;place-items:center;font-size:13px;font-weight:800;color:#07101e;
background:linear-gradient(145deg,#a9c8ff,#7aa7ff 45%,#b397ff);box-shadow:inset 0 1px 0 rgba(255,255,255,.5),0 6px 20px rgba(122,167,255,.28)}
.brandcopy{display:flex;flex-direction:column;gap:2px;line-height:1.1}
.brandcopy b{font-size:13px;letter-spacing:.2px}.brandcopy span{font-size:8px;letter-spacing:1.8px;text-transform:uppercase;color:var(--dim)}
.card{padding:30px 28px 24px;border:1px solid var(--line);border-radius:20px;
background:linear-gradient(180deg,rgba(18,25,35,.96),rgba(9,13,19,.96));
box-shadow:0 30px 90px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.04);backdrop-filter:blur(18px)}
.eyebrow{font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--dim);font-weight:650}
h1{margin:8px 0 5px;font-size:23px;font-weight:650;letter-spacing:-.4px}
p{margin:0 0 22px;color:var(--mut);font-size:11.5px;line-height:1.65}
.field{display:block}
.field span{display:block;margin-bottom:7px;font-size:8.5px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);font-weight:650}
input{width:100%;height:44px;padding:0 13px;border-radius:10px;border:1px solid var(--line);background:rgba(0,0,0,.28);color:var(--txt);
font-size:14px;letter-spacing:1.2px;outline:none;transition:border-color .15s,background .15s}
input::placeholder{color:#536072;letter-spacing:.5px}
input:focus{border-color:rgba(122,167,255,.55);background:rgba(0,0,0,.38)}
button{display:flex;align-items:center;justify-content:center;gap:8px;margin-top:15px;width:100%;height:42px;
border:1px solid rgba(122,167,255,.3);border-radius:10px;background:linear-gradient(135deg,#9dc0ff,#7aa7ff 55%,#b49bff);
color:#07101d;font-weight:650;font-size:12.5px;cursor:pointer;box-shadow:0 8px 24px rgba(122,167,255,.22);transition:filter .15s}
button:hover{filter:brightness(1.06)}
button svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.foot{margin-top:17px;padding-top:14px;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:12px;
color:var(--dim);font-size:9px;letter-spacing:.3px}
</style></head><body>
<div class="frame">
  <div class="brand"><div class="logo">G</div><div class="brandcopy"><b>G98 云手机</b><span>Cloud Phone Console</span></div></div>
  <form class="card" method="post" action="/login">
    <div class="eyebrow">Secure Access</div>
    <h1>欢迎回来</h1>
    <p>输入访问密钥，进入云手机控制台。</p>
    <label class="field"><span>Access key</span><input name="k" placeholder="输入 access key" autocomplete="off" autofocus></label>
    <button type="submit">进入控制台 <svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>
    <div class="foot"><span>bdy-g98 · RK3588</span><span>redroid</span></div>
  </form>
</div>
</body></html>"""

PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>G98 云手机控制台</title>
<style>
:root{--bg:#0a0d12;--panel:#131922;--panel2:#18202b;--line:#222b38;--txt:#e7eef7;--mut:#8592a5;
--acc:#5b9dff;--ok:#37c46b;--warn:#e2a33c;--bad:#ef5b5b}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1100px 560px at 18% -12%,#16202e,#0a0d12 62%);
color:var(--txt);font:14px/1.5 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{display:flex;align-items:center;gap:18px;padding:0 20px;height:58px;border-bottom:1px solid var(--line);
background:rgba(12,16,22,.86);backdrop-filter:blur(8px);position:sticky;top:0;z-index:20}
.brand{display:flex;align-items:center;gap:9px;font-weight:650;letter-spacing:.4px}
.logo{width:26px;height:26px;border-radius:8px;background:linear-gradient(135deg,#5b9dff,#7d5bff);
display:grid;place-items:center;font-size:13px;font-weight:800;color:#08111f}
.stats{display:flex;gap:20px;margin-left:6px;flex-wrap:wrap}
.stat{display:flex;flex-direction:column;line-height:1.25}
.stat b{font-size:13px;font-weight:600}.stat span{font-size:11px;color:var(--mut)}
.grow{flex:1}
.ghost{background:transparent;border:1px solid var(--line);color:var(--mut);padding:7px 12px;border-radius:9px;
font-size:12.5px;cursor:pointer}.ghost:hover{color:var(--txt);border-color:#33415a}
main{display:grid;grid-template-columns:296px minmax(0,1fr) 300px;gap:16px;padding:16px;align-items:start}
@media(max-width:1180px){main{grid-template-columns:1fr}.meta{order:3}}
aside{background:var(--panel);border:1px solid var(--line);border-radius:14px}
.listwrap{padding:12px;display:flex;flex-direction:column;gap:10px;max-height:calc(100vh - 92px);overflow:auto}
.search{width:100%;padding:9px 11px;border-radius:9px;border:1px solid var(--line);background:#0e141c;color:var(--txt);
font-size:13px;outline:none}.search:focus{border-color:#33415a}
.ghead{display:flex;align-items:center;justify-content:space-between;margin:8px 2px 2px;font-size:11px;
letter-spacing:1.2px;text-transform:uppercase;color:var(--mut)}
.card{padding:10px 11px;border-radius:11px;border:1px solid transparent;background:var(--panel2);cursor:pointer;
transition:border-color .12s,background .12s}
.card:hover{border-color:#2c3a52}
.card.sel{border-color:var(--acc);background:#16233a;box-shadow:0 0 0 1px rgba(91,157,255,.35) inset}
.r1{display:flex;align-items:center;gap:7px}
.r1 b{font-size:14px;letter-spacing:.4px}
.r2{display:flex;align-items:center;gap:6px;margin-top:5px;flex-wrap:wrap}
.r3{margin-top:5px;font-size:10.5px;color:#6c788a;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{width:7px;height:7px;border-radius:50%;background:#3a465a;flex:none}
.dot.on{background:var(--ok);box-shadow:0 0 0 3px rgba(55,196,107,.14)}
.dot.off{background:#46536a}
.b{font-size:11px;padding:2px 7px;border-radius:20px;border:1px solid var(--line);color:var(--mut)}
.b.ok{color:#8ff0b6;border-color:rgba(55,196,107,.35);background:rgba(55,196,107,.10)}
.b.warn{color:#f2cf8f;border-color:rgba(226,163,60,.35);background:rgba(226,163,60,.10)}
.b.bad{color:#ffa8a8;border-color:rgba(239,91,91,.35);background:rgba(239,91,91,.10)}
.mini{font-size:11px;color:var(--mut);margin-left:auto}
.stage{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.shead{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.shead h2{margin:0;font-size:15px;letter-spacing:.4px}
.scr{display:grid;place-items:center;padding:16px;background:
linear-gradient(45deg,#0d1218 25%,transparent 25%) 0 0/22px 22px,
linear-gradient(-45deg,#0d1218 25%,transparent 25%) 0 11px/22px 22px,
linear-gradient(45deg,transparent 75%,#0d1218 75%) 11px -11px/22px 22px,
linear-gradient(-45deg,transparent 75%,#0d1218 75%) -11px 0/22px 22px,#0e131a;min-height:300px}
.scrinner{position:relative;display:block;width:100%;max-width:min(100%,calc(62vh * 9 / 20));aspect-ratio:9/20;margin:0 auto;line-height:0;background:#000;border-radius:10px}
.scrinner canvas{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000;
border-radius:10px;border:1px solid #2a3444;pointer-events:none;z-index:2;
box-shadow:0 18px 48px rgba(0,0,0,.55)}
.scrinner video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000;
border-radius:10px;border:1px solid #2a3444;pointer-events:none;z-index:3;
box-shadow:0 18px 48px rgba(0,0,0,.55)}
.scr img{width:100%;height:100%;object-fit:contain;border-radius:10px;
box-shadow:0 18px 48px rgba(0,0,0,.55);cursor:crosshair;touch-action:none;user-select:none;
border:1px solid #2a3444;background:#000}
.ctrls{display:flex;flex-direction:column;gap:10px;padding:12px 14px 16px;border-top:1px solid var(--line)}
.krow{display:flex;gap:8px;flex-wrap:wrap}
.kbtn{padding:8px 13px;border-radius:9px;border:1px solid var(--line);background:var(--panel2);color:var(--txt);
font-size:12.5px;cursor:pointer}.kbtn:hover{border-color:#33415a;background:#1d2634}
.trow{display:flex;gap:8px}
.trow input{flex:1;padding:9px 12px;border-radius:9px;border:1px solid var(--line);background:#0e141c;color:var(--txt);
font-size:13px;outline:none}.trow input:focus{border-color:var(--acc)}
.primary{padding:9px 16px;border-radius:9px;border:0;background:var(--acc);color:#08111f;font-weight:650;font-size:13px;cursor:pointer}
.primary:hover{filter:brightness(1.08)}
.meta{padding:14px}
.meta h3{margin:0 0 10px;font-size:12px;letter-spacing:1.2px;text-transform:uppercase;color:var(--mut)}
.kv{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px dashed #1d2531;font-size:12.5px}
.kv:last-child{border-bottom:0}
.kv span{color:var(--mut);flex:none}.kv b{font-weight:500;text-align:right;word-break:break-all}
.sect{margin:16px 0 6px;font-size:11px;letter-spacing:1.2px;text-transform:uppercase;color:var(--mut)}
.empty{padding:60px 20px;text-align:center;color:var(--mut);font-size:13px}
.hint{padding:0 14px 12px;color:#6c788a;font-size:11.5px;text-align:center}
.scrinner #screen{position:absolute!important;inset:0!important;width:100%!important;height:100%!important;object-fit:contain!important}
.zoomwrap .scr img{max-height:none;max-width:min(100%,720px);width:auto}
#livewrap{position:fixed;inset:0;z-index:60;background:#0a0d12;display:flex;flex-direction:column}
#livewrap{background:#080b10}
.livebody{flex:1;display:flex;min-height:0}
.livescr{flex:1;display:grid;place-items:center;min-width:0;padding:10px}
.livescr img{max-height:calc(100vh - 60px);max-width:100%;object-fit:contain;background:#000;border-radius:6px;
cursor:crosshair;touch-action:none;user-select:none;-webkit-user-drag:none}
img.dragging{opacity:.92;filter:brightness(1.05)}
img.holding{outline:2px solid rgba(94,214,163,.55);outline-offset:-2px}
.livecam{width:270px;flex:none;border-left:1px solid var(--line);padding:14px;display:flex;flex-direction:column;gap:8px;background:#0c1016}
.livecam h4{margin:0 0 4px;font-size:13px}
.livecam select,.livecam input[type=file]{width:100%;background:#0e141c;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px;font-size:12px}
.camrow{display:flex;gap:8px;margin-top:4px}
.camrow button{flex:1}
#camlog{color:var(--mut);font-size:11.5px;line-height:1.5;word-break:break-all}
.livebar{display:flex;align-items:center;gap:12px;padding:8px 14px;border-bottom:1px solid var(--line);background:#0c1016}
.livebar b{font-size:13px}
.livebar a{padding:6px 11px;border-radius:8px;border:1px solid var(--line);color:var(--mut);text-decoration:none;font-size:12px}
.livebar a:hover{color:var(--txt);border-color:#33415a}
#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:#1d2634;border:1px solid #33415a;
padding:10px 16px;border-radius:10px;font-size:13px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:50}
#toast.show{opacity:1}
.hide{display:none!important}

/* ===== v2 design system ===== */
:root{
  --bg:#07090d;--bg-2:#0b1016;--panel:#0e141c;--panel-2:#121a24;--panel-3:#17212d;
  --line:rgba(255,255,255,.075);--line-2:rgba(255,255,255,.13);
  --txt:#f2f6fb;--mut:#8e9bae;--dim:#5f6b7c;
  --acc:#7aa7ff;--acc-2:#c39bff;--ok:#4ade80;--warn:#fbbf24;--bad:#fb7185;
  --r:18px;
}
html,body{height:100%}
body{
  background:
    radial-gradient(900px 560px at 6% -12%,rgba(122,167,255,.14),transparent 62%),
    radial-gradient(780px 540px at 102% -8%,rgba(195,155,255,.10),transparent 60%),
    var(--bg);
  color:var(--txt);
  font:13px/1.5 "Inter","SF Pro Display",-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  overflow:hidden;
  -webkit-font-smoothing:antialiased;
}
body::after{
  content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background:linear-gradient(rgba(255,255,255,.016) 1px,transparent 1px);
  background-size:100% 4px;opacity:.35;
}
::selection{background:rgba(122,167,255,.32);color:#fff}
.app{height:100vh;max-width:1760px;margin:0 auto;padding:13px 20px 17px;display:flex;flex-direction:column;gap:13px;position:relative;z-index:1;overflow:hidden}
.eyebrow{font-size:9px;letter-spacing:1.9px;text-transform:uppercase;color:var(--dim);font-weight:650}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}

/* top bar */
.topbar{height:56px;display:flex;align-items:center;gap:24px;padding:0 3px;flex:none;position:static;background:transparent;border-bottom:0;z-index:auto}
.brand{display:flex;align-items:center;gap:10px;min-width:185px}
.logo{
  width:34px;height:34px;border-radius:11px;display:grid;place-items:center;flex:none;
  font-size:14px;font-weight:800;color:#07101e;
  background:linear-gradient(145deg,#a9c8ff 0%,#7aa7ff 42%,#b397ff 100%);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.55),0 6px 22px rgba(122,167,255,.28);
}
.brandcopy{display:flex;flex-direction:column;line-height:1.08;gap:3px}
.brandcopy b{font-size:15px;letter-spacing:.2px;font-weight:680}
.brandcopy span{font-size:8.5px;letter-spacing:1.7px;text-transform:uppercase;color:var(--dim);font-weight:600}
.stats{display:flex;align-items:center;height:40px;padding:3px 4px;border:1px solid var(--line);border-radius:12px;background:rgba(255,255,255,.022);overflow:hidden}
.stat{display:flex;flex-direction:column;justify-content:center;gap:2px;padding:0 11px;border-left:1px solid var(--line);line-height:1.1;white-space:nowrap}
.stat:first-child{border-left:0}
.stat b{font-size:12px;font-weight:600;letter-spacing:.1px;font-variant-numeric:tabular-nums}
.stat span{font-size:8.5px;letter-spacing:1.3px;text-transform:uppercase;color:var(--dim);font-weight:600}
.topright{margin-left:auto;display:flex;align-items:center;gap:4px}
.build{font-size:9.5px;color:var(--dim);margin-right:7px;white-space:nowrap;letter-spacing:.2px}
.toolbtn{
  display:inline-flex;align-items:center;justify-content:center;gap:6px;height:33px;padding:0 10px;
  background:transparent;border:1px solid transparent;border-radius:9px;color:var(--mut);
  font:500 11.5px/1 inherit;cursor:pointer;white-space:nowrap;
  transition:background .15s,color .15s,border-color .15s,transform .15s;
}
.toolbtn svg{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;opacity:.9;flex:none}
.toolbtn:hover{background:rgba(255,255,255,.055);color:var(--txt);border-color:var(--line)}
.toolbtn:active{transform:scale(.97)}
#vmode{height:23px;padding:0 8px;border-radius:6px;background:rgba(74,222,128,.08);border:1px solid rgba(74,222,128,.22);color:#94f8b6;font:650 9px/21px inherit;letter-spacing:1px;display:inline-flex;align-items:center}
#vmode.b{height:23px;padding:0 8px;line-height:21px;background:rgba(74,222,128,.08);border-color:rgba(74,222,128,.22);color:#94f8b6}

/* shell */
.shell{display:grid;grid-template-columns:292px minmax(0,1fr) 320px;grid-template-rows:minmax(0,1fr);gap:14px;flex:1;min-height:0;padding:0;align-items:stretch;overflow:hidden}
.rail,.inspector{
  background:linear-gradient(180deg,rgba(17,24,33,.94),rgba(9,13,19,.94));
  border:1px solid var(--line);border-radius:20px;overflow:hidden;
  box-shadow:0 22px 70px rgba(0,0,0,.36),inset 0 1px 0 rgba(255,255,255,.035);
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);
}
.rail{display:flex;flex-direction:column;padding:18px 13px 11px;height:100%;min-height:0}
.railtop{display:flex;align-items:flex-start;justify-content:space-between;padding:0 5px 13px}
.railtop h1{margin:4px 0 0;font-size:19px;line-height:1;font-weight:650;letter-spacing:-.2px}
.searchbox{position:relative;margin:0 4px 3px}
.searchbox svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);width:14px;height:14px;fill:none;stroke:var(--dim);stroke-width:1.8;stroke-linecap:round}
.search{width:100%;height:38px;padding:0 11px 0 35px;border-radius:10px;border:1px solid var(--line);background:rgba(0,0,0,.24);color:var(--txt);font-size:12px;outline:none;transition:border-color .15s,background .15s}
.search::placeholder{color:#536072}
.search:focus{border-color:rgba(122,167,255,.5);background:rgba(0,0,0,.32)}
.groups{flex:1;min-height:0;overflow:auto;padding:2px 4px 4px;scrollbar-width:thin;scrollbar-color:#2a3442 transparent}
.groups::-webkit-scrollbar{width:4px}.groups::-webkit-scrollbar-thumb{background:#2a3442;border-radius:4px}
.ghead{display:flex;align-items:center;justify-content:space-between;padding:14px 5px 7px;font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);font-weight:650}
#c-run,#c-off{font-size:9px;color:var(--dim);font-variant-numeric:tabular-nums;border:1px solid var(--line);border-radius:5px;padding:1px 5px;line-height:1.3}
.cards{display:flex;flex-direction:column;gap:7px}
.card{position:relative;padding:11px 11px 10px 14px;border-radius:13px;border:1px solid transparent;background:rgba(255,255,255,.024);cursor:pointer;overflow:hidden;transition:background .15s,border-color .15s,transform .15s}
.card::before{content:"";position:absolute;left:0;top:12px;bottom:12px;width:2px;border-radius:2px;background:transparent;transition:background .15s,box-shadow .15s}
.card:hover{background:rgba(255,255,255,.045);border-color:var(--line);transform:translateY(-1px)}
.card.sel{background:linear-gradient(135deg,rgba(122,167,255,.16),rgba(122,167,255,.045));border-color:rgba(122,167,255,.36);box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}
.card.sel::before{background:var(--acc);box-shadow:0 0 12px var(--acc)}
.r1{display:flex;align-items:center;gap:7px;min-width:0}
.r1 b{font-size:13px;font-weight:650;letter-spacing:.2px}
.r1 .mini{margin-left:auto;font-size:9.5px;color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}
.r2{display:flex;align-items:center;gap:5px;margin-top:6px;flex-wrap:wrap}
.r3{margin-top:5px;font-size:9.5px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;letter-spacing:.1px}
.dot{width:6px;height:6px;border-radius:50%;background:#3b4657;flex:none}
.dot.on{background:var(--ok);box-shadow:0 0 0 3px rgba(74,222,128,.11)}
.dot.off{background:#465366}
.b{display:inline-flex;align-items:center;height:19px;padding:0 7px;border-radius:5px;border:1px solid var(--line);background:rgba(255,255,255,.02);color:var(--mut);font-size:9.5px;font-weight:500;letter-spacing:.1px;white-space:nowrap}
.b.ok{color:#8ff0b6;border-color:rgba(74,222,128,.3);background:rgba(74,222,128,.09)}
.b.warn{color:#f7d48f;border-color:rgba(251,191,36,.28);background:rgba(251,191,36,.08)}
.b.bad{color:#ffa8b5;border-color:rgba(251,113,133,.3);background:rgba(251,113,133,.08)}
.mini{font-size:10px;color:var(--mut)}
.railfoot{display:flex;align-items:center;gap:7px;padding:11px 5px 2px;margin-top:9px;border-top:1px solid var(--line);font-size:9.5px;color:var(--dim);letter-spacing:.2px}
.railfoot b{margin-left:auto;color:#8ff0b6;font-weight:550}
.pulse{width:5px;height:5px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 3px rgba(74,222,128,.1);animation:pulse 2.2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}

/* workspace / viewer */
.workspace{min-width:0;min-height:0;height:100%;display:flex;flex-direction:column;overflow:hidden}
#work{height:100%;min-height:0;display:flex;flex-direction:column;overflow:hidden}
.empty{flex:1;min-height:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:9px;padding:40px;text-align:center;border:1px dashed var(--line-2);border-radius:20px;background:rgba(255,255,255,.014)}
.emptyicon{width:52px;height:52px;border-radius:16px;display:grid;place-items:center;border:1px solid var(--line);background:rgba(255,255,255,.02);color:var(--dim);margin-bottom:3px}
.emptyicon svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.empty h2{margin:0;font-size:16px;font-weight:600;letter-spacing:-.1px;color:var(--txt)}
.empty p{margin:0;font-size:11.5px;color:var(--dim);max-width:260px;line-height:1.65}
.empty.small{padding:24px 8px;border:0;background:transparent;font-size:11px;color:var(--dim);align-items:flex-start;text-align:left}
.viewer{height:100%;min-height:0;display:flex;flex-direction:column;background:linear-gradient(180deg,rgba(17,24,33,.97),rgba(9,13,19,.98));border:1px solid var(--line);border-radius:20px;overflow:hidden;box-shadow:0 26px 80px rgba(0,0,0,.42),inset 0 1px 0 rgba(255,255,255,.035)}
.viewerbar{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:14px 18px 12px;border-bottom:1px solid var(--line);flex-wrap:nowrap;flex:none}
.vtitle{min-width:0}
.titlerow{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-top:3px}
.viewerbar h1{margin:0;font-size:19px;line-height:1.1;font-weight:650;letter-spacing:-.3px}
.badges{display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.vactions{display:flex;align-items:center;gap:2px;flex-wrap:nowrap;justify-content:flex-end;padding-top:2px;flex:none}
.vactions .toolbtn{height:29px;padding:0 8px;font-size:10.5px;color:var(--dim)}
.vactions .toolbtn:hover{color:var(--txt)}
.scr{flex:1 1 0;min-height:0;overflow:hidden;display:flex;align-items:center;justify-content:center;padding:14px;position:relative;background:
  radial-gradient(circle at 50% 45%,rgba(122,167,255,.055),transparent 55%),
  linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),
  linear-gradient(90deg,rgba(255,255,255,.018) 1px,transparent 1px);
  background-size:auto,30px 30px,30px 30px;
}
.scr::after{content:"";position:absolute;inset:0;pointer-events:none;background:radial-gradient(ellipse at center,transparent 52%,rgba(0,0,0,.28))}
.scrinner{position:relative;display:block;height:100%;width:auto;max-width:100%;aspect-ratio:9/20;margin:auto;line-height:0;background:#000;border-radius:25px;border:7px solid #151b24;overflow:hidden;box-shadow:0 0 0 1px rgba(255,255,255,.09),0 30px 90px rgba(0,0,0,.7),0 0 60px rgba(122,167,255,.06);z-index:1}
.scrinner::after{content:"";position:absolute;left:50%;top:5px;transform:translateX(-50%);width:32px;height:3px;border-radius:3px;background:rgba(255,255,255,.16);z-index:6;pointer-events:none}
.scrinner img,.scrinner canvas,.scrinner video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000;border-radius:18px}
.scrinner canvas,.scrinner video{pointer-events:none;z-index:3}
.scrinner video{z-index:4}
.scrinner img{cursor:crosshair;touch-action:none;user-select:none;-webkit-user-drag:none;z-index:2}
.scrinner img.dragging{opacity:.96}
.controlbar{padding:9px 16px 11px;border-top:1px solid var(--line);background:rgba(6,9,14,.35);flex:0 0 auto}
.hint{font-size:9.5px;color:var(--dim);text-align:center;margin-bottom:7px;letter-spacing:.15px}
.hint .sep{margin:0 5px;color:#394455}
.kbd{display:inline-flex;align-items:center;height:16px;padding:0 5px;border:1px solid var(--line);border-radius:4px;color:var(--mut);font-size:8.5px;letter-spacing:.2px}
.krow{display:flex;gap:5px;flex-wrap:wrap;justify-content:center}
.kbtn{height:27px;padding:0 10px;border-radius:7px;border:1px solid var(--line);background:rgba(255,255,255,.022);color:var(--mut);font:500 10.5px/25px inherit;cursor:pointer;transition:background .12s,color .12s,border-color .12s}
.kbtn:hover{background:rgba(255,255,255,.06);color:var(--txt);border-color:var(--line-2)}
.kbtn:active{transform:translateY(1px)}
.trow{display:flex;gap:6px;margin-top:8px}
.trow input{flex:1;min-width:0;height:34px;padding:0 11px;border:1px solid var(--line);border-radius:8px;background:rgba(0,0,0,.22);color:var(--txt);font-size:11.5px;outline:none;transition:border-color .15s}
.trow input::placeholder{color:#4f5b6c}
.trow input:focus{border-color:rgba(122,167,255,.45)}
.primary{height:34px;padding:0 15px;border:1px solid rgba(122,167,255,.25);border-radius:8px;background:linear-gradient(135deg,#9dc0ff,#7aa7ff 55%,#b49bff);color:#07101d;font:650 11.5px/1 inherit;cursor:pointer;box-shadow:0 5px 18px rgba(122,167,255,.2);transition:filter .15s,transform .15s}
.primary:hover{filter:brightness(1.06)}
.primary:active{transform:scale(.98)}

/* inspector */
.inspector{display:flex;flex-direction:column;padding:18px 15px 10px;height:100%;min-height:0}
.inspecthead{display:flex;align-items:flex-start;justify-content:space-between;padding:0 3px 11px;border-bottom:1px solid var(--line);flex:none}
.inspecthead h2{margin:4px 0 0;font-size:16px;line-height:1;font-weight:620;letter-spacing:-.1px}
.inspecthead .mono{font-size:9px;color:var(--dim);letter-spacing:1.2px;padding-top:2px}
.itabs{display:flex;gap:4px;padding:10px 2px 2px;flex:none}
.itab{flex:1;background:transparent;border:1px solid transparent;border-radius:9px;color:var(--dim);font:inherit;font-size:11px;padding:6px 0;cursor:pointer;transition:.15s}
.itab:hover{color:var(--txt);background:rgba(255,255,255,.04)}
.itab.on{background:rgba(255,255,255,.07);border-color:var(--line-2);color:var(--txt);font-weight:600}
.ipanes{flex:1;min-height:0;overflow:auto;padding-top:12px}
.ipane.hide{display:none!important}
#pane-cam .mini,#pane-loc .mini{display:block;margin:10px 0 5px}
#pane-cam select,#pane-cam input:not([type=file]),#pane-loc input{width:100%;background:rgba(0,0,0,.28);color:var(--txt);border:1px solid var(--line);border-radius:9px;padding:8px 9px;font-size:11px;outline:none;font-family:inherit;box-sizing:border-box}
#pane-cam select:focus,#pane-cam input:focus,#pane-loc input:focus{border-color:var(--acc)}
#pane-cam #camlog{margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}
.metabody{flex:1;min-height:0;overflow:auto;padding:4px 3px 6px;scrollbar-width:thin;scrollbar-color:#2a3442 transparent}
.metabody::-webkit-scrollbar{width:4px}.metabody::-webkit-scrollbar-thumb{background:#2a3442;border-radius:4px}
.sect{margin:15px 0 5px;font-size:8.5px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);font-weight:650}
.sect:first-child{margin-top:8px}
.kv{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:7.5px 0;border-bottom:1px solid rgba(255,255,255,.045);font-size:11.5px;line-height:1.55}
.kv:last-child{border-bottom:0}
.kv span{color:var(--dim);flex:none}
.kv b{font-weight:500;text-align:right;word-break:break-all;color:#c8d3e1;max-width:66%}
#meta .empty.small{padding:18px 2px;font-size:10.5px}

/* toast */
#toast{position:fixed;left:50%;bottom:24px;transform:translate(-50%,8px);z-index:120;background:rgba(18,26,36,.95);border:1px solid var(--line-2);color:var(--txt);padding:9px 15px;border-radius:10px;font-size:11.5px;opacity:0;pointer-events:none;transition:opacity .18s,transform .18s;backdrop-filter:blur(14px);box-shadow:0 16px 44px rgba(0,0,0,.5)}
#toast.show{opacity:1;transform:translate(-50%,0)}

/* zoom */
body.zoomwrap .viewer{position:fixed;inset:14px;z-index:70;height:auto;background:linear-gradient(180deg,#111821,#080c11);box-shadow:0 0 0 1px rgba(255,255,255,.08),0 40px 120px rgba(0,0,0,.8)}
body.zoomwrap .scr{padding:6px}
body.zoomwrap .scrinner{height:100%;width:auto;max-width:100%}
body.zoomwrap .scr img{max-width:none;width:100%;height:100%}

/* live overlay */
#livewrap{position:fixed;inset:0;z-index:90;background:rgba(5,8,12,.95);backdrop-filter:blur(18px);display:flex;flex-direction:column}
.livebar{display:flex;align-items:center;gap:13px;height:56px;padding:0 20px;border-bottom:1px solid var(--line);background:rgba(10,15,21,.72);flex:none}
.livebar b{font-size:13px;font-weight:600}
.livebar .mini{color:var(--dim)}
.livescr{flex:1;display:flex;align-items:center;justify-content:center;min-width:0;padding:14px}
.livescr img{max-height:calc(100vh - 84px);max-width:100%;object-fit:contain;background:#000;border-radius:12px;box-shadow:0 24px 70px rgba(0,0,0,.6);cursor:crosshair;touch-action:none;user-select:none;-webkit-user-drag:none}
.livecam{width:294px;flex:none;border-left:1px solid var(--line);padding:17px;display:flex;flex-direction:column;gap:7px;background:rgba(9,14,20,.72)}
.livecam h4{margin:0 0 3px;font-size:12px;font-weight:600}
.livecam .mini{font-size:9.5px;color:var(--dim);line-height:1.5}
.livecam select,.livecam input[type=file]{width:100%;background:rgba(0,0,0,.24);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px;font-size:10.5px;outline:none}
.camrow{display:flex;gap:6px;margin-top:3px}
.camrow button{flex:1;height:30px;font-size:10.5px}
.camrow .primary{padding:0 8px}
#camlog{color:var(--dim);font-size:9.5px;line-height:1.55;word-break:break-all}
.camscrim{position:fixed;inset:0;z-index:78;background:rgba(3,5,9,.5);backdrop-filter:blur(2px)}
.camdrawer{position:fixed;z-index:79;right:16px;top:78px;bottom:16px;width:370px;max-width:calc(100vw - 32px);border:1px solid var(--line-2);border-radius:18px;background:linear-gradient(180deg,rgba(15,22,31,.985),rgba(8,12,18,.985));box-shadow:0 30px 80px rgba(0,0,0,.55);padding:18px;overflow:auto}
.camdrawer.hide,.camscrim.hide{display:none!important}
.camhead{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:2px}
.camhead h3{margin:2px 0 0;font-size:16px;letter-spacing:-.01em}
.camdrawer .mini{font-size:10.5px;line-height:1.6}
.locbadge{display:inline-block;margin:6px 0 8px;padding:3px 9px;border-radius:999px;border:1px solid #24506e;background:#0e2233;color:#9fd0f0;font-size:10.5px}
.locbadge.warn{border-color:#7a5a12;background:#2a2210;color:#ffd76a}
.camfile-row{display:flex;align-items:center;gap:10px;margin:6px 0 8px}
.filepick{display:inline-flex;align-items:center;justify-content:center;height:32px;padding:0 14px;border:1px solid rgba(122,162,255,.38);border-radius:9px;background:rgba(70,110,255,.14);color:#d8e6ff;font-size:11px;cursor:pointer;white-space:nowrap}
.filepick:hover{background:rgba(70,110,255,.24)}
#camfile{display:none}
.camlib{max-height:168px;overflow:auto;border:1px solid var(--line);border-radius:9px;padding:4px;background:rgba(0,0,0,.18)}
.camlib-item{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:6px 7px;border-radius:7px;cursor:pointer;font-size:12px;color:var(--txt)}
.camlib-item:hover{background:rgba(90,150,255,.16)}
.camlib-item b{font-weight:600}
.camlib-item span{color:var(--dim);font-size:11px;white-space:nowrap}
#camfname{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#camstop{color:#ffc4c4;border-color:rgba(255,120,120,.3)}
.camdrawer select,.camdrawer input[type=file],.camdrawer input:not([type=file]){width:100%;background:rgba(0,0,0,.28);color:var(--txt);border:1px solid var(--line);border-radius:9px;padding:8px 9px;font-size:11px;outline:none}
.camdrawer select:focus,.camdrawer input:focus{border-color:var(--acc)}
.camdrawer #camlog{margin-top:auto;padding-top:10px;border-top:1px solid var(--line);min-height:54px}
.zoomwrap .empty{display:none}
.qrwrap{position:fixed;inset:0;z-index:95;background:rgba(2,5,9,.78);backdrop-filter:blur(7px);display:flex;align-items:center;justify-content:center;padding:20px}
.qrwrap.hide{display:none!important}
.qrcard{width:min(340px,92vw);background:linear-gradient(180deg,#131d28,#0a1119);border:1px solid var(--line-2);border-radius:18px;padding:18px;box-shadow:0 30px 90px rgba(0,0,0,.7);text-align:center}
.qrcard h3{margin:3px 0 4px;font-size:18px}.qrcard .mini{color:var(--muted);font-size:10.5px;line-height:1.6}
#qrimg{width:280px;height:280px;max-width:100%;margin:14px auto 6px;display:block;background:#fff;border-radius:12px;padding:6px;image-rendering:pixelated}
#qrlog{min-height:28px;color:var(--dim);font-size:9.5px;line-height:1.5;word-break:break-all}


/* responsive */
@media(max-width:1320px){
  .shell{grid-template-columns:270px minmax(0,1fr) 296px}
  .stat{padding:0 9px}
  .stats .stat:nth-child(5){display:none}
}
@media(max-width:1120px){
  body{overflow:auto}
  .app{height:auto;min-height:100vh}
  .shell{display:flex;flex-direction:column;gap:12px}
  .rail{height:auto;max-height:350px;flex:none}
  .inspector{height:auto;max-height:360px;flex:none}
  .viewer{min-height:620px}
  .topbar{flex-wrap:wrap;height:auto;min-height:56px;gap:12px;padding:6px 0}
  .stats{order:3;width:100%;height:38px}
  .topright{margin-left:auto}
  .build{display:none}
  .scr{min-height:400px}
}
@media(max-width:720px){
  .app{padding:10px}
  .brandcopy span{display:none}
  .stats{overflow:auto}
  .vactions{justify-content:flex-start}
  .viewerbar{padding:12px 13px}
  .controlbar{padding:8px 10px 10px}
  .kbtn{padding:0 8px}
  .livecam{display:none}
  .camdrawer{display:flex;left:10px;right:10px;width:auto;top:64px;bottom:10px}
}

</style></head><body>
<div id="diag" style="display:none;position:fixed;left:8px;bottom:8px;z-index:9999;max-width:min(560px,92vw);padding:8px 10px;border:1px solid rgba(120,255,180,.28);border-radius:8px;background:rgba(4,10,8,.82);color:#9dffc4;font:11px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre;pointer-events:none;text-shadow:0 1px 2px #000"></div>
<div class="app">
<header class="topbar">
  <div class="brand"><div class="logo">G</div><div class="brandcopy"><b>G98</b><span>Cloud Phone Console</span></div></div>
  <div class="stats" id="stats"></div>
  <div class="topright">
    <span class="build" id="build"></span>
    <button class="toolbtn" id="live"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>实时屏幕</button>
    <button class="toolbtn" id="reload"><svg viewBox="0 0 24 24"><path d="M20.5 12a8.5 8.5 0 1 1-2.4-5.9"/><path d="M21 3v6h-6"/></svg>刷新</button>
    <button class="toolbtn" id="logout"><svg viewBox="0 0 24 24"><circle cx="8" cy="15" r="4"/><path d="m11 12 8-8 3 3-2 2 2 2-3 3-3-3-1 1"/></svg>换 key</button>
  </div>
</header>
<main class="shell">
  <aside class="rail">
    <div class="railtop"><div><div class="eyebrow">Fleet</div><h1>设备</h1></div></div>
    <div class="searchbox"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><input class="search" id="q" placeholder="搜索实例 / wx1 / l14"></div>
    <div class="groups">
      <div class="ghead"><span>运行中</span><span id="c-run">0</span></div>
      <div id="grp-run" class="cards"></div>
      <div class="ghead"><span>已停止</span><span id="c-off">0</span></div>
      <div id="grp-off" class="cards"></div>
    </div>
    <div class="railfoot"><span class="pulse"></span><span>实时触摸通道</span><b>在线</b></div>
  </aside>
  <section class="workspace">
    <div class="empty" id="empty">
      <div class="emptyicon"><svg viewBox="0 0 24 24"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M10.5 18.5h3"/></svg></div>
      <h2>选择一台云手机</h2>
      <p>从左侧设备列表开始，屏幕会出现在这里。</p>
    </div>
    <div id="work" class="hide">
      <section class="viewer">
        <div class="viewerbar">
          <div class="vtitle"><div class="eyebrow">Now viewing</div><div class="titlerow"><h1 id="s-name">-</h1><div class="badges" id="s-badges"></div></div></div>
          <div class="vactions">
            <span id="vmode" class="mode">MJPEG</span>
            <button class="toolbtn" id="camopen" title="图片、视频或外部流注入云手机相机"><svg viewBox="0 0 24 24"><path d="M4 8h3l1.5-2h7L17 8h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg>相机</button>
            <button class="toolbtn" id="camqrbtn" title="手机扫码，把手机摄像头送进云手机">扫码</button>
            <button class="toolbtn" id="wechat">打开微信</button>
            <button class="toolbtn" id="zoom">放大</button>
            <button class="toolbtn" id="locopen" title="修改 GNSS/GPS 定位">定位</button>
            <button class="toolbtn" data-act="restart">重启</button>
            <button class="toolbtn" data-act="stop">停止</button>
            <button class="toolbtn" data-act="start">启动</button>
          </div>
        </div>
        <div class="scr"><div class="scrinner" id="scrinner"><img id="screen" alt="screen" style="width:100%;height:100%;object-fit:contain"><canvas id="h264canvas" class="hide"></canvas><video id="rtcvideo" class="hide" muted playsinline autoplay></video><video id="h264video" class="hide" muted playsinline autoplay></video></div></div>
        <div class="controlbar">
          <div class="hint"><span class="kbd">轻触</span> 点击屏幕 <span class="sep">·</span> <span class="kbd">滑动</span> 按住拖动</div>
          <div class="krow">
            <button class="kbtn" data-key="4">返回</button>
            <button class="kbtn" data-key="3">主页</button>
            <button class="kbtn" data-key="187">多任务</button>
            <button class="kbtn" data-key="26">电源</button>
            <button class="kbtn" data-key="224">点亮</button>
            <button class="kbtn" data-key="24">音量+</button>
            <button class="kbtn" data-key="25">音量-</button>
            <button class="kbtn" data-key="66">回车</button>
            <button class="kbtn" data-key="67">退格</button>
          </div>
          <div class="trow">
            <input id="txt" placeholder="输入文字后回车（支持中文 / 英文 / 数字）">
            <button class="primary" id="send">发送</button>
          </div>
        </div>
      </section>
    </div>
  </section>
  <aside class="inspector">
    <div class="inspecthead"><div><div class="eyebrow">Details</div><h2>实例详情</h2></div><span class="mono">G98</span></div>
    <div class="itabs" id="itabs">
      <button class="itab on" data-itab="info">详情</button>
      <button class="itab" data-itab="cam">相机</button>
      <button class="itab" data-itab="loc">定位</button>
    </div>
    <div class="ipanes">
      <div class="ipane" id="pane-info"><div id="meta" class="metabody"><div class="empty small">未选择</div></div></div>
      <div class="ipane hide" id="pane-cam">
        <div class="mini">把图片、视频或外部流注入云手机相机节点，手机上会自动打开相机预览。停止后恢复空闲源。</div>
        <label class="mini">注入到</label>
        <select id="camnode">
          <option value="">自动（按当前相机节点）</option>
          <option value="/dev/video11">/dev/video11 · 前置</option>
          <option value="/dev/video10">/dev/video10 · 后置</option>
        </select>
        <label class="mini">画面适配（按源比例补偿预览拉伸）</label>
        <select id="camfit">
          <option value="contain">完整显示（补黑边，默认）</option>
          <option value="cover">铺满裁切（不变形）</option>
          <option value="stretch">拉伸铺满（会变形）</option>
        </select>
        <label class="mini">同时注入前后摄</label>
        <select id="camboth">
          <option value="1">开（默认，切前置也能看到）</option>
          <option value="0">关（只注入所选节点）</option>
        </select>
        <label class="mini">旋转</label>
        <select id="camrot">
          <option value="0">不旋转</option>
          <option value="90">顺时针 90°</option>
          <option value="180">180°</option>
          <option value="270">顺时针 270°</option>
        </select>
        <label class="mini">图片 / 视频文件（上传到板子，约 15fps）</label>
        <div class="camfile-row">
          <label class="filepick" for="camfile">选择图片 / 视频</label>
          <span id="camfname" class="mini">未选择文件</span>
          <input type="file" id="camfile" accept="video/*,image/*">
        </div>
        <label class="mini">素材库（点一下即热切注入，无需重启相机）</label>
        <div id="camlib" class="camlib"><div class="mini">加载中…</div></div>
        <div class="camrow">
          <button class="primary" id="camstart">注入文件</button>
          <button class="ghost" id="camstop">停止</button>
        </div>
        <label class="mini">浏览器画面（需 HTTPS 8443 打开本页）</label>
        <div class="camrow"><button class="primary" id="camstartscreen">共享浏览器画面</button></div>
        <label class="mini">外部视频流（RTSP / HTTP / HTTPS）</label>
        <input id="camurl" placeholder="rtsp://192.168.1.20:554/live 或 https://.../stream.m3u8">
        <div class="camrow"><button class="primary" id="camstarturl">接入外部流</button></div>
        <div id="camlog" class="mini">选文件或填外部流地址 → 注入到云手机相机</div>
      </div>
      <div class="ipane hide" id="pane-loc">
        <div class="mini">默认用板子出口网线的位置。只有点「应用」才会改，改了会标记「人为设定」。微信、地图拿到的是真实 Location（不带 mock 标记）。</div>
        <div id="locbadge" class="locbadge">板子默认（出口网线）</div>
        <label class="mini">纬度（-90 ~ 90）</label>
        <input id="loclat" inputmode="decimal" placeholder="31.2304">
        <label class="mini">经度（-180 ~ 180）</label>
        <input id="loclon" inputmode="decimal" placeholder="121.4737">
        <label class="mini">海拔（米）</label>
        <input id="localt" inputmode="decimal" placeholder="10">
        <div class="camrow"><button class="primary" id="locapply">应用定位</button><button class="ghost" id="locrefresh">读取当前</button></div>
        <div class="camrow"><button class="ghost" id="locboard">板子位置</button></div>
        <label class="mini">快捷城市</label>
        <div style="display:flex;flex-wrap:wrap;gap:6px">
          <button class="ghost loccity" data-lat="39.9042" data-lon="116.4074" data-alt="50">北京</button>
          <button class="ghost loccity" data-lat="31.2304" data-lon="121.4737" data-alt="10">上海</button>
          <button class="ghost loccity" data-lat="22.5431" data-lon="114.0579" data-alt="20">深圳</button>
          <button class="ghost loccity" data-lat="23.1291" data-lon="113.2644" data-alt="20">广州</button>
          <button class="ghost loccity" data-lat="30.2741" data-lon="120.1551" data-alt="15">杭州</button>
        </div>
        <div id="loclog" class="mini">读取当前定位…</div>
      </div>
    </div>
  </aside>
</main>
</div>
<div id="toast"></div>
<div id="qrmodal" class="qrwrap hide">
  <div class="qrcard">
    <div class="eyebrow">Mobile camera</div>
    <h3>用手机扫码</h3>
    <div class="mini">手机和板子连同一网络。扫码后允许摄像头权限，手机画面会注入到云手机的相机节点。</div>
    <img id="qrimg" alt="手机摄像头二维码">
    <div id="qrlog">生成中…</div>
    <div class="camrow"><button class="ghost" id="qrclose">关闭</button></div>
  </div>
</div>
<div id="livewrap" class="hide">
  <div class="livebar">
    <b>实时屏幕 · 相机推流</b>
    <span class="mini" id="livewho"></span>
    <div class="grow"></div>
    <span class="mini" id="livefps">0 fps</span>
    <div class="grow"></div>
    <button class="ghost" id="liveclose">关闭 (Esc)</button>
  </div>
  <div class="livebody">
    <div class="livescr"><img id="liveimg" alt="live screen"></div>
  </div>
</div>

<script>
window.addEventListener('error', function(e){
  var d = document.getElementById('jserr');
  if(!d){ d = document.createElement('div'); d.id='jserr'; d.style.cssText='position:fixed;left:8px;top:8px;z-index:9999;max-width:70vw;padding:8px 10px;background:#5a1d1d;color:#ffd7d7;border:1px solid #a33;border-radius:8px;font:12px/1.4 monospace;white-space:pre-wrap'; document.body.appendChild(d); }
  d.textContent = 'JS ' + (e.message||'') + ' @' + (e.filename||'') + ':' + (e.lineno||'');
});
window.addEventListener('unhandledrejection', function(e){
  var d = document.getElementById('jserr');
  if(!d){ d = document.createElement('div'); d.id='jserr'; d.style.cssText='position:fixed;left:8px;top:8px;z-index:9999;max-width:70vw;padding:8px 10px;background:#5a1d1d;color:#ffd7d7;border:1px solid #a33;border-radius:8px;font:12px/1.4 monospace;white-space:pre-wrap'; document.body.appendChild(d); }
  d.textContent = 'PROMISE ' + ((e.reason && e.reason.message) || e.reason || '');
});
var S = {rows: [], board: {}, sel: null, filter: '', shotT: null, down: null};
function el(id){return document.getElementById(id)}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function toast(m){var t=el('toast');t.textContent=m;t.className='show';setTimeout(function(){t.className=''},2200)}
function post(url, obj){
  return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)})
   .then(function(r){return r.json().catch(function(){return {}})})
   .then(function(j){if(j&&j.error) toast(j.error);return j})
   .catch(function(){toast('请求失败，检查板子是否在线')});
}
var IW = {ws:null, name:null, ok:false};
function inputWS(){
  if(!S.sel) return null;
  if(IW.ws && IW.name===S.sel && IW.ws.readyState===1) return IW.ws;
  if(IW.ws){ try{IW.ws.close()}catch(e){} IW.ws=null; }
  IW.name = S.sel; IW.ok = false;
  try{
    var u = (location.protocol==='https:'?'wss://':'ws://') + location.host +
            '/ws/input?name=' + encodeURIComponent(S.sel);
    var ws = new WebSocket(u);
    ws.onopen = function(){ IW.ok = true; };
    ws.onclose = function(){ IW.ok = false; };
    ws.onerror = function(){ IW.ok = false; };
    IW.ws = ws;
    return ws;
  }catch(e){ IW.ws=null; return null; }
}
function sendInput(a, p){
  var ws = IW.ok ? IW.ws : inputWS();
  if(ws && ws.readyState===1 && IW.ok){
    try{ ws.send(JSON.stringify({a:a, fx:p.fx, fy:p.fy})); return Promise.resolve({ok:true, ch:'ws'}); }catch(e){}
  }
  return post('/api/touch', {name:S.sel, a:a, fx:p.fx, fy:p.fy});
}
function sendKey(code){
  var ws = IW.ok ? IW.ws : inputWS();
  if(ws && ws.readyState===1 && IW.ok){
    try{ ws.send(JSON.stringify({a:'k', code:code})); return Promise.resolve({ok:true, ch:'ws'}); }catch(e){}
  }
  return post('/api/key', {name:S.sel, code:code});
}
function pct(v){return (v==null||v==='')?'':String(v)}
function card(r){
  var w=r.wechat||{}, a=r.agent||{}, on=r.state==='running';
  var sel=S.sel===r.name?' sel':'';
  var badge = w.logged_in?'<span class="b ok">已登录</span>':(on?'<span class="b warn">未登录</span>':'<span class="b">离线</span>');
  var dev = a.device_id?a.device_id.slice(0,13)+'…':'无设备号';
  return '<div class="card'+sel+'" data-n="'+esc(r.name)+'">'+
    '<div class="r1"><span class="dot '+(on?'on':'off')+'"></span><b>'+esc(r.name)+'</b>'+
    '<span class="mini">'+(on?(pct(r.cpu)||'')+' · '+(pct(r.mem)||''):'stopped')+'</span></div>'+
    '<div class="r2">'+badge+(w.version?'<span class="b">微信 '+esc(w.version)+'</span>':'')+'</div>'+
    '<div class="r3 mono">'+esc(dev)+(a.state?' · '+esc(a.state):'')+'</div></div>';
}
function renderList(){
  var q=S.filter.toLowerCase();
  var rows=S.rows.filter(function(r){return !q||r.name.toLowerCase().indexOf(q)>=0});
  var run=rows.filter(function(r){return r.state==='running'});
  var off=rows.filter(function(r){return r.state!=='running'});
  el('grp-run').innerHTML=run.map(card).join('')||'<div class="r3" style="padding:6px 2px">无</div>';
  el('grp-off').innerHTML=off.map(card).join('')||'<div class="r3" style="padding:6px 2px">无</div>';
  el('c-run').textContent=run.length; el('c-off').textContent=off.length;
}
function renderStats(){
  var b=S.board||{}, run=S.rows.filter(function(r){return r.state==='running'}).length;
  var items=[
    ['负载', b.load||'-'],
    ['内存', (b.mem_used_gb||0)+' / '+(b.mem_total_gb||0)+' GB'],
    ['磁盘', b.disk||'-'],
    ['实例', run+' / '+S.rows.length],
    ['运行', b.uptime||'-']
  ];
  el('stats').innerHTML=items.map(function(i){return '<div class="stat"><b>'+esc(i[1])+'</b><span>'+i[0]+'</span></div>'}).join('');
if(el('build')) el('build').textContent = '实时触摸 ✓ · ' + (S.build || '');
}
function cur(){for(var i=0;i<S.rows.length;i++){if(S.rows[i].name===S.sel)return S.rows[i]}return null}
function renderHead(){
  var r=cur(); if(!r){el('work').className='hide';el('empty').className='empty';return}
  el('work').className=''; el('empty').className='empty hide';
  el('s-name').textContent=r.name;
  var w=r.wechat||{}, a=r.agent||{};
  var badges=[];
  badges.push('<span class="b '+(r.state==='running'?'ok':'')+'">'+esc(r.state)+'</span>');
  badges.push('<span class="b '+(w.logged_in?'ok':'warn')+'">微信 '+(w.logged_in?'已登录':'未登录')+'</span>');
  if(w.version) badges.push('<span class="b">'+esc(w.version)+'</span>');
  if(a.state) badges.push('<span class="b">agent '+esc(a.state)+'</span>');
  el('s-badges').innerHTML=badges.join(' ');
}
function renderMeta(){
  var r=cur();
  if(!r){el('meta').innerHTML='<div class="empty" style="padding:30px 0">未选择</div>';return}
  var w=r.wechat||{}, a=r.agent||{};
  function kv(k,v){return '<div class="kv"><span>'+k+'</span><b>'+esc(v==null||v===''?'-':v)+'</b></div>'}
  var h='';
  h+='<div class="sect">设备</div>';
  h+=kv('device_id', a.device_id);
  h+=kv('credential', a.credential_id);
  h+=kv('api_base', a.api_base);
  h+=kv('agent', a.agent_version); h+=kv('probe', a.probe_version);
  h+=kv('状态', a.state);
  h+='<div class="sect">微信</div>';
  h+=kv('版本', w.version);
  h+=kv('登录', w.logged_in?'是':'否');
  h+=kv('前台', w.top_activity);
  h+=kv('进程', (w.processes||[]).join(' '));
  h+='<div class="sect">资源</div>';
  h+=kv('CPU', r.cpu); h+=kv('内存', r.mem); h+=kv('占用率', r.mem_percent);
  h+=kv('镜像', r.image); h+=kv('运行', r.uptime); h+=kv('数据', r.data_dir);
  el('meta').innerHTML=h;
}
var MSEP = {gen:0, active:false, abort:null, ms:null, sb:null, v:null, url:null, timer:null, q:null, retry:0};
function mseStop(){
  MSEP.gen++;
  MSEP.active = false;
  if(MSEP.timer){ clearTimeout(MSEP.timer); MSEP.timer = null; }
  if(MSEP.abort){ try{ MSEP.abort.abort(); }catch(e){} MSEP.abort = null; }
  var v = MSEP.v;
  if(v){ try{ v.pause(); }catch(e){} try{ v.removeAttribute('src'); v.load(); }catch(e){} v.style.opacity = ''; }
  if(MSEP.url){ try{ URL.revokeObjectURL(MSEP.url); }catch(e){} MSEP.url = null; }
  MSEP.ms = null; MSEP.sb = null; MSEP.v = null; MSEP.q = null; MSEP.gotInit = false;
  var hv = el('h264video'); if(hv){ hv.classList.add('hide'); hv.style.opacity = ''; try{ hv.removeAttribute('src'); }catch(e){} }
  var img = el('screen'); if(img) img.style.opacity = '1';
  var vm = el('vmode'); if(vm){ vm.textContent = 'MJPEG'; vm.className = 'b'; }
}
function mseFail(gen, name){
  if(gen !== MSEP.gen) return;
  var retry = MSEP.retry || 0;
  mseStop();
  if(S.sel !== name) return;
  if(retry < 6){
    MSEP.retry = retry + 1;
    MSEP.timer = setTimeout(function(){
      if(S.sel === name && !mseStart(name)) shot();
    }, 700);
    return;
  }
  MSEP.retry = 0;
  S.stageName = null;
  shot();
}
function mseStart(name){
  if(!name || !window.MediaSource || !window.SourceBuffer) return false;
  mseStop();
  var img = el('screen'), v = el('h264video');
  if(!img || !v) return false;
  var gen = ++MSEP.gen;
  MSEP.active = true;
  MSEP.gotInit = false;
  S.stageName = name;
  img.onerror = null;
  img.setAttribute('data-mode','mse');
  img.src = '/screen/' + encodeURIComponent(name) + '?t=' + Date.now();
  img.style.opacity = '1';
  v.classList.remove('hide');
  v.style.opacity = '0';
  v.muted = true; v.playsInline = true;
  var vm = el('vmode'); if(vm){ vm.textContent = 'H264'; vm.className = 'b ok'; }
  var ms = new MediaSource(); MSEP.ms = ms; MSEP.v = v;
  var url = URL.createObjectURL(ms); MSEP.url = url; v.src = url;
  var ctl = new AbortController(); MSEP.abort = ctl;
  var sb = null, q = [];
  MSEP.q = q;
  function fail(){ if(gen !== MSEP.gen) return; mseFail(gen, name); }
  function pump(){
    if(gen !== MSEP.gen || !sb || sb.updating || !q.length) return;
    try{ sb.appendBuffer(q.shift()); }catch(e){ fail(); }
  }
  function liveEdge(){
    if(gen !== MSEP.gen || !v.buffered.length) return;
    var st = v.buffered.start(0), end = v.buffered.end(v.buffered.length - 1);
    if(v.currentTime < end - 0.15){ try{ v.currentTime = Math.max(st, end - 0.05); }catch(e){} }
    if(end - st > 20 && !sb.updating){
      try{ sb.remove(0, Math.max(st, v.currentTime - 10)); }catch(e){}
    }
  }
  ms.addEventListener('sourceopen', function(){
    if(gen !== MSEP.gen) return;
    try{ sb = ms.addSourceBuffer('video/mp4; codecs="avc1.42c01f"'); }
    catch(e){ fail(); return; }
    MSEP.sb = sb;
    try{ sb.mode = 'segments'; }catch(e){}
    sb.addEventListener('updateend', function(){
      if(gen !== MSEP.gen) return;
      liveEdge();
      pump();
      if(v.paused){ try{ v.play().catch(function(){}); }catch(e){} }
    });
    sb.addEventListener('error', fail);
    fetch('/h264mp4/' + encodeURIComponent(name) + '?t=' + Date.now(), {signal:ctl.signal, cache:'no-store'})
      .then(function(resp){ if(!resp.ok) throw new Error('h264mp4 ' + resp.status); return resp.body.getReader(); })
      .then(function(reader){
        function read(){
          reader.read().then(function(r){
            if(gen !== MSEP.gen) return;
            if(r.done){ fail(); return; }
            q.push(r.value);
            MSEP.gotInit = true;
            if(q.length > 100){ fail(); return; }
            pump(); read();
          }).catch(function(e){ if(gen === MSEP.gen && e && e.name !== 'AbortError') fail(); });
        }
        read();
      }).catch(function(e){ if(gen === MSEP.gen && e && e.name !== 'AbortError') fail(); });
  });
  MSEP.timer = setTimeout(function(){ if(gen === MSEP.gen && !MSEP.gotInit) fail(); }, 8000);
  v.addEventListener('playing', function onplay(){
    if(gen !== MSEP.gen) return;
    v.removeEventListener('playing', onplay);
    if(MSEP.timer){ clearTimeout(MSEP.timer); MSEP.timer = null; }
    MSEP.retry = 0;
  });
  function reveal(){
    if(gen !== MSEP.gen) return;
    v.style.opacity = '1';
    var im = el('screen'); if(im) im.style.opacity = '0';
  }
  if(v.requestVideoFrameCallback) v.requestVideoFrameCallback(reveal);
  else v.addEventListener('loadeddata', reveal, {once:true});
  v.addEventListener('error', fail);
  return true;
}
var H264P = {gen:0, active:false, abort:null, dec:null, fallback:null, frames:0, name:null};
function h264Stop(){
  H264P.gen++;
  H264P.active = false;
  if(H264P.fallback){ clearTimeout(H264P.fallback); H264P.fallback = null; }
  if(H264P.abort){ try{ H264P.abort.abort(); }catch(e){} H264P.abort = null; }
  if(H264P.dec){ try{ if(H264P.dec.state !== 'closed') H264P.dec.close(); }catch(e){} H264P.dec = null; }
  var c = el('h264canvas'); if(c) c.classList.add('hide');
  var hv = el('h264video'); if(hv){ hv.classList.add('hide'); try{ hv.pause(); }catch(e){} }
  var img = el('screen'); if(img) img.style.opacity = '1';
  var vm = el('vmode'); if(vm){ vm.textContent = 'MJPEG'; vm.className = 'b'; }
}
function h264Fallback(gen, name){
  if(gen !== H264P.gen) return;
  h264Stop();
  if(S.sel === name){ S.stageName = null; shot(); }
}
function h264Start(name){
  if(!name || !window.VideoDecoder) return false;
  h264Stop();
  var img = el('screen'), c = el('h264canvas');
  if(!img || !c) return false;
  var gen = ++H264P.gen;
  H264P.active = true; H264P.name = name; H264P.frames = 0;
  S.stageName = name;
  img.onerror = null;
  img.setAttribute('data-mode','h264');
  img.src = '/screen/' + encodeURIComponent(name) + '?t=' + Date.now();
  img.style.opacity = '0';
  c.classList.remove('hide');
  var ctx = c.getContext('2d'), dec = null, gotFrame = false;
  var ctl = new AbortController(); H264P.abort = ctl;
  function fail(){ h264Fallback(gen, name); }
  H264P.fallback = setTimeout(function(){ if(!gotFrame) fail(); }, 15000);
  fetch('/h264/' + encodeURIComponent(name) + '?t=' + Date.now(), {signal:ctl.signal, cache:'no-store'})
    .then(function(resp){ if(!resp.ok) throw new Error('h264 ' + resp.status); return resp.body.getReader(); })
    .then(function(reader){
      var buf = new Uint8Array(0);
      function parse(){
        while(true){
          if(gen !== H264P.gen) return;
          if(!dec){
            if(buf.length < 8) return;
            if(buf[0]!==0x47 || buf[1]!==0x39 || buf[2]!==0x38 || buf[3]!==0x48) throw new Error('bad h264 magic');
            var clen = ((buf[4]<<24)|(buf[5]<<16)|(buf[6]<<8)|buf[7])>>>0;
            if(buf.length < 8 + clen) return;
            var cfg = JSON.parse(new TextDecoder().decode(buf.subarray(8, 8+clen)));
            buf = buf.subarray(8+clen);
            var bin = atob(cfg.description), desc = new Uint8Array(bin.length);
            for(var i=0;i<bin.length;i++) desc[i] = bin.charCodeAt(i);
            dec = new VideoDecoder({
              output:function(frame){
                if(gen !== H264P.gen){ frame.close(); return; }
                var w = frame.displayWidth, h = frame.displayHeight;
                if(c.width !== w || c.height !== h){ c.width = w; c.height = h; }
                ctx.drawImage(frame, 0, 0, w, h);
                frame.close();
                if(!gotFrame){
                  gotFrame = true;
                  if(H264P.fallback){ clearTimeout(H264P.fallback); H264P.fallback = null; }
                }
                H264P.frames++;
              },
              error:function(){ fail(); }
            });
            H264P.dec = dec;
            dec.configure({codec:cfg.codec, description:desc, optimizeForLatency:true});
            continue;
          }
          if(buf.length < 14) return;
          var dv = new DataView(buf.buffer, buf.byteOffset, 14);
          var type = dv.getUint8(0), key = dv.getUint8(1);
          var pts = dv.getUint32(2) * 4294967296 + dv.getUint32(6);
          var len = dv.getUint32(10);
          if(buf.length < 14 + len) return;
          var payload = buf.subarray(14, 14+len);
          buf = buf.subarray(14+len);
          if(type === 1){
            try{ dec.decode(new EncodedVideoChunk({type:key ? 'key' : 'delta', timestamp:pts, data:payload})); }
            catch(e){ fail(); return; }
          }
        }
      }
      function pump(){
        reader.read().then(function(r){
          if(gen !== H264P.gen) return;
          if(r.done){ fail(); return; }
          var nb = new Uint8Array(buf.length + r.value.length);
          nb.set(buf); nb.set(r.value, buf.length); buf = nb;
          try{ parse(); }catch(e){ console.warn('h264', e); fail(); return; }
          pump();
        }).catch(function(e){ if(gen === H264P.gen && e && e.name !== 'AbortError') fail(); });
      }
      pump();
    }).catch(function(e){ if(gen === H264P.gen && e && e.name !== 'AbortError') fail(); });
  return true;
}
function shot(){
  if(!S.sel) return;
  if(MSEP.active) return;
  if(H264P.active) return;
  if(RTCP.active) return;
  var name = S.sel, img = el('screen');
  img.style.opacity = '1';
  var hc = el('h264canvas'); if(hc) hc.classList.add('hide');
  var hv = el('h264video'); if(hv){ hv.classList.add('hide'); hv.style.opacity = ''; }
  var vm = el('vmode'); if(vm){ vm.textContent = 'MJPEG'; vm.className = 'b'; }
  if(S.stageName === name && img.getAttribute('data-mode') === 'stream') return;
  S.stageName = name;
  img.setAttribute('data-mode','stream');
  function arm(){
    img.onerror = function(){
      img.onerror = null;
      // 流断了先显示一张静态图，2 秒后自动重连 MJPEG；避免永久退化成 1.5 秒一张的 PNG 轮询
      img.setAttribute('data-mode','png');
      img.src = '/screen/' + encodeURIComponent(name) + '?t=' + Date.now();
      clearTimeout(S.retryT);
      S.retryT = setTimeout(function(){
        if(S.sel !== name) return;
        img.setAttribute('data-mode','stream');
        arm();
        img.src = '/stream/' + encodeURIComponent(name) + '?t=' + Date.now();
      }, 2000);
    };
  }
  arm();
  img.src = '/stream/' + encodeURIComponent(name) + '?t=' + Date.now();
}
function sched(){
  clearTimeout(S.shotT);
  S.shotT = setTimeout(function(){
    if(!S.sel) return;
    var img = el('screen');
    if(img.getAttribute('data-mode') === 'png'){
      img.src = '/screen/' + encodeURIComponent(S.sel) + '?t=' + Date.now();
    }
    sched();
  }, 1500);
}
var RTCP = {gen:0, active:false, pc:null, v:null, timer:null, name:null, got:false, watch:null, lastT:-1, stall:0};
function rtcStop(){
  RTCP.gen++;
  RTCP.active = false;
  if(RTCP.timer){ clearTimeout(RTCP.timer); RTCP.timer = null; }
  if(RTCP.watch){ clearInterval(RTCP.watch); RTCP.watch = null; }
  if(RTCP.ping){ clearInterval(RTCP.ping); RTCP.ping = null; }
  RTCP.lastT = -1; RTCP.stall = 0;
  if(RTCP.pc){ try{ RTCP.pc.close(); }catch(e){} RTCP.pc = null; }
  var v = el('rtcvideo');
  if(v){ try{ v.pause(); }catch(e){} try{ v.srcObject = null; }catch(e){} v.classList.add('hide'); v.style.opacity = ''; }
  RTCP.name = null;
  // The server's rtc_start serializes replacement and stops the previous
  // pipeline under a lock.  Posting /api/rtc/stop from here races that start
  // request and can kill the stream we just asked for.
}
function rtcFallback(gen, name){
  if(gen !== RTCP.gen) return;
  rtcStop();
  if(S.sel !== name) return;
  // A transient encoder/MediaMTX startup failure used to strand the page on
  // the much slower MJPEG fallback forever.  Retry the low-latency path twice
  // before giving up.
  if((RTCP.retries || 0) < 2){
    RTCP.retries = (RTCP.retries || 0) + 1;
    var retryGen = RTCP.gen;
    setTimeout(function(){ if(S.sel === name && RTCP.gen === retryGen) rtcStart(name); }, 700);
    return;
  }
  toast('WebRTC 未接通，已回退 H264');
  var started = false;
  try{ started = mseStart(name); }catch(e){ started = false; }
  if(!started){ try{ started = h264Start(name); }catch(e){ started = false; } }
  if(!started) shot();
}
function rtcStart(name){
  if(!name || !window.RTCPeerConnection) return false;
  rtcStop();
  try{ mseStop(); }catch(e){}
  try{ h264Stop(); }catch(e){}
  var img = el('screen'), v = el('rtcvideo');
  if(!img || !v) return false;
  var gen = ++RTCP.gen;
  RTCP.active = true; RTCP.name = name; RTCP.got = false;
  S.stageName = name;
  img.onerror = null;
  img.setAttribute('data-mode','rtc');
  img.src = '/screen/' + encodeURIComponent(name) + '?t=' + Date.now();
  img.style.opacity = '1';
  v.classList.remove('hide'); v.style.opacity = '0';
  v.muted = true; v.playsInline = true;
  var vm = el('vmode'); if(vm){ vm.textContent = 'WebRTC'; vm.className = 'b ok'; }
  function fail(reason){
    if(gen !== RTCP.gen) return;
    console.warn('webrtc', reason || 'failed');
    rtcFallback(gen, name);
  }
  RTCP.timer = setTimeout(function(){ if(gen === RTCP.gen && !RTCP.got) fail('timeout'); }, 12000);
  function reveal(){
    if(gen !== RTCP.gen) return;
    RTCP.got = true; RTCP.retries = 0;
    if(RTCP.timer){ clearTimeout(RTCP.timer); RTCP.timer = null; }
    v.style.opacity = '1';
    img.style.opacity = '0';
    var vm = el('vmode'); if(vm){ vm.textContent = 'WebRTC'; vm.className = 'b ok'; }
  }
  v.onplaying = reveal;
  v.onloadeddata = reveal;
  v.onended = function(){ if(gen === RTCP.gen) fail('video ended'); };
  if(RTCP.watch){ clearInterval(RTCP.watch); }
  RTCP.lastT = -1; RTCP.stall = 0;
  RTCP.watch = setInterval(function(){
    if(gen !== RTCP.gen || !RTCP.got) return;
    if(document.visibilityState !== 'visible'){ RTCP.lastT = -1; RTCP.stall = 0; return; }
    var t = v.currentTime;
    if(!isFinite(t)) return;
    if(v.paused){ try{ v.play().catch(function(){}); }catch(e){} RTCP.lastT = -1; RTCP.stall = 0; return; }
    if(RTCP.lastT < 0){ RTCP.lastT = t; RTCP.stall = 0; return; }
    if(t > RTCP.lastT + 0.001){ RTCP.lastT = t; RTCP.stall = 0; return; }
    RTCP.stall++;
    if(RTCP.stall >= 4) fail('video stalled');
  }, 1000);
  post('/api/rtc/start', {name:name}).then(function(j){
    if(gen !== RTCP.gen) return;
    if(!j || j.error) throw new Error((j && j.error) || 'rtc start failed');
    // 观看端心跳：服务端 45s 收不到就停编码器（关标签页不再泄漏 screenrecord）
    if(RTCP.ping){ clearInterval(RTCP.ping); }
    RTCP.ping = setInterval(function(){
      if(gen !== RTCP.gen){ clearInterval(RTCP.ping); RTCP.ping = null; return; }
      post('/api/rtc/ping', {name:name}).catch(function(){});
    }, 15000);
    var pc = new RTCPeerConnection({iceServers:[]});
    RTCP.pc = pc;
    pc.addTransceiver('video', {direction:'recvonly'});
    pc.ontrack = function(ev){
      if(gen !== RTCP.gen) return;
      var stream = (ev.streams && ev.streams[0]) || new MediaStream([ev.track]);
      try{ ev.track.onended = function(){ if(gen === RTCP.gen) fail('track ended'); }; }catch(e){}
      try{ v.srcObject = stream; }catch(e){}
      try{ v.playoutDelayHint = 0; }catch(e){}
      try{ v.play().catch(function(){}); }catch(e){}
    };
    pc.onconnectionstatechange = function(){
      if(gen !== RTCP.gen) return;
      if(pc.connectionState === 'failed' || pc.connectionState === 'closed'){ fail('pc ' + pc.connectionState); return; }
      if(pc.connectionState === 'disconnected'){
        setTimeout(function(){
          if(gen === RTCP.gen && pc.connectionState === 'disconnected') fail('pc disconnected');
        }, 2000);
      }
    };
    pc.createOffer().then(function(off){
      return pc.setLocalDescription(off);
    }).then(function(){
      return new Promise(function(res){
        if(pc.iceGatheringState === 'complete') return res();
        var done = false;
        var t = setTimeout(function(){ if(!done){ done = true; res(); } }, 1500);
        pc.addEventListener('icegatheringstatechange', function h(){
          if(pc.iceGatheringState === 'complete' && !done){ done = true; clearTimeout(t); res(); }
        });
      });
    }).then(function(){
      function whep(attempt){
        return fetch('/whep/' + encodeURIComponent(name), {
          method:'POST', cache:'no-store',
          headers:{'Content-Type':'application/sdp'},
          body:pc.localDescription.sdp
        }).then(function(r){
          if(r.status === 404 && attempt < 3){
            return new Promise(function(res){ setTimeout(res, 500); }).then(function(){ return whep(attempt + 1); });
          }
          if(!r.ok) throw new Error('WHEP ' + r.status);
          return r.text();
        });
      }
      return whep(1);
    }).then(function(sdp){
      if(gen !== RTCP.gen) return;
      return pc.setRemoteDescription({type:'answer', sdp:sdp}).then(function(){
        try{
          pc.getReceivers().forEach(function(r){
            try{ r.jitterBufferTarget = 0; }catch(e){}
            try{ r.playoutDelayHint = 0; }catch(e){}
          });
        }catch(e){}
        try{ v.playoutDelayHint = 0; }catch(e){}
      });
    }).catch(function(e){ fail(e && e.message); });
  }).catch(function(e){ fail(e && e.message); });
  return true;
}
function select(n){
  try{ rtcStop(); }catch(e){ console.warn('rtc stop', e); }
  try{ mseStop(); }catch(e){ console.warn('mse stop', e); }
  MSEP.retry = 0;
  try{ h264Stop(); }catch(e){ console.warn('h264 stop', e); }
  S.sel = n; S.stageName = null;
  RTCP.retries = 0;
  try{ inputWS(); }catch(e){}            // 提前建好输入 WebSocket，第一次触摸就不用等握手
  post('/api/input/warm',{name:n});       // 提前拉起 inputd，避免第一次按键等 app_process 启动
  renderList(); renderHead(); renderMeta();
  var img = el('screen');
  img.onerror = null; img.removeAttribute('data-mode');
  var started = false;
  try{ started = rtcStart(n); }catch(e){ console.warn('rtc start', e); started = false; }
  if(!started){
    try{ started = mseStart(n); }catch(e){ console.warn('mse start', e); started = false; }
  }
  if(!started){
    try{ started = h264Start(n); }catch(e){ console.warn('h264 start', e); started = false; }
  }
  if(!started) shot();
}
function loadingCard(){
  var e=el('empty'); if(!e) return;
  e.innerHTML='<div class="emptyicon"><svg viewBox="0 0 24 24"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M10.5 18.5h3"/></svg></div><h2>正在连接控制台</h2><p>设备数据加载中，马上就好。</p>';
}
function snap(){
  return fetch('/api/snapshot',{cache:'no-store'}).then(function(r){
    if(!r.ok) throw new Error('HTTP '+r.status);
    return r.json();
  }).then(function(j){
    S.rows=j.rows||[]; S.board=j.board||{}; S.build=j.build||'';
    renderList(); renderStats();
    if(!S.rows.length){
      if(!S.sel) loadingCard();
      clearTimeout(S.snapT); S.snapT=setTimeout(snap,1500);
      return;
    }
    if(S.snapT){clearTimeout(S.snapT);S.snapT=null}
    if(!S.sel && S.rows.length){
      var run0=S.rows.filter(function(r){return r.state==='running'});
      var r0=run0.filter(function(r){return r.name.indexOf('wx')===0})[0]||run0[0];
      if(r0) select(r0.name);
    }
    if(S.sel){renderHead();renderMeta()}
  }).catch(function(e){
    var d=el('jserr');
    if(!d){ d=document.createElement('div'); d.id='jserr'; d.style.cssText='position:fixed;left:8px;top:8px;z-index:9999;max-width:70vw;padding:8px 10px;background:#5a1d1d;color:#ffd7d7;border:1px solid #a33;border-radius:8px;font:12px/1.4 monospace;white-space:pre-wrap'; document.body.appendChild(d); }
    d.textContent='SNAP '+((e&&e.message)||e);
    if(!S.sel) loadingCard();
    clearTimeout(S.snapT); S.snapT=setTimeout(snap,1500);
  });
}
el('grp-run').addEventListener('click', pick);
el('grp-off').addEventListener('click', pick);
function pick(e){
  var c=e.target.closest('.card'); if(!c) return; select(c.getAttribute('data-n'));
}
el('q').addEventListener('input', function(e){S.filter=e.target.value;renderList()});
el('reload').addEventListener('click', function(){snap();toast('已刷新')});
el('logout').addEventListener('click', function(){document.cookie='g98k=; Path=/; Max-Age=0';location.href='/login'});
function bindSurface(id){
  var img = el(id); if(!img) return;
  img.draggable = false;
  img.addEventListener('dragstart', function(e){ e.preventDefault(); });
  var d = null;
  function norm(e){
    var rect = img.getBoundingClientRect();
    return {fx:(e.clientX-rect.left)/rect.width, fy:(e.clientY-rect.top)/rect.height};
  }
  function tch(a, p){ return sendInput(a, p); }
  img.addEventListener('pointerdown', function(e){
    if(!S.sel) return;
    if(e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    var p = norm(e);
    d = {id:e.pointerId, x:e.clientX, y:e.clientY, t:Date.now(), fx:p.fx, fy:p.fy,
         moved:false, sent:false, last:0, held:false, downOk:null};
    try { img.setPointerCapture(e.pointerId); } catch(err){}
    img.classList.add('dragging');
    // 长按：按住 380ms 不动就先发 touch-down，抬手再发 touch-up；
    // 这样长按菜单/拖动排序等手势能拿到真实按住时长（不再被当成一次 tap）。
    d.holdT = setTimeout(function(){
      if(!d || d.moved || d.sent) return;
      d.held = true;                       // 只是 UI 提示 + 抬手时走长按接口
      img.classList.add('holding');
    }, 380);
  });
  img.addEventListener('pointermove', function(e){
    if(!d) return;
    e.preventDefault();
    var now = Date.now();
    if(now - d.last < 12) return;            // 走 WS 通道，约 80 Hz 上限，够跟手
    d.last = now;
    var p = norm(e);
    if(Math.abs(p.fx-d.fx) <= 0.004 && Math.abs(p.fy-d.fy) <= 0.004) return;
    d.moved = true; d.held = false; clearTimeout(d.holdT); img.classList.remove('holding');
    if(!d.sent){ d.sent = true; tch('d', p); }
    else tch('m', p);
  });
  function end(e){
    if(!d) return;
    e.preventDefault();
    var p = norm(e), st = d, dt = Date.now() - st.t;
    d = null; clearTimeout(st.holdT); img.classList.remove('dragging'); img.classList.remove('holding');
    try { img.releasePointerCapture(e.pointerId); } catch(err){}
    if(st.held && !st.moved){
      // 长按走 /api/longpress（容器内 input swipe 原地按住），实测能触发
      // 桌面壁纸菜单、微信长按菜单等；daemon 的孤立 D/U 组合不被系统认作按住。
      post('/api/longpress',{name:S.sel,fx:st.fx,fy:st.fy,dur:Math.max(450, Math.round(dt))})
        .then(function(j){ if(!(j && j.ok)) toast('长按失败：'+((j&&j.error)||'未知错误')); });
      if(img.getAttribute('data-mode') !== 'stream') setTimeout(shot, 300);
      return;
    }
    if(st.moved){
      tch('u', p).then(function(j){
        if(j && j.ok === false){              // 守护进程不可用 → 回退 input swipe
          var dur = Math.max(120, Math.min(900, Math.round(dt || 260)));
          post('/api/swipe',{name:S.sel,fx1:st.fx,fy1:st.fy,fx2:p.fx,fy2:p.fy,dur:dur});
          toast('触摸守护进程未就绪，已回退到 input 命令');
        }
      });
    } else {
      tch('t', p).then(function(j){
        if(j && j.ok === false) post('/api/tap',{name:S.sel,fx:p.fx,fy:p.fy});
      });
    }
    if(img.getAttribute('data-mode') !== 'stream') setTimeout(shot, 300);
  }
  img.addEventListener('pointerup', end);
  img.addEventListener('pointercancel', function(){
    if(!d) return;
    var st = d; d = null; clearTimeout(st.holdT);
    img.classList.remove('dragging'); img.classList.remove('holding');
    if(st.sent) tch('u', {fx:st.fx, fy:st.fy});
  });
  img.addEventListener('contextmenu', function(e){ e.preventDefault(); });
}
bindSurface('scrinner');
bindSurface('liveimg');
document.querySelectorAll('.kbtn').forEach(function(b){
  b.addEventListener('click', function(){
    if(!S.sel) return toast('先选一台实例');
    sendKey(parseInt(b.getAttribute('data-key'),10));
    setTimeout(shot, 400);
  });
});
function sendTextValue(v){
  if(!S.sel) return Promise.resolve(toast('先选一台实例'));
  v=String(v||''); if(!v) return Promise.resolve();
  return post('/api/text',{name:S.sel,text:v}).then(function(j){
    if(!(j&&j.ok)) toast((j&&j.error)||'输入失败');
    setTimeout(shot, 600);
    return j;
  });
}
function sendText(){
  if(!S.sel) return toast('先选一台实例');
  var v=el('txt').value; if(!v) return;
  sendTextValue(v);
  el('txt').value='';
}
el('send').addEventListener('click', sendText);
el('txt').addEventListener('keydown', function(e){ if(e.key==='Enter') sendText(); });
document.querySelectorAll('[data-act]').forEach(function(b){
  b.addEventListener('click', function(){
    if(!S.sel) return toast('先选一台实例');
    var a=b.getAttribute('data-act');
    toast('正在 '+a+'…');
    post('/api/action',{name:S.sel,action:a}).then(function(){setTimeout(snap,2500)});
  });
});
el('wechat').addEventListener('click', function(){
  if(!S.sel) return toast('先选一台实例');
  post('/api/app',{name:S.sel}).then(function(){setTimeout(shot,1600)});
});
el('zoom').addEventListener('click', function(){
  var on=document.body.classList.toggle('zoomwrap');
  el('zoom').textContent=on?'缩小':'放大';
  if(!on) shot();
});
var LOCM={manual:false,source:'',ts:0};
function locBadge(){
  var b=el('locbadge'); if(!b) return;
  if(LOCM.manual){ b.className='locbadge warn'; b.textContent='人为设定 · '+(LOCM.source||'手动')+(LOCM.ts?' · '+new Date(LOCM.ts*1000).toLocaleString():''); }
  else { b.className='locbadge'; b.textContent='板子默认（出口网线）'; }
}
function locBoard(){
  fetch('/api/location/board',{cache:'no-store'}).then(function(r){return r.json()}).then(function(j){
    if(j && j.ok){ el('loclat').value=Number(j.lat).toFixed(6); el('loclon').value=Number(j.lon).toFixed(6); el('localt').value=0;
      locLog('板子默认：'+(j.city||'')+' '+Number(j.lat).toFixed(4)+', '+Number(j.lon).toFixed(4)+'（'+(j.iface||'')+'）；点「应用定位」才写入'); toast('已填入板子位置，点应用才生效'); }
    else locLog('读取板子位置失败：'+((j&&j.error)||'未知'));
  }).catch(function(){ locLog('读取板子位置失败'); });
}
function locLog(m){ el('loclog').textContent = m; }
function locLoad(){
  if(!S.sel) return locLog('先选一台实例');
  fetch('/api/location?name='+encodeURIComponent(S.sel)).then(function(r){return r.json()}).then(function(j){
    if(j && j.ok){ el('loclat').value=j.lat; el('loclon').value=j.lon; el('localt').value=j.alt; LOCM.manual=!!j.manual; LOCM.source=j.source||''; LOCM.ts=j.ts||0; locBadge(); locLog((j.manual?'当前（人为设定）：':'当前（板子默认）：')+j.lat+', '+j.lon+'（海拔 '+j.alt+'m）'); }
    else locLog('读取失败：'+((j&&j.error)||'未知错误'));
  }).catch(function(){ locLog('读取失败：控制台不可达'); });
}
function locApply(lat,lon,alt){
  if(!S.sel) return toast('先选一台实例');
  if(lat===undefined){ lat=parseFloat(el('loclat').value); lon=parseFloat(el('loclon').value); alt=parseFloat(el('localt').value); }
  if(!isFinite(lat)||!isFinite(lon)||lat<-90||lat>90||lon<-180||lon>180){ locLog('坐标格式错误'); return toast('坐标超出范围'); }
  if(!isFinite(alt)) alt=0;
  locLog('正在写入 '+lat+', '+lon+'…');
  post('/api/location/manual',{name:S.sel,lat:lat,lon:lon,alt:alt,source:'manual'}).then(function(j){
    if(j && j.ok){ el('loclat').value=j.lat; el('loclon').value=j.lon; el('localt').value=j.alt; LOCM.manual=true; LOCM.source='manual'; LOCM.ts=Math.round(Date.now()/1000); locBadge(); locLog('已应用（人为设定）：'+j.lat+', '+j.lon+'（海拔 '+j.alt+'m）'); toast('定位已切换到 '+j.lat+', '+j.lon); }
    else locLog('写入失败：'+((j&&j.error)||'未知错误'));
  });
}
function setTab(name){
  var tabs = document.querySelectorAll('#itabs .itab');
  for(var i=0;i<tabs.length;i++) tabs[i].classList.toggle('on', tabs[i].getAttribute('data-itab')===name);
  var panes = document.querySelectorAll('.ipane');
  for(var j=0;j<panes.length;j++) panes[j].classList.toggle('hide', panes[j].id !== 'pane-' + name);
}
function openLoc(){ setTab('loc'); locLoad(); }
function closeLoc(){ setTab('info'); }
el('locopen').addEventListener('click', openLoc);
var itabbar = el('itabs');
if(itabbar) itabbar.addEventListener('click', function(e){
  var b = e.target && e.target.closest ? e.target.closest('.itab') : null;
  if(b && b.getAttribute('data-itab')==='loc') openLoc();
  else if(b && b.getAttribute('data-itab')==='cam') openCam();
  else if(b) closeLoc();
});
el('locapply').addEventListener('click', function(){ locApply(); });
el('locrefresh').addEventListener('click', locLoad);
el('locboard').addEventListener('click', locBoard);
document.querySelectorAll('.loccity').forEach(function(b){
  b.addEventListener('click', function(){ locApply(parseFloat(b.getAttribute('data-lat')),parseFloat(b.getAttribute('data-lon')),parseFloat(b.getAttribute('data-alt'))); });
});
document.addEventListener('keydown', function(e){
  if(e.key === 'Escape') closeLoc();
});
snap().then(function(){
  var run=S.rows.filter(function(r){return r.state==='running'});
  var want = null;
  try { want = new URLSearchParams(location.search).get('device'); } catch(e) {}
  if(want) S.rows.forEach(function(r){ if(r.name === want) select(r.name); });
  if(!S.sel){
    var r0=run.filter(function(r){return r.name.indexOf('wx')===0})[0]||run[0];
    if(r0) select(r0.name);
  }
  var live = null;
  try { live = new URLSearchParams(location.search).get('live'); } catch(e) {}
  if(live === '1') setTimeout(openLive, 600);
});
setInterval(snap, 8000);

var liveWS = null, liveURL = null, liveFrames = 0, liveFpsT = null,
    liveStatsT = null, liveMode = 'stream';
function wsBase(){ return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host; }
function openLive(){
  el('livewho').textContent = S.sel ? ('当前选中 ' + S.sel) : '未选中实例';
  el('livewrap').classList.remove('hide');
  startLive();
}
function startLive(){
  stopLive();
  if(!S.sel) return;
  var img = el('liveimg');
  liveMode = 'stream';
  el('livefps').textContent = '连接中…';
  img.onerror = function(){
    if(liveMode !== 'stream') return;
    liveMode = 'ws';
    startLiveWS();
  };
  img.src = '/stream/' + encodeURIComponent(S.sel) + '?t=' + Date.now();
  liveStatsT = setInterval(function(){
    if(liveMode !== 'stream' || !S.sel) return;
    fetch('/api/stream?name=' + encodeURIComponent(S.sel), {cache:'no-store'})
      .then(function(r){ return r.json(); })
      .then(function(j){
        if(liveMode !== 'stream') return;
        if(j && j.ok) el('livefps').textContent = ((j.fps||0) < 5)
            ? '静止画面 · 无变化（动起来可达 30 fps）'
            : ((j.fps||0) + ' fps · MJPEG');
        else el('livefps').textContent = '连接中…';
      }).catch(function(){});
  }, 2000);
}
function startLiveWS(){
  if(!S.sel) return;
  liveFrames = 0;
  if(liveFpsT) clearInterval(liveFpsT);
  liveFpsT = setInterval(function(){ el('livefps').textContent = liveFrames + ' fps'; liveFrames = 0; }, 1000);
  try { liveWS = new WebSocket(wsBase() + '/ws/live?device=' + encodeURIComponent(S.sel)); }
  catch(e){ el('livefps').textContent = '连接失败'; return; }
  liveWS.binaryType = 'arraybuffer';
  liveWS.onmessage = function(ev){
    if(typeof ev.data === 'string') return;
    var buf = new Uint8Array(ev.data);
    if(buf.length < 9 || buf[0] !== 0x47 || buf[1] !== 0x39 || buf[2] !== 0x38) return;
    var blob = new Blob([buf.subarray(8)], {type:'image/png'});
    var url = URL.createObjectURL(blob);
    var img = el('liveimg');
    var old = liveURL;
    liveURL = url;
    img.src = url;
    liveFrames++;
    if(old) setTimeout(function(){ URL.revokeObjectURL(old); }, 2000);
  };
  liveWS.onclose = function(){ if(liveFpsT) clearInterval(liveFpsT); el('livefps').textContent = '已断开'; };
}
function stopLive(){
  if(liveFpsT){ clearInterval(liveFpsT); liveFpsT = null; }
  if(liveStatsT){ clearInterval(liveStatsT); liveStatsT = null; }
  if(liveWS){ try{ liveWS.close(); }catch(e){} liveWS = null; }
  var img = el('liveimg'); if(img) img.onerror = null;
  liveMode = 'stream';
}
function closeLive(){
  stopLive();
  el('livewrap').classList.add('hide');
}
el('live').addEventListener('click', openLive);
el('liveclose').addEventListener('click', closeLive);
function camNodesFor(dev){
  var m = /^wx(\d+)$/.exec(dev || ''); var n = m ? parseInt(m[1], 10) : 1;
  var b = 20 + 2 * (n - 1);
  return { back: '/dev/video' + b, front: '/dev/video' + (b + 1) };
}
function camFillNodes(){
  var s = el('camnode'); if(!s || !S.sel) return;
  var cur = s.value, n = camNodesFor(S.sel);
  s.innerHTML = '<option value="">自动（按当前相机节点）</option>' +
    '<option value="' + n.back + '">后置 · ' + n.back + '</option>' +
    '<option value="' + n.front + '">前置 · ' + n.front + '</option>';
  var keep = false;
  for(var i = 0; i < s.options.length; i++){ if(s.options[i].value === cur){ keep = true; break; } }
  s.value = keep ? cur : n.back;
}
function openCam(){ camFillNodes(); setTab('cam'); setTimeout(camLibLoad, 150); }
function closeCam(){ setTab('info'); }
el('camopen').addEventListener('click', openCam);
// desktop camera controls now live in the inspector "相机" tab
function openQR(){
  if(!S.sel) return toast('先选一台实例');
  var node = el('camnode').value || '/dev/video10';
  el('qrmodal').classList.remove('hide');
  el('qrimg').removeAttribute('src');
  el('qrlog').textContent = '正在生成二维码…';
  fetch('/api/cam/qr?name=' + encodeURIComponent(S.sel) + '&node=' + encodeURIComponent(node))
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(!j || j.error){ el('qrlog').textContent = (j && j.error) || '生成失败'; return; }
      el('qrimg').src = j.png;
      el('qrlog').textContent = 'https://' + j.host + ':' + j.port + '/m · 手机全功能操控';
    })
    .catch(function(e){ el('qrlog').textContent = '请求失败：' + e; });
}
function closeQR(){ el('qrmodal').classList.add('hide'); }
el('camqrbtn').addEventListener('click', function(){ closeCam(); openQR(); });
el('qrclose').addEventListener('click', closeQR);
el('qrmodal').addEventListener('click', function(e){ if(e.target === this) closeQR(); });
document.addEventListener('keydown', function(e){
  if(e.key === 'Escape') closeCam();
  if(e.key === 'Escape' && !el('livewrap').classList.contains('hide')) closeLive();
});

// 远程画面操作时，直接敲物理键盘也能输入 ASCII。
// 中文仍走底部输入框，因为浏览器在不可编辑区域拿不到 IME 组合串。
var KBB = {buf:'', timer:null};
function kbEditable(t){
  var tag=((t&&t.tagName)||'').toLowerCase();
  return tag==='input' || tag==='textarea' || tag==='select' || !!(t&&t.isContentEditable);
}
function kbFlush(){
  if(KBB.timer){ clearTimeout(KBB.timer); KBB.timer=null; }
  var s=KBB.buf; KBB.buf='';
  if(s) sendTextValue(s);
}
function kbAdd(s){
  KBB.buf += s;
  if(KBB.timer) clearTimeout(KBB.timer);
  KBB.timer = setTimeout(kbFlush, 180);
}
document.addEventListener('keydown', function(e){
  if(!S.sel || kbEditable(e.target) || e.ctrlKey || e.altKey || e.metaKey) return;
  if(el('pane-cam') && !el('pane-cam').classList.contains('hide')) return;
  if(!el('livewrap').classList.contains('hide')) return;
  if(e.isComposing) return;
  if(e.key==='Enter'){
    e.preventDefault();
    if(KBB.timer){ clearTimeout(KBB.timer); KBB.timer=null; }
    var s=KBB.buf; KBB.buf='';
    var p = s ? sendTextValue(s) : Promise.resolve();
    p.then(function(){ post('/api/key',{name:S.sel,code:66}); });
    return;
  }
  if(e.key==='Backspace'){ e.preventDefault(); kbFlush(); post('/api/key',{name:S.sel,code:67}); return; }
  if(e.key==='Tab'){ e.preventDefault(); post('/api/key',{name:S.sel,code:61}); return; }
  if(e.key==='Escape'){ e.preventDefault(); post('/api/key',{name:S.sel,code:111}); return; }
  if(e.key.length===1){ e.preventDefault(); kbAdd(e.key); }
});

var camWS = null, camVideo = null, camImg = null, camKind = 'video', camReady = false,
    camURL = null, camCanvas = null, camTimer = null, camW = 640, camH = 480,
    camScreenStream = null;
function camLog(m){ el('camlog').textContent = m; }
function camStartWS(){
  if(!S.sel){ camLog('先选一台实例'); return; }
  var f = el('camfile').files[0];
  if(!f){ camLog('先选一个视频或图片文件'); return; }
  camStop(true);
  camKind = (f.type || '').indexOf('image/') === 0 ? 'image' : 'video';
  camURL = URL.createObjectURL(f);
  camCanvas = document.createElement('canvas');
  camCanvas.width = camW; camCanvas.height = camH;
  camImg = null;
  if(camKind === 'video'){
    camVideo = document.createElement('video');
    camVideo.muted = true; camVideo.loop = true; camVideo.playsInline = true;
    camVideo.src = camURL;
    camVideo.onerror = function(){ camLog('视频解码失败'); };
  } else {
    camVideo = null;
    camImg = new Image();
    camImg.onerror = function(){ camLog('图片解码失败'); };
    camImg.src = camURL;
  }
  camReady = false;
  try { camWS = new WebSocket(wsBase() + '/ws/cam?device=' + encodeURIComponent(S.sel)); }
  catch(e){ camLog('连接失败'); return; }
  camWS.binaryType = 'arraybuffer';
  camWS.onopen = function(){
    camWS.send(JSON.stringify({type:'camera_config', node: el('camnode').value, width: camW, height: camH}));
    camLog('已连接，等待相机就绪…');
  };
  camWS.onmessage = function(ev){
    if(typeof ev.data !== 'string') return;
    var m = {}; try { m = JSON.parse(ev.data); } catch(e) { return; }
    if(m.type === 'camera_ready'){
      camReady = true;
      camLog('已注入 ' + m.node + ' ' + m.width + 'x' + m.height + '，开始推帧（' +
             (camKind === 'image' ? '静态图片循环' : '视频') + '）');
      if(camKind === 'video'){
        camVideo.play().then(camPump).catch(function(err){ camLog('视频播放失败：' + err); });
      } else {
        camPump();
      }
    } else if(m.type === 'camera_error'){
      camLog('相机错误：' + (m.message || ''));
    }
  };
  camWS.onclose = function(){ camLog('相机连接已关闭'); };
}
function camFitOpts(){
  var fitEl=el('camfit'), rotEl=el('camrot'), bothEl=el('camboth');
  var o={fit: fitEl?fitEl.value:'contain'};
  if(rotEl) o.rotate=parseInt(rotEl.value,10)||0;
  o.both = bothEl ? bothEl.value !== '0' : true;
  return o;
}
function camStartPost(body){
  return post('/api/cam/start', Object.assign({}, body, camFitOpts()));
}
function camGeomShow(node){
  if(!S.sel) return;
  post('/api/cam/geom', {name:S.sel, node:node||''}).then(function(j){
    var g=j&&j.geometry;
    if(!g){ camLog('几何信息不可用（camd 未接管该节点）'); return; }
    camLog('几何：源 '+g.src_w+'×'+g.src_h+' → 节点 '+g.out_w+'×'+g.out_h+
           ' · 内容 '+g.content_w+'×'+g.content_h+' @('+g.content_x+','+g.content_y+
           ') · 显示比 '+g.display_dar+'（源 '+g.src_dar+'，补偿 ×'+g.ystretch+'）');
  }).catch(function(){});
}
function camStartFile(){
  if(!S.sel){ camLog('先选一台实例'); return; }
  var f = el('camfile').files[0];
  if(!f){ camLog('先选一个视频或图片文件'); return; }
  var kind = ((f.type || '').indexOf('image/') === 0 || /\\.(png|jpe?g|webp|bmp|gif)$/i.test(f.name || '')) ? 'image' : 'video';
  var node = el('camnode').value;
  camLog('上传到板子… ' + Math.round(f.size / 1024) + ' KB');
  fetch('/api/cam/file', {method: 'POST',
      headers: {'Content-Type': 'application/octet-stream', 'X-Filename': f.name || 'upload.bin'},
      body: f})
   .then(function(r){ return r.json(); })
   .then(function(j){
     if(!j || j.error){ camLog('上传失败：' + ((j && j.error) || '未知')); return null; }
     camLog('已存 ' + j.path + '（' + j.bytes + ' B），板子侧起 ffmpeg…');
     setTimeout(camLibLoad, 400);
     return camStartPost({name: S.sel, node: node, path: j.path, kind: kind});
   })
   .then(function(j){
     if(!j) return;
     if(j.error){ camLog('启动失败：' + j.error + ' ' + (j.log || '')); return; }
     camLog('已提交注入 ' + (j.node || node) + ' · ' + kind + '，等待云手机相机…');
     camTrack(S.sel, j.node || node);
   })
   .catch(function(e){ camLog('出错：' + e); });
}
function camLibSize(n){ return n >= 1048576 ? (n/1048576).toFixed(1)+' MB' : Math.round(n/1024)+' KB'; }
function camLibInject(path, kind){
  if(!S.sel){ camLog('先选一台实例'); return; }
  var node = el('camnode').value;
  camLog('热切注入 ' + path.split('/').pop() + ' …');
  camStartPost({name: S.sel, node: node, path: path, kind: kind})
   .then(function(j){
     if(!j || j.error){ camLog('注入失败：' + ((j && j.error) || '未知')); return; }
     camLog('已提交 ' + (j.node || node) + ' · ' + kind + '，等待云手机相机…');
     camTrack(S.sel, j.node || node);
   }).catch(function(e){ camLog('出错：' + e); });
}
function camLibLoad(){
  var box = el('camlib');
  if(!box) return;
  fetch('/api/cam/files', {cache: 'no-store'})
   .then(function(r){ return r.json(); })
   .then(function(j){
     var files = (j && j.files) || [];
     box.textContent = '';
     if(!files.length){
       var e0 = document.createElement('div'); e0.className = 'mini';
       e0.textContent = '素材库为空：用上面的“选择图片 / 视频”上传，之后可一键热切。';
       box.appendChild(e0); return;
     }
     files.forEach(function(f){
       var row = document.createElement('div');
       row.className = 'camlib-item';
       var nm = document.createElement('b'); nm.textContent = f.name;
       var meta = document.createElement('span');
       meta.textContent = (f.kind === 'video' ? '视频 · ' : '图片 · ') + camLibSize(f.size);
       row.appendChild(nm); row.appendChild(meta);
       row.addEventListener('click', function(){ camLibInject(f.path, f.kind); });
       box.appendChild(row);
     });
   }).catch(function(){ box.textContent = '素材库读取失败'; });
}
function camTrack(name, node){
  var n = 0;
  (function tick(){
    if(++n > 90){ camLog('等待云手机相机超时'); return; }
    fetch('/api/cam/status?name=' + encodeURIComponent(name), {cache: 'no-store'})
     .then(function(r){ return r.json(); })
     .then(function(j){
        var st = (j && j.state) || {};
        if(st.state === 'streaming'){
          camLog('注入完成 ' + (st.node || node) + ' · ' + (st.kind || '') + ' · ' + (st.size || '?') +
                 ' · ' + (st.duration || '?') + 's · devices=' + (st.devices || '?') +
                 ' · resumed=' + (st.resumed || '-'));
          return;
        }
        if(st.state === 'error'){ camLog('注入失败：' + (st.error || '未知')); return; }
        setTimeout(tick, 400);
     })
     .catch(function(){ setTimeout(tick, 700); });
  })();
}
function camStartURL(){
  if(!S.sel){ camLog('先选一台实例'); return; }
  var url = (el('camurl').value || '').trim();
  if(!new RegExp('^(rtsp|rtsps|http|https)://', 'i').test(url)){ camLog('填 rtsp:// 或 http(s):// 地址'); return; }
  var node = el('camnode').value;
  camLog('连接外部流… ' + url);
  camStartPost({name:S.sel, node:node, path:url, kind:'stream'})
   .then(function(j){
     if(!j || j.error){ camLog('接入失败：' + ((j && j.error) || '未知') + ' ' + ((j && j.log) || '')); return; }
     camLog('已提交接入 ' + (j.node || node) + '，等待云手机相机…');
     camTrack(S.sel, j.node || node);
   })
   .catch(function(e){ camLog('出错：' + e); });
}
function camStopScreen(){
  if(camScreenStream){
    var s = camScreenStream; camScreenStream = null;
    try{ s.getTracks().forEach(function(t){ t.onended = null; t.stop(); }); }catch(e){}
  }
}
function camPumpScreen(){
  if(camTimer) return;
  var ctx = camCanvas.getContext('2d');
  function step(){
    if(!camWS || camWS.readyState !== 1){ camTimer = null; return; }
    if(camVideo && camVideo.readyState >= 2){
      try { ctx.drawImage(camVideo, 0, 0, camW, camH); } catch(e) {}
      var img = ctx.getImageData(0, 0, camW, camH);
      var data = img.data, n = data.length;
      var buf = new Uint8Array(8 + n);
      buf[0] = 0x47; buf[1] = 0x39; buf[2] = 0x38; buf[3] = 0x46;
      buf[4] = (camW >> 8) & 255; buf[5] = camW & 255;
      buf[6] = (camH >> 8) & 255; buf[7] = camH & 255;
      buf.set(data, 8);
      try { camWS.send(buf.buffer); } catch(e) {}
    }
    camTimer = setTimeout(step, 100);
  }
  step();
}
function camOpenScreenWS(){
  camWS = new WebSocket(wsBase() + '/ws/cam?device=' + encodeURIComponent(S.sel));
  camWS.binaryType = 'arraybuffer';
  camWS.onopen = function(){
    camWS.send(JSON.stringify({type:'camera_config', node: el('camnode').value,
                                width: camW, height: camH, codec:'rgba'}));
    camLog('已连接，等待相机就绪…');
  };
  camWS.onmessage = function(ev){
    if(typeof ev.data !== 'string') return;
    var m = {}; try { m = JSON.parse(ev.data); } catch(e) { return; }
    if(m.type === 'camera_ready'){
      camReady = true;
      camLog('正在共享浏览器画面 → ' + m.node + '（RGBA 10fps）');
      camPumpScreen();
    } else if(m.type === 'camera_error'){
      camLog('相机错误：' + (m.message || ''));
    }
  };
  camWS.onclose = function(){ camLog('屏幕共享连接已关闭'); };
}
function camStartScreen(){
  if(!S.sel){ camLog('先选一台实例'); return; }
  if(!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia){
    camLog('共享浏览器画面需要 HTTPS 打开本页（https://' + location.hostname + ':8443/），当前不是安全上下文');
    return;
  }
  camStop(true);
  navigator.mediaDevices.getDisplayMedia({video:{frameRate:10, width:640, height:480}, audio:false})
   .then(function(stream){
      camScreenStream = stream;
      camKind = 'screen';
      camW = 640; camH = 480;
      camCanvas = document.createElement('canvas');
      camCanvas.width = camW; camCanvas.height = camH;
      camVideo = document.createElement('video');
      camVideo.muted = true; camVideo.playsInline = true; camVideo.autoplay = true;
      camVideo.style.display = 'none';
      camVideo.srcObject = stream;
      var track = stream.getVideoTracks()[0];
      if(track) track.onended = function(){ camLog('屏幕共享已停止'); camStop(false); };
      camVideo.play().then(camOpenScreenWS).catch(function(err){
        camLog('屏幕流播放失败：' + ((err && err.message) || err));
        camStop(true);
      });
   })
   .catch(function(err){
      if(err && err.name === 'NotAllowedError'){ camLog('已取消共享'); }
      else { camLog('屏幕共享失败：' + ((err && err.message) || err)); }
   });
}
function camPump(){
  if(camTimer) return;
  var ctx = camCanvas.getContext('2d');
  var period = camKind === 'image' ? 100 : 66;
  function ready(){
    if(camKind === 'image') return !!(camImg && camImg.complete && camImg.naturalWidth > 0);
    return !!(camVideo && camVideo.readyState >= 2);
  }
  function step(){
    if(!camWS || camWS.readyState !== 1){ camTimer = null; return; }
    if(ready()){
      var src = camKind === 'image' ? camImg : camVideo;
      try { ctx.drawImage(src, 0, 0, camCanvas.width, camCanvas.height); } catch(e) {}
      var data = ctx.getImageData(0, 0, camCanvas.width, camCanvas.height);
      var buf = new Uint8Array(8 + data.data.length);
      buf[0] = 0x47; buf[1] = 0x39; buf[2] = 0x38; buf[3] = 0x46;
      buf[4] = (camCanvas.width >> 8) & 255; buf[5] = camCanvas.width & 255;
      buf[6] = (camCanvas.height >> 8) & 255; buf[7] = camCanvas.height & 255;
      buf.set(data.data, 8);
      try { camWS.send(buf.buffer); } catch(e) {}
    }
    camTimer = setTimeout(step, period);
  }
  step();
}
function camStop(silent){
  camStopScreen();
  if(camTimer){ clearTimeout(camTimer); camTimer = null; }
  if(camVideo){ try{ camVideo.pause(); }catch(e){} camVideo.src = ''; camVideo = null; }
  camImg = null;
  camReady = false;
  if(camWS){ try{ camWS.close(); }catch(e){} camWS = null; }
  if(camURL){ try{ URL.revokeObjectURL(camURL); }catch(e){} camURL = null; }
  var node = el('camnode') ? el('camnode').value : '';
  if(S.sel){
    camLog('正在停止并恢复相机空闲源…');
    post('/api/cam/stop', {name: S.sel, node: node}).then(function(j){
      if(j && !j.error){ camLog('已停止 · 空闲源已恢复'); }
    });
  } else if(!silent){ camLog('已停止'); }
}
el('camfile').addEventListener('change', function(){
  var f = this.files[0];
  el('camfname').textContent = f ? (f.name + ' · ' + Math.round(f.size / 1024) + ' KB') : '未选择文件';
});
el('camstart').addEventListener('click', camStartFile);
el('camlib').addEventListener('click', function(){ if(!el('camlib').children.length) camLibLoad(); });
el('camstartscreen').addEventListener('click', camStartScreen);
el('camstarturl').addEventListener('click', camStartURL);
el('camstop').addEventListener('click', function(){ camStop(false); });

var DIAGMODE = /(?:[?&])diag=1(?:&|$)/.test(location.search);
(function(){
  if(!DIAGMODE) return;
  var box=el('diag'); if(!box) return;
  box.style.display='block';
  var dv=el('rtcvideo'), sc=el('scrinner');
  var cv=document.createElement('canvas'); cv.width=36; cv.height=80;
  var cx=cv.getContext('2d',{willReadFrequently:true});
  var prev=null,tapAt=0,lat=-1;
  function sample(){
    if(!dv || dv.readyState < 2 || !dv.videoWidth) return null;
    cx.drawImage(dv,0,0,cv.width,cv.height);
    return cx.getImageData(0,0,cv.width,cv.height).data;
  }
  function tick(){
    var now=performance.now(), px=sample();
    if(px){
      if(prev){
        var diff=0, n=cv.width*cv.height;
        for(var i=0;i<px.length;i+=4){
          var a=(px[i]+px[i+1]+px[i+2])/3, b=(prev[i]+prev[i+1]+prev[i+2])/3;
          if(Math.abs(a-b)>18) diff++;
        }
        if(tapAt && diff > n*0.06){ lat=Math.round(now-tapAt); tapAt=0; }
      }
      prev=px;
    }
    if(dv && dv.requestVideoFrameCallback) dv.requestVideoFrameCallback(tick);
    else setTimeout(tick,33);
  }
  if(sc) sc.addEventListener('pointerdown',function(){ tapAt=performance.now(); },true);
  if(dv && dv.requestVideoFrameCallback) dv.requestVideoFrameCallback(tick); else setTimeout(tick,200);
  setInterval(function(){
    var pc=RTCP.pc, v=el('rtcvideo');
    if(!pc){ box.textContent='diag: no peer connection'; return; }
    pc.getStats().then(function(ss){
      var inb=null,pair=null,tr=null,cands={};
      ss.forEach(function(s){
        if(s.type==='inbound-rtp' && s.kind==='video') inb=s;
        if(s.type==='transport') tr=s;
        if(s.type==='candidate-pair' && s.state==='succeeded') pair=s;
        if(s.type==='local-candidate'||s.type==='remote-candidate') cands[s.id]=s;
      });
      if(tr && tr.selectedCandidatePairId && ss.get(tr.selectedCandidatePairId)) pair=ss.get(tr.selectedCandidatePairId);
      var jb=(inb&&inb.jitterBufferEmittedCount)?(1000*inb.jitterBufferDelay/inb.jitterBufferEmittedCount):-1;
      var dec=(inb&&inb.framesDecoded)?(1000*inb.totalDecodeTime/inb.framesDecoded):-1;
      var lc=pair?cands[pair.localCandidateId]:null, rc=pair?cands[pair.remoteCandidateId]:null;
      var lines=[
        'conn='+pc.connectionState+' ice='+pc.iceConnectionState+' got='+RTCP.got,
        'cand='+(lc?lc.candidateType+'/'+lc.protocol:'?')+' -> '+(rc?rc.candidateType:'?')+' rtt='+(pair&&pair.currentRoundTripTime?Math.round(pair.currentRoundTripTime*1000):'-')+'ms',
        'jb='+(jb<0?'-':jb.toFixed(0))+'ms fps='+(inb&&inb.framesPerSecond||0)+' lost='+(inb&&inb.packetsLost||0)+' nack='+(inb&&inb.nackCount||0)+' freeze='+(inb&&inb.freezeCount||0),
        'dec='+(dec<0?'-':dec.toFixed(1))+'ms frames='+(inb&&inb.framesDecoded||0),
        'LAT='+(lat<0?'-':lat)+'ms tap='+(tapAt?Math.round(performance.now()-tapAt)+'ms':'-')+' '+v.videoWidth+'x'+v.videoHeight+' rs='+v.readyState
      ];
      box.textContent=lines.join(String.fromCharCode(10));
    }).catch(function(e){ box.textContent='stats: '+e.message; });
  },1000);
})();
</script></body></html>"""

CAMPAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>G98 手机摄像头</title>
<style>
:root{color-scheme:dark;--bg:#080d12;--card:#111a23;--line:#203040;--txt:#eaf2fa;--mut:#8195a8;--acc:#5b8cff;--ok:#36d399;--warn:#f5a524}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
body{min-height:100dvh;padding:16px 16px calc(16px + env(safe-area-inset-bottom))}
.top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}
.eyebrow{font-size:10px;letter-spacing:.2em;color:var(--acc);font-weight:700}.top h1{font-size:22px;margin:4px 0}.mini{font-size:12px;color:var(--mut);line-height:1.5}
#dot{width:10px;height:10px;border-radius:50%;background:#3a4654;margin-top:8px;flex:0 0 auto;box-shadow:0 0 0 4px rgba(58,70,84,.15)}
#dot.on{background:var(--ok);box-shadow:0 0 0 4px rgba(54,211,153,.16)}
.preview{position:relative;background:#000;border:1px solid var(--line);border-radius:16px;overflow:hidden;aspect-ratio:4/3}
#pv{width:100%;height:100%;object-fit:cover;display:block}
.placeholder{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:24px;color:#9fb0c0;font-size:13px;line-height:1.7;background:radial-gradient(circle at 50% 35%,#162534,#05080c 70%)}
.badge{position:absolute;left:10px;top:10px;padding:5px 9px;border-radius:999px;background:rgba(4,10,16,.72);border:1px solid rgba(255,255,255,.14);font-size:11px;color:#cdd9e5}
.row{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:8px;margin:12px 0}
button{appearance:none;border:1px solid var(--line);background:#17232e;color:var(--txt);border-radius:11px;padding:12px 8px;font-size:14px;font-weight:600}
button.primary{background:linear-gradient(180deg,#6d9bff,#4b7bf0);border-color:#6d9bff;color:#fff}
button:disabled{opacity:.42}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-top:10px}
.card label{display:block;font-size:11px;color:var(--mut);margin-bottom:6px}
select{width:100%;background:#0c151e;color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:11px;font-size:14px}
.kv{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:12px;color:var(--mut)}
.kv:last-child{border-bottom:0}.kv b{color:var(--txt);font-weight:600}
.log{margin-top:10px;font-size:12px;line-height:1.55;color:#a9bbcb;min-height:34px;word-break:break-word}
.hint{margin-top:12px;font-size:11px;line-height:1.65;color:#6f8396}
</style></head><body>
<div class="top"><div><div class="eyebrow">G98 · CAMERA BRIDGE</div><h1>手机摄像头</h1><div class="mini" id="sub">正在准备…</div></div><span id="dot"></span></div>
<div class="preview"><video id="pv" playsinline autoplay muted></video><div class="placeholder" id="ph">点击「开启摄像头」<br>把这部手机的画面送进云手机</div><div class="badge" id="state">未开启</div></div>
<div class="row"><button class="primary" id="start">开启摄像头</button><button id="stop" disabled>停止</button><button id="flip" disabled>切换</button></div>
<div class="card">
<label for="node">注入到云手机相机节点</label>
<select id="node"><option value="/dev/video10">后置摄像头（/dev/video10）</option><option value="/dev/video11">前置摄像头（/dev/video11）</option></select>
<div class="kv"><span>云手机实例</span><b id="dev">wx1</b></div>
<div class="kv"><span>画质</span><b>WebRTC · 自适应</b></div>
<div class="kv"><span>已发送</span><b id="frames">0 帧</b></div>
<div class="kv"><span>数据量</span><b id="bytes">0.0 MB</b></div>
</div>
<div class="log" id="log">等待开启摄像头…</div>
<div class="hint">手机和板子需要连同一个网络。首次使用请允许摄像头权限；如果浏览器提示证书不安全，选择「高级 → 继续访问」。推流成功后云手机会自动打开相机预览。</div>
<script>
(function(){
var q=new URLSearchParams(location.search);
var dev=q.get('device')||'wx1';
var node=q.get('node')||'/dev/video10';
var pv=document.getElementById('pv'), ph=document.getElementById('ph');
var canvas=document.createElement('canvas'); canvas.width=480; canvas.height=360;
var ctx=canvas.getContext('2d',{willReadFrequently:true});
var stream=null, ws=null, pc=null, videoSender=null, timer=null, whipTimer=null, whipStats=null, camStarted=false, frames=0, bytes=0, busy=false, facing='environment', closedByUs=false;
var reconnects=0, reconnectTimer=null, discTimer=null;
var startBtn=document.getElementById('start'), stopBtn=document.getElementById('stop'), flipBtn=document.getElementById('flip');
document.getElementById('dev').textContent=dev;
document.getElementById('node').value=node;
function log(m){ document.getElementById('log').textContent=m; }
function state(s,on){ document.getElementById('state').textContent=s; var d=document.getElementById('dot'); if(on)d.classList.add('on'); else d.classList.remove('on'); }
function stopPump(){ if(timer){ clearInterval(timer); timer=null; } }
function stopJpegEarly(){ try{ if(jpegTimer){ clearInterval(jpegTimer); jpegTimer=null; } jpegBusy=false; }catch(e){} }
function stopAll(silent){
  stopPump(); stopJpegEarly(); closedByUs=true;
  if(reconnectTimer){ clearTimeout(reconnectTimer); reconnectTimer=null; }
  if(discTimer){ clearTimeout(discTimer); discTimer=null; }
  reconnects=0;
  if(whipTimer){ clearTimeout(whipTimer); whipTimer=null; }
  if(whipStats){ clearInterval(whipStats); whipStats=null; }
  if(pc){ try{ pc.onconnectionstatechange=null; pc.close(); }catch(e){} pc=null; }
  var hadCam=camStarted; camStarted=false;
  if(ws){ try{ ws.onclose=null; ws.close(); }catch(e){} ws=null; }
  if(stream){ try{ stream.getTracks().forEach(function(t){t.stop();}); }catch(e){} stream=null; }
  try{ pv.srcObject=null; }catch(e){}
  ph.style.display='flex'; startBtn.disabled=false; stopBtn.disabled=true; flipBtn.disabled=true;
  state('已停止',false);
  if(!silent){
    log('已停止。正在恢复云手机相机空闲源…');
    try{ fetch('/api/cam/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:dev,node:node})}).catch(function(){}); }catch(e){}
  }else if(hadCam){
    try{ fetch('/api/cam/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:dev,node:node})}).catch(function(){}); }catch(e){}
  }
}
function connect(){
  if(ws){ try{ws.onclose=null; ws.close();}catch(e){} ws=null; }
  var proto=location.protocol==='https:'?'wss://':'ws://';
  try{ ws=new WebSocket(proto+location.host+'/ws/cam?device='+encodeURIComponent(dev)); }
  catch(e){ log('连接失败：'+e); state('连接失败',false); return; }
  ws.binaryType='arraybuffer';
  ws.onopen=function(){
    log('已连接板子，等待相机节点就绪…');
    ws.send(JSON.stringify({type:'camera_config',node:node,width:480,height:360,codec:'rgba'}));
  };
  ws.onmessage=function(ev){
    if(typeof ev.data!=='string') return;
    var m={}; try{ m=JSON.parse(ev.data); }catch(e){ return; }
    if(m.type==='camera_ready'){
      state('正在注入 '+m.node,true); log('推流中 → '+m.node+'（'+m.width+'×'+m.height+'）');
      startPump();
    }else if(m.type==='camera_error'){
      state('相机错误',false); log('相机错误：'+(m.message||''));
    }
  };
  ws.onerror=function(){ log('WebSocket 连接失败'); };
  ws.onclose=function(){ stopPump(); if(!closedByUs){ state('连接已关闭',false); } };
  closedByUs=false;
}
function startPump(){ stopPump(); timer=setInterval(pump,100); }
function pump(){
  if(busy||!ws||ws.readyState!==1||!pv.videoWidth) return;
  busy=true;
  try{
    var sw=pv.videoWidth, sh=pv.videoHeight, sc=Math.max(480/sw,360/sh);
    var dw=sw*sc, dh=sh*sc;
    ctx.fillStyle='#000'; ctx.fillRect(0,0,480,360);
    ctx.drawImage(pv,(480-dw)/2,(360-dh)/2,dw,dh);
    var img=ctx.getImageData(0,0,480,360), data=img.data, n=data.length;
    if(ws.bufferedAmount>4*1024*1024){ busy=false; return; }
    var buf=new Uint8Array(8+n);
    buf[0]=0x47;buf[1]=0x39;buf[2]=0x38;buf[3]=0x46;
    buf[4]=1;buf[5]=224;buf[6]=1;buf[7]=104;
    buf.set(data,8);
    ws.send(buf.buffer); frames++; bytes+=n;
    if(frames%10===0){
      document.getElementById('frames').textContent=frames+' 帧';
      document.getElementById('bytes').textContent=(bytes/1048576).toFixed(1)+' MB';
    }
  }catch(e){ log('发送失败：'+e); }
  busy=false;
}
// ---- JPEG 降级通道 -------------------------------------------------------
// WebRTC/WHIP 不可用时（局域网被拦、浏览器不支持、TURN 缺失），把手机画面
// 转成 640x480 JPEG 走 HTTP POST 给 dashboard，camd 用逐帧 ffmpeg 解码成 YU12。
var jpegTimer=null, jpegCanvas=null, jpegCtx=null, jpegBusy=false, jpegFrames=0;
function stopJpeg(){ if(jpegTimer){ clearInterval(jpegTimer); jpegTimer=null; } jpegBusy=false; }
function jpegPaint(){
  if(!jpegCanvas){ jpegCanvas=document.createElement("canvas"); jpegCanvas.width=1280; jpegCanvas.height=960; jpegCtx=jpegCanvas.getContext("2d"); }
  var c=jpegCtx; c.fillStyle="#000"; c.fillRect(0,0,1280,960);
  try{
    var sw=pv.videoWidth, sh=pv.videoHeight;
    if(sw&&sh){
      var sc=Math.min(1280/sw,960/sh), dw=sw*sc, dh=sh*sc;
      c.drawImage(pv,(1280-dw)/2,(960-dh)/2,dw,dh);
    }else{ c.fillStyle="#333"; c.fillRect(0,0,1280,960); c.fillStyle="#fff"; c.font="bold 60px sans-serif"; c.fillText("等待画面…",500,500); }
  }catch(e){}
}
function startJpegFallback(){
  stopJpeg(); closedByUs=false;
  try{ if(pc){ pc.onconnectionstatechange=null; pc.close(); pc=null; } }catch(e){}
  ph.style.display="none"; startBtn.disabled=true; stopBtn.disabled=false; flipBtn.disabled=false;
  state("JPEG 通道准备中…",true);
  log("WebRTC 不可用，切换到 JPEG 低延迟通道（10 fps）…");
  // 先让 camd 把该节点切到 jpeg 源，再开始喂帧。
  fetch("/api/cam/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:dev,node:node,path:"webcam",kind:"webcam"})}).catch(function(){});
  jpegFrames=0;
  jpegTimer=setInterval(function(){
    if(jpegBusy) return; jpegBusy=true;
    jpegPaint();
    jpegCanvas.toBlob(function(b){
      if(!b){ jpegBusy=false; return; }
      fetch("/api/cam/frame?name="+encodeURIComponent(dev)+"&node="+encodeURIComponent(node),
            {method:"POST",headers:{"Content-Type":"image/jpeg"},body:b})
       .then(function(r){ return r.json(); })
       .then(function(j){
          if(j&&j.ok){ jpegFrames++;
            if(jpegFrames%10===0){ document.getElementById("frames").textContent=jpegFrames+" 帧"; document.getElementById("bytes").textContent=(jpegFrames*0.02).toFixed(1)+" MB"; }
          }
       }).catch(function(){}).then(function(){ jpegBusy=false; });
    },"image/jpeg",0.85);
  },120);
  setTimeout(function(){
    camStatus(function(j){
      var st=(j&&j.state)||{};
      if(st.state==="streaming"){ state("JPEG 推流中 → "+node,true); log("JPEG 通道已激活 → "+node); }
      else { log("JPEG 帧已开始发送（云手机相机状态："+(st.state||"准备中")+"）"); }
    });
  },1800);
}
function beginStream(){
  ph.style.display='none'; startBtn.disabled=true; stopBtn.disabled=false; flipBtn.disabled=false;
  if(q.get('jpeg')==='1'){ startJpegFallback(); return; }
  state('WebRTC 连接中…',true); log('摄像头已打开，正在通过 WebRTC 推流…');
  whipStart().catch(function(e){
    log('WebRTC 失败：'+(e&&e.message?e.message:e));
    startJpegFallback();
  });
}
function whipStart(){
  return new Promise(function(resolve,reject){
    var p=new RTCPeerConnection({iceServers:[]}); pc=p; var settled=false;
    function fail(e){
      if(settled) return; settled=true;
      if(whipTimer){ clearTimeout(whipTimer); whipTimer=null; }
      try{ p.onconnectionstatechange=null; p.close(); }catch(x){}
      if(pc===p) pc=null;
      reject(e);
    }
    function good(){
      if(settled) return; settled=true;
      if(whipTimer){ clearTimeout(whipTimer); whipTimer=null; }
      resolve();
    }
    p.onconnectionstatechange=function(){
      if(p.connectionState==='connected'){
        state('WebRTC 已连接，启动云手机相机…',true);
        log('WebRTC 已连接，正在打开云手机相机…'); startWhipStats(p); camStart(); good();
        reconnects=0; armWatch(p);
      }else if(p.connectionState==='failed'||p.connectionState==='closed'){
        fail(new Error('ICE '+p.connectionState));
      }
    };
    stream.getVideoTracks().forEach(function(t){ videoSender=p.addTrack(t,stream); });
    p.createOffer()
     .then(function(o){ return p.setLocalDescription(o); })
     .then(function(){
       return new Promise(function(r){
         if(p.iceGatheringState==='complete') return r();
         var done=false; function fin(){ if(done) return; done=true; r(); }
         p.onicegatheringstatechange=function(){ if(p.iceGatheringState==='complete') fin(); };
         setTimeout(fin,2500);
       });
     })
     .then(function(){
       return fetch('/whip/'+encodeURIComponent('cam-'+dev),{method:'POST',headers:{'Content-Type':'application/sdp'},body:p.localDescription.sdp});
     })
     .then(function(r){ if(!r.ok) throw new Error('WHIP '+r.status); return r.text(); })
     .then(function(sdp){ return p.setRemoteDescription({type:'answer',sdp:sdp}); })
     .catch(fail);
    whipTimer=setTimeout(function(){ fail(new Error('WebRTC 超时')); },9000);
  });
}
function armWatch(p){
  // 连接建立后的掉线看护：断线自动重连，不再无声停在「推流中」。
  p.onconnectionstatechange=function(){
    if(closedByUs||pc!==p) return;
    var s=p.connectionState;
    if(s==='connected'){ if(discTimer){ clearTimeout(discTimer); discTimer=null; } return; }
    if(s==='disconnected'){
      if(discTimer) return;
      discTimer=setTimeout(function(){ discTimer=null; if(!closedByUs&&pc===p) onDrop(p); },5000);
      return;
    }
    if(s==='failed'||s==='closed') onDrop(p);
  };
}
function onDrop(p){
  if(closedByUs||pc!==p) return;
  try{ p.onconnectionstatechange=null; p.close(); }catch(e){}
  pc=null;
  if(whipStats){ clearInterval(whipStats); whipStats=null; }
  state('连接中断，正在重连…',false);
  log('推流连接中断，正在自动重连…（云手机若显示彩条，表示暂未收到手机画面）');
  scheduleReconnect();
}
function scheduleReconnect(){
  if(closedByUs||!stream||reconnectTimer) return;
  var wait=Math.min(8000,Math.round(1000*Math.pow(1.5,reconnects++)));
  reconnectTimer=setTimeout(function(){
    reconnectTimer=null;
    if(closedByUs||!stream) return;
    camStarted=false;
    whipStart().catch(function(e){
      log('重连失败：'+(e&&e.message?e.message:e)+'，继续重试…');
      scheduleReconnect();
    });
  },wait);
}
function startWhipStats(p){
  if(whipStats){ clearInterval(whipStats); whipStats=null; }
  whipStats=setInterval(function(){
    if(!pc||pc!==p) return;
    p.getStats().then(function(rs){
      rs.forEach(function(r){
        if(r.type==='outbound-rtp'&&r.kind==='video'){
          if(typeof r.framesEncoded==='number') frames=r.framesEncoded;
          if(typeof r.bytesSent==='number') bytes=r.bytesSent;
          document.getElementById('frames').textContent=frames+' 帧';
          document.getElementById('bytes').textContent=(bytes/1048576).toFixed(1)+' MB';
        }
      });
    }).catch(function(){});
  },1000);
}
function camStatus(cb){
  fetch('/api/cam/status?name='+encodeURIComponent(dev),{cache:'no-store'})
   .then(function(r){ return r.json(); }).then(function(j){ cb(j||{}); }).catch(function(){ cb({}); });
}
function camFail(msg){
  camStarted=false; state('相机错误',false); log('云手机相机启动失败：'+msg);
  try{ if(pc){ pc.onconnectionstatechange=null; pc.close(); pc=null; } }catch(e){}
  try{ connect(); }catch(e){}
}
function camWait(n){
  if(!camStarted) return;
  if(n>90){ camFail('等待云手机相机超时'); return; }
  camStatus(function(j){
    var st=(j&&j.state)||{};
    if(st.state==='streaming'){
      var extra=st.duration?('，'+(st.duration)+'s'):'';
      state('推流中 → '+node,true); log('WebRTC 推流中 → '+node+'（云手机相机已打开'+extra+'）'); return;
    }
    if(st.state==='error'){ camFail(st.error||'未知错误'); return; }
    if(n===0) log('已提交推流，正在启动云手机相机…');
    setTimeout(function(){ camWait(n+1); },400);
  });
}
function camStart(){
  if(camStarted) return; camStarted=true;
  var body={name:dev,node:node,path:'rtsp://127.0.0.1:8554/cam-'+dev,kind:'stream',both:true};
  fetch('/api/cam/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
   .then(function(r){ return r.json(); })
   .then(function(j){
     if(j&&j.queued){ camWait(0); return; }
     if(j&&j.ok){ camWait(0); return; }
     if(j&&j.error&&/没结束/.test(j.error)){ camStarted=false; setTimeout(camStart,900); return; }
     camFail((j&&j.error)||'提交失败');
   }).catch(function(e){ camFail(e); });
}
function openCam(){
  if(q.get('test')==='1'){
    stopAll(true);
    var cv=document.createElement('canvas'); cv.width=1280; cv.height=720; var cc=cv.getContext('2d'); var t=0;
    setInterval(function(){ cc.fillStyle='hsl('+((t*7)%360)+',80%,45%)'; cc.fillRect(0,0,1280,720); cc.fillStyle='#fff'; cc.font='bold 84px sans-serif'; cc.fillText('PHONE '+(++t),80,400); },100);
    stream=cv.captureStream(10); pv.srcObject=stream; try{ pv.play(); }catch(e){}
    beginStream(); return;
  }
  if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){
    log('浏览器不支持摄像头 API，请用 HTTPS 打开本页。'); state('需要 HTTPS',false); return;
  }
  stopAll(true); ph.style.display='flex'; ph.textContent='正在打开摄像头…'; state('请求权限…',false);
  navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:{ideal:facing},width:{ideal:1280},height:{ideal:720}}})
   .then(function(s){ stream=s; pv.srcObject=s; return pv.play().catch(function(){}); })
   .then(function(){ beginStream(); })
   .catch(function(e){
      log('摄像头打开失败：'+(e&&e.message?e.message:e)); state('打开失败',false);
      ph.style.display='flex'; ph.textContent='摄像头未开启';
   });
}
startBtn.addEventListener('click',openCam);
stopBtn.addEventListener('click',function(){ stopAll(false); });
flipBtn.addEventListener('click',function(){
  facing=(facing==='environment'?'user':'environment');
  var nc=(facing==='environment'?'后置':'前置');
  log('切换摄像头：'+nc);
  // 热切换：同一个 peer connection 上 replaceTrack，不重开推流、不重启云手机相机。
  if(!videoSender||!pc||pc.connectionState!=='connected'||!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){ openCam(); return; }
  navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:{ideal:facing},width:{ideal:1280},height:{ideal:720}}})
   .then(function(s){
     var t=(s.getVideoTracks()||[])[0];
     if(!t) throw new Error('没有视频轨');
     return videoSender.replaceTrack(t).then(function(){
       if(stream&&stream!==s){ stream.getVideoTracks().forEach(function(o){ if(o!==t){ try{o.stop();}catch(e){} } }); }
       stream=s; try{ pv.srcObject=s; pv.play().catch(function(){}); }catch(e){}
       log('已热切换到'+nc+'摄像头，推流未中断');
     });
   })
   .catch(function(e){ log('热切换失败，改为重开摄像头：'+(e&&e.message?e.message:e)); openCam(); });
});
document.getElementById('node').addEventListener('change',function(){ node=this.value; if(stream){ stopAll(true); openCam(); } });
window.addEventListener('pagehide',function(){ try{ stopAll(true); }catch(e){} try{ navigator.sendBeacon('/api/cam/stop',new Blob([JSON.stringify({name:dev,node:node})],{type:'application/json'})); }catch(e){} });
if(q.get('auto')==='1'){ setTimeout(openCam, 300); }
})();
</script></body></html>"""

MSENSOR = r"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#05080c">
<title>传感器同步</title>
<style>
:root{--bg:#05080c;--panel:#0d1319;--panel2:#121a22;--line:#1e2a36;--txt:#dfe9f3;--mut:#8094a6;--ok:#2fd08a;--warn:#f0b429;--bad:#f2555a;--acc:#3ea6ff}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;padding:calc(10px + env(safe-area-inset-top)) 12px calc(18px + env(safe-area-inset-bottom))}
h1{font-size:16px;margin:0}
.sub{color:var(--mut);font-size:12px;margin-top:2px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px;margin-top:12px}
.row{display:flex;align-items:center;gap:10px}
.grow{flex:1}
.sw{position:relative;width:52px;height:30px;border-radius:999px;background:#1d2937;border:1px solid var(--line);transition:background .18s;flex:0 0 auto}
.sw i{position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#7c8b99;transition:transform .18s,background .18s}
.sw.on{background:rgba(62,166,255,.22);border-color:#2f6d9e}
.sw.on i{transform:translateX(22px);background:var(--acc)}
.list{margin-top:10px;display:grid;grid-template-columns:1fr 1fr;gap:8px}
.item{display:flex;align-items:center;gap:8px;background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:9px 10px;font-size:13px}
.item input{width:17px;height:17px;accent-color:var(--acc);margin:0}
.item.na{opacity:.5}
.item.na .nm::after{content:" 不支持";color:var(--mut);font-size:11px}
.nm{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stat{margin-top:10px;font-size:12.5px;color:var(--mut);min-height:20px}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#4a5563;margin-right:6px;vertical-align:1px}
.dot.on{background:var(--ok)}
.dot.err{background:var(--bad)}
.tip{margin-top:8px;font-size:11.5px;color:#6d8093}
.btn{height:38px;padding:0 14px;border-radius:10px;border:1px solid #2f4a63;background:#142232;color:#dbe9f7;font-size:13px;font-weight:600}
</style></head><body>
<div class="row"><div class="grow"><h1>传感器同步</h1><div class="sub" id="devline">云手机 wx1</div></div>
<div class="sw" id="sw"><i></i></div></div>
<div class="card">
  <div class="row"><b>推送到云机</b><div class="grow"></div><span id="mode" class="sub"></span></div>
  <div class="list" id="list"></div>
  <div class="stat"><span class="dot" id="dot"></span><span id="stat">未开启</span></div>
  <div class="tip" id="tip">只有你打开上面的开关后，本机传感器数据才会发送给云手机；关闭即停止，云机回落到内置模拟数据。</div>
</div>
<div class="card" id="permcard" style="display:none">
  <div class="row"><b>需要授权</b><div class="grow"></div><button class="btn" id="ask">授权/重试</button></div>
  <div class="tip" id="permtip">浏览器要求先交互一次才允许读取传感器；iOS 还需在设置里允许“运动与方向”。</div>
</div>
<script>
(function(){
var q=new URLSearchParams(location.search);
var DEV=q.get('device')||'wx1', TEST=q.get('test')==='1';
var SLOTS=[['acc','加速度计',3],['gyro','陀螺仪',3],['mag','磁力计',3],['rv','旋转矢量',4],['grav','重力',3],['lin','线性加速度',3],['light','光线',1],['prox','距离',1],['pressure','气压',1],['step','计步',1]];
var CTRL={acc:'Accelerometer',gyro:'Gyroscope',mag:'Magnetometer',rv:'AbsoluteOrientationSensor',grav:'GravitySensor',lin:'LinearAccelerationSensor',light:'AmbientLightSensor',prox:'ProximitySensor'};
var have={}, enabled={}, reading={}, ctrls={}, ws=null, timer=null, t0=Date.now(), sent=0, got=0, mode='';
document.getElementById('devline').textContent='云手机 '+DEV;
function $(id){return document.getElementById(id)}
function toast(m){var s=$('stat');s.textContent=m}
function support(){
  SLOTS.forEach(function(s){
    var c=CTRL[s[0]];
    have[s[0]]= !!(TEST || (c && window[c]));
  });
  if(!TEST && (window.DeviceMotionEvent||window.DeviceOrientationEvent)){ have.acc=have.acc||!!window.DeviceMotionEvent; have.rv=have.rv||!!window.DeviceOrientationEvent; }
}
function build(){
  var l=$('list'); l.innerHTML='';
  SLOTS.forEach(function(s){
    var k=s[0], ok=have[k];
    enabled[k]=ok;
    var d=document.createElement('label');
    d.className='item'+(ok?'':' na');
    d.innerHTML='<input type="checkbox" data-k="'+k+'"'+(ok?' checked':'')+(ok?'':' disabled')+'><span class="nm">'+s[1]+'</span>';
    l.appendChild(d);
  });
  l.addEventListener('change',function(e){
    var k=e.target.getAttribute('data-k'); if(!k) return;
    enabled[k]=e.target.checked;
    if(ws&&ws.readyState===1) send(true);
  });
}
function dot(cls){$('dot').className='dot'+(cls?' '+cls:'')}
function wsOpen(){
  if(ws&&(ws.readyState===0||ws.readyState===1)) return ws;
  var proto=location.protocol==='https:'?'wss://':'ws://';
  try{ ws=new WebSocket(proto+location.host+'/ws/sensor?name='+encodeURIComponent(DEV)); }catch(e){ dot('err'); toast('无法连接：'+e.message); return null; }
  ws.onopen=function(){ dot('on'); toast('已连接，正在同步'); };
  ws.onclose=function(){ if(timer){ dot(''); toast('已断开'); } };
  ws.onerror=function(){ dot('err'); };
  ws.onmessage=function(ev){ try{ var m=JSON.parse(ev.data); if(m&&m.type==='ack'){ got++; } }catch(e){} };
  return ws;
}
function payload(){
  var on=[], v={};
  SLOTS.forEach(function(s){
    var k=s[0]; if(!enabled[k]) return;
    var a=reading[k]; if(!a||!a.length) return;
    on.push(k); v[k]=a.slice(0,s[2]);
  });
  return {on:on, v:v, t:Date.now()};
}
function send(force){
  if(!ws||ws.readyState!==1) return;
  var p=payload(); if(!p.on.length) return;
  ws.send(JSON.stringify(p)); sent++;
}
function startSensors(){
  SLOTS.forEach(function(s){
    var k=s[0]; if(!enabled[k]||ctrls[k]||!have[k]||TEST) return;
    var c=CTRL[k]; if(!c||!window[c]) return;
    try{
      var ctl=new window[c]({frequency:25});
      ctl.addEventListener('reading',function(){
        var arr=[];
        if(k==='rv'){ var qq=ctl.quaternion||[]; arr=[qq[0]||0,qq[1]||0,qq[2]||0,qq[3]||0]; }
        else if(k==='light'||k==='prox'){ arr=[ctl.illuminance!=null?ctl.illuminance:(ctl.distance!=null?ctl.distance:0)]; }
        else { arr=[ctl.x||0,ctl.y||0,ctl.z||0]; }
        reading[k]=arr;
      });
      ctl.start(); ctrls[k]=ctl;
    }catch(e){}
  });
}
function stopSensors(){
  Object.keys(ctrls).forEach(function(k){ try{ctrls[k].stop()}catch(e){} delete ctrls[k]; });
}
function motion(){
  window.addEventListener('devicemotion',function(e){
    var a=e.accelerationIncludingGravity||e.acceleration; if(a&&enabled.acc) reading.acc=[a.x||0,a.y||0,a.z||0];
    var g=e.acceleration; if(g&&enabled.lin) reading.lin=[g.x||0,g.y||0,g.z||0];
    var r=e.rotationRate; if(r&&enabled.gyro) reading.gyro=[(r.beta||0)*0.0174533,(r.gamma||0)*0.0174533,(r.alpha||0)*0.0174533];
  },true);
  window.addEventListener('deviceorientation',function(e){
    if(!enabled.rv) return;
    var z=(e.alpha||0)*0.0174533/2, x=(e.beta||0)*0.0174533/2, y=(e.gamma||0)*0.0174533/2;
    var cz=Math.cos(z),sz=Math.sin(z),cx=Math.cos(x),sx=Math.sin(x),cy=Math.cos(y),sy=Math.sin(y);
    reading.rv=[sx*cy*cz-cx*sy*sz, cx*sy*cz+sx*cy*sz, cx*cy*sz-sx*sy*cz, cx*cy*cz+sx*sy*sz];
  },true);
}
function fake(){
  setInterval(function(){
    var t=(Date.now()-t0)/1000;
    reading.acc=[1.25*Math.sin(t),1.25*Math.cos(t),9.8];
    reading.gyro=[0.3*Math.sin(t*2),0.3*Math.cos(t*2),0.1];
    reading.mag=[22*Math.cos(t),22*Math.sin(t),-42];
    reading.rv=[0,0,Math.sin(t/2),Math.cos(t/2)];
    reading.grav=[0,0,9.8];
    reading.lin=[Math.sin(t),Math.cos(t),0];
    reading.light=[Math.round(300+200*Math.sin(t))];
    reading.prox=[0];
    reading.pressure=[1013];
    reading.step=[Math.round(t)];
  },40);
}
function loop(){
  if(timer) clearInterval(timer);
  timer=setInterval(function(){ send(false); },40);
}
function on(){
  $('sw').classList.add('on');
  support(); build();
  if(TEST){ mode='自测模式（合成数据）'; fake(); }
  else { motion(); startSensors(); }
  $('mode').textContent=mode;
  wsOpen(); loop();
  var deadline=Date.now()+2500;
  (function check(){
    if(!ws||ws.readyState!==1){ if(Date.now()<deadline) return setTimeout(check,200); }
    else { send(true);
      if(got===0 && !TEST) setTimeout(function(){ send(true); },300);
    }
  })();
  toast('同步中 · '+(TEST?'合成':'本机')+'传感器');
}
function off(){
  $('sw').classList.remove('on');
  if(timer){ clearInterval(timer); timer=null; }
  stopSensors();
  try{ if(ws) ws.close(); }catch(e){}
  ws=null; dot(''); toast('已停止同步');
}
$('sw').addEventListener('click',function(){ if($('sw').classList.contains('on')) off(); else on(); });
$('ask').addEventListener('click',function(){
  try{
    if(window.DeviceMotionEvent&&DeviceMotionEvent.requestPermission) DeviceMotionEvent.requestPermission();
    if(window.DeviceOrientationEvent&&DeviceOrientationEvent.requestPermission) DeviceOrientationEvent.requestPermission();
  }catch(e){}
  if(TEST||!('Accelerometer' in window)&&!(window.DeviceMotionEvent)) $('permtip').textContent='这个浏览器没有传感器接口：请用 Chrome（安卓）或 Safari（iOS），或改在云机内使用自测模式。';
  if($('sw').classList.contains('on')) { off(); setTimeout(on,200); }
});
window.addEventListener('pagehide',function(){ try{ off(); }catch(e){} });
support();
if(!TEST && (!window.isSecureContext)) { $('permcard').style.display=''; $('permtip').textContent='当前不是 https 页面，部分浏览器会禁用传感器接口。请用控制台的 https 入口打开。'; }
if(q.get('auto')==='1') setTimeout(on,300);
})();
</script></body></html>"""
MPAGE = r"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#05080c">
<title>G98 云手机</title>
<style>
:root{color-scheme:dark;forced-color-adjust:none;--bg:#05080c;--card:#0e1720;--line:#223242;--txt:#eef5fc;--mut:#8ea2b6;--acc:#5b8cff;--ok:#35d39a;--warn:#f5a524;--bad:#ff6b6b}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;width:100%;height:100%;height:100dvh;overflow:hidden;background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;overscroll-behavior:none}
:root{--topbar-h:calc(46px + env(safe-area-inset-top));--dock-h:calc(64px + env(safe-area-inset-bottom))}
#stage{position:fixed;left:0;right:0;top:var(--topbar-h);height:calc(100vh - var(--topbar-h));height:calc(100dvh - var(--topbar-h));background:#000;overflow:hidden}
#v{position:absolute;left:0;top:0;width:100%;height:calc(100% - var(--dock-h));object-fit:contain;background:#000;touch-action:none;display:block}
#veil{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:12px;background:radial-gradient(circle at 50% 38%,#122131,#03060a 72%);z-index:20;text-align:center;padding:24px;color:#b9c9d9;font-size:13px;line-height:1.7}
#veil.hide{display:none}
.spin{width:28px;height:28px;border:2px solid #2b4055;border-top-color:var(--acc);border-radius:50%;animation:sp 1s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
#veil b{font-size:16px;color:#fff;font-weight:650}
#topbar{position:absolute;left:0;right:0;top:0;z-index:30;display:flex;align-items:center;gap:8px;padding:calc(8px + env(safe-area-inset-top)) 10px 8px;background:linear-gradient(180deg,rgba(0,0,0,.72),rgba(0,0,0,0));pointer-events:none}
#topbar .chip{pointer-events:auto;display:flex;align-items:center;gap:7px;height:34px;padding:0 11px;border-radius:999px;background:rgba(10,18,26,.72);border:1px solid rgba(255,255,255,.13);backdrop-filter:blur(10px);font-size:12px;color:#d8e5f2}
#dot{width:7px;height:7px;border-radius:50%;background:#607080}
#dot.on{background:var(--ok);box-shadow:0 0 8px rgba(53,211,154,.8)}
#stat{font-variant-numeric:tabular-nums;color:#a9bccd}
#topbar .grow{flex:1}
.iconbtn{pointer-events:auto;width:36px;height:36px;border-radius:11px;border:1px solid rgba(255,255,255,.14);background:rgba(10,18,26,.72);color:#e8f2fc;font-size:16px;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(10px)}
#dock{position:absolute;left:0;right:0;bottom:0;z-index:30;display:flex;justify-content:space-around;align-items:stretch;gap:4px;padding:7px 8px calc(7px + env(safe-area-inset-bottom));background:rgba(6,10,15,.94)}
#dock .db{appearance:none;-webkit-appearance:none;flex:1;min-width:0;height:54px;border:1px solid rgba(255,255,255,.08);background:rgba(20,32,44,.92);color:#e6f0fa;border-radius:13px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;font-size:10px;font-weight:600;letter-spacing:.02em}
#dock .db span{font-size:18px;line-height:1;font-weight:400}
#dock .db em{font-style:normal;color:#fff!important;-webkit-text-fill-color:#fff!important}
#dock .db span{color:#fff!important;-webkit-text-fill-color:#fff!important}
#dock .db:active{background:rgba(91,140,255,.24);border-color:rgba(91,140,255,.5)}
#kbar{position:absolute;left:8px;right:8px;bottom:calc(75px + env(safe-area-inset-bottom));z-index:35;display:none;gap:6px;align-items:center;padding:7px;border-radius:14px;background:rgba(12,20,29,.96);border:1px solid #26394b;box-shadow:0 10px 30px rgba(0,0,0,.5)}
#kbar.show{display:flex}
#kinp{flex:1;min-width:0;height:40px;border:1px solid #2a3d50;border-radius:10px;background:#071019;color:#fff;padding:0 10px;font-size:16px;outline:none}
#kbar button{height:40px;padding:0 10px;border-radius:10px;border:1px solid #2a3d50;background:#162433;color:#e8f2fc;font-size:12px;font-weight:600;white-space:nowrap}
#kbar button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
.sheet{position:absolute;left:0;right:0;bottom:0;z-index:40;display:none;flex-direction:column;padding:14px 14px calc(14px + env(safe-area-inset-bottom));background:#0c151e;border-top:1px solid #24384a;border-radius:18px 18px 0 0;box-shadow:0 -18px 50px rgba(0,0,0,.6);max-height:82dvh}
.sheet.show{display:flex}
/* 关掉弹窗不能把正在跑的东西停掉：display:none 会让浏览器把 iframe 里的定时器节流到
   1Hz（传感器同步掉帧）甚至断掉 WebRTC 推流。所以“关闭”只是把面板挪到屏幕外。 */
.sheet.park{display:flex;left:-200vw;right:auto;width:min(560px,96vw);pointer-events:none}
#dock .db{position:relative}
#dock .db i.bdg{position:absolute;top:3px;right:5px;font-style:normal;font-size:9px;line-height:1;padding:2px 4px;border-radius:6px;background:#3d7be0;color:#fff;display:none}
#dock .db i.bdg.on{display:block}
#dock .db i.bdg.warn{background:#c4553d}
#dock .db i.bdg.good{background:#1f9d68}
#cfgbar{position:absolute;left:8px;right:8px;bottom:calc(var(--dock-h) + 6px + env(safe-area-inset-bottom));z-index:29;display:flex;gap:6px;flex-wrap:wrap;pointer-events:none}
#cfgbar .ci{display:flex;align-items:center;gap:4px;padding:4px 9px;border-radius:999px;background:rgba(8,16,24,.88);border:1px solid rgba(120,170,230,.26);font-size:11px;color:#9fb6cb}
#cfgbar .ci b{font-weight:700;color:#e8f3ff}
#cfgbar .ci.live{border-color:rgba(70,210,150,.55);color:#8fe8bf}
#cfgbar .ci.live b{color:#b7ffe0}
.sh{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
.sh b{font-size:16px}
.sh button{width:34px;height:34px;border-radius:10px;border:1px solid #2a3d50;background:#152331;color:#dce9f6;font-size:18px}
.sheet p{margin:0 0 10px;color:#93a7ba;font-size:12px;line-height:1.6}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row button,.row a{flex:1;min-width:120px;height:42px;display:flex;align-items:center;justify-content:center;border-radius:11px;border:1px solid #2a3d50;background:#162433;color:#e8f2fc;font-size:13px;font-weight:600;text-decoration:none}
.row button.primary{background:linear-gradient(180deg,#6d9bff,#4b7bf0);border-color:#6d9bff;color:#fff}
#camsheet{max-height:none;padding:10px 12px calc(12px + env(safe-area-inset-bottom))}
#camsheet p{display:none}
#camsheet .sh{margin-bottom:6px}
#camframe{display:none;width:100%;height:150px;border:1px solid #24384a;border-radius:10px;margin-top:8px;background:#000}
#camframe.show{display:block}
#camsheet.mini{left:auto;right:10px;bottom:calc(var(--dock-h) + 10px + env(safe-area-inset-bottom));width:190px;max-width:62vw;border-radius:14px;padding:8px 9px}
#camsheet.mini #camrow{display:none}
#camsheet.mini #camframe{height:104px;margin-top:0}
#camsheet.mini .sh{margin-bottom:4px}
#camsheet.mini .sh b{font-size:12px}
#camsheet.mini .sh button{width:26px;height:26px;font-size:15px;border-radius:8px}
.camtab{display:flex;gap:6px;margin:0 0 10px}
.camtab button{flex:1;height:34px;border-radius:10px;border:1px solid #2a3d50;background:#101d29;color:#a9bccd;font-size:12px}
.camtab button.on{background:rgba(91,140,255,.22);border-color:rgba(91,140,255,.55);color:#fff;font-weight:650}
.campane{display:none}
.campane.on{display:block}
.campane p{margin:0 0 8px}
.campane input[type=file]{width:100%;font-size:12px;color:#cfe0ef;background:#0b1620;border:1px dashed #2a3d50;border-radius:10px;padding:12px 10px}
.camstat{margin-top:8px;font-size:11px;color:#8ea2b6;min-height:16px;word-break:break-all}
.camstat.ok{color:#5ad19b}
.camstat.bad{color:#ff9aa7}
.seg{display:flex;gap:6px;margin-top:10px}
.seg button{flex:1;height:34px;border-radius:10px;border:1px solid #2a3d50;background:#101d29;color:#a9bccd;font-size:12px}
.seg button.on{background:rgba(91,140,255,.22);border-color:rgba(91,140,255,.55);color:#fff}
#camsheet{max-height:86vh;overflow:auto}
.pickbtn{display:flex;align-items:center;justify-content:center;height:44px;border-radius:11px;border:1px solid #2f4a63;background:#142232;color:#dbe9f7;font-size:13px;font-weight:600}
.pickbtn:active{background:#1b2f44}
.fields{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0}
.fields label{display:block;font-size:10px;color:#8195a8;margin:0 0 4px 2px}
.fields input{width:100%;height:40px;border:1px solid #2a3d50;border-radius:10px;background:#071019;color:#fff;padding:0 9px;font-size:14px;font-variant-numeric:tabular-nums}
.cities{display:flex;gap:6px;flex-wrap:wrap;margin:2px 0 10px}
.cities button{height:32px;padding:0 10px;border-radius:999px;border:1px solid #2a3d50;background:#101d29;color:#cfe0ef;font-size:11px}
#loclog{min-height:18px;color:#8ea2b6;font-size:11px;line-height:1.5;word-break:break-all}
.lbadge{display:inline-block;margin:0 0 10px;padding:3px 9px;border-radius:999px;border:1px solid #24506e;background:#0e2233;color:#9fd0f0;font-size:11px}
.lbadge.warn{border-color:#7a5a12;background:#2a2210;color:#ffd76a}
#toast{position:fixed;left:50%;bottom:calc(150px + env(safe-area-inset-bottom));transform:translateX(-50%) translateY(14px);z-index:60;max-width:86vw;padding:9px 14px;border-radius:10px;background:rgba(10,18,26,.96);border:1px solid #2a3d50;color:#eaf3fb;font-size:12px;opacity:0;pointer-events:none;transition:.18s;text-align:center}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media (orientation:landscape) and (max-height:520px){:root{--topbar-h:calc(46px + env(safe-area-inset-top));--dock-h:calc(58px + env(safe-area-inset-bottom))}#dock .db{height:44px}#dock .db span{font-size:15px}#dock .db em{display:none}#topbar{padding-top:calc(5px + env(safe-area-inset-top))}#kbar{bottom:calc(58px + env(safe-area-inset-bottom))}}
</style></head><body>
<div id="stage">
<video id="v" autoplay playsinline muted></video>
<div id="veil"><div class="spin"></div><b id="vtitle">正在连接云手机</b><div id="vtext">wx1 · 低延迟画面</div></div>
<div id="topbar"><div class="chip"><span id="dot"></span><b id="devname">wx1</b><span id="stat">连接中</span></div><div class="grow"></div><button class="iconbtn" id="full" title="全屏">⛶</button></div>
<div id="kbar"><input id="kinp" type="text" inputmode="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="输入文字，自动发送"><button id="ksend">发送</button><button id="kenter" class="primary">回车</button><button id="kclose">×</button></div>
<div id="dock">
<div class="db" data-key="4"><span>←</span><em>返回</em></div>
<div class="db" data-key="3"><span>⌂</span><em>主页</em></div>
<div class="db" data-key="187"><span>▣</span><em>任务</em></div>
<div class="db" id="kbtn"><span>⌨</span><em>键盘</em></div>
<div class="db" id="cambtn"><span>◉</span><em>相机</em><i class="bdg" id="camBdg"></i></div>
<div class="db" id="locbtn"><span>◎</span><em>定位</em><i class="bdg" id="locBdg"></i></div>
<div class="db" id="sensebtn"><span>⌁</span><em>传感器</em><i class="bdg" id="senBdg"></i></div>
</div>
<div id="cfgbar"></div>
<div id="camsheet" class="sheet"><div class="sh"><b>相机注入</b><button id="camclose">×</button></div>
<div class="camtab"><button data-tab="live" class="on">手机实时</button><button data-tab="image">图片</button><button data-tab="video">视频</button></div>
<div class="campane on" id="pane-live"><p>把这部手机的摄像头画面实时注入云手机。首次使用请允许摄像头权限；自签证书需要先点“继续”。</p><div class="row" id="camrow"><button class="primary" id="camstart">打开摄像头</button><a id="camlink" href="#" target="_blank" rel="noopener">新页面</a></div><iframe id="camframe" allow="camera; microphone; autoplay; fullscreen"></iframe></div>
<div class="campane" id="pane-image"><p>选一张图片，热注入云手机相机（不重启相机、不重启 provider，最多 1 秒生效）。</p><label class="pickbtn" for="camimg">选择图片…</label><input type="file" accept="image/*" id="camimg" hidden><div class="camstat" id="imgstat"></div></div>
<div class="campane" id="pane-video"><p>选一段视频循环注入云手机相机。</p><label class="pickbtn" for="camvideo">选择视频…</label><input type="file" accept="video/*" id="camvideo" hidden><div class="camstat" id="vidstat"></div></div>
<div class="seg"><button data-cnode="back" class="on">后置</button><button data-cnode="front">前置</button><button id="camidle">恢复空闲</button></div>
</div>
<div id="locsheet" class="sheet"><div class="sh"><b>云手机定位</b><button id="locclose">×</button></div><p>云手机默认用板子出口网线的位置。只有你点按钮才会改，改了会标记「人为设定」，不会自动跟随。</p><div id="locbadge" class="lbadge">板子默认（出口网线）</div><div class="fields"><div><label>纬度</label><input id="lat" inputmode="decimal"></div><div><label>经度</label><input id="lon" inputmode="decimal"></div></div><div class="fields" style="grid-template-columns:1fr"><div><label>海拔（米）</label><input id="alt" inputmode="decimal"></div></div><div class="row" style="grid-template-columns:1fr 1fr"><button class="primary" id="myloc">◎ 取我现在的位置</button><button id="boardloc">板子位置</button></div><div class="cities"><button data-city="39.9042,116.4074,50">北京</button><button data-city="31.2304,121.4737,10">上海</button><button data-city="22.5431,114.0579,20">深圳</button><button data-city="23.1291,113.2644,20">广州</button><button data-city="30.2741,120.1551,15">杭州</button></div><div class="row"><button id="locrefresh">刷新</button><button class="primary" id="locapply">应用定位</button></div><div id="loclog"></div></div>
<div id="sensesheet" class="sheet"><div class="sh"><b>传感器同步</b><button id="senseclose">×</button></div>
<iframe id="senseframe" allow="accelerometer; gyroscope; magnetometer; ambient-light-sensor; autoplay" style="width:100%;height:64vh;border:0;border-radius:12px;background:#05080c"></iframe>
</div>
<div id="toast"></div>
</div>
<script>
(function(){
var q=new URLSearchParams(location.search);
var DEV=q.get('device')||'wx1', K=q.get('k')||'';
document.getElementById('devname').textContent=DEV;
var V=document.getElementById('v'), VEIL=document.getElementById('veil'), DOT=document.getElementById('dot'), STAT=document.getElementById('stat');
var pc=null, gen=0, got=false, retries=0, statsT=null, timer=null, pingT=null;
function toast(m){var t=document.getElementById('toast');t.textContent=m;t.className='show';clearTimeout(t._t);t._t=setTimeout(function(){t.className=''},2400)}
function post(url,obj){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)}).then(function(r){return r.json().catch(function(){return {}})}).then(function(j){if(j&&j.error)toast(j.error);return j}).catch(function(){toast('网络请求失败');return {ok:false,error:'network'}})}
function setStat(s){STAT.textContent=s}
function veil(show,title,text){VEIL.classList.toggle('hide',!show);if(title)document.getElementById('vtitle').textContent=title;if(text)document.getElementById('vtext').textContent=text}
function stopPC(){gen++;if(timer){clearTimeout(timer);timer=null}if(statsT){clearInterval(statsT);statsT=null}if(pingT){clearInterval(pingT);pingT=null}if(pc){try{pc.close()}catch(e){}pc=null}got=false;DOT.className='';}
function connect(){
  if(!window.RTCPeerConnection){veil(true,'浏览器不支持','请使用最新版 Chrome/Safari');return}
  stopPC();var g=++gen;veil(true,'正在连接云手机',DEV+' · WebRTC 低延迟');setStat('连接中');
  fetch('/api/rtc/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:DEV})}).then(function(r){return r.json()}).then(function(j){
    if(g!==gen)return;if(!j||j.error)throw new Error((j&&j.error)||'rtc start failed');
    if(pingT){clearInterval(pingT)}
    pingT=setInterval(function(){if(g!==gen){clearInterval(pingT);pingT=null;return}post('/api/rtc/ping',{name:DEV}).catch(function(){})},15000);
    pc=new RTCPeerConnection({iceServers:[]});
    pc.addTransceiver('video',{direction:'recvonly'});
    pc.ontrack=function(ev){if(g!==gen)return;var st=(ev.streams&&ev.streams[0])||new MediaStream([ev.track]);try{V.srcObject=st}catch(e){}try{V.play().catch(function(){})}catch(e){}};
    pc.onconnectionstatechange=function(){if(g!==gen)return;if(pc.connectionState==='failed')fail('连接失败');if(pc.connectionState==='disconnected')setTimeout(function(){if(g===gen&&pc&&pc.connectionState==='disconnected')fail('连接断开')},2200)};
    pc.createOffer().then(function(o){return pc.setLocalDescription(o)}).then(function(){return new Promise(function(res){if(pc.iceGatheringState==='complete')return res();var d=false;var t=setTimeout(function(){if(!d){d=true;res()}},1600);pc.addEventListener('icegatheringstatechange',function h(){if(pc.iceGatheringState==='complete'&&!d){d=true;clearTimeout(t);res()}})})}).then(function(){
      return fetch('/whep/'+encodeURIComponent(DEV),{method:'POST',cache:'no-store',headers:{'Content-Type':'application/sdp'},body:pc.localDescription.sdp}).then(function(r){if(!r.ok)throw new Error('WHEP '+r.status);return r.text()});
    }).then(function(sdp){if(g!==gen)return;return pc.setRemoteDescription({type:'answer',sdp:sdp}).then(function(){try{pc.getReceivers().forEach(function(r){try{r.jitterBufferTarget=0}catch(e){}try{r.playoutDelayHint=0}catch(e){}})}catch(e){}try{V.playoutDelayHint=0}catch(e){}})}).catch(function(e){if(g===gen)fail(e.message||'WebRTC 失败')});
  }).catch(function(e){if(g===gen)fail(e.message||'启动失败')});
  timer=setTimeout(function(){if(g===gen&&!got)fail('连接超时')},12000);
}
function fail(m){if(pc){try{pc.close()}catch(e){}pc=null}got=false;DOT.className='';setStat('未连接');veil(true,'连接失败',m||'点按重试');if(retries<1){retries++;setTimeout(connect,1500)}else{VEIL.onclick=function(){retries=0;connect()}}}
V.onplaying=function(){got=true;retries=0;if(timer){clearTimeout(timer);timer=null}VEIL.classList.add('hide');DOT.className='on';setStat('已连接');startStats()};
V.onloadeddata=V.onplaying;
V.onended=function(){if(!document.hidden)fail('画面结束')};
function startStats(){if(statsT)clearInterval(statsT);statsT=setInterval(function(){if(!pc)return;pc.getStats(null).then(function(rs){var fps=0,rtt=-1,jb=-1;rs.forEach(function(s){if(s.type==='inbound-rtp'&&s.kind==='video'){fps=s.framesPerSecond||0;jb=s.jitter?Math.round(s.jitter*1000):-1}if(s.type==='candidate-pair'&&s.state==='succeeded'&&s.currentRoundTripTime!=null)rtt=Math.round(s.currentRoundTripTime*1000)});if(got)setStat((fps||0)+'fps'+(rtt>=0?' · '+rtt+'ms':''))}).catch(function(){})},1500)}
window.addEventListener('pagehide',function(){stopPC()});
document.addEventListener('visibilitychange',function(){if(!document.hidden&&got&&V.paused)try{V.play().catch(function(){})}catch(e){}});
var IW={ws:null,ok:false,name:null};
function inputWS(){if(IW.ws&&IW.ws.readyState===1)return IW.ws;if(IW.ws){try{IW.ws.close()}catch(e){}IW.ws=null}try{var u=(location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws/input?name='+encodeURIComponent(DEV);var w=new WebSocket(u);w.onopen=function(){IW.ok=true};w.onclose=function(){IW.ok=false};w.onerror=function(){IW.ok=false};IW.ws=w;return w}catch(e){return null}}
function sendInput(a,fx,fy){var w=IW.ok?IW.ws:inputWS();if(w&&w.readyState===1&&IW.ok){try{w.send(JSON.stringify({a:a,fx:fx,fy:fy}));return Promise.resolve({ok:true})}catch(e){}}return post('/api/touch',{name:DEV,a:a,fx:fx,fy:fy})}
function sendKey(code){var w=IW.ok?IW.ws:inputWS();if(w&&w.readyState===1&&IW.ok){try{w.send(JSON.stringify({a:'k',code:code}));return Promise.resolve({ok:true})}catch(e){}}return post('/api/key',{name:DEV,code:code})}
function norm(e){var r=V.getBoundingClientRect(),vw=V.videoWidth||480,vh=V.videoHeight||1066,s=Math.min(r.width/vw,r.height/vh),w=vw*s,h=vh*s,l=r.left+(r.width-w)/2,t=r.top+(r.height-h)/2;var fx=(e.clientX-l)/w,fy=(e.clientY-t)/h;return {fx:Math.max(0,Math.min(1,fx)),fy:Math.max(0,Math.min(1,fy))}}
var T=null,longT=null;
V.addEventListener('pointerdown',function(e){if(T)return;if(e.button!==undefined&&e.button!==0)return;e.preventDefault();var p=norm(e);T={id:e.pointerId,st:p,last:0,moved:false,started:false,t:Date.now()};try{V.setPointerCapture(e.pointerId)}catch(err){}longT=setTimeout(function(){if(T&&!T.moved)T.long=true},380)});
V.addEventListener('pointermove',function(e){if(!T||e.pointerId!==T.id)return;e.preventDefault();var p=norm(e);if(!T.started){if(Math.abs(p.fx-T.st.fx)<0.007&&Math.abs(p.fy-T.st.fy)<0.007)return;clearTimeout(longT);T.moved=true;T.started=true;sendInput('d',T.st.fx,T.st.fy);}var now=Date.now();if(now-T.last<16)return;T.last=now;sendInput('m',p.fx,p.fy)});
function end(e){if(!T||e.pointerId!==T.id)return;e.preventDefault();clearTimeout(longT);var st=T;T=null;try{V.releasePointerCapture(e.pointerId)}catch(err){}clearTimeout(longT);if(st.started){var pe=norm(e);sendInput('u',pe.fx,pe.fy)}else if(st.long){post('/api/longpress',{name:DEV,fx:st.st.fx,fy:st.st.fy,dur:Math.max(450,Date.now()-st.t)}).then(function(j){if(!(j&&j.ok))toast((j&&j.error)||'长按失败')})}else{var p=norm(e);sendInput('t',p.fx,p.fy).then(function(j){if(j&&j.ok===false)post('/api/tap',{name:DEV,fx:p.fx,fy:p.fy})})}}
V.addEventListener('pointerup',end);V.addEventListener('pointercancel',function(e){if(!T||e.pointerId!==T.id)return;clearTimeout(longT);var st=T;T=null;if(st.started)sendInput('u',st.st.fx,st.st.fy)});V.addEventListener('contextmenu',function(e){e.preventDefault()});
document.querySelectorAll('#dock [data-key]').forEach(function(b){b.addEventListener('click',function(){sendKey(parseInt(b.getAttribute('data-key'),10))})});
var kbar=document.getElementById('kbar'),kinp=document.getElementById('kinp'),kt=null,kbuf='';
function kbShow(){kbar.classList.add('show');setTimeout(function(){kinp.focus()},30)}
function kbFlush(){var v=kinp.value;if(!v)return Promise.resolve();kinp.value='';return post('/api/text',{name:DEV,text:v}).then(function(j){if(!(j&&j.ok))toast((j&&j.error)||'输入失败');return j})}
document.getElementById('kbtn').addEventListener('click',function(){if(kbar.classList.contains('show')){kbar.classList.remove('show');kinp.blur()}else kbShow()});
document.getElementById('kclose').addEventListener('click',function(){kbar.classList.remove('show');kinp.blur()});
document.getElementById('ksend').addEventListener('click',function(){clearTimeout(kt);kbFlush().then(function(){kinp.focus()})});
document.getElementById('kenter').addEventListener('click',function(){clearTimeout(kt);kbFlush().then(function(){setTimeout(function(){sendKey(66);kinp.focus()},160)})});
kinp.addEventListener('input',function(e){if(e.isComposing)return;clearTimeout(kt);kt=setTimeout(kbFlush,260)});
kinp.addEventListener('compositionend',function(){clearTimeout(kt);kt=setTimeout(kbFlush,120)});
kinp.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();clearTimeout(kt);kbFlush().then(function(){setTimeout(function(){sendKey(66)},140)})}else if(e.key==='Backspace'&&!kinp.value){e.preventDefault();sendKey(67)}});
document.addEventListener('keydown',function(e){var t=e.target;if(t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable))return;if(e.ctrlKey||e.altKey||e.metaKey)return;if(e.key==='Enter'){sendKey(66);return}if(e.key==='Backspace'){sendKey(67);return}if(e.key.length===1){kbuf+=e.key;clearTimeout(kt);kt=setTimeout(function(){var v=kbuf;kbuf='';if(v)post('/api/text',{name:DEV,text:v})},220)}});
function openSheet(id){
  document.querySelectorAll('.sheet').forEach(function(s){
    if(s.id===id)return;
    // 带 iframe 的面板（相机/传感器）关掉时改停到屏幕外，否则 display:none 会把
    // 里面的推流与传感器定时器节流/掐断。
    if(s.classList.contains('show')&&(s.id==='camsheet'||s.id==='sensesheet')){s.classList.remove('show');s.classList.add('park');return;}
    s.classList.remove('show');
  });
  var t=document.getElementById(id);if(t){t.classList.remove('park');t.classList.add('show');}
}
function closeSheets(){document.querySelectorAll('.sheet').forEach(function(s){s.classList.remove('show')})}
var CAM={node:'back',busy:false,url:''};
var SEN={url:''};
function camNode(){return camNodeFor(DEV,CAM.node);}
function camNodeFor(dev,which){var n=parseInt(String(dev).replace(/[^0-9]/g,''),10)||1;var base=20+2*(n-1);return '/dev/video'+(which==='front'?base+1:base);}
function camStatus(el,msg,cls){if(!el)return;el.textContent=msg;el.className='camstat'+(cls?' '+cls:'');}
function camSheetTab(name){document.querySelectorAll('.camtab button').forEach(function(b){b.classList.toggle('on',b.getAttribute('data-tab')===name)});['live','image','video'].forEach(function(k){document.getElementById('pane-'+k).classList.toggle('on',k===name)});}
function camUrl(){return '/cam?device='+encodeURIComponent(DEV)+'&node='+encodeURIComponent(camNode())+'&auto=1'+(K?'&k='+encodeURIComponent(K):'')+(new URLSearchParams(location.search).get('test')==='1'?'&test=1':'')+(new URLSearchParams(location.search).get('jpeg')==='1'?'&jpeg=1':'');}
function camOpen(){var f=document.getElementById('camframe');var u=camUrl();if(CAM.url!==u){f.src=u;CAM.url=u;}f.classList.add('show');document.getElementById('camlink').href=u;var s=document.getElementById('camsheet');s.classList.remove('park');openSheet('camsheet');cfgPoll();}
/* 只收起面板，不销毁 iframe：手机摄像头的 WHIP 推流、传感器同步都活在 iframe 里，
   src 一清就全停了。要真的停，用面板里的「恢复空闲」。 */
function camHide(){var s=document.getElementById('camsheet');s.classList.remove('show','mini');s.classList.add('park');closeSheets();}
function camUpload(file,kind){var el=document.getElementById(kind==='image'?'imgstat':'vidstat');return new Promise(function(res,rej){var x=new XMLHttpRequest();x.open('POST','/api/cam/file');x.setRequestHeader('X-Filename',encodeURIComponent(file.name));x.upload.onprogress=function(e){if(e.lengthComputable)camStatus(el,'上传中 '+Math.round(e.loaded/e.total*100)+'%');};x.onload=function(){var j={};try{j=JSON.parse(x.responseText)}catch(e){}if(j&&j.path)res(j);else rej(new Error((j&&j.error)||'上传失败'));};x.onerror=function(){rej(new Error('网络错误'));};x.send(file);});}
function camInjectFile(file,kind){if(!file)return;if(CAM.busy){toast('上一个注入还在进行中');return;}CAM.busy=true;var el=document.getElementById(kind==='image'?'imgstat':'vidstat');camStatus(el,file.name+' · 准备上传…');camUpload(file,kind).then(function(j){camStatus(el,'已上传，正在切源…');return post('/api/cam/start',{name:DEV,node:camNode(),path:j.path,kind:kind});}).then(function(j){if(j&&(j.ok||j.queued)){camStatus(el,'已注入 '+(kind==='image'?'图片':'视频')+'，云手机相机已切源',true);toast('已切换注入源');}else{camStatus(el,'注入失败：'+((j&&j.error)||'未知错误'),false);}}).catch(function(e){camStatus(el,'失败：'+(e&&e.message?e.message:e),false);}).then(function(){CAM.busy=false;setTimeout(cfgPoll,600);});}
document.querySelectorAll('.camtab button').forEach(function(b){b.addEventListener('click',function(){camSheetTab(b.getAttribute('data-tab'))});});
document.querySelectorAll('.seg [data-cnode]').forEach(function(b){b.addEventListener('click',function(){CAM.node=b.getAttribute('data-cnode');document.querySelectorAll('.seg [data-cnode]').forEach(function(x){x.classList.toggle('on',x===b)});if(document.getElementById('camframe').classList.contains('show'))camOpen();toast((CAM.node==='front'?'前置':'后置')+'摄像头 · '+camNode());});});
document.getElementById('camidle').addEventListener('click',function(){post('/api/cam/stop',{name:DEV}).then(function(){toast('已恢复空闲画面');camStatus(document.getElementById('imgstat'),'已恢复空闲');setTimeout(cfgPoll,600);});});
document.getElementById('camimg').addEventListener('change',function(e){camInjectFile(e.target.files&&e.target.files[0],'image');});
document.getElementById('camvideo').addEventListener('change',function(e){camInjectFile(e.target.files&&e.target.files[0],'video');});
document.getElementById('cambtn').addEventListener('click',function(){openSheet('camsheet');camSheetTab('live');});

/* ── 已配置项外显：贴条 + 按钮角标 ─────────────────────────────────────── */
var CFG={cam:null,loc:null,sen:null,senAge:99};
var CINKIND={image:'图片',video:'视频',stream:'直播',jpeg:'图片',rtc:'直播'};
function cfgBadge(id,text,cls){
  var e=document.getElementById(id);if(!e)return;
  if(!text){e.className='bdg';e.textContent='';return;}
  e.className='bdg '+(cls||'on');e.textContent=text;
}
function cfgChip(k,v,live){return '<div class="ci'+(live?' live':'')+'">'+k+' <b>'+v+'</b></div>';}
function cfgRender(){
  var bar=document.getElementById('cfgbar');if(!bar)return;
  var st=CFG.cam||{}, kind=st.kind||'', streaming=(st.state==='streaming');
  var camTxt=streaming&&CINKIND[kind]?CINKIND[kind]:(st.state==='error'?'异常':'空闲');
  cfgBadge('camBdg',streaming&&CINKIND[kind]?CINKIND[kind]:'',streaming?'good':'on');
  var loc=CFG.loc||{},locTxt=loc.ok&&loc.manual?(loc.source==='phone-gps'?'手机定位':'手动定位'):'板子默认';
  cfgBadge('locBdg',loc.ok&&loc.manual?(loc.source==='phone-gps'?'手机':'手动'):'',loc.ok&&loc.manual?'good':'on');
  var sen=CFG.sen||{},n=(sen.on||[]).length,senLive=sen.ok&&n>0&&CFG.senAge<3;
  var senTxt=n?(senLive?'同步 '+n+'/10':'已断开 '+n+'/10'):'未开启';
  cfgBadge('senBdg',n?(n+'/10'):'',senLive?'good':(n?'warn':'on'));
  bar.innerHTML=cfgChip('相机',camTxt,streaming)+cfgChip('定位',locTxt,loc.ok&&loc.manual)+cfgChip('传感器',senTxt,senLive);
}
function cfgPoll(){
  var j1=fetch('/api/cam/status?name='+encodeURIComponent(DEV),{cache:'no-store'}).then(function(r){return r.json()}).then(function(j){CFG.cam=(j&&j.state)||null;}).catch(function(){});
  var j2=fetch('/api/location?name='+encodeURIComponent(DEV),{cache:'no-store'}).then(function(r){return r.json()}).then(function(j){CFG.loc=j||null;}).catch(function(){});
  var j3=fetch('/api/sensors/live?name='+encodeURIComponent(DEV),{cache:'no-store'}).then(function(r){return r.json()}).then(function(j){CFG.sen=j||null;CFG.senAge=(j&&typeof j.age==='number')?j.age:99;}).catch(function(){});
  Promise.all([j1,j2,j3]).then(cfgRender).catch(function(){});
}
cfgPoll();setInterval(cfgPoll,4000);
function senseOpen(){var f=document.getElementById('senseframe');var qs=new URLSearchParams(location.search);if(!SEN.url){SEN.url='/ms?device='+encodeURIComponent(DEV)+'&auto=1'+(K?'&k='+encodeURIComponent(K):'')+(qs.get('test')==='1'?'&test=1':'');f.src=SEN.url;}var s=document.getElementById('sensesheet');s.classList.remove('park');openSheet('sensesheet');cfgPoll();}
document.getElementById('sensebtn').addEventListener('click',senseOpen);
document.getElementById('senseclose').addEventListener('click',function(){var s=document.getElementById('sensesheet');s.classList.remove('show');s.classList.add('park');closeSheets();});
document.getElementById('camclose').addEventListener('click',camHide);
document.getElementById('camstart').addEventListener('click',camOpen);
function locBadge(){
  var b=document.getElementById('locbadge');
  if(!b)return;
  if(LOC.manual){b.className='lbadge warn';b.textContent='人为设定 · '+(LOC.source||'手动')+(LOC.ts?' · '+new Date(LOC.ts*1000).toLocaleString():'');}
  else{b.className='lbadge';b.textContent='板子默认（出口网线）';}
}
function locLoad(){
  return fetch('/api/location?name='+encodeURIComponent(DEV),{cache:'no-store'}).then(function(r){return r.json()}).then(function(j){
    if(j&&j.ok){
      document.getElementById('lat').value=j.lat;document.getElementById('lon').value=j.lon;document.getElementById('alt').value=j.alt;
      LOC.manual=!!j.manual;LOC.source=j.source||'';LOC.ts=j.ts||0;locBadge();
      document.getElementById('loclog').textContent='当前：'+j.lat+', '+j.lon+' · '+j.alt+'m';
    }else document.getElementById('loclog').textContent=(j&&j.error)||'读取失败';
  }).catch(function(){document.getElementById('loclog').textContent='读取失败'})
}
function locApply(lat,lon,alt,quiet,source){
  document.getElementById('loclog').textContent='应用中…';
  return post('/api/location/manual',{name:DEV,lat:Number(lat),lon:Number(lon),alt:Number(alt||0),source:source||'manual'}).then(function(j){
    if(j&&j.ok){
      LOC.manual=true;LOC.source=source||'manual';LOC.ts=Math.round(Date.now()/1000);locBadge();
      document.getElementById('loclog').textContent='已应用（人为设定）：'+j.lat+', '+j.lon+' · '+j.alt+'m';
      if(!quiet)toast('定位已切换（已标记人为设定）');
      setTimeout(cfgPoll,600);
      return true;
    }
    document.getElementById('loclog').textContent=(j&&j.error)||'应用失败';return false;
  })
}
function geoOnce(){
  if(!navigator.geolocation){toast('浏览器不支持定位');return Promise.resolve(null)}
  return new Promise(function(res){navigator.geolocation.getCurrentPosition(function(p){
    var lat=p.coords.latitude,lon=p.coords.longitude,alt=p.coords.altitude;
    if(alt==null||isNaN(alt))alt=0;
    document.getElementById('lat').value=lat.toFixed(6);
    document.getElementById('lon').value=lon.toFixed(6);
    document.getElementById('alt').value=Math.round(alt);
    locApply(lat.toFixed(6),lon.toFixed(6),Math.max(0,Math.round(alt)),false,'phone-gps').then(function(ok){res(ok?{lat:lat,lon:lon}:null)});
  },function(e){
    var m='定位失败：'+((e&&e.message)||'权限被拒绝');
    var secure=(location.protocol==='https:'||location.hostname==='localhost'||location.hostname==='127.0.0.1');
    if(!secure)m='当前是 HTTP 页面，浏览器不提供定位；请用二维码里的 HTTPS 地址打开';
    else if(e&&e.code===1)m='请允许此网页获取位置（地址栏左侧锁形图标 → 位置 → 允许）';
    toast(m);document.getElementById('loclog').textContent=m;res(null);
  },{enableHighAccuracy:true,timeout:12000,maximumAge:0})});
}
document.getElementById('myloc').addEventListener('click',function(){toast('正在获取你的位置…');geoOnce()});
document.getElementById('boardloc').addEventListener('click',function(){
  toast('正在取板子出口位置…');
  fetch('/api/location/board',{cache:'no-store'}).then(function(r){return r.json()}).then(function(j){
    if(j&&j.ok){
      document.getElementById('lat').value=Number(j.lat).toFixed(6);document.getElementById('lon').value=Number(j.lon).toFixed(6);
      document.getElementById('loclog').textContent='板子默认位置：'+(j.city||'')+' '+Number(j.lat).toFixed(4)+', '+Number(j.lon).toFixed(4)+'（'+(j.iface||'')+'）';
      toast('这是板子出口位置；要点「应用」才写入云手机');
    }else toast((j&&j.error)||'读取板子位置失败');
  }).catch(function(){toast('读取板子位置失败')});
});
locLoad();
document.getElementById('locbtn').addEventListener('click',function(){openSheet('locsheet');locLoad()});
document.getElementById('locclose').addEventListener('click',function(){closeSheets()});
document.getElementById('locrefresh').addEventListener('click',locLoad);
document.getElementById('locapply').addEventListener('click',function(){locApply(document.getElementById('lat').value,document.getElementById('lon').value,document.getElementById('alt').value)});
document.querySelectorAll('.cities button').forEach(function(b){b.addEventListener('click',function(){var a=b.getAttribute('data-city').split(',');document.getElementById('lat').value=a[0];document.getElementById('lon').value=a[1];document.getElementById('alt').value=a[2];locApply(a[0],a[1],a[2])})});
document.getElementById('full').addEventListener('click',function(){var d=document.documentElement;if(d.requestFullscreen)d.requestFullscreen().catch(function(){toast('当前浏览器不支持全屏')});else toast('iOS 可点分享 → 添加到主屏幕')});
window.addEventListener('error',function(e){console.warn('m err',e.message)});
function fitBars(){var tb=document.getElementById('topbar'),dk=document.getElementById('dock'),rt=document.documentElement;if(tb&&tb.offsetHeight)rt.style.setProperty('--topbar-h',tb.offsetHeight+'px');if(dk&&dk.offsetHeight)rt.style.setProperty('--dock-h',dk.offsetHeight+'px')}
fitBars();window.addEventListener('resize',fitBars);window.addEventListener('orientationchange',function(){setTimeout(fitBars,120)});window.addEventListener('load',fitBars);
connect();inputWS();
})();
</script></body></html>"""


def sh(args, timeout=30):
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or b"").decode("utf-8", "replace"), (r.stderr or b"").decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def dexec(name, args, timeout=20):
    return sh(["docker", "exec", name] + args, timeout=timeout)


def get_gnss_location(name):
    rc, out, err = dexec(name, ["cat", "/data/local/tmp/g98_gnss.txt"], timeout=10)
    if rc != 0:
        return {"ok": False, "error": (out + err).strip()[:160] or "GNSS 坐标文件不可读"}
    parts = (out or "").strip().split(",")
    try:
        lat = float(parts[0]); lon = float(parts[1]); alt = float(parts[2]) if len(parts) > 2 else 0.0
    except (ValueError, IndexError):
        return {"ok": False, "error": "GNSS 坐标文件格式错误"}
    return {"ok": True, "lat": lat, "lon": lon, "alt": alt, "raw": (out or "").strip()[:120]}


_NETGEO = {"t": 0.0, "v": None}


def board_route():
    """板子默认出口：源 IP 和网卡名（有线 eth0 优先）。"""
    src = iface = ""
    try:
        rc, out, _ = sh(["sh", "-c", "ip route get 1.1.1.1 2>/dev/null | head -1"], timeout=6)
        m = re.search(r"dev (\S+)", out or "")
        if m:
            iface = m.group(1)
        m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out or "")
        if m:
            src = m.group(1)
    except Exception:  # noqa: BLE001
        pass
    if not iface:
        try:
            rc, out, _ = sh(["sh", "-c", "ip route show default 2>/dev/null | head -3"], timeout=6)
            m = re.search(r"dev (\S+)", out or "")
            if m:
                iface = m.group(1)
        except Exception:  # noqa: BLE001
            pass
    return {"ip": src, "iface": iface}


def net_geo():
    """板子当前所在位置 = 默认出口（网线）的公网 IP 城市，作为云手机开机默认定位。"""
    now = time.time()
    if _NETGEO["v"] and now - _NETGEO["t"] < 600:
        return _NETGEO["v"]
    route = board_route()
    try:
        import urllib.request as _u
        req = _u.Request("https://api.ip.sb/geoip", headers={"User-Agent": "g98-console/1.0"})
        with _u.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        lat = float(data.get("latitude")); lon = float(data.get("longitude"))
        out = {"ok": True, "lat": lat, "lon": lon, "alt": 0.0,
               "city": str(data.get("city") or ""), "region": str(data.get("region") or ""),
               "ip": str(data.get("ip") or route.get("ip") or ""), "iface": route.get("iface") or ""}
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": str(exc)[:120], "ip": route.get("ip") or "", "iface": route.get("iface") or ""}
    _NETGEO["t"] = now
    _NETGEO["v"] = out
    return out


def get_gnss_manual(name):
    """读取人为标记：有就说明当前坐标是人改过的，开机默认定位不会覆盖。"""
    rc, out, err = dexec(name, ["sh", "-c", "cat /data/local/tmp/g98_gnss_manual 2>/dev/null"], timeout=8)
    txt = (out or "").strip()
    if rc != 0 or not txt:
        return {"manual": False}
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    info = {"manual": True, "source": lines[1] if len(lines) > 1 else "manual",
            "ts": int(lines[2]) if len(lines) > 2 and lines[2].isdigit() else 0}
    if lines and "," in lines[0]:
        parts = lines[0].split(",")
        try:
            info.update({"lat": float(parts[0]), "lon": float(parts[1]),
                         "alt": float(parts[2]) if len(parts) > 2 else 0.0})
        except (ValueError, IndexError):
            pass
    return info


def set_gnss_location(name, lat, lon, alt, manual=False, source="auto"):
    payload = "%.7f,%.7f,%.2f" % (lat, lon, alt)
    rc, out, err = dexec(name, ["sh", "-c", "printf '%s\\n' '" + payload + "' > /data/local/tmp/g98_gnss.txt"], timeout=10)
    if rc == 0:
        # 人为改过的坐标要标记下来，开机默认定位（板子出口位置）就不会覆盖它。
        if manual:
            body = payload + "\\n" + (source or "manual") + "\\n" + str(int(time.time()))
            mark = "printf '%b\\n' '" + body + "' > /data/local/tmp/g98_gnss_manual"
        else:
            mark = "rm -f /data/local/tmp/g98_gnss_manual"
        dexec(name, ["sh", "-c", mark], timeout=8)
    return rc, (out + err).strip(), payload


# ---- 容器内触摸注入守护进程（真实 MotionEvent 流，替代每次 input tap/swipe） ----
INPUTD_JAR = "/root/inputd.jar"
INPUTD_ENV = "/tmp/inputd.env"
_DAEMONS = {}
_DAEMON_LOCK = threading.Lock()


def _ensure_input_env():
    """app_process 需要 init 那套 ANDROID_*/BOOTCLASSPATH，从 Agent 进程抄一份。"""
    if os.path.exists(INPUTD_ENV):
        return True
    try:
        names = [r.get("name") for r in list_containers() if r.get("state") == "running"]
    except Exception:  # noqa: BLE001
        names = []
    for n in names:
        if not n:
            continue
        rc, out, _ = dexec(n, ["pidof", "com.salespartner.workphone.agent"], timeout=10)
        pid = (out or "").split()
        if rc != 0 or not pid:
            continue
        rc, env, _ = dexec(n, ["sh", "-c", "tr '\\0' '\\n' < /proc/%s/environ" % pid[0]], timeout=10)
        keep = [l for l in (env or "").splitlines()
                if l.startswith("ANDROID_") or l.startswith("BOOTCLASSPATH=")
                or l.startswith("SYSTEMSERVERCLASSPATH=")]
        if keep:
            with open(INPUTD_ENV, "w") as f:
                f.write("\n".join(keep) + "\nCLASSPATH=/data/local/tmp/inputd.jar\n")
            return True
    return False


def input_daemon(name):
    with _DAEMON_LOCK:
        d = _DAEMONS.get(name)
        if d is not None and d.poll() is None:
            return d
        if not _ensure_input_env() or not os.path.exists(INPUTD_JAR):
            return None
        rc, _, _ = dexec(name, ["test", "-f", "/data/local/tmp/inputd.jar"], timeout=10)
        if rc != 0:
            sh(["docker", "cp", INPUTD_JAR, "%s:/data/local/tmp/inputd.jar" % name], timeout=30)
        try:
            _if = open("/tmp/inputd-%s.log" % name, "ab", buffering=0)
            p = subprocess.Popen(
                ["docker", "exec", "-i", "--env-file", INPUTD_ENV, name, "sh", "-c",
                 "app_process /data/local/tmp inputd.InputDaemon"],
                stdin=subprocess.PIPE, stdout=_if, stderr=subprocess.STDOUT,
                start_new_session=True)
        except Exception:  # noqa: BLE001
            return None
        try:
            _if.close()
        except Exception:  # noqa: BLE001
            pass
        _DAEMONS[name] = p
        _alog("inputd start %s pid=%s" % (name, p.pid))
        return p


_SHELLS = {}
_SHELL_LOCK = threading.Lock()


class _CShell:
    """容器内常驻 sh：文字/按键走它，省掉每次 docker exec 的 200~300ms 启动开销。"""

    def __init__(self, name):
        self.name = name
        self.lock = threading.Lock()
        self.seq = 0
        self.p = subprocess.Popen(
            ["docker", "exec", "-i", name, "sh"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0)
        self.buf = b""

    def alive(self):
        return self.p.poll() is None

    def run(self, cmd, timeout=6.0):
        if not self.alive():
            raise RuntimeError("shell dead")
        with self.lock:
            self.seq += 1
            tag = b"__G98_%d__" % self.seq
            payload = cmd.encode("utf-8") + b"\nprintf '\\n%s\\n' " + tag + b"\n"
            self.p.stdin.write(payload)
            self.p.stdin.flush()
            end = time.time() + timeout
            while tag not in self.buf:
                if time.time() > end:
                    raise RuntimeError("shell timeout")
                chunk = self.p.stdout.read(1) if False else self.p.stdout.read1(65536)
                if not chunk:
                    raise RuntimeError("shell eof")
                self.buf += chunk
            out, _, rest = self.buf.partition(tag)
            self.buf = rest
            return out.decode("utf-8", "replace")


def cshell(name):
    with _SHELL_LOCK:
        sh_ = _SHELLS.get(name)
        if sh_ is None or not sh_.alive():
            sh_ = _CShell(name)
            _SHELLS[name] = sh_
        return sh_


def cshell_run(name, cmd, timeout=6.0):
    for attempt in (0, 1):
        try:
            return True, cshell(name).run(cmd, timeout=timeout)
        except Exception:  # noqa: BLE001
            with _SHELL_LOCK:
                old = _SHELLS.pop(name, None)
            if old is not None:
                try:
                    old.p.kill()
                except Exception:  # noqa: BLE001
                    pass
            if attempt:
                return False, ""
    return False, ""


def touch_send(name, line):
    p = input_daemon(name)
    if p is None:
        return False
    if p.poll() is not None:
        drop_daemon(name)
        p = input_daemon(name)
        if p is None:
            return False
    try:
        p.stdin.write((line + "\n").encode())
        p.stdin.flush()
        _alog("inputd send %s %r pid=%s" % (name, line, p.pid))
        return True
    except Exception:  # noqa: BLE001
        with _DAEMON_LOCK:
            _DAEMONS.pop(name, None)
        return False


def drop_daemon(name):
    with _DAEMON_LOCK:
        p = _DAEMONS.pop(name, None)
    if p is not None and p.poll() is None:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass


CAM_DIR = "/root/camin"
CAM_JOBS = {}
CAM_LOCK = threading.Lock()
CAM_SEQ = threading.Lock()
FFMPEG_BIN = "/root/ffmpeg" if os.path.exists("/root/ffmpeg") else "ffmpeg"
CAM_DEFAULT_NODE = "/dev/video11"

# 手机传感器实时同步：浏览器 /ws/sensor → 本文件的 180 字节小端结构 → 容器内假传感器 HAL 轮询。
SENSOR_SLOTS = [("acc", 0), ("gyro", 1), ("mag", 2), ("rv", 3), ("grav", 4),
                ("lin", 5), ("light", 6), ("prox", 7), ("pressure", 8), ("step", 9)]
SENSOR_KEYS = dict(SENSOR_SLOTS)
SENSOR_MAGIC = 0x4739384C
SENSOR_NAMES = {"acc": "加速度计", "gyro": "陀螺仪", "mag": "磁力计", "rv": "旋转矢量",
                "grav": "重力", "lin": "线性加速度", "light": "光线", "prox": "距离",
                "pressure": "气压", "step": "计步"}


def sensor_live_path(name):
    return "/srv/redroid/%s/local/tmp/g98-sensors-live.bin" % name


def sensor_write_live(name, on, vals):
    """写 HAL 轮询的实时传感器文件：magic/ver/mask/wallMs + 10 槽 x 4 float。
    mask 位为 1 的槽位用手机值，为 0 的槽位 HAL 继续回放真机 trace。"""
    mask = 0
    slots = [0.0] * 40
    for k in (on or ()):
        idx = SENSOR_KEYS.get(str(k))
        if idx is None:
            continue
        mask |= 1 << idx
        arr = (vals or {}).get(str(k)) or []
        for j in range(min(4, len(arr))):
            try:
                slots[idx * 4 + j] = float(arr[j])
            except (TypeError, ValueError):
                slots[idx * 4 + j] = 0.0
    blob = struct.pack("<iiIq40f", SENSOR_MAGIC, 1, mask, int(time.time() * 1000), *slots)
    path = sensor_live_path(name)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o666)
    os.replace(tmp, path)
    return mask


def sensor_live_state(name):
    path = sensor_live_path(name)
    try:
        with open(path, "rb") as f:
            buf = f.read(180)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": path}
    if len(buf) < 24:
        return {"ok": False, "error": "short file", "path": path}
    magic, ver, mask, wall = struct.unpack_from("<iiIq", buf, 0)
    age = time.time() - wall / 1000.0
    return {"ok": magic == SENSOR_MAGIC and ver == 1, "mask": mask, "age": round(age, 2),
            "on": [k for k, i in SENSOR_SLOTS if mask & (1 << i)], "path": path}


def cam_nodes_for(name):
    """每台云手机独占一对 v4l2loopback 节点：wx1=video20/21 … wx6=video30/31。
    以前 5 台容器共用 video10/11，任何一台开相机都会和别人抢同一条 v4l2 队列。"""
    m = re.match(r"^wx(\d+)$", name or "")
    idx = min(max(int(m.group(1)) if m else 1, 1), 6)
    base = 20 + 2 * (idx - 1)
    return "/dev/video%d" % base, "/dev/video%d" % (base + 1)


def cam_norm_node(name, node):
    """把逻辑节点名映射到本实例真实节点：/dev/video10=后置、/dev/video11=前置。
    UI 与二维码历史上写死 video10/11，这里集中做一次映射，避免改所有前端。"""
    back, front = cam_nodes_for(name)
    if not node or node in ("back", "/dev/video10"):
        return back
    if node in ("front", "/dev/video11"):
        return front
    if re.match(r"^/dev/video(2\d|3[01])$", node or ""):
        return node
    return back


def cam_pair_node(name, node):
    """同一实例的另一路节点（后置 <-> 前置）；不是本实例的节点时返回 None。
    相机 App 会记住上次用的摄像头，前后摄不同步时切过去就看不到注入内容。"""
    back, front = cam_nodes_for(name)
    if node == back:
        return front
    if node == front:
        return back
    return None
CAM_PROVIDER = "vendor.camera-provider-2-4-legacy"


def cam_open_nodes(name):
    script = ("for p in /proc/[0-9]*/fd/*; do "
              "l=$(readlink $p 2>/dev/null) || continue; "
              "case $l in /dev/video1[0-9]) echo $l;; esac; done | sort -u")
    try:
        rc, out, err = dexec(name, ["sh", "-c", script], timeout=10)
    except Exception:  # noqa: BLE001
        out = ""
    return sorted({x.strip() for x in (out or "").splitlines()
                   if re.match(r"^/dev/video[0-9]{1,2}$", x.strip())})


def cam_active_node(name):
    back, front = cam_nodes_for(name)
    nodes = cam_open_nodes(name)
    if len(nodes) == 1:
        return nodes[0]
    if front in nodes:
        return front
    if back in nodes:
        return back
    return nodes[0] if nodes else back


def cam_idle(action):
    try:
        rc, out, err = sh(["systemctl", action, "g98-camidle"], timeout=15)
        return (out + err).strip()[:160]
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:160]


def cam_idle_ensure(timeout=5.0):
    """camidle 单元没跑起来时，两个节点会只剩一路空闲源，下一次 cam start 就只注册到
    1 个 camera device（camera 0 落到 video11），相机 App 会报 Can't connect to the camera。
    这里确认 systemd 单元真的回到 active，并保证两个节点都有 producer。"""
    if cam_camd_health():
        cam_ensure_nodes()
        return "camd"
    state = ""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            rc, out, err = sh(["systemctl", "is-active", "g98-camidle"], timeout=10)
        except Exception:  # noqa: BLE001
            out = ""
        state = (out or "").strip()
        if state == "active":
            break
        cam_idle("start")
        time.sleep(0.4)
    cam_ensure_nodes()
    return state


def cam_keepf(node, value):
    tool = "/root/keepf" if os.path.exists("/root/keepf") else "/root/v4l2keep"
    if not os.path.exists(tool):
        return ""
    try:
        rc, out, err = sh([tool, node, str(value)], timeout=10)
        return (out + err).strip()[:120]
    except Exception:  # noqa: BLE001
        return ""


CAM_CAMD_URL = os.environ.get("G98_CAMD_URL", "http://127.0.0.1:8100")


def cam_camd_call(path, payload=None, timeout=3.0):
    """g98-camd 是本机常驻的帧路由守护进程：它一直持有 /dev/video1x 并持续出帧，
    换源只需一次 HTTP 调用，不用重启 CameraProvider / Camera App。"""
    url = CAM_CAMD_URL + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=("POST" if payload is not None else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def cam_camd_health():
    try:
        return bool(cam_camd_call("/health", timeout=1.0).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def cam_camd_nodes():
    try:
        body = cam_camd_call("/state", timeout=2.0)
        return {n.get("node"): n for n in body.get("nodes") or []}
    except Exception:  # noqa: BLE001
        return {}


def cam_camd_set(node, kind, path=None, timeout=6.0, geom=None):
    payload = {"node": node, "kind": kind}
    if path:
        payload["path" if kind in ("image", "video") else "url"] = path
    if geom:
        for key in ("fit", "rotate", "mirror", "ystretch"):
            if geom.get(key) is not None:
                payload[key] = geom[key]
    return cam_camd_call("/set", payload, timeout=timeout)


def cam_camd_node_state(node, timeout=4.0):
    """Return camd's snapshot for one node (geometry, source, counters)."""
    try:
        with urllib.request.urlopen(CAM_CAMD_URL + "/state", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None
    for item in data.get("nodes") or []:
        if item.get("node") == node:
            return item
    return None


def cam_camd_kind(kind):
    if kind == "stream":
        return "rtsp"
    if kind == "webcam":
        return "jpeg"
    return kind


def cam_camd_frame(node, data, timeout=2.0):
    """把一帧 JPEG 推给 camd 的 jpeg 解码器（浏览器降级通道）。"""
    req = urllib.request.Request(CAM_CAMD_URL + "/frame?node=" + quote(node), data=data,
                                 headers={"Content-Type": "image/jpeg"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return bool(json.loads(resp.read().decode("utf-8", "replace")).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def _cam_producer_pid(node):
    rc, out, _ = sh(["ps", "-eo", "pid,args"], timeout=10)
    for line in (out or "").splitlines():
        if "ffmpeg" in line and node in line and "-f v4l2" in line:
            try:
                return int(line.split()[0])
            except (ValueError, IndexError):
                pass
    return None


def cam_stop(node):
    killed = []
    pid = _cam_producer_pid(node)
    while pid and len(killed) < 8:
        sh(["kill", "-9", str(pid)], timeout=10)
        killed.append(pid)
        time.sleep(0.2)
        pid = _cam_producer_pid(node)
    with CAM_LOCK:
        CAM_JOBS.pop(node, None)
    return killed


def cam_start(node, path, kind, out_w=0, out_h=0):
    if not out_w or not out_h:
        out_w, out_h = ((1280, 720) if kind == "stream" else (640, 480))
    cam_stop(node)
    log = "/tmp/cam-%s.log" % os.path.basename(node)
    args = [FFMPEG_BIN, "-hide_banner", "-loglevel", "warning"]
    if kind == "image":
        args += ["-loop", "1", "-framerate", "15", "-i", path]
    elif kind == "stream":
        if path.lower().startswith(("rtsp://", "rtsps://")):
            args += ["-rtsp_transport", "tcp"]
        args += ["-i", path]
    else:
        args += ["-re", "-stream_loop", "-1", "-i", path]
    args += ["-an", "-vf", "scale=%d:%d,fps=15" % (out_w, out_h), "-pix_fmt", "yuv420p",
             "-s", "%dx%d" % (out_w, out_h), "-f", "v4l2", node]
    cam_keepf(node, 0)
    logf = open(log, "wb")
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=logf,
                            stderr=subprocess.STDOUT, start_new_session=True)
    vfile = "/sys/devices/virtual/video4linux/%s/format" % os.path.basename(node)
    seen = ""
    t0 = time.time()
    while time.time() - t0 < 6.0:
        try:
            with open(vfile) as fh:
                seen = fh.read().strip()
        except OSError:
            seen = ""
        if ("%dx%d" % (out_w, out_h)) in seen or proc.poll() is not None:
            break
        time.sleep(0.15)
    if kind == "stream":
        t0 = time.time()
        while time.time() - t0 < 5.0:
            try:
                with open(log, "rb") as fh:
                    head = fh.read(4096)
            except OSError:
                head = b""
            if b"Keyframe missing" in head or b"Stream #0" in head:
                break
            time.sleep(0.2)
        time.sleep(0.8)
    keep = cam_keepf(node, 1)
    with CAM_LOCK:
        CAM_JOBS[node] = {"path": path, "kind": kind, "pid": proc.pid, "log": log, "t": time.time()}
    return {"pid": proc.pid, "alive": proc.poll() is None, "format": seen[:60],
            "keep": keep, "size": "%dx%d" % (out_w, out_h), "log": cam_log_tail(log)}


def cam_log_tail(log, n=400):
    try:
        with open(log, "rb") as fh:
            data = fh.read()[-n * 4:]
        return data.decode("utf-8", "replace").strip().replace("\n", " | ")[-n:]
    except OSError:
        return ""


def cam_qr_png(text):
    try:
        proc = subprocess.run(["qrencode", "-t", "PNG", "-s", "8", "-m", "2", "-o", "-", text],
                              capture_output=True, timeout=8)
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except Exception:  # noqa: BLE001
        pass
    return None


CAM_STATE = {}
CAM_STATE_LOCK = threading.Lock()
CAM_TASKS = {}


def cam_state(name):
    with CAM_STATE_LOCK:
        return dict(CAM_STATE.get(name) or {})


def cam_state_set(name, **kw):
    with CAM_STATE_LOCK:
        cur = dict(CAM_STATE.get(name) or {})
        cur.update(kw)
        cur["ts"] = time.time()
        CAM_STATE[name] = cur
        return dict(cur)


# 一个动作要串很多 docker exec，容器卡住时线程可能长时间不返回。没有看门狗时
# CAM_TASKS 里留着一个活着的线程，后续所有 /api/cam/start 都直接 409，页面
# 永远停在"正在切换"。超过 CAM_TASK_MAX 秒视为失效，允许新动作接管。
CAM_TASK_MAX = float(os.environ.get("G98_CAM_TASK_MAX", "75"))


def cam_task_alive(name):
    ent = CAM_TASKS.get(name)
    if not ent:
        return False
    th = ent.get("th")
    age = time.time() - float(ent.get("t0") or 0.0)
    return bool(th and th.is_alive() and age < CAM_TASK_MAX)


def cam_busy(name):
    if cam_task_alive(name):
        return True
    ent = CAM_TASKS.get(name)
    if ent:
        with CAM_STATE_LOCK:
            CAM_TASKS.pop(name, None)
        cam_state_set(name, state="error", error="上一次动作超时（%ds）" % int(CAM_TASK_MAX))
        _alog("cam %s %s stale task cleared after %.1fs" % (
            ent.get("kind"), name, time.time() - float(ent.get("t0") or 0.0)))
    return False


def cam_submit(name, kind, fn):
    """摄像头动作放后台跑：HTTP 立刻返回，页面不再卡十几秒。"""
    if cam_busy(name):
        return False
    cam_state_set(name, state=("starting" if kind == "start" else "stopping"),
                  action=kind, msg="", error=None, t0=time.time())

    def _run():
        t0 = time.time()
        try:
            fn()
            _alog("cam %s %s done %.1fs" % (kind, name, time.time() - t0))
        except Exception as exc:  # noqa: BLE001
            cam_state_set(name, state="error", error=str(exc)[:180])
            _alog("cam %s %s failed %.1fs %s" % (kind, name, time.time() - t0, str(exc)[:160]))
        finally:
            with CAM_STATE_LOCK:
                ent = CAM_TASKS.get(name)
                if ent and ent.get("th") is th:
                    CAM_TASKS.pop(name, None)

    th = threading.Thread(target=_run, name="cam-%s-%s" % (kind, name), daemon=True)
    with CAM_STATE_LOCK:
        CAM_TASKS[name] = {"th": th, "t0": time.time(), "kind": kind}
    th.start()
    return True


def cam_do_start_camd(name, node, path, kind, t0, geom=None):
    """camd 常驻帧路由：只换源，不重启 CameraProvider，也不杀 Camera App。

    只有在没人看相机（App 没开、也没有微信之类在扫码）时才需要把相机拉起来；
    拉起来之后必须确认 Device 0 真的 open，否则用户看到的是
    "Camera error: Can't connect to the camera." 对话框，而状态里却写着 streaming。
    """
    cam_idle("stop")
    cam_camd_set(node, cam_camd_kind(kind), path, geom=geom)
    cam_keepf(node, 1)
    # 默认把同一个源也下发给另一路（后置 <-> 前置）。相机 App 记住上次摄像头时，
    # 只注入一路会出现“切过去全黑/看到旧画面”，像是前置拿到了后置的数据。
    both = True if not geom or geom.get("both") is None else bool(geom.get("both"))
    peer = cam_pair_node(name, node)
    if both and peer and kind in ("image", "video", "stream"):
        try:
            cam_camd_set(peer, cam_camd_kind(kind), path, geom=geom)
            cam_keepf(peer, 1)
        except Exception as exc:  # noqa: BLE001
            _alog("cam start(camd) %s peer %s failed %s" % (name, peer, str(exc)[:120]))
    t1 = time.time()
    top = cam_top_activity(name)
    scanning = (top or "").startswith("com.tencent.mm/")
    recovered = False
    if not cam_client_active(name) and not scanning:
        # 节点刚交到 camd 手上时，provider 可能是在旧 producer 名下初始化过的，
        # 这时 devices 会掉到 1 或 App 一开就 busy(-16)。先确保枚举到 2 路再拉 App。
        if cam_devices(name) < 2:
            cam_camera_stop(name)
            cam_camera_start(name, launch=False)
        if kind == "stream":
            # 手机直播流要等 WHIP 推上来才有帧；没帧时节点读不到 buffer，
            # App 必然打不开，此时走恢复流程只会白等 25s（观感就是“很卡”）。
            _fs = time.time() + 8.0
            while time.time() < _fs:
                _ns = cam_camd_node_state(node) or {}
                if (_ns.get("source_frames") or 0) > 0:
                    break
                time.sleep(0.4)
        cam_camera_start(name, launch=True)
        top = cam_wait_resumed(name, "com.android.camera2/", 6.0) or top
        # The app needs a moment to actually open the device; checking once
        # right after 'am start' made every cold start fall into a full camera
        # stack recovery (~25 s). Poll briefly first.
        _open_deadline = time.time() + (20.0 if kind == "stream" else 10.0)
        while time.time() < _open_deadline:
            if cam_device0_open(name) is True:
                break
            time.sleep(0.4)
        if cam_device0_open(name) is not True:
            _ns = cam_camd_node_state(node) or {}
            if kind == "stream" and (_ns.get("source_frames") or 0) == 0:
                # 源还没出帧：恢复相机栈也没用，保留状态让页面重试推流。
                _alog("cam start(camd) %s: waiting for stream frames (%s)" % (name, node))
                err = "等待手机直播流（节点还没有帧）"
            else:
                _alog("cam start(camd) %s: device0 not open after launch, recovering" % name)
                cam_recover(name)
                recovered = True
            top = cam_wait_resumed(name, "com.android.camera2/", 6.0) or top
    t2 = time.time()
    open0 = cam_device0_open(name)
    err = None
    if not scanning and open0 is False:
        err = "相机未就绪（Device 0 仍为 closed）"
    st = cam_state_set(name, state="streaming", node=node, kind=kind, pid=None,
                       size="1280x960", provider="camd",
                       resumed=cam_top_activity(name) or top,
                       devices=cam_devices(name), error=err, camd=True,
                       recovered=recovered, camera_open=open0,
                       duration=round(t2 - t0, 2),
                       steps={"source": round(t1 - t0, 2), "ui": round(t2 - t1, 2)})
    _alog("cam start(camd) %s %s %s %s dur=%.2fs resumed=%s open=%s recovered=%s" % (
        name, node, kind, os.path.basename(path or ""), t2 - t0, st.get("resumed"), open0, recovered))
    return st


def cam_do_start(name, node, path, kind, geom=None):
    with CAM_SEQ:
        t0 = time.time()
        if cam_camd_health():
            return cam_do_start_camd(name, node, path, kind, t0, geom=geom)
        top = cam_top_activity(name)
        # WebRTC 重连时不要重启 CameraProvider/Camera App；重启会让已有 client
        # 重新取流，恰好造成“每次有信号就重启、只看到一秒后卡住”。
        preserve = (kind == "stream" and cam_devices(name) >= 2 and cam_client_active(name))
        if preserve:
            # Keep the Android client/provider, but always stop the idle supervisor.
            # Leaving g98-camidle active lets it respawn a second writer immediately
            # after cam_start kills its old writer; v4l2loopback then alternates or
            # replays the last buffer, which looks like a ~1s video loop.
            cam_idle("stop")
            _alog("cam stream reconnect %s: preserve camera client %s" % (name, top))
            t1 = time.time()
            t2 = t1
        else:
            cam_idle("stop")
            t1 = time.time()
            cam_camera_stop(name)
            t2 = time.time()
        # A live stream writer owns the loopback queue.  Replacing it while the
        # Android client is in PREVIEW invalidates legacy-provider dma-buf handles
        # and causes the observed one-second replay / camera reconnect loop.
        # WebRTC reconnects use the same RTSP path, so keep the live writer.
        existing = None
        with CAM_LOCK:
            job = dict(CAM_JOBS.get(node) or {})
            pid = job.get("pid")
            if pid:
                try:
                    os.kill(int(pid), 0)
                    existing = job
                except (OSError, TypeError, ValueError):
                    existing = None
        if preserve and existing:
            info = {"pid": existing.get("pid"), "alive": True,
                    "size": existing.get("size"), "format": existing.get("format"),
                    "keep": "preserved", "log": cam_log_tail(existing.get("log", ""))}
            _alog("cam stream reconnect %s: keep writer pid=%s" % (name, existing.get("pid")))
        else:
            info = cam_start(node, path, kind)
        if not info.get("alive"):
            cam_idle("start")
            if not preserve:
                cam_camera_start(name, launch=False)
            cam_state_set(name, state="error", error="ffmpeg 退出", node=node)
            return
        t3 = time.time()
        cam_ensure_nodes()
        time.sleep(0.4)
        if preserve:
            svc = "running"
            t4 = t3
            resumed = top
        else:
            svc = cam_camera_start(name, launch=False)
            t4 = time.time()
            resumed = cam_resume_activity(name, top)
            resumed = cam_wait_resumed(name, "com.android.camera2/", 5.0)
            if not resumed.startswith("com.android.camera2/") and "com.tencent.mm/" not in (top or ""):
                cam_camera_start(name, launch=True)
                resumed = cam_wait_resumed(name, "com.android.camera2/", 5.0)
        t5 = time.time()
        st = cam_state_set(name, state="streaming", node=node, kind=kind, pid=info.get("pid"),
                           size=info.get("size"), provider=svc, resumed=resumed,
                           devices=cam_devices(name), error=None, duration=round(t5 - t0, 1),
                           steps={"idle": round(t1 - t0, 1), "provider_stop": round(t2 - t1, 1),
                                  "writer": round(t3 - t2, 1), "provider_start": round(t4 - t3, 1),
                                  "resume": round(t5 - t4, 1)})
        _alog("cam start %s %s %s %s pid=%s resumed=%s steps=%s" % (
            name, node, kind, os.path.basename(path), info.get("pid"), resumed, st.get("steps")))


def cam_do_stop(name, node=None):
    with CAM_SEQ:
        t0 = time.time()
        if cam_camd_health():
            # camd 模式下停流 = 把节点切回 idle，provider 与 App 都不动；
            # 不指定 node 时同样要把每一路都切回 idle，不能留一个节点在放视频。
            # camd 自己持有所有节点，直接问它要列表即可；cam_open_nodes 要在容器里
            # 扫一遍所有 /proc/*/fd（docker exec 每次 4-6 秒），camd 模式下纯属浪费。
            targets = [node] if node else list(cam_nodes_for(name))
            for extra in cam_camd_nodes().keys():
                if extra in targets or extra not in cam_nodes_for(name):
                    continue
                targets.append(extra)
            for target in targets:
                try:
                    cam_camd_set(target, "idle", timeout=4.0)
                except Exception as exc:  # noqa: BLE001
                    _alog("cam stop(camd) %s idle failed: %s" % (target, str(exc)[:120]))
            st = cam_state_set(name, state="idle", node=node, killed=[], camidle="camd",
                               devices=cam_devices(name), error=None, camd=True,
                               duration=round(time.time() - t0, 2),
                               steps={"source": round(time.time() - t0, 2)})
            _alog("cam stop(camd) %s %s dur=%.2fs" % (name, node, time.time() - t0))
            return st
        cam_camera_stop(name)
        t1 = time.time()
        # 不指定 node 时必须把每一路都收干净：只杀默认节点会留下另一路的 ffmpeg
        # 一直在往 v4l2 推（-loop 1 的图片源实测吃满 400% CPU，把整台板子拖垮）。
        targets = [node] if node else list(cam_nodes_for(name))
        for extra in cam_open_nodes(name):
            if extra not in targets:
                targets.append(extra)
        killed = []
        for target in targets:
            killed += cam_stop(target)
            cam_keepf(target, 0)
        t2 = time.time()
        idle = cam_idle_ensure()
        cam_provider_ensure(name)
        t3 = time.time()
        cam_state_set(name, state="idle", node=node, killed=killed, camidle=idle,
                      devices=cam_devices(name), error=None, duration=round(t3 - t0, 1),
                      steps={"provider_stop": round(t1 - t0, 1), "writer": round(t2 - t1, 1),
                             "provider_start": round(t3 - t2, 1)})
        _alog("cam stop %s %s killed=%s devices=%s dur=%.1fs" % (
            name, node, killed, cam_devices(name), t3 - t0))


def cam_camera_stop(name, prop=CAM_PROVIDER):
    dexec(name, ["am", "force-stop", "com.android.camera2"], timeout=20)
    dexec(name, ["setprop", "ctl.stop", prop], timeout=20)
    # 该 provider 由 init 托管，ctl.stop 后会自己回到 running，等 "stopped" 只会白等 6 秒；
    # 这里只确认它确实离开过 running 状态，剩下的交给 cam_camera_start 的 devices 检查。
    svc = ""
    t0 = time.time()
    while time.time() - t0 < 1.6:
        rc, out, _ = dexec(name, ["getprop", "init.svc." + prop], timeout=10)
        svc = (out or "").strip()
        if svc != "running":
            break
        time.sleep(0.2)
    time.sleep(0.3)
    return svc


def cam_camera_start(name, prop=CAM_PROVIDER, launch=True):
    dexec(name, ["setprop", "ctl.start", prop], timeout=20)
    svc = ""
    t0 = time.time()
    while time.time() - t0 < 8.0:
        rc, out, _ = dexec(name, ["getprop", "init.svc." + prop], timeout=10)
        svc = (out or "").strip()
        if svc == "running":
            break
        time.sleep(0.25)
    # 必须两个节点都注册好再拉起相机 App：只见 1 个设备时 camera 0 会落到 video11，
    # 之后 video10 再上 STREAMON 也来不及，App 会报 Can't connect to the camera。
    t0 = time.time()
    devs = 0
    while time.time() - t0 < 8.0:
        rc, out, _ = dexec(name, ["dumpsys", "media.camera"], timeout=20)
        m = re.search(r"Number of camera devices:\s*(\d+)", out or "")
        devs = int(m.group(1)) if m else 0
        if devs >= 2:
            break
        cam_ensure_nodes()
        time.sleep(0.45)
    if launch:
        target = "com.android.camera2/com.android.camera.CameraLauncher"
        for _ in range(3):
            dexec(name, ["am", "start", "-n", target], timeout=25)
            top = cam_wait_resumed(name, "com.android.camera2/", 6.0)
            if top.startswith("com.android.camera2/") and cam_client_active(name):
                break
            time.sleep(0.6)
    return svc

def cam_node_busy(node):
    try:
        rc, out, err = sh(["sh", "-c", "pgrep -af 'ffmpeg.*v4l2 %s'" % node], timeout=8)
    except Exception:  # noqa: BLE001
        return False
    return any("camidle.sh" not in ln and "pgrep" not in ln for ln in (out or "").splitlines())


def cam_idle_one(node):
    """给单个节点补一路空闲源（不占用 systemd 的 camidle 循环）。"""
    try:
        rc, out, err = sh(["/root/camidle.sh", node], timeout=15)
        return (out + err).strip()[:120]
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:120]


def cam_ensure_nodes(name=None):
    """HAL 只有在两个节点都 STREAMON 时才会注册 2 个摄像头。"""
    if cam_camd_health():
        # camd 常驻出帧，这里绝不能再起 ffmpeg：两路 producer 会互相抢 v4l2 队列。
        return
    for node in (cam_nodes_for(name) if name else
                 ("/dev/video10", "/dev/video11")):
        if not cam_node_busy(node):
            cam_idle_one(node)
        cam_keepf(node, 1)


def cam_top_activity(name):
    """返回真正的前台 Activity（dumpsys activity top 会列出 not running 的旧记录，不能用）。"""
    try:
        rc, out, _ = dexec(name, ["dumpsys", "activity", "activities"], timeout=20)
    except Exception:  # noqa: BLE001
        return ""
    for pat in (r"topResumedActivity=ActivityRecord\{\S+ u0 ([\w.]+)/([\w.$]+)",
                r"mResumedActivity:? ActivityRecord\{\S+ u0 ([\w.]+)/([\w.$]+)",
                r"ResumedActivity: ActivityRecord\{\S+ u0 ([\w.]+)/([\w.$]+)"):
        m = re.search(pat, out or "")
        if m:
            return m.group(1) + "/" + m.group(2)
    return ""


def cam_resume_activity(name, comp):
    if not comp:
        comp = "com.android.camera2/com.android.camera.CameraLauncher"
    if comp.startswith("com.tencent.mm/") and "scanner" in comp:
        # 微信扫码页不会自己重连相机：退回上一页再重新进入扫码，才能拿到新的 camera client
        dexec(name, ["input", "keyevent", "4"], timeout=15)
        time.sleep(1.4)
        dexec(name, ["am", "start", "-n",
                     "com.tencent.mm/.plugin.scanner.ui.BaseScanUI"], timeout=25)
        time.sleep(1.2)
        return "com.tencent.mm/.plugin.scanner.ui.BaseScanUI"
    pkg, _, cls = comp.partition("/")
    cls = cls.strip()
    if cls.startswith("."):
        cls = pkg + cls
    dexec(name, ["am", "start", "-n", pkg + "/" + cls], timeout=25)
    return comp


def cam_wait_resumed(name, prefix="com.android.camera2/", timeout=6.0):
    """等相机真的到前台：am start 返回 ok 不代表 Activity 已经 resumed。"""
    t0 = time.time()
    top = cam_top_activity(name)
    while time.time() - t0 < timeout:
        if top.startswith(prefix):
            return top
        time.sleep(0.35)
        top = cam_top_activity(name)
    return top


def cam_client_active(name):
    rc, out, _ = dexec(name, ["dumpsys", "media.camera"], timeout=20)
    return "package com.android.camera2" in (out or "")


def cam_device0_open(name):
    """Camera ID 0 是否已被客户端打开（dumpsys 的 'Device 0 is open/closed'）。"""
    rc, out, _ = dexec(name, ["dumpsys", "media.camera"], timeout=20)
    out = out or ""
    if re.search(r"Device 0 is open", out):
        return True
    if re.search(r"Device 0 is closed", out):
        return False
    return None


def cam_recover(name, prop=CAM_PROVIDER):
    """干净序列恢复相机：先杀 App，再依次重启 provider 与 cameraserver。

    camd 已经占住自己的节点时，如果 provider 是在节点被别的进程先打开的情况下
    起过的，open 会一直报 Device or resource busy (-16)，App 弹
    "Camera error: Can't connect to the camera."，之后 5 秒 CameraServiceWatchdog
    abort。只有按 force-stop → provider → cameraserver → am start 的顺序才会干净。
    """
    _alog("cam recover %s: clean restart of camera stack" % name)
    dexec(name, ["am", "force-stop", "com.android.camera2"], timeout=20)
    dexec(name, ["setprop", "ctl.restart", prop], timeout=20)
    time.sleep(2.0)
    dexec(name, ["setprop", "ctl.restart", "cameraserver"], timeout=20)
    time.sleep(2.0)
    t0 = time.time()
    while time.time() - t0 < 8.0:
        if cam_devices(name) >= 2:
            break
        time.sleep(0.4)
    return cam_camera_start(name, prop=prop, launch=True)


def cam_devices(name):
    rc, out, _ = dexec(name, ["dumpsys", "media.camera"], timeout=20)
    m = re.search(r"Number of camera devices:\s*(\d+)", out or "")
    return int(m.group(1)) if m else 0


def cam_provider_ensure(name, want=2, tries=3):
    """空闲态相机也要能打开：provider 没起就拉起，起早了只看到 video11 就重试。"""
    cam_ensure_nodes()
    time.sleep(0.8)
    for _ in range(max(1, tries)):
        if cam_devices(name) >= want:
            return True
        cam_camera_start(name, launch=False)
        if cam_devices(name) >= want:
            return True
        cam_camera_stop(name)
        time.sleep(0.6)
    return cam_devices(name) >= 1


_CAM_WD = {"t": 0.0}


def cam_watchdog(names):
    """没有注入任务时保证 camera provider 在线，否则微信扫码/相机 App 会打不开。"""
    # 注入中的 writer 可能因手机端停推/断网而退出；发现尸体立刻补回空闲源，
    # 否则 /dev/video10 没有 producer，相机 App 预览全黑、状态却停在 streaming。
    for node, job in list(CAM_JOBS.items()):
        pid = int(job.get("pid") or 0)
        if pid > 0 and os.path.exists("/proc/%d" % pid):
            continue
        _alog("cam writer died node=%s pid=%s -> idle" % (node, pid))
        with CAM_LOCK:
            CAM_JOBS.pop(node, None)
        cam_idle_one(node)
        for name in names:
            st = cam_state(name)
            if st.get("node") == node and st.get("state") == "streaming":
                cam_state_set(name, state="idle", error="推流中断，已恢复空闲源", pid=0)
    if CAM_JOBS or time.time() - _CAM_WD["t"] < 45:
        return
    if not CAM_SEQ.acquire(blocking=False):
        return
    _CAM_WD["t"] = time.time()
    try:
        for name in names:
            rc, out, _ = dexec(name, ["getprop", "init.svc." + CAM_PROVIDER], timeout=8)
            svc = (out or "").strip()
            if svc == "running" and cam_devices(name) >= 2:
                continue
            if svc != "running":
                cam_idle("start")
                time.sleep(1.0)
            cam_ensure_nodes()
            ok = cam_provider_ensure(name)
            _alog("cam watchdog %s svc=%s devices=%s ok=%s" % (name, svc, cam_devices(name), ok))
    except Exception:  # noqa: BLE001
        pass
    finally:
        CAM_SEQ.release()


def board_info():
    info = {"load": "-", "mem_used_gb": 0, "mem_total_gb": 0, "disk": "-", "uptime": "-"}
    try:
        info["load"] = "%.2f %.2f %.2f" % os.getloadavg()
    except OSError:
        pass
    try:
        mem = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0])
        info["mem_used_gb"] = round((mem["MemTotal"] - mem["MemAvailable"]) / 1048576, 1)
        info["mem_total_gb"] = round(mem["MemTotal"] / 1048576, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        info["disk"] = "%.0fG / %.0fG (%.0f%%)" % ((total - free) / 2 ** 30, total / 2 ** 30, 100.0 * (total - free) / total)
    except OSError:
        pass
    rc, out, _ = sh(["uptime", "-p"], timeout=8)
    if rc == 0 and out.strip():
        info["uptime"] = out.strip().replace("up ", "")
    return info


def _one(r):
    row = dict(r)
    row["wechat"] = {}
    row["agent"] = {}
    if r.get("state") == "running":
        try:
            info = container_info(r["name"]) or {}
            for k in ("cpu", "mem", "mem_percent", "data_dir", "started_at"):
                if info.get(k) is not None:
                    row[k] = info.get(k)
            row["wechat"] = wechat_state(r["name"]) or {}
            row["agent"] = agent_state(r["name"]) or {}
        except Exception:  # noqa: BLE001
            pass
    return row


def refresh_loop():
    while True:
        t0 = time.time()
        try:
            base = list_containers()
            with ThreadPoolExecutor(max_workers=8) as ex:
                rows = list(ex.map(_one, base))
            rows.sort(key=lambda x: (x.get("state") != "running", x.get("name") or ""))
            snap = {"ts": time.time(), "rows": rows, "board": board_info()}
            with SNAP_LOCK:
                SNAP.update(snap)
            cam_watchdog([r.get("name") for r in rows if r.get("state") == "running"])
        except Exception:  # noqa: BLE001
            pass
        time.sleep(max(1.0, REFRESH_SEC - (time.time() - t0)))


def screen_size(name):
    if name in SIZE_CACHE:
        return SIZE_CACHE[name]
    rc, out, _ = dexec(name, ["wm", "size"], timeout=8)
    w, h = 720, 1280
    if rc == 0:
        m = re.search(r"(\d+)x(\d+)", out)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
    SIZE_CACHE[name] = (w, h)
    return w, h


def screenshot(name):
    rc, out_bin, _ = 1, b"", ""
    try:
        r = subprocess.run(["docker", "exec", name, "screencap", "-p"], capture_output=True, timeout=25)
        rc, out_bin = r.returncode, r.stdout or b""
    except Exception:  # noqa: BLE001
        rc = 1
    return (out_bin if rc == 0 and out_bin else None)


ADB_IME = "com.android.adbkeyboard/.AdbIME"


def type_text(name, text):
    tmp = "/data/local/tmp/g98_type.txt"
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    ok, _ = cshell_run(name, "printf '%s' " + b64 + " | base64 -d > " + tmp, timeout=6)
    if not ok:
        try:
            subprocess.run(["docker", "exec", "-i", name, "sh", "-c", "cat > " + tmp],
                           input=text.encode("utf-8"), capture_output=True, timeout=12)
            ok = True
        except Exception as exc:  # noqa: BLE001
            return False, "写入失败: " + str(exc)
    if any(ord(ch) > 127 for ch in text):
        rc, out, _ = dexec(name, ["sh", "-c", "ime list -s"], timeout=10)
        if "adbkeyboard" not in out:
            return False, "这个容器里没装 ADBKeyboard，中文打不进去（英文数字可以）"
        _, prev, _ = dexec(name, ["sh", "-c", "settings get secure default_input_method"], timeout=10)
        prev = prev.strip()
        dexec(name, ["sh", "-c", "ime set " + ADB_IME], timeout=10)
        time.sleep(0.35)
        rc, out, err = dexec(name, ["sh", "-c", 'am broadcast -a ADB_INPUT_TEXT --es msg "$(cat ' + tmp + ')"'], timeout=15)
        time.sleep(0.45)
        if prev and "adbkeyboard" not in prev:
            dexec(name, ["sh", "-c", "ime set " + prev], timeout=10)
        return rc == 0 and "Broadcast completed" in (out + err), (out + err).strip()[:200]
    ok2, out2 = cshell_run(name, "input text \"$(cat " + tmp + ")\"", timeout=8)
    if ok2:
        return True, ""
    rc, out, err = dexec(name, ["sh", "-c", 'input text "$(cat ' + tmp + ')"'], timeout=15)
    return rc == 0, (out + err).strip()[:200]


class Handler(BaseHTTPRequestHandler):
    server_version = "g98-console/3.0"
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        try:                                   # 小包立即发出，不等 Nagle 合并
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:  # noqa: BLE001
            pass

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        headers = list(extra or [])
        headers.extend(getattr(self, "_token_cookie", []))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except Exception:  # noqa: BLE001
                pass

    def _fail(self):
        try:
            self._send(500, "internal error", "text/plain; charset=utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _cookie(self):
        raw = self.headers.get("Cookie", "") or ""
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    @staticmethod
    def _eq(a, b):
        try:
            ab = a.encode("utf-8", "ignore") if isinstance(a, str) else bytes(a or b"")
            bb = b.encode("utf-8", "ignore") if isinstance(b, str) else bytes(b or b"")
        except Exception:  # noqa: BLE001
            return False
        if not ab or not bb:
            return False
        return hmac.compare_digest(ab, bb)

    def _authed(self):
        return self._eq(self._cookie().get("g98k", ""), api_token())

    def _ws_tunnel(self, upstream_path):
        """浏览器 <-> 本机 8080 的 WebSocket 原始隧道（WS 帧原样透传）。"""
        key = (self.headers.get("Sec-WebSocket-Key") or "").strip()
        if not key:
            return self._send(400, "websocket only", "text/plain; charset=utf-8")
        try:
            up = socket.create_connection(("127.0.0.1", 8080), timeout=10)
        except Exception:  # noqa: BLE001
            return self._send(502, "live backend down", "text/plain; charset=utf-8")
        try:
            req = ("GET %s HTTP/1.1\r\nHost: 127.0.0.1:8080\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n" % (upstream_path, key))
            up.sendall(req.encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = up.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            first = head.split(b"\r\n")[0] if head else b""
            if b" 101" not in first:
                up.close()
                return self._send(502, "live backend refused", "text/plain; charset=utf-8")
        except Exception:  # noqa: BLE001
            try:
                up.close()
            except Exception:  # noqa: BLE001
                pass
            return self._send(502, "live backend error", "text/plain; charset=utf-8")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        resp = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept)
        try:
            self.wfile.write(resp.encode())
            self.wfile.flush()
        except Exception:  # noqa: BLE001
            up.close()
            return
        self.close_connection = True
        down = self.connection
        try:
            down.settimeout(None)
            up.settimeout(None)
        except Exception:  # noqa: BLE001
            pass
        if rest:
            try:
                down.sendall(rest)
            except Exception:  # noqa: BLE001
                pass

        def pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:  # noqa: BLE001
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except Exception:  # noqa: BLE001
                        pass

        t = threading.Thread(target=pump, args=(down, up), daemon=True)
        t.start()
        pump(up, down)
        t.join(timeout=5)

    def do_GET(self):
        try:
            return self._get()
        except Exception:  # noqa: BLE001
            return self._fail()

    def _get(self):
        u = urlparse(self.path)
        p = u.path
        qs = parse_qs(u.query)
        qk = qs.get("k", [""])[0]
        token_ok = bool(qk and self._eq(qk, api_token()))
        self._token_cookie = []
        if token_ok:
            self._token_cookie = [("Set-Cookie", "g98k=" + api_token() + "; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")]
        if p == "/login":
            return self._send(200, LOGIN)
        if not (self._authed() or token_ok):
            return self._send(200, LOGIN)
        if p in ("/ws/live", "/ws/cam"):
            dev = (qs.get("device") or [""])[0]
            if not NAME_RE.match(dev or ""):
                return self._send(400, "bad device", "text/plain; charset=utf-8")
            upstream = ("/ws/screen?device=" if p == "/ws/live" else "/ws/camera?device=") + quote(dev)
            return self._ws_tunnel(upstream)
        if p == "/ws/sensor":
            dev = (qs.get("name") or qs.get("device") or [""])[0]
            if not NAME_RE.match(dev or ""):
                return self._send(400, "bad name", "text/plain; charset=utf-8")
            return self._ws_sensor(dev)
        if p == "/api/sensors/live":
            name = (qs.get("name") or qs.get("device") or ["wx1"])[0]
            if not NAME_RE.match(name or ""):
                return self._json({"error": "bad name"}, 400)
            return self._json(sensor_live_state(name))
        if p == "/ws/input":
            dev = (qs.get("name") or qs.get("device") or [""])[0]
            if not NAME_RE.match(dev or ""):
                return self._send(400, "bad name", "text/plain; charset=utf-8")
            return self._ws_input(dev)
        if p.startswith("/h264mp4/"):
            name = unquote(p[len("/h264mp4/"):]).strip("/")
            if not NAME_RE.match(name or ""):
                return self._json({"error": "bad name"}, 400)
            if rtc_active():
                return self._json({"error": "WebRTC active; refresh console"}, 409)
            return self._h264mp4(name)
        if p.startswith("/h264/"):
            name = unquote(p[len("/h264/"):]).strip("/")
            if not NAME_RE.match(name or ""):
                return self._json({"error": "bad name"}, 400)
            if rtc_active():
                return self._json({"error": "WebRTC active; refresh console"}, 409)
            return self._h264(name)
        if p.startswith("/stream/"):
            name = unquote(p[len("/stream/"):]).strip("/")
            if not NAME_RE.match(name or ""):
                return self._json({"error": "bad name"}, 400)
            if rtc_active():
                return self._json({"error": "WebRTC active; refresh console"}, 409)
            return self._mjpeg(name)
        if p == "/api/rtc/status":
            with RTC_LOCK:
                out = [{"name": n, "alive": st.alive(), "pid": st.proc.pid if st.proc else None,
                        "log": st.tail(200)} for n, st in RTC_SESSIONS.items()]
            return self._json({"ok": True, "jobs": out})
        if p == "/api/location":
            name = (qs.get("name") or [""])[0]
            if not NAME_RE.match(name or ""):
                return self._json({"error": "bad name"}, 400)
            info = get_gnss_location(name)
            if info.get("ok"):
                info.update(get_gnss_manual(name))
            return self._json(info)
        if p == "/api/location/board":
            g = net_geo()
            return self._json({"ok": bool(g.get("ok")), "city": g.get("city"), "region": g.get("region"),
                               "ip": g.get("ip"), "iface": g.get("iface"),
                               "lat": g.get("lat"), "lon": g.get("lon"), "error": g.get("error")})
        if p == "/api/cam/files":
            items = []
            try:
                for fn in sorted(os.listdir(CAM_DIR)):
                    fp = os.path.join(CAM_DIR, fn)
                    if not os.path.isfile(fp):
                        continue
                    low = fn.lower()
                    if low.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")):
                        fk = "image"
                    elif low.endswith((".mp4", ".webm", ".mkv", ".mov", ".ts", ".avi")):
                        fk = "video"
                    else:
                        continue
                    stt = os.stat(fp)
                    items.append({"name": fn, "path": fp, "kind": fk,
                                  "size": stt.st_size, "mtime": int(stt.st_mtime)})
            except OSError:
                items = []
            items.sort(key=lambda x: -x["mtime"])
            return self._json({"ok": True, "files": items[:60]})
        if p == "/api/cam/qr":
            name = (qs.get("name") or qs.get("device") or [""])[0]
            if not NAME_RE.match(name or ""):
                return self._json({"error": "bad name"}, 400)
            node = (qs.get("node") or ["/dev/video10"])[0]
            if not re.match(r"^/dev/video[0-9]{1,2}$", node or ""):
                return self._json({"error": "bad node"}, 400)
            host = (self.headers.get("Host") or "").split(":")[0].strip()
            if not host or host in ("127.0.0.1", "localhost"):
                host = TLS_IP
            target = "https://%s:%d/m?%s" % (
                host, TLS_PORT,
                urlencode({"k": api_token(), "device": name, "node": node}))
            png = cam_qr_png(target)
            if not png:
                return self._json({"error": "qrencode 不可用"}, 500)
            return self._json({"ok": True, "host": host, "port": TLS_PORT,
                               "png": "data:image/png;base64," + base64.b64encode(png).decode()})
        if p == "/api/cam/status":
            name = (qs.get("name") or ["wx1"])[0]
            if not NAME_RE.match(name or ""):
                return self._json({"error": "bad name"}, 400)
            return self._json({"ok": True, "state": cam_state(name), "busy": cam_busy(name)})
        if p == "/cam":
            return self._send(200, CAMPAGE)
        if p == "/ms":
            return self._send(200, MSENSOR)
        if p == "/m":
            return self._send(200, MPAGE)
        if p == "/api/stream":
            name = (qs.get("name") or [""])[0]
            if not NAME_RE.match(name or ""):
                return self._json({"error": "bad name"}, 400)
            st = STREAMS.get(name)
            ok = bool(st and st.healthy())
            return self._json({"ok": ok, "fps": round(st.fps(), 1) if st else 0,
                               "age": round(time.time() - st.last_frame, 1) if st else 999,
                               "clients": st.clients if st else 0,
                               "error": st.error if st else "未启动"})
        if p in ("/", "/index.html"):
            return self._send(200, PAGE)
        if p == "/api/snapshot":
            with SNAP_LOCK:
                data = json.dumps(dict(SNAP, build=BUILD), ensure_ascii=False)
            return self._send(200, data, "application/json; charset=utf-8")
        if p.startswith("/screen/"):
            name = p[len("/screen/"):].strip("/")
            if not NAME_RE.match(name):
                return self._send(400, "bad name", "text/plain; charset=utf-8")
            png = screenshot(name)
            if not png:
                return self._send(502, "screenshot failed", "text/plain; charset=utf-8")
            return self._send(200, png, "image/png")
        return self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        try:
            return self._post()
        except Exception:  # noqa: BLE001
            return self._fail()

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "json" in ctype:
            try:
                return json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception:  # noqa: BLE001
                return {}
        f = parse_qs(raw.decode("utf-8", "replace"))
        return {k: v[0] for k, v in f.items()}

    def _recvn(self, n):
        out = b""
        while len(out) < n:
            d = self.connection.recv(n - len(out))
            if not d:
                raise ConnectionError("closed")
            out += d
        return out

    def _ws_send(self, text):
        p = text.encode("utf-8")
        n = len(p)
        if n < 126:
            hdr = bytes([0x81, n])
        elif n < 65536:
            hdr = bytes([0x81, 126]) + n.to_bytes(2, "big")
        else:
            hdr = bytes([0x81, 127]) + n.to_bytes(8, "big")
        self.connection.sendall(hdr + p)

    def _ws_input(self, name):
        """输入通道：浏览器 → 本机 → 容器内触摸守护进程，省掉每个 pointermove 一次 HTTP 往返。"""
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            return self._json({"error": "not a websocket"}, 400)
        acc = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers()
        self.close_connection = True
        w, h = screen_size(name)
        self._ws_send(json.dumps({"type": "hello", "device": name, "w": w, "h": h}))
        try:
            while True:
                b0, b1 = self._recvn(2)
                op = b0 & 0x0F
                masked = b1 & 0x80
                ln = b1 & 0x7F
                if ln == 126:
                    ln = int.from_bytes(self._recvn(2), "big")
                elif ln == 127:
                    ln = int.from_bytes(self._recvn(8), "big")
                mask = self._recvn(4) if masked else b"\x00\x00\x00\x00"
                data = self._recvn(ln) if ln else b""
                if masked and data:
                    data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
                if op == 0x8:
                    return
                if op == 0x9:
                    self.connection.sendall(bytes([0x8A, len(data)]) + data)
                    continue
                if op != 0x1:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001
                    continue
                a = str(msg.get("a") or "")[:1].lower()
                if a == "k":
                    code = int(msg.get("code") or 0)
                    if not touch_send(name, "K %d" % code):
                        cshell_run(name, "input keyevent %d" % code, timeout=8)
                    continue
                m = {"d": "D", "m": "M", "u": "U", "t": "T"}.get(a)
                if not m:
                    continue
                try:
                    x = int(round(float(msg.get("fx")) * w))
                    y = int(round(float(msg.get("fy")) * h))
                except Exception:  # noqa: BLE001
                    continue
                touch_send(name, "%s %d %d" % (m, max(0, min(w - 1, x)), max(0, min(h - 1, y))))
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                self.connection.close()
            except Exception:  # noqa: BLE001
                pass



    def _ws_sensor(self, name):
        """手机传感器通道：浏览器持续推送选中的传感器值 → 写实时文件给容器内假传感器 HAL。"""
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            return self._json({"error": "not a websocket"}, 400)
        acc = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers()
        self.close_connection = True
        self._ws_send(json.dumps({"type": "hello", "device": name,
                                  "slots": [k for k, _i in SENSOR_SLOTS]}))
        last_ack = 0.0
        try:
            os.makedirs(os.path.dirname(sensor_live_path(name)), exist_ok=True)
        except OSError:
            pass
        try:
            while True:
                b0, b1 = self._recvn(2)
                op = b0 & 0x0F
                masked = b1 & 0x80
                ln = b1 & 0x7F
                if ln == 126:
                    ln = int.from_bytes(self._recvn(2), "big")
                elif ln == 127:
                    ln = int.from_bytes(self._recvn(8), "big")
                mask_key = self._recvn(4) if masked else b"\x00\x00\x00\x00"
                data = self._recvn(ln) if ln else b""
                if masked and data:
                    data = bytes(c ^ mask_key[i % 4] for i, c in enumerate(data))
                if op == 0x8:
                    return
                if op == 0x9:
                    self.connection.sendall(bytes([0x8A, len(data)]) + data)
                    continue
                if op != 0x1:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001
                    continue
                on = msg.get("on") or []
                if not isinstance(on, list):
                    on = []
                vals = msg.get("v") or {}
                if not isinstance(vals, dict):
                    vals = {}
                try:
                    m = sensor_write_live(name, on, vals)
                except OSError as exc:
                    self._ws_send(json.dumps({"type": "error", "error": str(exc)}))
                    time.sleep(0.2)
                    continue
                now = time.time()
                if now - last_ack >= 1.0:
                    last_ack = now
                    self._ws_send(json.dumps({"type": "ack", "mask": m, "n": len(on)}))
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                sensor_write_live(name, [], {})
            except Exception:  # noqa: BLE001
                pass
            try:
                self.connection.close()
            except Exception:  # noqa: BLE001
                pass

    def _whep(self, name):
        if not self._authed():
            return self._json({"error": "unauthorized"}, 401)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        sdp = self.rfile.read(max(0, min(n, 2 * 1024 * 1024)))
        if not sdp:
            return self._json({"error": "empty sdp"}, 400)
        # 懒启动：编码器在这里才拉起，然后等 mediamtx 看到 publisher 再转发 offer。
        # 直接转发会 404（WHEP 早于推流），所以对 404/502 做服务端重试，
        # 浏览器完全无感，只是首帧慢 1-2 秒。
        try:
            rtc_ensure(name)
        except Exception as exc:  # noqa: BLE001
            _alog("whep %s ensure failed: %s" % (name, str(exc)[:160]))
            return self._json({"error": str(exc)[:200]}, 503)
        deadline = time.time() + WHEP_READY_TIMEOUT
        code = 502
        data = b""
        ctype = "text/plain; charset=utf-8"
        tries = 0
        while True:
            tries += 1
            code, data, ctype = whep_post(name, sdp)
            if code not in (404, 502, 503):
                break
            if time.time() >= deadline:
                break
            time.sleep(0.2)
        _alog("whep %s ready in %d tries (code=%d)" % (name, tries, code))
        if 200 <= code < 300:
            # 真正连上观看端：刷新心跳时间并清掉重连计数，正常刷新页面不会被限流
            with RTC_LOCK:
                st = RTC_SESSIONS.get(name)
            if st is not None:
                st.last_view = time.time()
            st.viewed = True
            RTC_STARTS.pop(name, None)
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:  # noqa: BLE001
            pass

    def _whip(self, name):
        """Relay a WHIP offer from the phone page to MediaMTX and return its SDP answer."""
        if not self._authed():
            return self._json({"error": "unauthorized"}, 401)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        sdp = self.rfile.read(max(0, min(n, 2 * 1024 * 1024)))
        if not sdp:
            return self._json({"error": "empty sdp"}, 400)
        req = urllib.request.Request(
            "http://127.0.0.1:8889/%s/whip" % quote(name),
            data=sdp, method="POST",
            headers={"Content-Type": "application/sdp", "Accept": "application/sdp"})
        code = 502
        data = b""
        ctype = "text/plain; charset=utf-8"
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                code = resp.status
                data = resp.read()
                ctype = resp.headers.get("Content-Type") or "application/sdp"
        except urllib.error.HTTPError as exc:
            code = exc.code
            data = exc.read()
            ctype = exc.headers.get("Content-Type") or "text/plain; charset=utf-8"
        except Exception as exc:  # noqa: BLE001
            data = ("MediaMTX WHIP: " + str(exc)).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:  # noqa: BLE001
            pass

    def _h264(self, name):
        # H264 与 MJPEG 共用同一个 screenrecord 编码器，先停 MJPEG 管道再起 H264。
        drop_stream(name)
        st = H264Session(name)
        try:
            st.start()
        except Exception as exc:  # noqa: BLE001
            return self._send(503, "H264 start failed: " + str(exc)[:160],
                              "text/plain; charset=utf-8")
        cfg = st.wait_config(7.0)
        if not cfg:
            st.stop()
            return self._send(503, "H264 encoder not ready", "text/plain; charset=utf-8")
        raw = json.dumps(cfg, ensure_ascii=False).encode("utf-8")
        try:
            try:
                self.connection.settimeout(8.0)
            except Exception:  # noqa: BLE001
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-store, no-cache")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            # G98H + config JSON length + config JSON，后面是 [type,key,pts,len]+NAL
            self.wfile.write(b"G98H" + struct.pack(">I", len(raw)) + raw)
            self.wfile.flush()
            while True:
                frame = st.next_frame(2.0)
                if frame is None:
                    if st.error:
                        break
                    # 静止画面没有新帧，发一个 14 字节心跳，顺便探测浏览器是否已断开
                    self.wfile.write(struct.pack(">BBQI", 2, 0, 0, 0))
                    self.wfile.flush()
                    continue
                key, pts, nal = frame
                self.wfile.write(struct.pack(">BBQI", 1, 1 if key else 0, pts, len(nal)) + nal)
                self.wfile.flush()
        except Exception:  # noqa: BLE001
            pass
        finally:
            st.stop()

    def _h264mp4(self, name):
        # MSE 需要干净的 init 段；新连接接管同一设备时先停旧编码器，避免两个 screenrecord 抢帧。
        drop_stream(name)
        with FMP4_LOCK:
            old = FMP4_SESSIONS.pop(name, None)
        if old is not None:
            try:
                old.stop()
            except Exception:  # noqa: BLE001
                pass
        st = FMP4Session(name)
        try:
            st.start()
        except Exception as exc:  # noqa: BLE001
            return self._send(503, "fMP4 start failed: " + str(exc)[:160],
                              "text/plain; charset=utf-8")
        with FMP4_LOCK:
            FMP4_SESSIONS[name] = st
        mine = False
        try:
            if not st.wait_first(8.0):
                if st.error:
                    return self._send(503, "fMP4 encoder: " + st.error[:160],
                                      "text/plain; charset=utf-8")
                return self._send(504, "fMP4 encoder timeout", "text/plain; charset=utf-8")
            try:
                self.connection.settimeout(10.0)
            except Exception:  # noqa: BLE001
                pass
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store, no-cache")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Codec", "avc1.42c01f")
            self.end_headers()
            self.close_connection = True
            while True:
                if st.stop_event.is_set() and not st.chunks:
                    break
                chunk = st.next_chunk(4.0)
                if chunk is None:
                    if st.error or st.stop_event.is_set():
                        break
                    continue
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:  # noqa: BLE001
            pass
        finally:
            with FMP4_LOCK:
                mine = FMP4_SESSIONS.get(name) is st
                if mine:
                    FMP4_SESSIONS.pop(name, None)
            if mine:
                st.stop()

    def _mjpeg(self, name):
        try:
            st = get_stream(name)
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": str(exc)[:200]}, 503)
        st.clients += 1
        st.last_use = time.time()
        try:
            try:
                self.connection.settimeout(8.0)
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
            except Exception:  # noqa: BLE001
                pass
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=g98frame")
            self.send_header("Cache-Control", "no-store, no-cache")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            seq = -1
            while True:
                if st.stopped or st.error:
                    break
                seq, jpg = st.wait(seq, timeout=5.0)
                if st.stopped or st.error:
                    break
                if jpg is None:
                    time.sleep(0.1)
                    continue
                head = (b"--g98frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpg)).encode() + b"\r\n\r\n")
                self.wfile.write(head + jpg + b"\r\n")
                st.last_use = time.time()
        except Exception:  # noqa: BLE001
            pass
        finally:
            st.clients -= 1

    def _json(self, obj, code=200):
        return self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _upload(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 512 * 1024 * 1024:
            return self._json({"error": "bad size"}, 400)
        fn = str(self.headers.get("X-Filename") or "upload.bin")
        ext = os.path.splitext(fn)[1].lower()[:8] or ".bin"
        if ext not in (".mp4", ".webm", ".mkv", ".mov", ".avi", ".jpg", ".jpeg",
                       ".png", ".gif", ".bmp", ".webp"):
            ext = ".bin"
        try:
            os.makedirs(CAM_DIR, exist_ok=True)
        except OSError:
            pass
        path = os.path.join(CAM_DIR, "%d%s" % (int(time.time() * 1000), ext))
        left = n
        got = 0
        with open(path, "wb") as fh:
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                left -= len(chunk)
        _alog("cam upload %s %d B" % (path, got))
        return self._json({"ok": True, "path": path, "bytes": got, "name": fn[:80]})

    def _post(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/api/cam/file":
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            return self._upload()
        if p == "/api/cam/frame":
            # 手机浏览器 WebRTC 不可用时的降级通道：raw JPEG → camd 的 jpeg 解码器。
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            qs = parse_qs(u.query)
            name = str((qs.get("name") or [""])[0])
            node = str((qs.get("node") or [""])[0])
            if not NAME_RE.match(name) or not re.match(r"^/dev/video[0-9]{1,2}$", node):
                return self._json({"error": "bad args"}, 400)
            try:
                n = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                n = 0
            if n <= 0 or n > 4 * 1024 * 1024:
                return self._json({"error": "bad length"}, 400)
            data = self.rfile.read(n)
            return self._json({"ok": cam_camd_frame(node, data)})
        if p.startswith("/whip/"):
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            wname = unquote(p[len("/whip/"):]).strip("/")
            if not NAME_RE.match(wname or ""):
                return self._json({"error": "bad name"}, 400)
            return self._whip(wname)
        if p.startswith("/whep/"):
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            wname = unquote(p[len("/whep/"):]).strip("/")
            if not NAME_RE.match(wname or ""):
                return self._json({"error": "bad name"}, 400)
            return self._whep(wname)
        body = self._body()
        if p == "/login":
            k = body.get("k", "")
            if self._eq(k, api_token()):
                return self._send(302, "", extra=[("Location", "/"),
                                                  ("Set-Cookie", "g98k=" + api_token() + "; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")])
            return self._send(200, LOGIN)
        if not self._authed():
            return self._json({"error": "unauthorized"}, 401)
        name = str(body.get("name") or "")
        if not NAME_RE.match(name):
            return self._json({"error": "bad name"}, 400)
        if p == "/api/input/warm":
            return self._json({"ok": input_daemon(name) is not None})
        if p == "/api/touch":
            w, h = screen_size(name)
            try:
                a = str(body.get("a") or "t")[:1].lower()
                x = int(round(float(body.get("fx")) * w))
                y = int(round(float(body.get("fy")) * h))
            except Exception:  # noqa: BLE001
                return self._json({"error": "bad coords"}, 400)
            x = max(0, min(w - 1, x))
            y = max(0, min(h - 1, y))
            m = {"d": "D", "m": "M", "u": "U", "t": "T"}.get(a)
            if not m:
                return self._json({"error": "bad action"}, 400)
            ok = touch_send(name, "%s %d %d" % (m, x, y))
            return self._json({"ok": ok, "x": x, "y": y, "daemon": ok})
        if p == "/api/tap":
            w, h = screen_size(name)
            try:
                x = int(round(float(body.get("fx")) * w))
                y = int(round(float(body.get("fy")) * h))
            except Exception:  # noqa: BLE001
                return self._json({"error": "bad coords"}, 400)
            x = max(0, min(w - 1, x))
            y = max(0, min(h - 1, y))
            rc, out, err = dexec(name, ["input", "tap", str(x), str(y)], timeout=15)
            _alog("tap %s %d %d rc=%s" % (name, x, y, rc))
            return self._json({"ok": rc == 0, "x": x, "y": y, "error": (out + err).strip()[:160] or None})
        if p == "/api/swipe":
            w, h = screen_size(name)
            try:
                x1 = int(round(float(body.get("fx1")) * w)); y1 = int(round(float(body.get("fy1")) * h))
                x2 = int(round(float(body.get("fx2")) * w)); y2 = int(round(float(body.get("fy2")) * h))
            except Exception:  # noqa: BLE001
                return self._json({"error": "bad coords"}, 400)
            dur = int(body.get("dur") or 220)
            rc, out, err = dexec(name, ["input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur)], timeout=20)
            _alog("swipe %s %d %d %d %d %d rc=%s %s" % (name, x1, y1, x2, y2, dur, rc, (out + err).strip()[:120]))
            return self._json({"ok": rc == 0, "error": (out + err).strip()[:160] or None})
        if p == "/api/longpress":
            # 长按：守护进程可用时前端直接发 D..U；这里是没有守护进程时的兜底，
            # Android 把"原地 swipe"当作一次长按（不移动、时长即按住时间）。
            w, h = screen_size(name)
            try:
                x = int(round(float(body.get("fx")) * w)); y = int(round(float(body.get("fy")) * h))
            except Exception:  # noqa: BLE001
                return self._json({"error": "bad coords"}, 400)
            try:
                dur = int(body.get("dur") or 700)
            except Exception:  # noqa: BLE001
                dur = 700
            dur = max(400, min(5000, dur))
            x = max(0, min(w - 1, x)); y = max(0, min(h - 1, y))
            rc, out, err = dexec(name, ["input", "swipe", str(x), str(y), str(x), str(y), str(dur)], timeout=25)
            _alog("longpress %s %d %d dur=%d rc=%s" % (name, x, y, dur, rc))
            return self._json({"ok": rc == 0, "x": x, "y": y, "dur": dur,
                               "error": (out + err).strip()[:160] or None})
        if p == "/api/key":
            try:
                code = int(body.get("code"))
            except Exception:  # noqa: BLE001
                return self._json({"error": "bad code"}, 400)
            if touch_send(name, "K %d" % code):
                return self._json({"ok": True, "daemon": True})
            rc, out, err = dexec(name, ["input", "keyevent", str(code)], timeout=15)
            return self._json({"ok": rc == 0, "error": (out + err).strip()[:160] or None})
        if p == "/api/text":
            text = str(body.get("text") or "")
            if not text:
                return self._json({"error": "empty"}, 400)
            ok, msg = type_text(name, text)
            return self._json({"ok": ok, "error": None if ok else (msg or "输入失败"), "info": msg})
        if p in ("/api/location", "/api/location/manual"):
            try:
                lat = float(body.get("lat")); lon = float(body.get("lon"))
                alt = float(body.get("alt") if body.get("alt") not in (None, "") else 0)
            except (TypeError, ValueError):
                return self._json({"error": "bad coords"}, 400)
            if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(alt)) or not (-90 <= lat <= 90) or not (-180 <= lon <= 180) or not (-1000 <= alt <= 100000):
                return self._json({"error": "坐标超出范围"}, 400)
            source = str(body.get("source") or "").strip()[:32]
            if not source:
                source = "phone-gps" if p == "/api/location/manual" else "manual"
            rc, msg, payload = set_gnss_location(name, lat, lon, alt, manual=True, source=source)
            _alog("location %s %s rc=%s source=%s" % (name, payload, rc, source))
            return self._json({"ok": rc == 0, "lat": lat, "lon": lon, "alt": alt, "source": source,
                               "manual": True, "error": None if rc == 0 else (msg or "写入失败")})
        if p == "/api/location/self":
            g = net_geo()
            if not g.get("ok"):
                return self._json({"ok": False, "error": g.get("error") or "网络定位失败",
                                   "ip": g.get("ip"), "iface": g.get("iface")})
            lat, lon = g["lat"], g["lon"]
            rc, msg, payload = set_gnss_location(name, lat, lon, 0.0, manual=False, source="board")
            _alog("location-self %s %s rc=%s city=%s ip=%s iface=%s" % (
                name, payload, rc, g.get("city"), g.get("ip"), g.get("iface")))
            return self._json({"ok": rc == 0, "lat": lat, "lon": lon, "alt": 0.0,
                               "city": g.get("city"), "region": g.get("region"),
                               "ip": g.get("ip"), "iface": g.get("iface"),
                               "error": None if rc == 0 else (msg or "写入失败")})
        if p == "/api/app":
            comp = str(body.get("component") or "com.tencent.mm/com.tencent.mm.ui.LauncherUI")
            rc, out, err = dexec(name, ["am", "start", "-n", comp], timeout=25)
            return self._json({"ok": rc == 0, "error": (out + err).strip()[:200] or None})
        if p == "/api/cam/start":
            requested = str(body.get("node") or "").strip()
            if requested and not re.match(r"^/dev/video[0-9]{1,2}$", requested):
                return self._json({"error": "bad node"}, 400)
            path = str(body.get("path") or "").strip()
            kind = str(body.get("kind") or "video")
            if kind == "webcam":
                path = "webcam"
            elif kind == "stream":
                if not re.match(r"^(rtsp|rtsps|http|https)://[^\s]{3,500}$", path):
                    return self._json({"error": "bad stream url"}, 400)
            else:
                kind = "image" if kind == "image" else "video"
                if not (path.startswith(CAM_DIR + "/") and os.path.exists(path)):
                    return self._json({"error": "bad path"}, 400)
            node = cam_norm_node(name, requested) if requested else cam_active_node(name)
            geom = {}
            fit = str(body.get("fit") or "").strip().lower()
            if fit in ("contain", "cover", "stretch"):
                geom["fit"] = fit
            try:
                if int(body.get("rotate") or 0) in (0, 90, 180, 270):
                    geom["rotate"] = int(body.get("rotate") or 0)
            except (TypeError, ValueError):
                pass
            if body.get("both") is not None:
                geom["both"] = bool(body.get("both"))
            if body.get("mirror") is not None:
                geom["mirror"] = bool(body.get("mirror"))
            try:
                ys = float(body.get("ystretch") or 0)
                if 0.5 <= ys <= 3.0:
                    geom["ystretch"] = round(ys, 4)
            except (TypeError, ValueError):
                pass
            if not cam_submit(name, "start", lambda: cam_do_start(name, node, path, kind, geom)):
                return self._json({"ok": False, "queued": False, "error": "上一个摄像头操作还没结束",
                                   "state": cam_state(name)}, 409)
            return self._json({"ok": True, "queued": True, "node": node, "kind": kind,
                               "state": cam_state(name)})
        if p == "/api/cam/stop":
            requested = str(body.get("node") or "").strip()
            if requested and not re.match(r"^/dev/video[0-9]{1,2}$", requested):
                return self._json({"error": "bad node"}, 400)
            # 空表示停本实例全部节点，避免漏掉另一路的 writer/ffmpeg
            node = cam_norm_node(name, requested) if requested else ""
            if not cam_submit(name, "stop", lambda: cam_do_stop(name, node)):
                return self._json({"ok": True, "queued": False, "state": cam_state(name)})
            return self._json({"ok": True, "queued": True, "node": node or "all",
                               "state": cam_state(name)})
        if p == "/api/cam/status":
            return self._json({"ok": True, "state": cam_state(name), "busy": cam_busy(name)})
        if p == "/api/cam/geom":
            node = cam_norm_node(name, str(body.get("node") or "").strip()) \
                if body.get("node") else cam_active_node(name)
            snap = cam_camd_node_state(node) or {}
            return self._json({"ok": True, "node": node,
                               "geometry": snap.get("geometry"),
                               "src": snap.get("src"),
                               "kind": snap.get("kind"),
                               "source_frames": snap.get("source_frames")})
        if p == "/api/rtc/start":
            try:
                info = rtc_start(name, spawn=False)
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": str(exc)[:240]}, 500)
            if info.get("pending"):
                _alog("rtc prepare %s (lazy, encoder starts on first WHEP)" % (name,))
            elif info.get("reused"):
                _alog("rtc reuse %s pid=%s" % (name, info.get("pid")))
            else:
                _alog("rtc spawn %s pid=%s (whep)" % (name, info.get("pid")))
            return self._json(info)
        if p == "/api/rtc/ping":
            with RTC_LOCK:
                st = RTC_SESSIONS.get(name)
            if st is not None:
                st.last_view = time.time()
            return self._json({"ok": True, "alive": st is not None})
        if p == "/api/rtc/stop":
            n = rtc_stop(name)
            _alog("rtc stop %s count=%s" % (name, n))
            return self._json({"ok": True, "stopped": n})
        if p == "/api/action":
            action = str(body.get("action") or "")
            if action not in ("start", "stop", "restart"):
                return self._json({"error": "bad action"}, 400)
            rc, out, err = sh(["docker", action, name], timeout=90)
            return self._json({"ok": rc == 0, "error": (out + err).strip()[:200] or None})
        return self._json({"error": "not found"}, 404)


def cleanup_orphans():
    """服务重启时上一代的管道子进程会活下来（start_new_session），必须收尸，
    否则残留的 screenrecord/ffmpeg 会一直占 CPU，把新流挤到 20fps。"""
    # The host-side bash loop restarts screenrecord when its docker exec dies,
    # so kill the loop first, then the publisher and the frame pump.
    for pat in ("docker exec .* screenrecord",
                "frame_pump.py",
                "rtsp://127.0.0.1:8554/wx",
                "screenrecord --output-format=raw-frames",
                "dd bs=.*iflag=fullblock",
                "image2pipe -c:v mjpeg -"):
        try:
            subprocess.run(["pkill", "-9", "-f", pat], timeout=10,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass
    # Killing only the host docker-exec shim leaves the Android-side
    # screenrecord/screencap alive inside the container.
    try:
        names = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}"],
            timeout=10, stderr=subprocess.DEVNULL).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        names = ""
    for n in names.split():
        if not re.fullmatch(r"wx\d+", n):
            continue
        for pat in ("screenrecord --output-format", "while true; do screencap"):
            try:
                subprocess.run(["docker", "exec", n, "pkill", "-9", "-f", pat],
                               timeout=8, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:  # noqa: BLE001
                pass
        try:
            subprocess.run(["docker", "exec", n, "pkill", "-9", "-x", "screencap"],
                           timeout=8, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass


def main():
    cleanup_orphans()
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=rtc_janitor, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    tls_ok = False
    try:
        tls = ThreadingHTTPServer(("0.0.0.0", TLS_PORT), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        tls.socket = ctx.wrap_socket(tls.socket, server_side=True)
        threading.Thread(target=tls.serve_forever, daemon=True).start()
        tls_ok = True
        print("g98 console tls on", TLS_PORT, flush=True)
    except Exception as exc:  # noqa: BLE001
        print("g98 console tls disabled:", repr(exc), flush=True)
    print("g98 console on", PORT, "tls=" + str(tls_ok), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    # kill -USR1 <pid> dumps every thread's stack; a stuck camera/UI helper then
    # shows exactly which subprocess call is blocking instead of guessing.
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except Exception:
        pass
    main()
