#!/usr/bin/env python3
"""Transparent TCP and DNS proxy for a redroid container.

Run this inside the container network namespace, for example:
  nsenter -t <container-pid> -n /usr/bin/python3 g98-native-proxy.py

iptables redirects external TCP to --listen and UDP/53 to --dns-listen.
The proxy tunnels each flow through the per-container HTTP CONNECT proxy.
"""

import argparse
import ipaddress
import select
import signal
import socket
import struct
import sys
import threading
import time

BUFSIZE = 65536
SO_ORIGINAL_DST = 80
LOG_LOCK = threading.Lock()


def log(*args):
    with LOG_LOCK:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, file=sys.stderr, flush=True)


def parse_addr(value, default_port):
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host, port
    if ":" in value:
        host, port = value.rsplit(":", 1)
        return host, int(port)
    return value, default_port


def recv_exact(sock, size):
    out = b""
    while len(out) < size:
        chunk = sock.recv(size - len(out))
        if not chunk:
            raise OSError("unexpected eof")
        out += chunk
    return out


def open_upstream(host, port, dst_ip, dst_port, timeout=25):
    sock = socket.create_connection((host, port), timeout)
    sock.settimeout(timeout)
    req = (
        "CONNECT %s:%d HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "Proxy-Connection: keep-alive\r\n\r\n"
        % (dst_ip, dst_port, dst_ip, dst_port)
    )
    sock.sendall(req.encode("ascii"))
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("proxy closed before CONNECT response")
        head += chunk
        if len(head) > 65536:
            raise OSError("CONNECT response too large")
    first = head.split(b"\r\n", 1)[0]
    if b" 200 " not in first:
        raise OSError("CONNECT failed: %s" % first.decode("latin-1", "replace"))
    sock.settimeout(None)
    return sock


def original_dst(sock):
    raw = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
    if len(raw) < 8:
        raise OSError("short SO_ORIGINAL_DST")
    port = struct.unpack("!H", raw[2:4])[0]
    return socket.inet_ntoa(raw[4:8]), port


def relay(a, b):
    readers = {a, b}
    try:
        while readers:
            readable, _, broken = select.select(list(readers), [], list(readers), 120)
            if broken or not readable:
                break
            for src in readable:
                dst = b if src is a else a
                data = src.recv(BUFSIZE)
                if not data:
                    readers.discard(src)
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                dst.sendall(data)
    except (OSError, ValueError):
        return


def is_excluded(ip, networks):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return any(addr in net for net in networks)


class TcpHandler(threading.Thread):
    def __init__(self, client, peer, args):
        super().__init__(daemon=True)
        self.client = client
        self.peer = peer
        self.args = args

    def run(self):
        upstream = None
        dst_ip = "?"
        dst_port = 0
        try:
            dst_ip, dst_port = original_dst(self.client)
            if is_excluded(dst_ip, self.args.exclude):
                upstream = socket.create_connection((dst_ip, dst_port), 20)
            else:
                upstream = open_upstream(
                    self.args.upstream_host,
                    self.args.upstream_port,
                    dst_ip,
                    dst_port,
                )
            relay(self.client, upstream)
        except Exception as exc:
            log("tcp", self.peer, "->", dst_ip, dst_port, "error", exc)
        finally:
            for sock in (self.client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass


def tcp_server(args, stop):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.listen_host, args.listen_port))
    server.listen(128)
    server.settimeout(1.0)
    log("tcp listening", args.listen_host, args.listen_port)
    while not stop.is_set():
        try:
            client, peer = server.accept()
        except socket.timeout:
            continue
        except OSError as exc:
            if stop.is_set():
                break
            log("accept error", exc)
            time.sleep(0.2)
            continue
        TcpHandler(client, peer, args).start()
    server.close()


def dns_exchange(query, args):
    upstream = open_upstream(
        args.upstream_host,
        args.upstream_port,
        args.dns_upstream_host,
        args.dns_upstream_port,
    )
    try:
        upstream.sendall(struct.pack("!H", len(query)) + query)
        size = struct.unpack("!H", recv_exact(upstream, 2))[0]
        return recv_exact(upstream, size)
    finally:
        upstream.close()


def dns_one(server, data, client, args):
    try:
        answer = dns_exchange(data, args)
        server.sendto(answer, client)
    except Exception as exc:
        log("dns", client, "error", exc)


def dns_server(args, stop):
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.dns_listen_host, args.dns_listen_port))
    server.settimeout(1.0)
    log("dns listening", args.dns_listen_host, args.dns_listen_port)
    while not stop.is_set():
        try:
            data, client = server.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError as exc:
            if stop.is_set():
                break
            log("dns recv error", exc)
            continue
        threading.Thread(target=dns_one, args=(server, data, client, args), daemon=True).start()
    server.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="127.0.0.1:19000")
    parser.add_argument("--dns-listen", default="127.0.0.1:19053")
    parser.add_argument("--upstream", default="172.17.0.1:18081")
    parser.add_argument("--dns-upstream", default="8.8.8.8:53")
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()

    args.listen_host, args.listen_port = parse_addr(args.listen, 19000)
    args.dns_listen_host, args.dns_listen_port = parse_addr(args.dns_listen, 19053)
    args.upstream_host, args.upstream_port = parse_addr(args.upstream, 18081)
    args.dns_upstream_host, args.dns_upstream_port = parse_addr(args.dns_upstream, 53)

    exclude = args.exclude or [
        "127.0.0.0/8",
        "172.17.0.0/16",
        "169.254.0.0/16",
        "224.0.0.0/4",
    ]
    args.exclude = []
    for item in exclude:
        try:
            args.exclude.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log("bad exclude", item)

    stop = threading.Event()

    def on_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    threading.Thread(target=tcp_server, args=(args, stop), daemon=True).start()
    threading.Thread(target=dns_server, args=(args, stop), daemon=True).start()
    log("native proxy ready", "upstream", args.upstream, "dns", args.dns_upstream)
    while not stop.is_set():
        time.sleep(1)


if __name__ == "__main__":
    main()
