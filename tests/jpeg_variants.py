import subprocess, select, os, time, glob
frames=sorted(glob.glob("/tmp/jf/*.jpg"))[:10]
def run(name, extra, infmt):
    cmd=["/root/ffmpeg","-hide_banner","-loglevel","error"]+extra+["-f",infmt,"-i","pipe:0",
         "-vf","scale=640:480,format=yuv420p","-f","rawvideo","-pix_fmt","yuv420p","-"]
    p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
    os.set_blocking(p.stdout.fileno(),False)
    for f in frames:
        try:
            p.stdin.write(open(f,"rb").read()); p.stdin.flush()
        except Exception as e:
            print(name,"write err",e); break
        time.sleep(0.05)
    buf=b""; end=time.time()+4
    while time.time()<end and len(buf)<460800:
        r,_,_=select.select([p.stdout],[],[],0.5)
        if r:
            c=p.stdout.read(460800)
            if c: buf+=c
    print(name, "bytes", len(buf), "frames", len(buf)//460800)
    p.kill()
run("mjpeg-plain", [], "mjpeg")
run("image2pipe", ["-framerate","30"], "image2pipe")
run("mjpeg-nobuffer", ["-fflags","nobuffer","-flags","low_delay","-probesize","32","-analyzeduration","0"], "mjpeg")
