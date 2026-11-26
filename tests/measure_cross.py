import subprocess,sys
FF="/root/ffmpeg"
path=sys.argv[1]
d=subprocess.run([FF,"-hide_banner","-loglevel","error","-i",path,"-f","rawvideo","-pix_fmt","rgb24","-"],capture_output=True,check=True).stdout
w,h=1080,2400
X0,X1,Y0,Y1=200,880,500,1950
rows={}; cols={}
for y in range(Y0,Y1):
    base=y*w*3
    run=best=0
    for x in range(X0,X1):
        i=base+x*3
        if d[i]>200 and d[i+1]>200 and d[i+2]>200:
            run+=1
            if run>best: best=run
        else: run=0
    if best>=20: rows[y]=best
    for x in ():
        pass
for x in range(X0,X1):
    run=best=0
    for y in range(Y0,Y1):
        i=(y*w+x)*3
        if d[i]>200 and d[i+1]>200 and d[i+2]>200:
            run+=1
            if run>best: best=run
        else: run=0
    if best>=20: cols[x]=best
if not rows or not cols:
    print("  no cross found"); sys.exit(0)
hw=max(rows.values()); vh=max(cols.values())
hy=[y for y,l in rows.items() if l>hw*0.9]; vx=[x for x,l in cols.items() if l>vh*0.9]
print("  horiz_bar=%d px (rows %d..%d) vert_bar=%d px (cols %d..%d) ratio_h_over_v=%.3f" % (hw,hy[0],hy[-1],vh,vx[0],vx[-1],hw/float(vh)))
