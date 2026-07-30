#!/usr/bin/env python3
"""hyprpad — turn your phone into a mouse, keyboard and trackpad for a
Wayland desktop (Hyprland/Sway), over the browser. No app install.

Injects input via a virtual uinput mouse+keyboard, serves one page (no
build step, no deps besides python-evdev), optionally shows a live
screenshot of the desktop (grim) so you can see where the cursor is.

Run on the machine you want to control:
    python3 hyprpad.py
Then open http://<that-machine-ip>:8123 on your phone (same Wi-Fi).

Setup uinput access once (udev rule + your user in the 'input' group),
see README.md.
"""
import hashlib
import json
import mimetypes
import os
import struct
import subprocess
import tempfile
import tomllib
import zlib
from urllib.parse import parse_qs, quote, unquote, urlparse

from evdev import UInput, ecodes as e
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("HYPRPAD_PORT", "8123"))
TOKEN = os.environ.get("HYPRPAD_TOKEN", "")
PASSWORD = os.environ.get("HYPRPAD_PASSWORD", "")
PWHASH = hashlib.sha256(PASSWORD.encode()).hexdigest() if PASSWORD else ""
WAYLAND_DISPLAY = os.environ.get("WAYLAND_DISPLAY", "wayland-1")
XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
OMARCHY_THEME_DIR = os.path.expanduser("~/.config/omarchy/current/theme")


def _mix(hex_a, hex_b, t):
    """Linear-interpolate two #rrggbb colors, t=0 -> hex_a, t=1 -> hex_b."""
    a = tuple(int(hex_a[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(hex_b[i:i + 2], 16) for i in (1, 3, 5))
    m = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return "#%02x%02x%02x" % m


_theme_cache = {"mtime": None, "result": (None, None)}


def omarchy_theme():
    """Reads the active Omarchy theme's colors.toml, if present, and derives our
    CSS vars from it. Returns (css_vars_str, 'light'|'dark') or (None, None) when
    there's no Omarchy theme (any other distro/WM — falls back to the built-in
    Yerba Mate palette and time-of-day light/dark switch). Cached by the file's
    mtime, so switching themes in Omarchy is picked up without a re-parse on
    every request."""
    path = os.path.join(OMARCHY_THEME_DIR, "colors.toml")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, None
    if mtime != _theme_cache["mtime"]:
        _theme_cache["mtime"] = mtime
        _theme_cache["result"] = _parse_omarchy_theme(path)
    return _theme_cache["result"]


def _parse_omarchy_theme(path):
    try:
        with open(path, "rb") as f:
            c = tomllib.load(f)
        bg, fg = c["background"], c["foreground"]
        accent = c.get("accent", fg)
        accent2 = c.get("color2", accent)  # green/"strings" slot — second accent for 2-color UI hints
        surface = c.get("color0", _mix(bg, fg, 0.08))
        tx3 = c.get("color8", _mix(fg, bg, 0.45))
    except Exception:
        return None, None
    mode = "light" if os.path.exists(os.path.join(OMARCHY_THEME_DIR, "light.mode")) else "dark"
    css = (f"--bg:{bg};--surface:{surface};--ui:{_mix(bg, fg, 0.16)};--ui-2:{_mix(bg, fg, 0.24)};"
           f"--tx:{fg};--tx-2:{_mix(fg, bg, 0.22)};--tx-3:{tx3};"
           f"--accent:{accent};--accent-2:{accent2};--border:{fg}20;")
    return css, mode


# --- virtual mouse + keyboard (uinput) ---
def _mkdev(caps, name, product):
    try:
        return UInput(caps, name=name, vendor=0x1234, product=product, version=1)
    except Exception as ex:
        print(f"[warn] uinput '{name}' off ({ex}); rode o setup do udev (README).")
        return None


MOUSE = _mkdev({e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL],
               e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE]},
              "hyprpad Virtual Mouse", 0x5679)

