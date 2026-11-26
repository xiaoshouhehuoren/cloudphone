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
import os
import queue
import select
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
# A reader that produces nothing within START_TIMEOUT seconds is restarted; a
# running source that goes silent for MAX_SILENCE seconds is restarted too.
START_TIMEOUT = float(os.environ.get("G98_CAMD_START_TIMEOUT", "8"))
MAX_SILENCE = float(os.environ.get("G98_CAMD_MAX_SILENCE", "10"))
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


def jpeg_vf():
    return ("scale=%d:%d:force_original_aspect_ratio=decrease,"
            "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p"
            % (W, H, W, H))


def jpeg_one_cmd(path):
    """Decode ONE JPEG file into one YU12 frame (about 44 ms per frame)."""
    return [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "mjpeg",
            "-i", path, "-vf", jpeg_vf(), "-f", "rawvideo",
            "-pix_fmt", "yuv420p", "-"]


def jpeg_cmd():
    """Decode JPEG frames pushed over HTTP (browser fallback when WebRTC fails)."""
    # nobuffer/low_delay: without them the mjpeg demuxer sits on the pipe
    # (never EOF for a live stream) and emits nothing at all.
    # A single long-lived mjpeg decoder on pipe:0 stalls after the first
    # frame here (40 JPEGs in, 1 frame out), so JPEG frames are decoded one
    # at a time by JpegDecoder instead.
    return jpeg_one_cmd("/tmp/camd-unused.jpg")


def source_cmd(kind, arg):
    if kind == "jpeg":
        return jpeg_one_cmd("/tmp/camd-unused.jpg")
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
        self.restarts = 0
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
            self._run_once()
            if self.stop.is_set():
                break
            self.restarts += 1
            print("camd: source %s %s ended, restart #%d" %
                  (self.kind, self.arg, self.restarts), flush=True)
            time.sleep(1.0)

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
        cmd = idle_cmd(self.node) if self.kind == "idle" else source_cmd(self.kind, self.arg)
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

    def __init__(self, node, on_frame):
        super().__init__(daemon=True)
        self.node = node
        self.on_frame = on_frame
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
                res = subprocess.run(jpeg_one_cmd(self.tmp), capture_output=True,
                                     timeout=5)
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
            old, old_dec = self.reader, self.decoder
            self.kind, self.arg, self.since = kind, arg, time.time()
        for obj in (old, old_dec):
            if obj is not None:
                obj.stop.set()
                if getattr(obj, "proc", None) is not None and obj.proc.poll() is None:
                    obj.proc.terminate()
        if kind == "jpeg":
            dec = JpegDecoder(self.node, self._on_frame)
            with self.lock:
                self.reader, self.decoder = None, dec
            dec.start()
            return dec
        reader = Reader(self.node, kind, arg, self._on_frame)
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
                "frames": self.seq,
                "written": self.written,
                "errors": self.errors,
                "since": self.since,
                "source_frames": (self.decoder or self.reader).frames
                                 if (self.decoder or self.reader) else 0,
                "source_restarts": (self.decoder or self.reader).restarts
                                   if (self.decoder or self.reader) else 0,
                "source_dropped": (self.decoder or self.reader).dropped
                                  if (self.decoder or self.reader) else 0,
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
    # kill -USR1 <pid> dumps every reader/writer stack; invaluable when a source
    # silently stops delivering frames.
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except Exception:
        pass
    main()
