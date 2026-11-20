"""Use a phone as a remote control.

A small web page served on the home network. Opening its address in the phone's browser
gives play/pause, seeking, volume, skip intro and next episode. The address holds a random
key that changes every time CineHaven starts, so only someone who can see the screen can use it.
Commands are handed to the main window on the GTK thread.
"""
import json
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cast import lan_ip

PREFERRED_PORT = 8765

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#16161d"><title>CineHaven remote</title>
<style>
:root{--bg:#16161d;--card:#23232e;--fg:#f3f3f6;--dim:#9a9aae;--acc:#e5a50a}
@media (prefers-color-scheme: light){:root{--bg:#f4f4f7;--card:#fff;--fg:#1d1d24;--dim:#6b6b7a;--acc:#c27c0e}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--fg);font:16px system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
 padding:16px;max-width:520px;margin:auto;user-select:none}
h1{font-size:15px;color:var(--dim);font-weight:600;margin:4px 0 14px;letter-spacing:.04em;text-transform:uppercase}
#title{font-size:21px;font-weight:700;margin:0 0 4px;min-height:1.3em}
#sub{color:var(--dim);font-size:14px;min-height:1.2em}
.card{background:var(--card);border-radius:18px;padding:16px;margin:14px 0}
input[type=range]{width:100%;accent-color:var(--acc);height:34px}
.times{display:flex;justify-content:space-between;color:var(--dim);font-size:13px;font-variant-numeric:tabular-nums}
.row{display:flex;gap:10px;justify-content:center;align-items:center}
button{flex:1;border:0;border-radius:16px;background:var(--card);color:var(--fg);font-size:17px;font-weight:600;
 padding:16px 8px;min-height:58px}
button:active{transform:scale(.96);opacity:.8}
#pp{flex:1.4;background:var(--acc);color:#111;font-size:26px}
.extra button{font-size:15px}
#skip,#next{display:none;background:var(--acc);color:#111}
#off{display:none;text-align:center;color:var(--dim);padding:30px 0}
</style></head><body>
<h1>CineHaven remote</h1>
<div id="on">
 <div id="title">Nothing playing</div><div id="sub"></div>
 <div class="card">
  <input id="seek" type="range" min="0" max="1" step="1" value="0">
  <div class="times"><span id="pos">0:00</span><span id="dur">--:--</span></div>
 </div>
 <div class="row"><button data-c="back">⏪ 10</button><button id="pp" data-c="toggle">⏯</button><button data-c="fwd">30 ⏩</button></div>
 <div class="row" style="margin-top:10px"><button id="skip" data-c="skip">Skip intro ⏭</button><button id="next" data-c="next">Next episode ▶</button></div>
 <div class="card"><div class="row"><span>🔈</span><input id="vol" type="range" min="0" max="150" step="5"><span>🔊</span></div></div>
 <div class="row extra"><button data-c="subs">Subtitles</button><button data-c="audio">Audio</button><button data-c="full">Full screen</button></div>
 <div class="row extra" style="margin-top:10px"><button data-c="stop">⏹ Stop</button></div>
</div>
<div id="off">CineHaven isn't reachable – is it still open on the computer?</div>
<script>
const K=location.pathname.split('/')[1];let drag=false,vdrag=false;
const $=id=>document.getElementById(id);
const fmt=t=>{if(t==null||isNaN(t))return'--:--';t=Math.floor(t);const h=Math.floor(t/3600),m=Math.floor(t/60)%60,s=t%60;
 return(h?h+':'+String(m).padStart(2,'0'):m)+':'+String(s).padStart(2,'0')};
function cmd(c,v){fetch('/'+K+'/cmd',{method:'POST',body:JSON.stringify({c:c,v:v})}).then(poll);if(navigator.vibrate)navigator.vibrate(15)}
document.querySelectorAll('button[data-c]').forEach(b=>b.onclick=()=>cmd(b.dataset.c));
$('seek').oninput=()=>{drag=true;$('pos').textContent=fmt(+$('seek').value)};
$('seek').onchange=()=>{cmd('seek',+$('seek').value);drag=false};
$('vol').oninput=()=>{vdrag=true};$('vol').onchange=()=>{cmd('vol',+$('vol').value);vdrag=false};
function poll(){fetch('/'+K+'/state',{cache:'no-store'}).then(r=>r.json()).then(s=>{
 $('on').style.display='';$('off').style.display='none';
 $('title').textContent=s.title||'Nothing playing';$('sub').textContent=s.where||'';
 $('pp').textContent=s.paused?'▶':'⏸';
 if(!drag){$('seek').max=Math.max(1,Math.floor(s.dur||1));$('seek').value=Math.floor(s.pos||0);
  $('pos').textContent=fmt(s.pos);$('dur').textContent=s.live?'LIVE':fmt(s.dur)}
 if(!vdrag&&s.vol!=null)$('vol').value=s.vol;
 $('skip').style.display=s.skip?'block':'none';if(s.skip)$('skip').textContent=s.skip+' ⏭';
 $('next').style.display=s.next?'block':'none';
}).catch(()=>{$('on').style.display='none';$('off').style.display='block'})}
poll();setInterval(poll,1000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "CineHaven"

    def log_message(self, *_a):
        pass

    def _key_ok(self):
        parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        if not parts or not secrets.compare_digest(parts[0], self.server.key):
            self.send_error(404)
            return None
        return parts[1:]

    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        rest = self._key_ok()
        if rest is None:
            return
        if not rest or rest == [""]:
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif rest == ["state"]:
            self._send(200, json.dumps(self.server.state), "application/json")
        else:
            self.send_error(404)

    def do_POST(self):
        rest = self._key_ok()
        if rest != ["cmd"]:
            if rest is not None:
                self.send_error(404)
            return
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 1000)
            d = json.loads(self.rfile.read(n) or b"{}")
            c, v = str(d.get("c", "")), d.get("v")
        except (ValueError, TypeError):
            self.send_error(400)
            return
        if c in self.server.commands:
            self.server.on_command(c, v if isinstance(v, (int, float)) else None)
            self._send(200, "{}", "application/json")
        else:
            self.send_error(400)


class Remote:
    COMMANDS = {"toggle", "back", "fwd", "seek", "vol", "skip", "next", "subs", "audio", "full", "stop"}

    def __init__(self, on_command):
        """on_command(c, v) is called from a server thread – the caller moves it to the GTK thread."""
        self.on_command = on_command
        self.httpd = None
        self.state = {}

    @property
    def running(self):
        return self.httpd is not None

    def start(self):
        if self.httpd:
            return self.url
        for port in (PREFERRED_PORT, 0):
            try:
                self.httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
                break
            except OSError:
                continue
        h = self.httpd
        h.daemon_threads = True
        h.key = secrets.token_urlsafe(6)
        h.state = self.state
        h.commands = self.COMMANDS
        h.on_command = self.on_command
        threading.Thread(target=h.serve_forever, daemon=True).start()
        return self.url

    @property
    def url(self):
        if not self.httpd:
            return None
        return f"http://{lan_ip()}:{self.httpd.server_address[1]}/{self.httpd.key}/"

    def update(self, **state):
        self.state = state                    # swapped whole, so a reader never sees it half-written
        if self.httpd:
            self.httpd.state = state

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


def qr_texture(text):
    """A QR code as a Gdk.Texture if a QR library is installed (segno or qrcode), else None."""
    rows = None
    try:
        import segno
        rows = [list(r) for r in segno.make(text, error="m", boost_error=False).matrix]
    except Exception:
        try:
            import qrcode
            q = qrcode.QRCode(border=0)
            q.add_data(text)
            q.make(fit=True)
            rows = [[1 if c else 0 for c in r] for r in q.get_matrix()]
        except Exception:
            return None
    from gi.repository import GLib, Gdk
    scale, border = 8, 4
    n = len(rows)
    size = (n + 2 * border) * scale
    px = bytearray(b"\xff" * size * size)
    for y, r in enumerate(rows):
        for x, on in enumerate(r):
            if on:
                for dy in range(scale):
                    o = ((y + border) * scale + dy) * size + (x + border) * scale
                    px[o:o + scale] = b"\x00" * scale
    return Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.G8, GLib.Bytes.new(bytes(px)), size)