_KBNAMES = (["KEY_" + c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
            + ["KEY_" + d for d in "0123456789"]
            + ["KEY_SPACE", "KEY_ENTER", "KEY_BACKSPACE", "KEY_TAB", "KEY_ESC",
               "KEY_LEFTSHIFT", "KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_LEFTMETA",
               "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_HOME", "KEY_END",
               "KEY_PAGEUP", "KEY_PAGEDOWN", "KEY_DELETE", "KEY_SYSRQ",
               "KEY_MINUS", "KEY_EQUAL", "KEY_LEFTBRACE", "KEY_RIGHTBRACE",
               "KEY_BACKSLASH", "KEY_SEMICOLON", "KEY_APOSTROPHE", "KEY_GRAVE",
               "KEY_COMMA", "KEY_DOT", "KEY_SLASH"])
KBD = _mkdev({e.EV_KEY: [getattr(e, k) for k in _KBNAMES if hasattr(e, k)]},
             "hyprpad Virtual Keyboard", 0x567a)


# char -> (KEY_code, needs_shift) — US layout (what most keyboards send)
def _charmap():
    m = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        code = getattr(e, "KEY_" + c.upper())
        m[c] = (code, False); m[c.upper()] = (code, True)
    for d in "0123456789":
        m[d] = (getattr(e, "KEY_" + d), False)
    sym = {" ": ("SPACE", 0), "-": ("MINUS", 0), "_": ("MINUS", 1),
           "=": ("EQUAL", 0), "+": ("EQUAL", 1), "[": ("LEFTBRACE", 0), "{": ("LEFTBRACE", 1),
           "]": ("RIGHTBRACE", 0), "}": ("RIGHTBRACE", 1), "\\": ("BACKSLASH", 0), "|": ("BACKSLASH", 1),
           ";": ("SEMICOLON", 0), ":": ("SEMICOLON", 1), "'": ("APOSTROPHE", 0), '"': ("APOSTROPHE", 1),
           "`": ("GRAVE", 0), "~": ("GRAVE", 1), ",": ("COMMA", 0), "<": ("COMMA", 1),
           ".": ("DOT", 0), ">": ("DOT", 1), "/": ("SLASH", 0), "?": ("SLASH", 1),
           "!": ("1", 1), "@": ("2", 1), "#": ("3", 1), "$": ("4", 1), "%": ("5", 1),
           "^": ("6", 1), "&": ("7", 1), "*": ("8", 1), "(": ("9", 1), ")": ("0", 1)}
    for ch, (nm, sh) in sym.items():
        code = getattr(e, "KEY_" + nm, None)
        if code is not None:
            m[ch] = (code, bool(sh))
    return m


CHARMAP = _charmap()
NAMEDKEYS = {"enter": "KEY_ENTER", "backspace": "KEY_BACKSPACE", "tab": "KEY_TAB",
             "esc": "KEY_ESC", "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT",
             "right": "KEY_RIGHT", "space": "KEY_SPACE", "delete": "KEY_DELETE",
             "home": "KEY_HOME", "end": "KEY_END", "pageup": "KEY_PAGEUP", "pagedown": "KEY_PAGEDOWN",
             "print": "KEY_SYSRQ"}
MODKEYS = {"shift": e.KEY_LEFTSHIFT, "ctrl": e.KEY_LEFTCTRL, "alt": e.KEY_LEFTALT, "super": e.KEY_LEFTMETA}


def kbd_send(d):
    """Injects into the virtual keyboard. d: {char} | {key,state?} | {mods:[...]} combinable."""
    if not KBD:
        return
    mods = [MODKEYS[m] for m in d.get("mods", []) if m in MODKEYS]
    for k in mods:
        KBD.write(e.EV_KEY, k, 1)
    if d.get("char") in CHARMAP:
        code, sh = CHARMAP[d["char"]]
        if sh:
            KBD.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
        KBD.write(e.EV_KEY, code, 1); KBD.syn(); KBD.write(e.EV_KEY, code, 0)
        if sh:
            KBD.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
    elif d.get("key") in NAMEDKEYS:
        k = getattr(e, NAMEDKEYS[d["key"]])
        st = d.get("state")
        if st is None:                       # tap
            KBD.write(e.EV_KEY, k, 1); KBD.syn(); KBD.write(e.EV_KEY, k, 0)
        else:                                # hold/release
            KBD.write(e.EV_KEY, k, 1 if st else 0)
    for k in reversed(mods):
        KBD.write(e.EV_KEY, k, 0)
    KBD.syn()


# --- voice: record on the phone, transcribe + type here (lazy-loaded, optional) ---
_WHISPER = None  # None = not loaded yet, False = unavailable


def _voice_model():
    global _WHISPER
    if _WHISPER is False:
        return None
    if _WHISPER is None:
        try:
            from faster_whisper import WhisperModel
            _WHISPER = WhisperModel("base.en", device="cpu", compute_type="int8")
        except Exception as ex:
            print(f"[warn] voice off ({ex}); pip install faster-whisper (ou AUR "
                  "python-faster-whisper) pra habilitar.")
            _WHISPER = False
            return None
    return _WHISPER


def transcribe_and_type(audio_bytes):
    """Transcribes a recorded clip and types the result via the virtual keyboard.
    Returns the transcribed text (empty string if voice is unavailable)."""
    model = _voice_model()
    if not model or not audio_bytes:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".webm") as f:
        f.write(audio_bytes)
        f.flush()
        segments, _ = model.transcribe(f.name)
        text = "".join(s.text for s in segments).strip()
    for ch in text:
        kbd_send({"char": ch})
    return text


MEDIA_ACTIONS = {"play-pause", "next", "previous"}
MEDIA_FORMAT = "{{playerName}}\t{{status}}\t{{title}}\t{{artist}}\t{{mpris:artUrl}}\t{{volume}}"


def _all_players():
    """Every MPRIS player's info in one call (Spotify, browser tabs, mpv, ...).
    Returns a list of dicts, possibly empty (no player running / playerctl missing)."""
    try:
        r = subprocess.run(["playerctl", "-a", "metadata", "--format", MEDIA_FORMAT],
                            capture_output=True, text=True, timeout=2)
    except Exception:
        return []
    if r.returncode != 0 or not r.stdout.strip():
        return []
    rows = []
    for line in r.stdout.rstrip("\n").split("\n"):
        name, status, title, artist, art, volume = (line.split("\t") + [""] * 6)[:6]
        rows.append({"player": name, "status": status, "title": title,
                     "artist": artist, "art": art, "volume": volume})
    return rows


def _pick_player(rows, want=None):
    """`want` (an explicit player name from the phone) wins if it's still around —
    that's how the UI lets you choose when more than one player is going at once
    (e.g. a Spotify track and a YouTube tab both Playing has no correct guess).
    Otherwise: prefer one that's Playing, else just the first player found."""
    if want:
        hit = next((r for r in rows if r["player"] == want), None)
        if hit:
            return hit
    playing = [r for r in rows if r["status"] == "Playing"]
    return playing[0] if playing else (rows[0] if rows else None)


def media_info(want=None):
    """Returns {status, title, artist, art, volume, player, players} for the
    picked player, or None if nothing's running. `art` is a URL the phone can
    fetch directly: the player's own http(s) URL when it has one, or our own
    /api/media-art proxy when it only points at a local file:// (common for
    browser tabs — the phone can't reach a path on this machine's filesystem
    directly). `players` lists every running player so the UI can offer a
    switcher when there's more than one."""
    rows = _all_players()
    row = _pick_player(rows, want)
    if not row:
        return None
    art = row["art"]
    if art.startswith("http"):
        art_url = art
    elif art.startswith("file://"):
        art_url = f"/api/media-art?player={quote(row['player'])}"
    else:
        art_url = None
    return {
        "status": row["status"] or None,
        "title": row["title"] or None,
        "artist": row["artist"] or None,
        "art": art_url,
        "volume": float(row["volume"]) if row["volume"] else None,
        "player": row["player"],
        "players": [{"name": r["player"], "title": r["title"] or r["player"]} for r in rows]
                   if len(rows) > 1 else [],
    }


