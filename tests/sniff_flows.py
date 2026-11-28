import socket, struct, sys, time
DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 600
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3)); s.settimeout(1.0)
end = time.time() + DUR
while time.time() < end:
    try: pkt = s.recv(65535)
    except socket.timeout: continue
    if len(pkt) < 54 or pkt[12:14] != b'\x08\x00': continue
    ip = pkt[14:]
    if ip[9] != 6: continue
    ihl = (ip[0] & 15) * 4
    src = socket.inet_ntoa(ip[12:16]); dst = socket.inet_ntoa(ip[16:20])
    if src != '172.17.0.2' and dst != '172.17.0.2': continue
    tcp = ip[ihl:]
    sp, dp = struct.unpack('!HH', tcp[:4])
    port = dp if dp in (80,443,8080) else sp
    if port not in (80,443,8080): continue
    fl = tcp[13]; off = (tcp[12] >> 4) * 4; n = len(tcp) - off
    flags = ''.join(c for bit, c in ((2,'S'),(16,'A'),(4,'R'),(1,'F')) if fl & bit)
    if n > 0 or (fl & 0x07):
        print(time.strftime('%H:%M:%S'), src, sp, '->', dst, dp, flags, 'len', n, flush=True)