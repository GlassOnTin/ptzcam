#!/usr/bin/env python3
"""ptzcam — Logitech Group camera HTTP bridge: live picture + steering.

Endpoints (all GET):
  /            control page: live stream, arrows, zoom slider, crop row
  /stream      multipart/x-mixed-replace MJPEG
  /frame.jpg   latest single frame
  /ptz         op=left|right|up|down|stop|zoom_in|zoom_out with d=<ms>,
               or op=zoom with zoom=<100..1000>
  /crop        c=<1..8> digital crop-zoom centered on the brightest blob
  /status      JSON device state

Camera: /dev/video0, any UVC device exposing relative pan/tilt
(pan_speed/tilt_speed) and zoom_absolute. Tested with the Logitech
Group (046d:0858). Tilt conventions differ between devices/firmware —
if up/down move the wrong way, swap the two tilt_speed signs in ptz().
"""
import subprocess, threading, time, re, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEV = "/dev/video0"
PORT = 8612

TILT_UP = +1      # some firmwares want -1; if up/down feel inverted, swap
TILT_DOWN = -1

_cropgen = 0      # bump to make the capture loop restart with the new CROP
CROP = None       # (w, h, x, y) ffmpeg crop window; None = full frame
_cond = threading.Condition()
_latest = None    # most recent complete JPEG
_gen = 0
_ptzlock = threading.Lock()


# image settings re-applied whenever the capture (re)opens — the device
# resets them on each new stream
IMG_PREFS = [
    ["-c", "exposure_dynamic_framerate=0"],
    ["-c", "auto_exposure=1"],
    ["-c", "exposure_time_absolute=2047"],
    ["-c", "gain=190"],
    ["-c", "backlight_compensation=1"],
]

def _prefs_later():
    time.sleep(1.8)
    apply_img_prefs()

def apply_img_prefs():
    for args in IMG_PREFS:
        v4l2(args)

def capture_cmd():
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-f", "v4l2", "-input_format", "yuyv422",
           "-video_size", "1280x720", "-framerate", "30",
           "-i", DEV]
    if CROP:
        w, h, x, y = CROP
        cmd += ["-vf", f"crop={w}:{h}:{x}:{y},scale=1280:720,fps=8"]
    else:
        cmd += ["-vf", "fps=8"]
    cmd += ["-c:v", "mjpeg", "-q:v", "5", "-an", "-f", "mjpeg", "pipe:1"]
    return cmd


def capture_loop():
    """Runs ffmpeg continuously, splits its MJPEG bitstream into frames."""
    global _latest, _gen, _cropgen
    while True:
        with _cond:
            gen = _cropgen
            cmd = capture_cmd()
        buf = b""
        p = None
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
            apply_img_prefs()
            # the camera resets image settings again when the stream
            # actually starts, so re-assert once it has
            threading.Thread(target=_prefs_later, daemon=True).start()
            print("capture started", flush=True)
            while True:
                if _cropgen != gen:  # crop changed; reopen with new window
                    break
                chunk = p.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:      # emit complete SOI..EOI frames
                    soi = buf.find(b"\xff\xd8")
                    if soi < 0:
                        buf = buf[-2:]
                        break
                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if eoi < 0:
                        buf = buf[soi:]
                        break
                    frame = buf[soi:eoi + 2]
                    buf = buf[eoi + 2:]
                    with _cond:
                        _latest = frame
                        _gen += 1
                        _cond.notify_all()
        except Exception as e:
            print("capture error", e, flush=True)
        finally:
            if p:
                try:
                    p.kill()
                except Exception:
                    pass
        time.sleep(2)


