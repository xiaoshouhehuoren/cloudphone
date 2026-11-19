import socket, subprocess, sys, time
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5701
W, H, BPP = 640, 480, 4
dst = sys.argv[2] if len(sys.argv) > 2 else '/dev/video10'
ff = subprocess.Popen(['/root/ffmpeg','-hide_banner','-loglevel','error',
                        '-f','rawvideo','-pix_fmt','bgra','-s','%dx%d'%(W,H),'-framerate','30','-re',
                        '-i','pipe:0','-pix_fmt','yuv420p','-f','v4l2',dst],
                       stdin=subprocess.PIPE)
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('0.0.0.0', PORT)); s.listen(1)
print('listening on %d -> %s' % (PORT, dst), flush=True)
n = W*H*BPP
while True:
    c, addr = s.accept()
    print('client %s' % (addr,), flush=True)
    frames = 0; t0 = time.time(); last = t0
    try:
        while True:
            buf = bytearray()
            while len(buf) < n:
                d = c.recv(n - len(buf))
                if not d: break
                buf += d
            if len(buf) < n: break
            ff.stdin.write(bytes(buf)); ff.stdin.flush()
            frames += 1
            now = time.time()
            if now - last >= 2.0:
                last = now
                print('frames=%d fps=%.1f' % (frames, frames/(now-t0)), flush=True)
    except Exception as e:
        print('err %s' % e, flush=True)
    finally:
        c.close(); print('client gone, frames=%d' % frames, flush=True)