def media_art_file(want=None):
    """Local path behind the picked player's file:// art URL, if any."""
    row = _pick_player(_all_players(), want)
    if row and row["art"].startswith("file://"):
        return unquote(row["art"][len("file://"):])
    return None


def media_control(action, want=None):
    row = _pick_player(_all_players(), want)
    if not row:
        return False
    player = row["player"]
    if action in MEDIA_ACTIONS:
        cmd = ["playerctl", "-p", player, action]
    elif action == "volume-up":
        cmd = ["playerctl", "-p", player, "volume", "0.05+"]
    elif action == "volume-down":
        cmd = ["playerctl", "-p", player, "volume", "0.05-"]
    else:
        return False
    try:
        subprocess.run(cmd, capture_output=True, timeout=2)
        return True
    except Exception:
        return False


def active_window_class():
    """Focused window's app class via hyprctl (Hyprland only). None if
    unavailable (other compositor, hyprctl missing, nothing focused)."""
    try:
        r = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=2)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout).get("class") or None
    except Exception:
        return None


def screen_frame():
    """Current screenshot via grim (wlroots), downscaled JPEG to fit over wifi."""
    env = {**os.environ, "WAYLAND_DISPLAY": WAYLAND_DISPLAY, "XDG_RUNTIME_DIR": XDG_RUNTIME_DIR}
    try:
        r = subprocess.run(["grim", "-s", "0.5", "-t", "jpeg", "-q", "55", "-"],
                           capture_output=True, env=env, timeout=5)
        return r.stdout if r.returncode == 0 else b""
    except Exception:
        return b""


# classic pointer-cursor silhouette, normalized 0..1, tip at top-left
_CURSOR_POLY = [(0.21, 0.09), (0.21, 0.82), (0.41, 0.65), (0.52, 0.91),
                (0.65, 0.86), (0.51, 0.60), (0.79, 0.60)]


def _point_in_poly(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def make_icon(size=512):
    """Icon PNG (pure Python, no image deps): an amber pointer-cursor on a dark background."""
    bg = (40, 45, 28)       # #282d1c
    fg = (212, 160, 51)     # #d4a033
    poly = [(x * size, y * size) for x, y in _CURSOR_POLY]
    px = bytearray()
    for y in range(size):
        px.append(0)        # per-row filter byte
        for x in range(size):
            px += bytes(fg if _point_in_poly(x, y, poly) else bg)
    idat = zlib.compress(bytes(px), 9)

    def chunk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


ICON_PNG = make_icon()
MANIFEST = json.dumps({
    "name": "hyprpad", "short_name": "hyprpad",
    "start_url": "/", "display": "standalone", "orientation": "any",
    "background_color": "#282d1c", "theme_color": "#282d1c",
    "icons": [
        {"src": "/icon.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/icon.png", "sizes": "192x192", "type": "image/png"},
    ],
}).encode()


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>hyprpad</title>
<link rel=manifest href=/manifest.json><link rel=icon href=/icon.png>
<meta name=theme-color content=#282d1c>
<style>
/* Yerba Mate palette — light/dark, same as oikos */
:root{
  --bg:#fbf1c7; --surface:#ebdfb0; --ui:#ddd2a0; --ui-2:#cbbe8a;
  --tx:#3c3836; --tx-2:#504945; --tx-3:#7c6f64;
  --accent:#c88010; --accent-2:#79740e; --border:#00000018;
}
:root[data-theme=dark]{
  --bg:#282d1c; --surface:#363c26; --ui:#4f5b4a; --ui-2:#5a6a54;
  --tx:#dce0d9; --tx-2:#a8b09f; --tx-3:#7a8573;
  --accent:#d4a033; --accent-2:#7a9e38; --border:#ffffff16;
}
/* overridden per-request with the live Omarchy theme, if one is active */
:root{/*OMARCHY_VARS*/}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;margin:0;background:var(--bg);color:var(--tx);
  font:15px/1.4 -apple-system,system-ui,sans-serif;overscroll-behavior:none;user-select:none}
#app{position:fixed;inset:0;display:flex;flex-direction:column;overflow:hidden}
#deskmain{flex:1;min-height:0;display:flex;flex-direction:column}
#medview{display:none;position:absolute;inset:0;background:var(--bg);z-index:40;
  flex-direction:column;align-items:center;justify-content:center;gap:14px;padding:24px;
  text-align:center;overflow-y:auto}
body[data-media] #medview{display:flex}
body[data-media] #deskmain{display:none}
#mcover{width:min(60vw,220px);height:min(60vw,220px);object-fit:contain;border-radius:16px;
  display:none;flex:0 0 auto}
#mcover[src]{display:block}
#mtitle{font-size:18px;font-weight:700;max-width:80vw;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;color:var(--tx)}
#martist{color:var(--tx-2);font-size:14px;margin-top:-8px;max-width:80vw;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
#mctrls,#mvol{display:flex;align-items:center;gap:14px;flex:0 0 auto}
#medview button{background:var(--ui);color:var(--tx);border:0;font:inherit;font-weight:600}
#medview button:active{background:var(--accent);color:#fff;transform:scale(.93)}
#mctrls button{width:56px;height:56px;border-radius:8px;font-size:20px}
#mvol button{width:40px;height:40px;border-radius:8px;font-size:18px;padding:0}
#mvolval{color:var(--tx-2);font-size:13px;min-width:34px}
#mctrlcol{display:contents}
@media (orientation:landscape){
  #medview{flex-direction:row;text-align:left;padding:0;gap:0}
  #mcover{order:2;flex:1 1 auto;width:auto;height:auto;max-width:none;max-height:none;
    border-radius:14px;align-self:stretch;margin:10px 14px 14px -20px}
  #mctrlcol{display:flex;order:1;flex:0 0 56%;flex-direction:column;align-items:center;
    justify-content:center;gap:14px;padding:20px;text-align:center}
}
#mclose{position:absolute;top:calc(env(safe-area-inset-top,0px) + 14px);left:14px;
  padding:9px 16px;border-radius:8px}
