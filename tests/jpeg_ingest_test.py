import json, glob, time, urllib.request
def api(path, data=None, ctype="application/json"):
    req=urllib.request.Request("http://127.0.0.1:8100"+path, data=data, headers={"Content-Type":ctype})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())
api("/set", json.dumps({"node":"/dev/video20","kind":"jpeg","url":"webcam"}).encode())
time.sleep(1.2)
frames=sorted(glob.glob("/tmp/jf/*.jpg"))
ok=0
for i in range(40):
    d=open(frames[i%len(frames)],"rb").read()
    req=urllib.request.Request("http://127.0.0.1:8100/frame?node=/dev/video20", data=d, headers={"Content-Type":"image/jpeg"})
    try:
        if json.loads(urllib.request.urlopen(req, timeout=3).read()).get("ok"): ok+=1
    except Exception as e:
        print("post err", e); break
    time.sleep(0.1)
print("posted", ok)
for k in range(6):
    time.sleep(1)
    st=[n for n in api("/state").get("nodes",[]) if n["node"]=="/dev/video20"][0]
    print(k, "src", st["source_frames"], "rst", st["source_restarts"], "kind", st["kind"])
