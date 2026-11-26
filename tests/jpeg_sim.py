import subprocess, select, os, time, glob
cmd=["/root/ffmpeg","-hide_banner","-loglevel","error","-f","mjpeg","-i","pipe:0",
     "-vf","scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2:color=black,fps=30,format=yuv420p",
     "-f","rawvideo","-pix_fmt","yuv420p","-"]
p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
os.set_blocking(p.stdout.fileno(),False)
frames=sorted(glob.glob("/tmp/jf/*.jpg"))[:5]
print("jpegs",[(f,os.path.getsize(f)) for f in frames])
for f in frames:
    d=open(f,"rb").read()
    n=p.stdin.write(d); p.stdin.flush(); print("wrote",n,len(d)); time.sleep(0.1)
got=0; buf=b""
end=time.time()+6
while time.time()<end and got<3:
    r,_,_=select.select([p.stdout],[],[],0.5)
    if not r: continue
    c=p.stdout.read(460800*2)
    if c: buf+=c; got=len(buf)//460800
print("frames",got,"poll",p.poll())
p.kill()
print("stderr", p.stderr.read()[:300])