#mplayers{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;max-width:85vw}
#mplayers button{padding:8px 12px;border-radius:8px;font-size:12px;max-width:38vw;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#mplayers button.on{background:var(--accent)!important;color:#fff!important}
/* landscape: keys on the LEFT, trackpad on the RIGHT (two thumbs).
   portrait stays default: keys on top, trackpad below. */
@media (orientation:landscape){
  #deskmain{flex-direction:row}
  #deskmain #deskkeys{flex:0 0 56%;align-content:center;justify-content:center;gap:7px;
    padding:10px 8px;overflow-y:auto;-webkit-overflow-scrolling:touch}
  #deskmain #padrow{flex:1 1 auto;padding:10px 14px 14px 6px}
  #tpad{max-height:none}
}
#deskscreen{display:none;flex:0 0 auto;margin:8px 14px 4px;border-radius:12px;overflow:hidden;
  background:#000;border:1px solid var(--border);aspect-ratio:16/9;align-items:center;justify-content:center}
body[data-screen] #deskscreen{display:flex}
#deskimg{width:100%;height:100%;object-fit:contain;display:block}
#padrow{flex:0 0 44vh;min-height:0;padding:2px 14px 34px}   /* bottom padding clears the home-indicator area */
#tpad2{touch-action:none}
#tpad{width:100%;height:100%;background:var(--surface);border:1px solid var(--border);
  background-image:radial-gradient(circle,var(--tx-3) 1px,transparent 1px);
  background-size:18px 18px;background-position:center;
  border-radius:14px;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:6px;color:var(--tx-3);font-size:14px;text-align:center;padding:0 14px;touch-action:none;user-select:none}
#tpad small{font-size:10.5px;opacity:.7;line-height:1.4}
#screentoggle.on,#kbtoggle.on{background:var(--accent)!important;color:#fff!important}
#deskkeys{display:flex;flex-wrap:wrap;gap:8px;padding:calc(env(safe-area-inset-top,0px) + 12px) 14px 8px;
  flex:1;align-content:center;justify-content:center}
/* when the Tridactyl row shows up, everything else shrinks a bit instead of
   scrolling or floating on top */
body[data-trid] #deskkeys{gap:5px;padding-top:6px;padding-bottom:4px}
body[data-trid] #deskkeys button{padding:8px 11px;font-size:.92em}
body[data-trid] .deskgroup{padding:2px 0}
body[data-trid] #arrows{grid-template-columns:repeat(3,38px);grid-auto-rows:38px}
body[data-trid] #arrowrow #tpad2,body[data-trid] #arrowrow #tpadDrag{width:48px}
body[data-screen] #deskkeys{flex:0 0 auto;padding-top:8px}   /* with the live screen on, it takes the top */
#deskkeys button{background:var(--ui);color:var(--tx);border:0;border-radius:8px;padding:11px 15px;
  font:inherit;font-weight:600;min-width:46px;transition:transform .06s}
#deskkeys button:active,#deskkeys button.on{background:var(--accent);color:#fff}
#deskkeys button:active{transform:scale(.93);box-shadow:inset 0 2px 5px rgba(0,0,0,.3)}
#deskkeys button[data-mod].on{outline:2px solid var(--accent);outline-offset:2px}
#arrowrow{display:flex;align-items:stretch;gap:4px}
#arrowrow #tpad2,#arrowrow #tpadDrag{width:56px;font-size:12px;letter-spacing:.03em;padding:0;min-width:0}
#tpadDrag{background:var(--accent-2)!important;color:#fff!important;border:0}
#tpadDrag.on{filter:brightness(1.3)}
#tpad2{background:var(--accent)!important;color:#fff!important;border:0}
#tpad2.on{filter:brightness(1.3)}
#arrows{display:grid;grid-template-columns:repeat(3,44px);grid-auto-rows:44px;gap:4px}
#arrows button{padding:0;min-width:0;display:flex;align-items:center;justify-content:center}
#arrows .au{grid-column:2;grid-row:1}
#arrows .al{grid-column:1;grid-row:2}
#arrows .ad{grid-column:2;grid-row:2}
#arrows .ar{grid-column:3;grid-row:2}
/* groups: each row stacks (mode -> nav -> shortcuts), most-used shortcuts
   come last = closer to the thumb in a landscape grip */
.deskgroup{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;flex:1 0 100%;padding:4px 0}
#navgrp{border-top:1px solid var(--border);border-bottom:1px solid var(--border);margin:2px 0}
#shortgrp button.flash,#medview button.flash,#tridgrp button.flash{background:var(--accent)!important;color:#fff!important}
#kbin{position:fixed;bottom:0;left:0;width:1px;height:1px;opacity:0;border:0;padding:0;
  resize:none;background:transparent;color:transparent;caret-color:transparent}
#kbecho{position:fixed;top:0;left:0;right:0;z-index:9998;display:none;
  background:#111;color:#eee;font:16px/1.4 monospace;padding:10px 14px;
  border-bottom:2px solid var(--accent);white-space:pre-wrap;word-break:break-all;
  min-height:20px;box-shadow:0 4px 12px rgba(0,0,0,.4)}
#kbecho.on{display:block}
#kbcaret{display:inline-block;width:2px;height:1.1em;background:var(--accent);
  vertical-align:text-bottom;margin-left:1px;animation:kbblink 1s steps(1) infinite}
@keyframes kbblink{50%{opacity:0}}
</style></head><body>

