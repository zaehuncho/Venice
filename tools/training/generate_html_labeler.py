#!/usr/bin/env python3
"""Generate a SELF-CONTAINED HTML stamina-bar labeler (opens in any phone/desktop browser, no GUI app).

Embeds the frames as base64, shows one at a time on a canvas; the user drags a box around the stamina
bar (touch or mouse), then taps Copy to get YOLO-format label lines to paste back. Exact coordinates
(canvas->image), so no letterbox/alignment problems like phone-photo markups. See docs/ANIMATION_ANCHOR.md.

Usage: C:\\Python314\\python.exe tools/training/generate_html_labeler.py [--max 30] [--out logs/diagnostics/bar_label/labeler.html]
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default="logs/diagnostics/bar_label/images/*.jpg")
    ap.add_argument("--out", default="logs/diagnostics/bar_label/labeler.html")
    ap.add_argument("--max", type=int, default=30)
    args = ap.parse_args()

    files = sorted(glob.glob(args.images))[: args.max]
    if not files:
        print("no frames - run extract_bar_label_frames.py first")
        return 1
    import cv2
    items = []
    for f in files:
        im = cv2.imread(f)
        if im is None:
            continue
        h, w = im.shape[:2]
        if w > 1280:                                  # downscale for a light phone-friendly file
            im = cv2.resize(im, (1280, int(h * 1280 / w)))
        ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 70])
        b64 = base64.b64encode(buf.tobytes()).decode()  # labels are NORMALIZED -> resolution-independent
        items.append({"name": os.path.basename(f), "data": "data:image/jpeg;base64," + b64})
    payload = json.dumps(items)

    html = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Bar Labeler</title><style>
body{margin:0;font-family:sans-serif;background:#111;color:#eee;text-align:center}
#c{touch-action:none;max-width:100vw;background:#000}
button{font-size:17px;padding:10px 14px;margin:4px}
#out{width:96%;height:90px}#hud{padding:6px;font-size:15px}
</style></head><body>
<div id=hud></div>
<canvas id=c></canvas>
<div>
<button onclick=prev()>&lt; Prev</button>
<button onclick=nobar()>No bar</button>
<button onclick=clr()>Clear box</button>
<button onclick=next()>Next &gt;</button>
</div>
<div><button onclick=copy() style="background:#2a7">Copy labels</button></div>
<textarea id=out placeholder="labels appear here -> paste back to chat"></textarea>
<script>
const IMG=__PAYLOAD__;
let i=0, boxes={}, drag=null, img=new Image(), c=document.getElementById('c'), x=c.getContext('2d'), sc=1;
function load(){img=new Image();img.onload=draw;img.src=IMG[i].data;}
function draw(){
 let W=Math.min(window.innerWidth, img.width); sc=W/img.width;
 c.width=img.width*sc; c.height=img.height*sc;
 x.drawImage(img,0,0,c.width,c.height);
 let b=boxes[IMG[i].name];
 if(b){x.strokeStyle='#0f0';x.lineWidth=3;x.strokeRect(b[0]*sc,b[1]*sc,b[2]*sc,b[3]*sc);}
 if(drag){x.strokeStyle='#ff0';x.lineWidth=2;x.strokeRect(drag.x0,drag.y0,drag.x1-drag.x0,drag.y1-drag.y0);}
 document.getElementById('hud').textContent=(i+1)+'/'+IMG.length+'  '+IMG[i].name+'   (drag a box around YOUR stamina bar)';
}
function pos(e){let r=c.getBoundingClientRect();let t=e.touches?e.touches[0]:e;return{x:t.clientX-r.left,y:t.clientY-r.top};}
function down(e){e.preventDefault();let p=pos(e);drag={x0:p.x,y0:p.y,x1:p.x,y1:p.y};}
function move(e){if(!drag)return;e.preventDefault();let p=pos(e);drag.x1=p.x;drag.y1=p.y;draw();}
function up(e){if(!drag)return;e.preventDefault();
 let x0=Math.min(drag.x0,drag.x1),y0=Math.min(drag.y0,drag.y1),w=Math.abs(drag.x1-drag.x0),h=Math.abs(drag.y1-drag.y0);
 if(w>4&&h>4) boxes[IMG[i].name]=[x0/sc,y0/sc,w/sc,h/sc]; drag=null; draw();}
c.addEventListener('mousedown',down);c.addEventListener('mousemove',move);c.addEventListener('mouseup',up);
c.addEventListener('touchstart',down,{passive:false});c.addEventListener('touchmove',move,{passive:false});c.addEventListener('touchend',up,{passive:false});
function next(){if(i<IMG.length-1){i++;drag=null;load();}}
function prev(){if(i>0){i--;drag=null;load();}}
function nobar(){boxes[IMG[i].name]='none';next();}
function clr(){delete boxes[IMG[i].name];draw();}
function copy(){let s='';for(const it of IMG){let b=boxes[it.name];if(!b)continue;
  if(b==='none'){s+=it.name+' none\\n';continue;}
  let cx=(b[0]+b[2]/2)/img.width,cy=(b[1]+b[3]/2)/img.height,w=b[2]/img.width,h=b[3]/img.height;
  // note: img is the CURRENT image; widths differ per frame but all are 1920x1080 here
  s+=it.name+' '+cx.toFixed(4)+' '+cy.toFixed(4)+' '+w.toFixed(4)+' '+h.toFixed(4)+'\\n';}
 document.getElementById('out').value=s; document.getElementById('out').select();
 try{document.execCommand('copy');}catch(e){}}
load();
</script></body></html>"""
    html = html.replace("__PAYLOAD__", payload)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"wrote {args.out} ({len(files)} frames, {os.path.getsize(args.out)//1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
