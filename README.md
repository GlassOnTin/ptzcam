# ptzcam

HTTP bridge that turns a UVC pan/tilt/zoom camera (tested with the
**Logitech Group**, `046d:0858`) into a one-page web console: live MJPEG
picture and steering, from any browser on the LAN.

Python stdlib + `ffmpeg` + `v4l2-ctl` (from `v4l-utils`). No other
dependencies.

## Endpoints (GET only)

| Path | What |
|---|---|
| `/` | control page: live stream, arrows, zoom slider, crop row, keyboard (arrows/±/space) |
| `/stream` | multipart MJPEG, ~8 fps at 1280×720 |
| `/frame.jpg` | latest still frame |
| `/ptz` | `op=left\|right\|up\|down\|stop\|zoom_in\|zoom_out`, `d=<ms>` move duration; `op=zoom&zoom=100..1000` |
| `/crop` | `c=1..8`: digital crop-zoom centered on the brightest blob (useful for reading small displays) |
| `/status` | JSON: device, zoom, crop state |

`/ptz` replies immediately and runs the move in the background; movement is
bounded and self-stops, so a lost request cannot leave the camera in
motion.

## Camera requirements

A UVC device exposing `pan_speed`/`tilt_speed` (relative pan/tilt) and
`zoom_absolute`. Logitech Group exposes exactly that. Check with:

    v4l2-ctl -d /dev/video0 --list-ctrls

Tilt conventions differ between devices and firmware versions — if up/down
move the wrong way for you, swap the `TILT_UP`/`TILT_DOWN` constants at the
top of `cam.py`.

The Group's UVC zoom is digital and really does span 1×–10×
(`zoom_absolute` 100–1000 verified), but at 1× the Group has a very wide
field of view, so a small subject stays small even at 10×. The `/crop`
endpoint adds an ffmpeg crop window centered on the brightest region of
the frame — enough to read a small display that the 10× leaves tiny.

## Install

    sudo apt install ffmpeg v4l-utils     # Debian/Ubuntu
    # copy cam.py somewhere, then optionally:

    # user unit (survives logout; enable linger for boot persistence)
    mkdir -p ~/.config/systemd/user
    cp ptzcam.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now ptzcam
    loginctl enable-linger $USER    # needs root once

Then open `http://<host>:8612/`.

## License

AGPL-3.0.