<div id=app>
  <div id=deskscreen><img id=deskimg alt="desktop screen"></div>
  <div id=deskmain>
    <div id=deskkeys>
      <div class=deskgroup id=modegrp>
        <button id=kbtoggle>Type</button>
        <button id=screentoggle>Screen</button>
        <button id=voicetoggle>Voice</button>
        <button id=mediatoggle style=display:none>Media</button>
      </div>
      <div class=deskgroup id=navgrp>
        <button data-key=esc>Esc</button><button data-key=tab>Tab</button>
        <button data-key=backspace>⌫</button><button data-key=enter>Enter</button>
        <button data-key=print>PrtSc</button>
        <div id=arrowrow>
          <button id=tpadDrag aria-label="hold = click and drag" title="hold = click and drag">DRAG</button>
          <button id=tpad2 aria-label="hold = 2 fingers (scroll/right-click)" title="hold = 2 fingers (scroll/right-click)">2F</button>
          <div id=arrows>
            <button data-key=up class="au rep">↑</button>
            <button data-key=left class="al rep">←</button>
            <button data-key=down class="ad rep">↓</button>
            <button data-key=right class="ar rep">→</button>
          </div>
        </div>
      </div>
      <div class=deskgroup id=shortgrp>
        <button data-mod=ctrl>Ctrl</button><button data-mod=alt>Alt</button><button data-mod=super>Super</button>
        <button data-char="1">1</button>
        <button data-char="2">2</button>
        <button data-char="3">3</button>
      </div>
      <div class=deskgroup id=tridgrp style=display:none></div>
    </div>
    <div id=padrow>
      <div id=tpad>trackpad<br><small>drag to move · tap to click · 2 fingers to scroll</small></div>
    </div>
  </div>
  <div id=medview>
    <img id=mcover alt="">
    <div id=mctrlcol>
      <div id=mplayers></div>
      <div id=mtitle></div>
      <div id=martist></div>
      <div id=mctrls>
        <button id=mprev aria-label=previous>⏮</button>
        <button id=mplay aria-label="play/pause">⏯</button>
        <button id=mnext aria-label=next>⏭</button>
      </div>
      <div id=mvol>
        <button id=mvoldown aria-label="volume down">−</button>
        <span id=mvolval></span>
        <button id=mvolup aria-label="volume up">+</button>
      </div>
    </div>
    <button id=mclose>Back</button>
  </div>
  <input id=kbin type=text inputmode=email autocomplete=new-password autocapitalize=off autocorrect=off spellcheck=false>
  <div id=kbecho><span id=kbechot></span><span id=kbcaret></span></div>
</div>

<script>
// theme: follows the live Omarchy theme if one's active (fixed mode, no
// time-of-day switch — Omarchy themes ship one mode at a time); otherwise
// falls back to the built-in Yerba Mate palette, light (6am-6pm) / dark (6pm-6am)
const OMARCHY_MODE = __OMARCHY_MODE__;
(function(){var forced=new URLSearchParams(location.search).get('theme');
  function t(){if(forced){document.documentElement.dataset.theme=forced;return;}
    if(OMARCHY_MODE){document.documentElement.dataset.theme=OMARCHY_MODE;return;}
    var h=new Date().getHours();
    document.documentElement.dataset.theme=(h>=6&&h<18)?'light':'dark';}
  t();setInterval(t,600000);})();

const buzz=(ms=12)=>{try{navigator.vibrate&&navigator.vibrate(ms)}catch(_){}};

function goFullscreen(){
  const el=document.documentElement;
  const fn=el.requestFullscreen||el.webkitRequestFullscreen;
  if(fn && !document.fullscreenElement){ try{fn.call(el);}catch(_){} }
}
window.addEventListener('touchend', goFullscreen, {passive:true});
window.addEventListener('click', goFullscreen);

// live screen (grim), updates while enabled
let deskT=null;
function deskTick(){const img=document.getElementById('deskimg');if(!img)return;
  const u='/api/screen?'+Date.now();const pre=new Image();pre.onload=()=>{img.src=u;};pre.src=u;}
function startDeskScreen(){deskTick();if(!deskT)deskT=setInterval(deskTick,700);}
function stopDeskScreen(){if(deskT){clearInterval(deskT);deskT=null;}}

