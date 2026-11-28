import socket, struct, sys, time
DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 600
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
s.settimeout(1.0)
end = time.time() + DUR
def ts(): return time.strftime('%H:%M:%S')
def dns_name(b, off):
    labels = []
    while off < len(b) and b[off] and len(labels) < 12:
        n = b[off]
        if n & 0xC0: break
        labels.append(b[off+1:off+1+n].decode('latin1')); off += 1 + n
    return '.'.join(labels)
def sni(b):
    i = b.find(b'\x00\x00')  # fallback scan below
    # walk TLS record -> handshake -> extensions looking for server_name
    try:
        if b[0] != 22: return ''
        p = 5 + 4 + 2 + 32 + 1
        cs = b[5+4+2+32]
        p += cs + 2
        cm = b[p-1] if False else 0
        # simpler: search for the server_name extension pattern
        idx = b.find(b'\x00\x00\x00')
        for j in range(43, len(b)-9):
            if b[j] == 0 and b[j+1] == 0 and b[j+4] == 0 and b[j+5] == 0:
                ln = b[j+7] << 8 | b[j+8]
                if 3 < ln < 80 and j+9+ln <= len(b):
                    name = b[j+9:j+9+ln]
                    if all(32 < c < 127 for c in name) and b'.' in name:
                        return name.decode('latin1')
    except Exception: pass
    return ''
flows = {}
while time.time() < end:
    try: pkt = s.recv(65535)
    except socket.timeout: continue
    if len(pkt) < 42 or pkt[12:14] != b'\x08\x00': continue
    ip = pkt[14:]
    ihl = (ip[0] & 15) * 4
    src = socket.inet_ntoa(ip[12:16]); dst = socket.inet_ntoa(ip[16:20])
    proto = ip[9]
    if not (src.startswith('172.17.') or dst.startswith('172.17.')): continue
    if proto == 17:
        udp = ip[ihl:]
        sp, dp = struct.unpack('!HH', udp[:4])
        if dp == 53:
            print(ts(), 'DNS', src, dns_name(udp, 12), flush=True)
        continue
    if proto != 6: continue
    tcp = ip[ihl:]
    sp, dp = struct.unpack('!HH', tcp[:4])
    port = dp if sp < 1024 or dp in (80, 443, 8080) else sp
    if port not in (80, 443, 8080): continue
    fl = tcp[13]; off = (tcp[12] >> 4) * 4
    payload = tcp[off:]
    key = (src, sp, dst, dp)
    if fl & 0x02 and not fl & 0x10:
        print(ts(), 'SYN', src, '->', dst, dp, flush=True)
    if fl & 0x04:
        print(ts(), 'RST', src, sp, '->', dst, dp, flush=True)
    if not payload: continue
    flows[key] = flows.get(key, 0) + len(payload)
    if dp in (80, 8080) and payload[:4] in (b'POST', b'GET '):
        head = payload.split(b'\r\n\r\n')[0].decode('latin1', 'replace')
        lines = [l for l in head.split('\r\n') if l.startswith(('POST','GET','Host','Content-Length','Upgrade'))]
        print(ts(), 'HTTP', src, '->', dst, ' | '.join(lines), flush=True)
    elif sp in (80, 8080) and payload[:5] == b'HTTP/':
        print(ts(), 'RESP', dst, dp, payload[:60].decode('latin1','replace').replace('\r\n',' '), flush=True)
    elif dp == 443 and payload[:1] == b'\x16':
        print(ts(), 'TLS_CH', src, '->', dst, sni(payload), flush=True)
print('FLOWS', sorted(flows.items(), key=lambda kv: -kv[1])[:15], flush=True)