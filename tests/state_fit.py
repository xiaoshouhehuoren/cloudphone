import json,sys,urllib.request
node=sys.argv[1] if len(sys.argv)>1 else "/dev/video20"
d=json.load(urllib.request.urlopen("http://127.0.0.1:8100/state",timeout=5))
for n in d["nodes"]:
    if n["node"]==node:
        g=n.get("geometry") or {}
        print(n.get("kind"),g.get("fit"),g.get("content_w"),g.get("content_h"),g.get("display_dar"),"src_frames",n.get("source_frames"),"written",n.get("written"),"err",n.get("errors"))
