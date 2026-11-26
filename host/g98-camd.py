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
import errno
import json
import os
import signal
import stat
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

W, H = 640, 480
FRAME_SIZE = W * H * 3 // 2
FPS = int(os.environ.get("G98_CAMD_FPS", "30"))
IDLE_FPS = int(os.environ.get("G98_CAMD_IDLE_FPS", "5"))
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


def source_cmd(kind, arg):
    if kind == "image":
        return [FFMPEG, "-hide_banner", "-loglevel", "error", "-re",
                "-loop", "1", "-i", arg,
                "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease,"
                       "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p"
                       % (W, H, W, H),
                "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
    if kind == "video":
        return [FFMPEG, "-hide_banner", "-loglevel", "error", "-re",
                "-stream_loop", "-1", "-i", arg,
                "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease,"
                       "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,fps=%d,format=yuv420p"
                       % (W, H, W, H, FPS),
                "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
    if kind == "rtsp":
        return [FFMPEG, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                "-i", arg,
                "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease,"
                       "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,fps=%d,format=yuv420p"
                       % (W, H, W, H, FPS),
                "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
    raise ValueError("bad kind " + kind)


class Reader(threading.Thread):
    """Runs one ffmpeg source and publishes complete frames."""

    def __init__(self, node, kind, arg, on_frame):
        super().__init__(daemon=True)
        self.node = node
        self.kind = kind
        self.arg = arg
        self.on_frame = on_frame
        self.proc = None
        self.stop = threading.Event()
        self.frames = 0

    def run(self):
        cmd = idle_cmd(self.node) if self.kind == "idle" else source_cmd(self.kind, self.arg)
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=0)
        except Exception as exc:  # noqa: BLE001
            print("camd: spawn failed %s: %s" % (self.kind, exc), flush=True)
            return
        buf = bytearray()
        fd = self.proc.stdout
        while not self.stop.is_set():
            chunk = fd.read(FRAME_SIZE * 4)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= FRAME_SIZE:
                frame = bytes(buf[:FRAME_SIZE])
                del buf[:FRAME_SIZE]
                self.frames += 1
                self.on_frame(frame)
        self.stop.set()
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


class NodeRouter:
    def __init__(self, node):
        self.node = node
        self.lock = threading.Lock()
        self.frame = None
        self.seq = 0
        self.kind = None
        self.arg = None
        self.since = 0.0
        self.reader = None
        self.fd = None
        self.written = 0
        self.errors = 0
        self.consecutive_errors = 0
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
                frame = self.frame
                kind = self.kind
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
            period = 1.0 / (IDLE_FPS if kind == "idle" else FPS)
            dt = period - (time.time() - t0)
            if dt > 0:
                time.sleep(dt)

    def _on_frame(self, frame):
        with self.lock:
            self.frame = frame
            self.seq += 1

    def set_source(self, kind, arg):
        with self.lock:
            old = self.reader
            self.kind, self.arg, self.since = kind, arg, time.time()
        if old is not None:
            old.stop.set()
            if old.proc is not None and old.proc.poll() is None:
                old.proc.terminate()
        reader = Reader(self.node, kind, arg, self._on_frame)
        with self.lock:
            self.reader = reader
        reader.start()
        return reader

    def snapshot(self):
        with self.lock:
            return {
                "node": self.node,
                "kind": self.kind,
                "arg": self.arg,
                "frames": self.seq,
                "written": self.written,
                "errors": self.errors,
                "since": self.since,
                "source_frames": self.reader.frames if self.reader else 0,
            }


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
        try:
            router.set_source(kind, arg)
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
    main()