def v4l2(args):
    try:
        subprocess.run(["v4l2-ctl", "-d", DEV] + args,
                       capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def v4l2_get(ctrl):
    try:
        out = subprocess.run(["v4l2-ctl", "-d", DEV, f"--get-ctrl={ctrl}"],
                             capture_output=True, timeout=5).stdout.decode()
    except Exception:
        return None
    m = re.search(r"(?:value|:\s*)(\d+)\s*$", out.strip())
    return int(m.group(1)) if m else None


def ptz(op, d, zoom):
    with _ptzlock:
        if op == "stop":
            v4l2(["-c", "pan_speed=0", "tilt_speed=0"])
        elif op in ("left", "right"):
            v4l2(["-c", f"pan_speed={-1 if op == 'left' else 1}"])
            time.sleep(max(0.05, d) / 1000.0)
            v4l2(["-c", "pan_speed=0"])
        elif op in ("up", "down"):
            v4l2(["-c", f"tilt_speed={TILT_UP if op == 'up' else TILT_DOWN}"])
            time.sleep(max(0.05, d) / 1000.0)
            v4l2(["-c", "tilt_speed=0"])
        elif op in ("zoom_in", "zoom_out"):
            val = v4l2_get("zoom_absolute") or 100
            step = max(5, d // 20)
            val = val + (step if op == "zoom_in" else -step)
            v4l2(["-c", f"zoom_absolute={max(100, min(1000, val))}"])
        elif op == "zoom":
            v4l2(["-c", f"zoom_absolute={max(100, min(1000, zoom))}"])


def sharpness(gray):
    """Focus signal: count strong local gradients (noise and uniform
    shading contribute nothing)."""
    if not gray:
        return 0
    s = 0
    for i, v in enumerate(gray):
        r, c = i // 128, i % 128
        if c < 127:
            d = v - gray[i + 1]
            if d > 24 or d < -24:
                s += abs(d) - 24
        if r < 35:
            d = v - gray[i + 128]
            if d > 24 or d < -24:
                s += abs(d) - 24
    return s

def autofocus():
    """Software AF: sweep focus, sit on sharpest frame. Assumes the subject
    of interest is the dominant sharp/high-contrast object (lit display)."""
    global FOCUS, _cropgen
    best, best_v = None, -1
    curve = []
    for v in (30, 60, 90, 120, 150, 180, 210, 120, 90, 60, 30):
        v4l2(["-c", "focus_automatic_continuous=0"])
        v4l2(["-c", f"focus_absolute={v}"])
        time.sleep(0.65)  # fresh frame at ~3 fps under max exposure
        with _cond:
            frame = _latest
        g = luma128x72(frame) if frame else None
        sc = sharpness(g)
        curve.append([v, sc])
        if sc > best_v:
            best_v, best = sc, v
    if best is not None:
        FOCUS = best
        v4l2(["-c", "focus_automatic_continuous=0"])
        v4l2(["-c", f"focus_absolute={best}"])
    return {"best": best, "sharpness": best_v, "curve": curve}

def op_move(op, d):
    ptz(op, d, 0)


# bright-blob locator: downscale via ffmpeg, centroid of lit pixels
FOCUS = 120       # manual focus, set by slider or AF sweep
EXPO = 2047       # exposure_time_absolute (manual mode)
GAIN = 190        # sensor gain
WB = "auto"       # "auto" or kelvin int
def luma128x72(jpeg):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                        "-i", "pipe:0", "-vf", "scale=128:72",
                        "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       input=jpeg, capture_output=True, timeout=5)
    return p.stdout[:128 * 72] if len(p.stdout) >= 128 * 72 else None


def blob_centroid(gray, thresh=140, min_px=6):
    xs, ys, n = 0, 0, 0
    for i, v in enumerate(gray):
        if v > thresh:
            xs += i % 128
            ys += i // 128
            n += 1
    if n < min_px:
        # dim scenes: re-threshold relative to the brightest pixels present
        m = max(gray) if gray else 0
        if m < 40:
            return None
        t = max(45, int(m * 3 / 4))
        for i, v in enumerate(gray):
            if v > t:
                xs += i % 128
                ys += i // 128
                n += 1
        if n < 12:
            return None
    return (xs / n * 10.0, ys / n * 7.5)  # frame coords at 1280x720


def set_crop(n):
    """Digital crop-zoom to n× (1..8), centered on the brightest blob."""
    global CROP, _cropgen
    n = max(1.0, min(8.0, n))
    with _cond:
        frame = _latest
    c = blob_centroid(luma128x72(frame)) if frame else None
    if c is None and n > 1:
        return {"error": "no bright blob in view to center on"}
    x, y = c if c else (640, 360)
    if CROP:  # c is in displayed coords; map back to full frame
        cw, ch = CROP[0], CROP[1]
        x = CROP[2] + x / 1280.0 * cw
        y = CROP[3] + y / 720.0 * ch
    w, h = int(1280 / n), int(720 / n)
    x = max(0, min(1280 - w, int(x - w / 2)))
    y = max(0, min(720 - h, int(y - h / 2)))
    CROP = (w, h, x, y)
    with _cond:
        _cropgen += 1   # capture loop reopens with the new window
    return {"crop": CROP}


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>ptzcam</title><style>
body{font-family:sans-serif;background:#1f252b;color:#eee;text-align:center;margin:0;padding:8px}
img{max-width:100%;border-radius:8px}
.b{background:#1fa3ec;border:0;border-radius:10px;color:#fff;font-size:1.2rem;padding:12px 22px;margin:3px;cursor:pointer}
.s{background:#55606a}
input[type=range]{width:70%}
</style></head><body>
<img src="/stream" width="640">
<div>
<button class=b onclick="go('up')">▲</button><br>
<button class=b onclick="go('left')">◀</button>
<button class=b onclick="go('stop')">■</button>
<button class=b onclick="go('right')">▶</button><br>
<button class=b onclick="go('down')">▼</button>
</div>
<div>
Zoom <input type=range id=zr min=100 max=1000 value=100 oninput="live('z')">
<span id=zv></span>
</div>
<div>
Focus <input type=range id=fr min=0 max=255 value=120 oninput="live('f')">
<span id=fv></span>
<button class=b onclick="af()">AF hunt</button>
</div>
<div>
Exposure <input type=range id=er min=100 max=2047 value=2047 oninput="live('e')">
<span id=ev></span>
</div>
<div>
WB
<button class=b onclick="wb('auto')">auto</button>
<button class=b onclick="wb(3000)">3000</button>
<button class=b onclick="wb(4600)">4600</button>
<button class=b onclick="wb(5600)">5600</button>
</div>
<div>
Crop:
<button class=b onclick="crop(1)">1×</button>
<button class=b onclick="crop(2)">2×</button>
<button class=b onclick="crop(3)">3×</button>
<button class=b onclick="crop(4)">4×</button>
<button class=b onclick="crop(6)">6×</button>
</div>
<p><span id=cropinfo class=s></span></p>
<script>
async function go(op, d, zoom){
  await fetch('/ptz?op=' + op + (d ? '&d=' + d : '') + (zoom ? '&zoom=' + zoom : ''));
}
let lastSet = 0;
function live(which){
  const now = Date.now();
  if (now - lastSet < 120) { clearTimeout(live._t); live._t = setTimeout(() => live(which), 140); return; }
  lastSet = now;
  if (which === 'z') {
    const v = document.getElementById('zr').value;
    document.getElementById('zv').textContent = (v/100).toFixed(1) + 'x';
    fetch('/ptz?op=zoom&zoom=' + v);
  } else if (which === 'e') {
    const v = document.getElementById('er').value;
    document.getElementById('ev').textContent = v;
    setE(v);
  } else {
    const v = document.getElementById('fr').value;
    document.getElementById('fv').textContent = v;
    fetch('/focus?v=' + v);
  }
}
fetch('/status').then(r => r.json()).then(j => {
  if (j.zoom_absolute) { document.getElementById('zr').value = j.zoom_absolute;
    document.getElementById('zv').textContent = (j.zoom_absolute/100).toFixed(1) + 'x'; }
  if (j.focus_absolute != null) { document.getElementById('fr').value = j.focus_absolute;
    document.getElementById('fv').textContent = j.focus_absolute; }
  if (j.exposure) { document.getElementById('er').value = j.exposure;
    document.getElementById('ev').textContent = j.exposure; }
});
function crop(n){ fetch('/crop?c=' + n).then(r => r.json()).then(j => {
  document.getElementById('cropinfo').textContent =
    j.error ? j.error : 'crop ' + j.crop.join("x,"); }); }
async function af(){
  document.getElementById('cropinfo').textContent = 'focusing...';
  const j = await (await fetch('/focus?af=1')).json();
  document.getElementById('cropinfo').textContent = 'AF best ' + j.best;
}
function wb(w){ fetch('/img?wb=' + w); }
function setE(v){ fetch('/img?e=' + v); }
window.addEventListener('keydown', e => {
  const k = {ArrowLeft:'left',ArrowRight:'right',ArrowUp:'up',ArrowDown:'down'}.get(e.key)
            || (e.key === '+' ? 'zoom_in' : e.key === '-' ? 'zoom_out' : null);
  if (k) { go(k, 400); e.preventDefault(); }
  if (e.key === ' ') { go('stop'); e.preventDefault(); }
});
</script>
Move duration: <input id=dur type=number value=400 style="width:5em"
 min=100 max=3000 step=50> ms
</body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/stream":
            gen = 0
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    with _cond:
                        if gen == _gen:
                            _cond.wait(1.0)
                            continue
                        gen = _gen
                        frame = _latest
                    if frame:
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
            except Exception:
                return
        elif path == "/frame.jpg":
            with _cond:
                frame = _latest
            if frame:
                self._send(200, frame, "image/jpeg")
            else:
                self._send(503, b"no frame yet", "text/plain")
        elif path == "/ptz":
            def arg(name, default):
                m = re.search(name + r"=(-?[\d.]+)", self.path)
                return m.group(1) if m else default
            m = re.search(r"op=([a-z_]+)", self.path)
            op = m.group(1) if m else "status"
            d = float(arg("d", 250))
            zoom = int(float(arg("zoom", 100)))
            thr = threading.Thread(target=ptz, args=(op, d, zoom), daemon=True)
            thr.start()  # bounded moves must not tie up the HTTP handler
            self._send(200, b'{"ok":true}', "application/json")
        elif path == "/img":
            def q(name, default):
                m = re.search(name + r"=([\w]+)", self.path)
                return m.group(1) if m else default
            global EXPO, GAIN, WB
            e = q("e", None)
            if e is not None:
                EXPO = max(100, min(2047, int(e)))
                v4l2(["-c", f"exposure_time_absolute={EXPO}"])
            g = q("g", None)
            if g is not None:
                GAIN = max(0, min(255, int(g)))
                v4l2(["-c", f"gain={GAIN}"])
            w = q("wb", None)
            if w is not None:
                WB = "auto" if w == "auto" else max(2300, min(7500, int(w)))
                v4l2(["-f", "wb_auto=1"] if WB == "auto"
                     else ["-f", f"white_balance_temperature={WB}"])
            self._send(200, json.dumps({"exposure": EXPO, "gain": GAIN,
                                        "wb": WB}).encode(), "application/json")
        elif path == "/focus":
            if re.search(r"af=1", self.path):
                self._send(200, json.dumps(autofocus()).encode(), "application/json")
                return
            def q(name, default):
                m = re.search(name + r"=(-?[\d.]+)", self.path)
                return m.group(1) if m else default
            if q("auto", "0") == "1":
                v4l2(["-c", "focus_automatic_continuous=1"])
                self._send(200, b'{"focus":"auto"}', "application/json")
                return
            v4l2(["-c", "focus_automatic_continuous=0"])
            v4l2(["-c", f"focus_absolute={int(float(q('v', 100)))}"])
            self._send(200, b'{"focus":"manual"}', "application/json")
        elif path == "/crop":
            n = float(re.search(r"c=([\d.]+)", self.path).group(1) or 1)
            self._send(200, json.dumps(set_crop(n)).encode(),
                       "application/json")
        elif path == "/status":
            try:
                st = json.dumps({
                "device": DEV,
                "zoom_absolute": v4l2_get("zoom_absolute"),
                "focus_absolute": v4l2_get("focus_absolute"),
                "focus_auto": v4l2_get("focus_automatic_continuous") == 1,
                "exposure": EXPO, "gain": GAIN, "wb": WB,
                    "crop": CROP,
                    "tilt_up_sign": TILT_UP,
                })
                self._send(200, st.encode(), "application/json")
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}).encode(),
                           "application/json")
        elif path == "/":
            self._send(200, PAGE.encode())
        else:
            self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    print(f"ptzcam on :{PORT} ({DEV})", flush=True)
    threading.Thread(target=capture_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
