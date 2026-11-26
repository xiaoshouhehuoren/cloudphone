import subprocess, glob, time
frames=sorted(glob.glob("/tmp/jf/*.jpg"))
data=b"".join(open(f,"rb").read() for f in frames[:40])
open("/tmp/all.mjpeg","wb").write(data)
r=subprocess.run(["/root/ffmpeg","-hide_banner","-loglevel","error","-f","mjpeg","-i","/tmp/all.mjpeg","-f","null","-"],capture_output=True,text=True)
print("file-decode rc",r.returncode,"err",r.stderr[:100],"size",len(data))
t0=time.time()
for i in range(5):
    r=subprocess.run(["/root/ffmpeg","-hide_banner","-loglevel","error","-f","mjpeg","-i",frames[i],"-vf","scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p","-f","rawvideo","-pix_fmt","yuv420p","-"],capture_output=True)
    print("one rc",r.returncode,"bytes",len(r.stdout))
print("per-frame ms", (time.time()-t0)/5*1000)
