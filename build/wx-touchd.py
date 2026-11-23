#!/usr/bin/env python3
"""Per-container touch bridge for cloudphone wxN.

Keeps one inputd.InputDaemon alive inside the Android container and exposes a
line-delimited JSON control socket.  wx-start.sh starts one instance per
container via g98-wx-touchd@N.service.

Protocol (one JSON object per line):
  {"a":"t","fx":0.5,"fy":0.5}   tap, normalized to the container screen
  {"a":"d","fx":0.5,"fy":0.5}   ACTION_DOWN
  {"a":"m","fx":0.5,"fy":0.5}   ACTION_MOVE
  {"a":"u","fx":0.5,"fy":0.5}   ACTION_UP
  {"a":"k","code":4}            KeyEvent
  {"a":"raw","line":"T 540 1200"} raw inputd command
Absolute {"x":540,"y":1200} is accepted as an alternative to fx/fy.

The Unix socket is /run/g98/wx-touch-<N>.sock (mode 0600).  The service also
binds 127.0.0.1:1810<N> for a local control plane; it is not exposed publicly.
"""
import argparse
import json
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

JAR_HOST = "/srv/redroid/inputd.jar"
JAR_GUEST = "/data/local/tmp/inputd.jar"
SOCK_DIR = "/run/g98"
DEFAULT_SCREEN = (1080, 2400)
ENV_KEYS = ("ANDROID_", "BOOTCLASSPATH=", "SYSTEMSERVERCLASSPATH=")
RAW_RE = re.compile(r"^[DMTUK](?:\s+-?\d+(?:\.\d+)?){1,2}$")