// ---------- use the phone as mouse + keyboard ----------
(function(){
  const tpad=document.getElementById('tpad'); if(!tpad) return;
  const ptr=o=>fetch('/ptr',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o),keepalive:true}).catch(()=>{});
  const key=o=>fetch('/key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o),keepalive:true}).catch(()=>{});
  const mods={ctrl:false,alt:false,super:false};
  const activeMods=()=>Object.keys(mods).filter(k=>mods[k]);
  const clearMods=()=>{for(const k in mods)mods[k]=false;
    for(const b of document.querySelectorAll('#deskkeys button[data-mod]'))b.classList.remove('on');};
  // --- trackpad ---
  let lx=0,ly=0,moved=false,t0=0,startFingers=1,lsx=0,lsy=0;
  // --- "2nd finger": hold the button with the other thumb to simulate 2 fingers on the trackpad ---
  let held2=false;
  const tpad2=document.getElementById('tpad2');
  if(tpad2){
    const on2=ev=>{ev.preventDefault();held2=true;tpad2.classList.add('on');buzz(8);};
    const off2=()=>{held2=false;tpad2.classList.remove('on');};
    tpad2.addEventListener('touchstart',on2,{passive:false});
    tpad2.addEventListener('touchend',off2); tpad2.addEventListener('touchcancel',off2);
    tpad2.addEventListener('mousedown',on2); tpad2.addEventListener('mouseup',off2); tpad2.addEventListener('mouseleave',off2);
  }
  // --- "click and drag": hold the button, the click stays down; move the trackpad to drag, release the button to let go ---
  let heldDrag=false;
  const tpadDrag=document.getElementById('tpadDrag');
  if(tpadDrag){
    const onD=ev=>{ev.preventDefault();heldDrag=true;tpadDrag.classList.add('on');buzz(8);ptr({click:'left',state:1});};
    const offD=()=>{if(!heldDrag)return;heldDrag=false;tpadDrag.classList.remove('on');ptr({click:'left',state:0});};
    tpadDrag.addEventListener('touchstart',onD,{passive:false});
    tpadDrag.addEventListener('touchend',offD); tpadDrag.addEventListener('touchcancel',offD);
    tpadDrag.addEventListener('mousedown',onD); tpadDrag.addEventListener('mouseup',offD); tpadDrag.addEventListener('mouseleave',offD);
  }
  tpad.addEventListener('touchstart',ev=>{ev.preventDefault();startFingers=held2?2:ev.targetTouches.length;
    const t=ev.targetTouches[0];lx=t.clientX;ly=t.clientY;lsx=t.clientX;lsy=t.clientY;moved=false;t0=Date.now();},{passive:false});
  tpad.addEventListener('touchmove',ev=>{ev.preventDefault();const t=ev.targetTouches[0];
    if(held2||ev.targetTouches.length>=2){
      const dy=t.clientY-lsy, dx=t.clientX-lsx;
      if(Math.abs(dy)>=Math.abs(dx)){ if(Math.abs(dy)>7){ptr({scroll:dy>0?-1:1});moved=true;} }
      else { if(Math.abs(dx)>7){ptr({hscroll:dx>0?-1:1});moved=true;} }
      lsx=t.clientX; lsy=t.clientY; return;
    }
    const dx=t.clientX-lx,dy=t.clientY-ly;
    if(Math.abs(dx)>1||Math.abs(dy)>1){moved=true;ptr({dx:Math.round(dx*1.7),dy:Math.round(dy*1.7)});lx=t.clientX;ly=t.clientY;}},{passive:false});
  tpad.addEventListener('touchend',ev=>{ev.preventDefault();
    if(!heldDrag&&!moved&&Date.now()-t0<220){const c=startFingers>=2?'right':'left';buzz(10);ptr({click:c,state:1});setTimeout(()=>ptr({click:c,state:0}),25);}},{passive:false});
  // --- fixed shortcuts (add here = that's it, no HTML changes needed) ---
  const SHORTCUTS=[{lbl:'Super+C',char:'c',mods:['super']},
    {lbl:'Copy',char:'c',mods:['ctrl']},{lbl:'Paste',char:'v',mods:['ctrl']}];
  const shortgrp=document.getElementById('shortgrp');
  for(const s of SHORTCUTS){
    const b=document.createElement('button'); b.textContent=s.lbl;
    b.onclick=()=>{buzz(15); key(s.char?{char:s.char,mods:s.mods}:{key:s.key,mods:s.mods});
      b.classList.add('flash'); setTimeout(()=>b.classList.remove('flash'),120);};
    shortgrp.appendChild(b);}
  // --- Tridactyl (Firefox/LibreWolf vim-style binds): shown only while LibreWolf is focused ---
  const TRIDACTYL=[{lbl:'New Tab',chars:['t']},{lbl:'Close Tab',chars:['d']},
    {lbl:'Prev Tab',chars:['g','T']},{lbl:'Next Tab',chars:['g','t']}];
  const tridgrp=document.getElementById('tridgrp');
  for(const s of TRIDACTYL){
    const b=document.createElement('button'); b.textContent=s.lbl;
    b.onclick=()=>{buzz(15); for(const ch of s.chars) key({char:ch});
      b.classList.add('flash'); setTimeout(()=>b.classList.remove('flash'),120);};
    tridgrp.appendChild(b);}
  const pollWin=()=>fetch('/api/active-window').then(r=>r.json()).then(d=>{
    const on=(d.class||'').toLowerCase()==='librewolf';
    tridgrp.style.display=on?'flex':'none';
    document.body.toggleAttribute('data-trid',on);
  }).catch(()=>{});
  pollWin(); setInterval(pollWin,1500);
  // --- hold arrow = repeat (long navigation/scroll without tapping repeatedly) ---
  for(const b of document.querySelectorAll('#arrows button.rep')){
    const k=b.dataset.key; let iv=null;
    const fire=()=>{buzz();key({key:k});};
    const start=ev=>{ev.preventDefault(); fire(); iv=setTimeout(function rep(){fire();iv=setTimeout(rep,70);},300);};
    const stop=()=>{clearTimeout(iv);iv=null;};
    b.addEventListener('touchstart',start,{passive:false});
    b.addEventListener('touchend',stop); b.addEventListener('touchcancel',stop);
    b.addEventListener('mousedown',start); b.addEventListener('mouseup',stop); b.addEventListener('mouseleave',stop);}
  // --- show/hide the live screen (grim only runs while enabled) ---
  const st=document.getElementById('screentoggle');
  st.onclick=()=>{const on=document.body.toggleAttribute('data-screen');st.classList.toggle('on',on);buzz();
    if(on)startDeskScreen();else stopDeskScreen();};
  // --- phone's native keyboard ---
  const kbin=document.getElementById('kbin');
  const kbt=document.getElementById('kbtoggle');
  const kbecho=document.getElementById('kbecho'), kbechot=document.getElementById('kbechot');
  // echo = the field's own value (no parallel buffer that could drift out of sync)
  const showEcho=()=>{ kbechot.textContent=(kbin.value||'').slice(-48); };
  // bar visibility controlled ONLY here (not on focus/blur: on Android those
  // fire spuriously with prediction and made the bar flicker).
  let openedAt=0;
  const openKb=()=>{ kbin.value=''; kblast=''; showEcho();
    kbecho.classList.add('on'); kbt.classList.add('on'); kbin.focus(); openedAt=Date.now(); };
  const closeKb=()=>{ kbin.blur(); kbecho.classList.remove('on'); kbt.classList.remove('on'); };
  kbt.onclick=()=>{ kbt.classList.contains('on')?closeKb():openKb(); };
  tpad.addEventListener('dblclick',openKb);
  // if the phone's own keyboard gets dismissed some other way (back button,
  // swipe away, ...) close our echo bar too instead of leaving it stuck open.
  // Ignore the first ~600ms after opening: the viewport hasn't finished
  // animating up yet and would otherwise look like an immediate close.
  if(window.visualViewport){
    window.visualViewport.addEventListener('resize',()=>{
      if(Date.now()-openedAt<600) return;
      const kb=Math.max(0, window.innerHeight - window.visualViewport.height - window.visualViewport.offsetTop);
      if(kb<=90 && kbecho.classList.contains('on')) closeKb();
    });
  }
  // capture by DIFF on the 'input' event (every Android keyboard fires input;
  // beforeinput/keydown don't always fire, e.g. LineageOS' AOSP keyboard)
  // DIFF on value: inputmode=email gives a keyboard with no prediction (letters
  // land instantly), type=text allows space, and the diff catches both
  // insertion AND backspace (value shrinks).
  let kblast='';
  kbin.addEventListener('input',()=>{
    const cur=kbin.value||'';
    let p=0; const m=Math.min(cur.length,kblast.length);
    while(p<m && cur[p]===kblast[p]) p++;
    const dele=kblast.length-p, ins=cur.slice(p);
    for(let i=0;i<dele;i++) key({key:'backspace'});             // deleted
    let sent=false;
    for(const ch of ins){ key({char:ch,mods:activeMods()}); sent=true; } // inserted
    if(sent) clearMods();
    kblast=cur;
    if(cur.length>160){ kbin.value=''; kblast=''; }              // doesn't grow forever
    showEcho();                                                  // echo = current value
  });
  kbin.addEventListener('keydown',ev=>{const m={Enter:'enter',Backspace:'backspace',Tab:'tab',Escape:'esc',ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right'};
    if(m[ev.key]){ev.preventDefault();key({key:m[ev.key],mods:activeMods()});clearMods();
      if(ev.key==='Enter'){ kbin.value=''; kblast=''; }          // Enter = sends and clears the bar
      showEcho();}});
  // --- special keys + modifiers ---
  for(const b of document.querySelectorAll('#deskkeys button[data-key]:not(.rep)')){const k=b.dataset.key;
    b.onclick=()=>{buzz();key({key:k,mods:activeMods()});clearMods();};}
  for(const b of document.querySelectorAll('#deskkeys button[data-char]')){const ch=b.dataset.char;
    b.onclick=()=>{buzz();key({char:ch,mods:activeMods()});clearMods();};}
  for(const b of document.querySelectorAll('#deskkeys button[data-mod]')){const mo=b.dataset.mod;
    b.onclick=()=>{mods[mo]=!mods[mo];b.classList.toggle('on',mods[mo]);buzz();};}
})();

