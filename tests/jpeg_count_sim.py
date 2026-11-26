import subprocess, select, os, time, glob
def run(name, infmt, extra):
    cmd=["/root/ffmpeg","-hide_banner","-loglevel","warning"]+extra+["-f",infmt,"-i","pipe:0",
         "-vf","scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p",
         "-f","rawvideo","-pix_fmt","yuv420p","-"]
    p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
    os.set_blocking(p.stdout.fileno(),False)
    frames=sorted(glob.glob("/tmp/jf/*.jpg"))
    for i in range(40):
        d=open(frames[i%len(frames)],"rb").read()
        v=memoryview(d)
        while v:
            n=p.stdin.write(v)
            if not n: break
            v=v[n:]
        p.stdin.flush(); time.sleep(0.1)
    buf=b""; end=time.time()+6
    while time.time()<end:
        r,_,_=select.select([p.stdout],[],[],0.5)
        if r:
            c=p.stdout.read(460800*8)
            if c: buf+=c
    print(name,"frames",len(buf)//460800,"poll",p.poll(),"err",p.stderr.read(200) if p.poll() is not None else b"running")
    p.kill()
run("nobuf-no-probe","mjpeg",["-fflags","nobuffer","-flags","low_delay"])
run("image2pipe","image2pipe",["-framerate","30","-vcodec","mjpeg"])
