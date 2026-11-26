import subprocess,sys
FF="/root/ffmpeg"
d=subprocess.run([FF,"-hide_banner","-loglevel","error","-i",sys.argv[1],"-f","rawvideo","-pix_fmt","rgb24","-"],capture_output=True,check=True).stdout
w,h=1080,2400
X0,X1,Y0,Y1=300,780,600,1900
xs=[];ys=[]
for y in range(Y0,Y1):
    row=d[y*w*3:(y+1)*w*3]
    hit=[x for x in range(X0,X1,2) if row[x*3]>190 and row[x*3+1]>190 and row[x*3+2]>190]
    if hit:
        xs.extend([hit[0],hit[-1]]); ys.append(y)
if not ys:
    print("  white bbox: none"); sys.exit(0)
x0,x1=min(xs),max(xs); y0,y1=ys[0],ys[-1]
print("  cross bbox x=%d..%d (w=%d) y=%d..%d (h=%d) bar_ratio=%.3f" % (x0,x1,x1-x0,y0,y1,y1-y0,(x1-x0)/float(y1-y0)))