// ---------- voice: hold the button to record, release to send for local transcription + typing ----------
(function(){
  const vt=document.getElementById('voicetoggle'); if(!vt) return;
  if(!(navigator.mediaDevices && window.MediaRecorder)){ vt.style.display='none'; return; }
  let recorder=null, chunks=[], stream=null;
  const label=t=>{vt.textContent=t;};
  const start=async ev=>{
    ev.preventDefault();
    if(recorder) return;
    try{ stream=await navigator.mediaDevices.getUserMedia({audio:true}); }
    catch(err){ label(err.name||'no mic'); console.error('hyprpad voice:',err); setTimeout(()=>label('Voice'),3000); return; }
    chunks=[];
    recorder=new MediaRecorder(stream);
    recorder.ondataavailable=e=>{ if(e.data.size) chunks.push(e.data); };
    recorder.onstop=()=>{
      stream.getTracks().forEach(t=>t.stop()); stream=null;
      const blob=new Blob(chunks,{type:'audio/webm'});
      vt.classList.remove('on'); label('…');
      fetch('/api/voice',{method:'POST',body:blob})
        .then(()=>label('Voice')).catch(()=>label('Voice'));
      recorder=null;
    };
    recorder.start(); vt.classList.add('on'); buzz(15); label('● rec');
  };
  const stop=()=>{ if(recorder && recorder.state==='recording') recorder.stop(); };
  vt.addEventListener('touchstart',start,{passive:false});
  vt.addEventListener('touchend',stop); vt.addEventListener('touchcancel',stop);
  vt.addEventListener('mousedown',start); vt.addEventListener('mouseup',stop); vt.addEventListener('mouseleave',stop);
})();

// ---------- media button: only shown while something's actually playable (MPRIS/playerctl) ----------
(function(){
  const mt=document.getElementById('mediatoggle'); if(!mt) return;
  let selectedPlayer=null;   // null = let the backend auto-pick (prefers whatever's Playing)
  const mplayers=document.getElementById('mplayers');
  const media=action=>fetch('/api/media',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action,player:selectedPlayer}),keepalive:true}).catch(()=>{});
  const q=()=>selectedPlayer?('?player='+encodeURIComponent(selectedPlayer)):'';
  const cover=document.getElementById('mcover'), title=document.getElementById('mtitle'),
    artist=document.getElementById('martist'), volval=document.getElementById('mvolval');
  const poll=()=>fetch('/api/media'+q()).then(r=>r.json()).then(d=>{
    mt.style.display=d.status?'inline-block':'none';
    if(!d.status && document.body.hasAttribute('data-media')) closePanel();
    if(!document.body.hasAttribute('data-media')) return;
    selectedPlayer=d.player||selectedPlayer;
    mplayers.innerHTML='';
    for(const p of (d.players||[])){
      const b=document.createElement('button'); b.textContent=p.title.slice(0,28);
      b.classList.toggle('on',p.name===d.player);
      b.onclick=()=>{buzz();selectedPlayer=p.name;poll();};
      mplayers.appendChild(b);
    }
    if(d.art){const sep=d.art.includes('?')?'&':'?'; cover.src=d.art+sep+'_='+Date.now();}
    else cover.removeAttribute('src');
    title.textContent=d.title||'';
    artist.textContent=d.artist||'';
    volval.textContent=d.volume!=null?Math.round(d.volume*100)+'%':'';
  }).catch(()=>{});
  cover.onerror=()=>cover.removeAttribute('src');
  // do the action, buzz + visual flash right away (no waiting on the network),
  // then re-poll fast so the panel reflects the real new state
  const act=(btn,action)=>{buzz(); btn.classList.add('flash'); setTimeout(()=>btn.classList.remove('flash'),120);
    media(action); setTimeout(poll,250);};
  document.getElementById('mprev').onclick=ev=>act(ev.target,'previous');
  document.getElementById('mplay').onclick=ev=>act(ev.target,'play-pause');
  document.getElementById('mnext').onclick=ev=>act(ev.target,'next');
  document.getElementById('mvoldown').onclick=ev=>act(ev.target,'volume-down');
  document.getElementById('mvolup').onclick=ev=>act(ev.target,'volume-up');
  const openPanel=()=>{buzz();document.body.setAttribute('data-media','1');poll();};
  const closePanel=()=>{buzz();document.body.removeAttribute('data-media');};
  mt.onclick=openPanel;
  document.getElementById('mclose').onclick=closePanel;
  poll(); setInterval(poll,2000);
})();