class Bridge:
    def __init__(self, n):
        self.n = n
        self.name = "wx%d" % n
        self.sock_path = os.path.join(SOCK_DIR, "wx-touch-%d.sock" % n)
        self.env_path = os.path.join(SOCK_DIR, "wx-touch-%d.env" % n)
        self.proc = None
        self.proc_lock = threading.Lock()
        self.ready = threading.Event()
        self.screen = DEFAULT_SCREEN
        self.stop_event = threading.Event()

    def log(self, msg):
        print("[wx-touchd %s] %s" % (self.name, msg), flush=True)

    def run(self, args, timeout=20):
        return subprocess.run(
            ["docker", "exec", self.name] + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout,
        )

    def container_running(self):
        try:
            r = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self.name],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=10,
            )
            return r.returncode == 0 and r.stdout.strip() == "true"
        except Exception:
            return False

    def wait_container(self, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            if self.container_running():
                r = self.run(["getprop", "sys.boot_completed"], timeout=8)
                if r.returncode == 0 and r.stdout.strip() == "1":
                    return True
            time.sleep(1)
        return False

    def ensure_jar(self):
        if not os.path.exists(JAR_HOST):
            raise RuntimeError("missing %s" % JAR_HOST)
        r = self.run(["test", "-s", JAR_GUEST], timeout=8)
        if r.returncode != 0:
            r = subprocess.run(
                ["docker", "cp", JAR_HOST, "%s:%s" % (self.name, JAR_GUEST)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
            )
            if r.returncode != 0:
                raise RuntimeError("docker cp inputd.jar: %s" % (r.stderr or r.stdout))
            self.log("copied inputd.jar")

    def build_env(self):
        """app_process needs the Android boot classpath from a live app process."""
        for pkg in ("com.salespartner.workphone.agent", "zygote64", "system_server"):
            r = self.run(["pidof", pkg], timeout=8)
            if r.returncode != 0:
                continue
            for pid in (r.stdout or "").split():
                if not pid.isdigit():
                    continue
                r2 = self.run(
                    ["sh", "-c", "tr '\\0' '\\n' < /proc/%s/environ" % pid],
                    timeout=8,
                )
                if r2.returncode != 0:
                    continue
                keep = [
                    line for line in (r2.stdout or "").splitlines()
                    if line.startswith(ENV_KEYS)
                ]
                if not any(line.startswith("BOOTCLASSPATH=") for line in keep):
                    continue
                keep.append("CLASSPATH=" + JAR_GUEST)
                os.makedirs(SOCK_DIR, exist_ok=True)
                with open(self.env_path, "w") as fh:
                    fh.write("\n".join(keep) + "\n")
                os.chmod(self.env_path, 0o600)
                self.log("env from %s pid=%s" % (pkg, pid))
                return True
        return False

    def refresh_screen(self):
        try:
            r = self.run(["wm", "size"], timeout=8)
            found = re.findall(r"(\d+)x(\d+)", r.stdout or "")
            if found:
                w, h = found[-1]
                self.screen = (int(w), int(h))
        except Exception:
            pass
        self.log("screen %dx%d" % self.screen)

    def _reader(self, proc):
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line == "READY":
                    self.ready.set()
                if line:
                    self.log("inputd: " + line[:300])
        except Exception as exc:
            self.log("reader: %s" % exc)
        finally:
            self.ready.clear()

    def start(self):
        with self.proc_lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            if self.proc is not None:
                self._kill_locked()
            if not self.wait_container():
                raise RuntimeError("container %s not booted" % self.name)
            self.ensure_jar()
            env_ok = False
            for _ in range(30):
                if self.build_env():
                    env_ok = True
                    break
                time.sleep(1)
            if not env_ok:
                raise RuntimeError("cannot obtain Android boot classpath")
            self.refresh_screen()
            self.ready.clear()
            cmd = [
                "docker", "exec", "-i", "--env-file", self.env_path,
                self.name, "sh", "-c", "app_process /data/local/tmp inputd.InputDaemon",
            ]
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        if not self.ready.wait(45):
            raise RuntimeError("inputd did not print READY")
        self.log("inputd ready pid=%s" % self.proc.pid)

    def _kill_locked(self):
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def stop(self):
        self.stop_event.set()
        with self.proc_lock:
            self._kill_locked()
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass

    def _coord(self, req):
        w, h = self.screen
        if "x" in req and "y" in req:
            x, y = float(req["x"]), float(req["y"])
        else:
            x = float(req.get("fx", 0.5)) * (w - 1)
            y = float(req.get("fy", 0.5)) * (h - 1)
        x = max(0.0, min(float(w - 1), x))
        y = max(0.0, min(float(h - 1), y))
        return int(round(x)), int(round(y))

    def dispatch(self, req):
        a = str(req.get("a", "")).lower()
        if a == "raw":
            line = str(req.get("line", "")).strip()
            if not RAW_RE.match(line):
                return {"ok": False, "error": "invalid raw command"}
        elif a in ("t", "tap", "d", "down", "m", "move", "u", "up"):
            code = {"t": "T", "tap": "T", "d": "D", "down": "D",
                    "m": "M", "move": "M", "u": "U", "up": "U"}[a]
            x, y = self._coord(req)
            line = "%s %d %d" % (code, x, y)
        elif a in ("k", "key"):
            code = int(req.get("code"))
            if not 0 <= code <= 1000:
                return {"ok": False, "error": "bad key code"}
            line = "K %d" % code
        else:
            return {"ok": False, "error": "unknown action"}

        if self.proc is None or self.proc.poll() is not None:
            self.start()
        with self.proc_lock:
            p = self.proc
            if p is None or p.poll() is not None:
                return {"ok": False, "error": "inputd unavailable"}
            try:
                p.stdin.write((line + "\n").encode())
                p.stdin.flush()
            except Exception as exc:
                self._kill_locked()
                return {"ok": False, "error": str(exc)}
        return {"ok": True, "line": line, "w": self.screen[0], "h": self.screen[1]}


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class _TcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(socketserver.StreamRequestHandler):
    bridge = None

    def handle(self):
        for raw in self.rfile:
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                req = json.loads(text)
            except Exception:
                if RAW_RE.match(text):
                    req = {"a": "raw", "line": text}
                else:
                    req = {"a": "raw", "line": text}
            try:
                if req.get("a") == "raw" and not RAW_RE.match(str(req.get("line", ""))):
                    resp = {"ok": False, "error": "invalid raw command"}
                else:
                    resp = self.bridge.dispatch(req)
            except Exception as exc:
                resp = {"ok": False, "error": str(exc)}
            self.wfile.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("n", type=int, help="container number, e.g. 1 for wx1")
    parser.add_argument("--tcp", default="", help="optional loopback TCP bind, e.g. 127.0.0.1:18101")
    args = parser.parse_args()
    if not 1 <= args.n <= 99:
        parser.error("n must be 1..99")

    os.makedirs(SOCK_DIR, exist_ok=True)
    bridge = Bridge(args.n)
    _Handler.bridge = bridge
    try:
        os.unlink(bridge.sock_path)
    except OSError:
        pass

    bridge.start()
    server = _UnixServer(bridge.sock_path, _Handler)
    os.chmod(bridge.sock_path, 0o600)
    bridge.log("unix %s" % bridge.sock_path)

    tcp = None
    if args.tcp:
        host, _, port = args.tcp.rpartition(":")
        try:
            tcp = _TcpServer((host or "127.0.0.1", int(port)), _Handler)
            threading.Thread(target=tcp.serve_forever, daemon=True).start()
            bridge.log("tcp %s" % args.tcp)
        except Exception as exc:
            bridge.log("tcp disabled: %s" % exc)

    def _term(_sig, _frame):
        bridge.log("stopping")
        bridge.stop()
        if tcp is not None:
            tcp.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    try:
        server.serve_forever()
    finally:
        bridge.stop()
        server.server_close()
        if tcp is not None:
            tcp.server_close()


if __name__ == "__main__":
    main()
