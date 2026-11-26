import subprocess, select, os, time, glob
cmd=["/root/ffmpeg","-hide_banner","-loglevel","error","-fflags","nobuffer","-flags","low_delay",
     "-probesize","32","-analyzeduration","0","-f","mjpeg","-i","pipe:0",
     "-vf","scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2:color=black,fps=30,format=yuv420p",
     "-f","rawvideo","-pix_fmt","yuv420p","-"]
p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
os.set_blocking(p.stdout.fileno(),False)
time.sleep(4)                      # camd: node sits idle for a while first
frames=sorted(glob.glob("/tmp/jf/*.jpg"))
for i in range(30):
    d=open(frames[i%len(frames)],"rb").read()
    v=memoryview(d)
    while v:
        n=p.stdin.write(v)
        if not n: break
        v=v[n:]
    p.stdin.flush()
    time.sleep(0.1)
buf=b""; end=time.time()+10
while time.time()<end:
    r,_,_=select.select([p.stdout],[],[],0.5)
    if r:
        c=p.stdout.read(460800*4)
        if c: buf+=c
print("bytes",len(buf),"frames",len(buf)//460800,"poll",p.poll())
p.kill()