// native keyboard open -> shrink the app area above it (trackpad moves up)
if(window.visualViewport){
  const vv=window.visualViewport, app=document.getElementById('app');
  const onVV=()=>{const kb=Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    app.style.bottom = kb>90 ? kb+'px' : '';};
  vv.addEventListener('resize',onVV); vv.addEventListener('scroll',onVV);
}
</script></body></html>"""


LOGIN_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>hyprpad</title><style>
html,body{height:100%;margin:0;background:#282d1c;color:#dce0d9;
  font:16px/1.4 -apple-system,system-ui,sans-serif;display:flex;align-items:center;justify-content:center}
form{display:flex;flex-direction:column;gap:10px;width:min(280px,86vw)}
input{padding:14px;border-radius:10px;border:1px solid #ffffff22;background:#363c26;color:#dce0d9;font:inherit}
button{padding:14px;border-radius:10px;border:0;background:#d4a033;color:#282d1c;font:inherit;font-weight:600}
.err{color:#ef5b5b;font-size:14px;text-align:center}
</style></head><body>
<form method=post action=/login>
  <div class=err><!--err--></div>
  <input type=password name=password placeholder="password" autofocus>
  <button type=submit>Sign in</button>
</form>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _authed(self):
        if not TOKEN and not PASSWORD:
            return True
        if TOKEN and parse_qs(urlparse(self.path).query).get("t", [""])[0] == TOKEN:
            return True
        cookie = self.headers.get("Cookie", "")
        return any(v and f"hyprpad={v}" in cookie for v in (TOKEN, PWHASH))

    def _deny(self):
        self.send_response(401)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"unauthorized: append ?t=<token>")

    def _login_page(self, err=""):
        body = LOGIN_PAGE.replace("<!--err-->", err).encode()
        self.send_response(401 if err else 200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _login(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        if "json" in self.headers.get("Content-Type", ""):
            try:
                pw = json.loads(raw or b"{}").get("password", "")
            except Exception:
                pw = ""
        else:
            pw = parse_qs(raw.decode("utf-8", "ignore")).get("password", [""])[0]
        if PASSWORD and hashlib.sha256(pw.encode()).hexdigest() == PWHASH:
            self.send_response(303)
            self.send_header("Set-Cookie",
                             f"hyprpad={PWHASH}; Path=/; Max-Age=31536000; SameSite=Lax")
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self._login_page("Wrong password")

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authed():
            if PASSWORD and (path == "/" or path == ""):
                return self._login_page()
            return self._deny()
        if path == "/" or path == "":
            css, mode = omarchy_theme()
            page = PAGE.replace("/*OMARCHY_VARS*/", css or "")
            page = page.replace("__OMARCHY_MODE__", json.dumps(mode))
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if TOKEN:
                self.send_header("Set-Cookie",
                                 f"hyprpad={TOKEN}; Path=/; Max-Age=31536000; SameSite=Lax")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/manifest.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Content-Length", str(len(MANIFEST)))
            self.end_headers()
            self.wfile.write(MANIFEST)
        elif path == "/icon.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "max-age=604800")
            self.send_header("Content-Length", str(len(ICON_PNG)))
            self.end_headers()
            self.wfile.write(ICON_PNG)
        elif path == "/api/media":
            want = parse_qs(urlparse(self.path).query).get("player", [None])[0]
            body = json.dumps(media_info(want) or {}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/active-window":
            body = json.dumps({"class": active_window_class()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/media-art":
            want = parse_qs(urlparse(self.path).query).get("player", [None])[0]
            fp = media_art_file(want)
            data = None
            if fp:
                try:
                    with open(fp, "rb") as f:
                        data = f.read()
                except OSError:
                    data = None
            if data:
                ctype = mimetypes.guess_type(fp)[0] or "image/jpeg"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(204); self.end_headers()
        elif path == "/api/screen":
            data = screen_frame()
            if data:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(204); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            return self._login()
        if not self._authed():
            return self._deny()
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        try:
            d = json.loads(raw or b"{}")
        except Exception:
            d = {}

        if path == "/ptr":
            try:
                if MOUSE:
                    if "dx" in d or "dy" in d:
                        MOUSE.write(e.EV_REL, e.REL_X, int(d.get("dx", 0)))
                        MOUSE.write(e.EV_REL, e.REL_Y, int(d.get("dy", 0)))
                    if d.get("scroll"):
                        MOUSE.write(e.EV_REL, e.REL_WHEEL, int(d["scroll"]))
                    if d.get("hscroll"):
                        MOUSE.write(e.EV_REL, e.REL_HWHEEL, int(d["hscroll"]))
                    if "click" in d:
                        btn = {"left": e.BTN_LEFT, "right": e.BTN_RIGHT,
                               "mid": e.BTN_MIDDLE}.get(d["click"])
                        if btn is not None:
                            MOUSE.write(e.EV_KEY, btn, 1 if d.get("state", 1) else 0)
                    MOUSE.syn()
                self.send_response(204); self.end_headers()
            except Exception:
                self.send_response(400); self.end_headers()
        elif path == "/key":
            try:
                kbd_send(d)
                self.send_response(204); self.end_headers()
            except Exception:
                self.send_response(400); self.end_headers()
        elif path == "/api/media":
            if media_control(d.get("action", ""), d.get("player")):
                self.send_response(204); self.end_headers()
            else:
                self.send_response(400); self.end_headers()
        elif path == "/api/voice":
            try:
                text = transcribe_and_type(raw)
                body = json.dumps({"text": text}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_response(400); self.end_headers()
        else:
            self.send_response(404); self.end_headers()


if __name__ == "__main__":
    print(f"hyprpad em http://0.0.0.0:{PORT}"
          + (f" (token: {TOKEN})" if TOKEN else ""))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
