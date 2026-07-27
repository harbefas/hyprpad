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
import os
import struct
import subprocess
import zlib
from urllib.parse import parse_qs, urlparse

from evdev import UInput, ecodes as e
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("HYPRPAD_PORT", "8123"))
TOKEN = os.environ.get("HYPRPAD_TOKEN", "")
PASSWORD = os.environ.get("HYPRPAD_PASSWORD", "")
PWHASH = hashlib.sha256(PASSWORD.encode()).hexdigest() if PASSWORD else ""
WAYLAND_DISPLAY = os.environ.get("WAYLAND_DISPLAY", "wayland-1")
XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")


# --- mouse + teclado virtuais (uinput) ---
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


# char -> (KEY_code, precisa_shift) — layout US (o que a maioria dos teclados manda)
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
    """Injeta no teclado virtual. d: {char} | {key,state?} | {mods:[...]} combinaveis."""
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
        else:                                # segurar/soltar
            KBD.write(e.EV_KEY, k, 1 if st else 0)
    for k in reversed(mods):
        KBD.write(e.EV_KEY, k, 0)
    KBD.syn()


def screen_frame():
    """Screenshot atual via grim (wlroots), JPEG reduzido pra caber no wifi."""
    env = {**os.environ, "WAYLAND_DISPLAY": WAYLAND_DISPLAY, "XDG_RUNTIME_DIR": XDG_RUNTIME_DIR}
    try:
        r = subprocess.run(["grim", "-s", "0.5", "-t", "jpeg", "-q", "55", "-"],
                           capture_output=True, env=env, timeout=5)
        return r.stdout if r.returncode == 0 else b""
    except Exception:
        return b""


def make_icon(size=512):
    """PNG do icone (Python puro, sem deps de imagem): blob azul num fundo escuro."""
    bg = (20, 22, 27)      # #14161b
    fg = (91, 141, 239)    # #5b8def
    px = bytearray()
    cx, cy = size / 2, size / 2
    for y in range(size):
        px.append(0)        # filtro de linha
        for x in range(size):
            dx = (x - cx) / (size * 0.36)
            dy = (y - cy) / (size * 0.30)
            px += bytes(fg if dx * dx + dy * dy <= 1 else bg)
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
    "background_color": "#14161b", "theme_color": "#14161b",
    "icons": [
        {"src": "/icon.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/icon.png", "sizes": "192x192", "type": "image/png"},
    ],
}).encode()


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>hyprpad</title>
<link rel=manifest href=/manifest.json><link rel=icon href=/icon.png>
<meta name=theme-color content=#14161b>
<style>
:root{
  --bg:#14161b; --surface:#1c1f26; --ui:#262b34; --ui-2:#2f3542;
  --tx:#e6e8ec; --tx-2:#9aa2b1; --tx-3:#6b7280;
  --accent:#5b8def; --border:#ffffff14;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;margin:0;background:var(--bg);color:var(--tx);
  font:15px/1.4 -apple-system,system-ui,sans-serif;overscroll-behavior:none;user-select:none}
#app{position:fixed;inset:0;display:flex;flex-direction:column;overflow:hidden}
#deskmain{flex:1;min-height:0;display:flex;flex-direction:column}
/* landscape (deitado): botoes a ESQUERDA, trackpad a DIREITA (dois polegares).
   portrait (vertical) fica no padrao: teclado em cima, trackpad embaixo. */
@media (orientation:landscape){
  #deskmain{flex-direction:row}
  #deskmain #deskkeys{flex:0 0 42%;align-content:center;justify-content:center;gap:7px;
    padding:10px 8px;overflow-y:auto;-webkit-overflow-scrolling:touch}
  #deskmain #padrow{flex:1 1 auto;padding:10px 14px 14px 6px}
  #tpad{max-height:none}
}
#deskscreen{display:none;flex:0 0 auto;margin:8px 14px 4px;border-radius:12px;overflow:hidden;
  background:#000;border:1px solid var(--border);aspect-ratio:16/9;align-items:center;justify-content:center}
body[data-screen] #deskscreen{display:flex}
#deskimg{width:100%;height:100%;object-fit:contain;display:block}
#padrow{flex:0 0 44vh;min-height:0;padding:2px 14px 34px}   /* padding de baixo afasta da alça do menu */
#tpad2{touch-action:none}
#tpad{width:100%;height:100%;background:var(--surface);border:1px solid var(--border);
  border-radius:14px;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:6px;color:var(--tx-3);font-size:14px;text-align:center;padding:0 14px;touch-action:none;user-select:none}
#tpad small{font-size:10.5px;opacity:.7;line-height:1.4}
#screentoggle.on,#kbtoggle.on{background:var(--accent)!important;color:#fff!important}
#deskkeys{display:flex;flex-wrap:wrap;gap:8px;padding:calc(env(safe-area-inset-top,0px) + 12px) 14px 8px;
  flex:1;align-content:center;justify-content:center}
body[data-screen] #deskkeys{flex:0 0 auto;padding-top:8px}   /* com a tela ligada, ela ocupa o topo */
#deskkeys button{background:var(--ui);color:var(--tx);border:0;border-radius:8px;padding:11px 15px;
  font:inherit;font-weight:600;min-width:46px}
#deskkeys button:active,#deskkeys button.on{background:var(--accent);color:#fff}
#arrowrow{display:flex;align-items:stretch;gap:4px}
#arrowrow #tpad2{width:56px;font-size:22px;padding:0;min-width:0}
#arrows{display:grid;grid-template-columns:repeat(3,44px);grid-auto-rows:44px;gap:4px}
#arrows button{padding:0;min-width:0;display:flex;align-items:center;justify-content:center}
#arrows .au{grid-column:2;grid-row:1}
#arrows .al{grid-column:1;grid-row:2}
#arrows .ad{grid-column:2;grid-row:2}
#arrows .ar{grid-column:3;grid-row:2}
/* grupos: cada linha empilha (modo -> nav -> atalhos), atalhos mais usados
   ficam por ultimo = mais perto do polegar na pegada em paisagem */
.deskgroup{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;flex:1 0 100%;padding:4px 0}
#navgrp{border-top:1px solid var(--border);border-bottom:1px solid var(--border);margin:2px 0}
#shortgrp button.flash{background:var(--accent)!important;color:#fff!important}
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
  <div id=deskscreen><img id=deskimg alt="tela do PC"></div>
  <div id=deskmain>
    <div id=deskkeys>
      <div class=deskgroup id=modegrp>
        <button id=kbtoggle>⌨ Digitar</button>
        <button id=screentoggle>👁 Tela</button>
      </div>
      <div class=deskgroup id=navgrp>
        <button data-key=esc>Esc</button><button data-key=tab>Tab</button>
        <button data-key=backspace>⌫</button><button data-key=enter>Enter</button>
        <button data-key=print>PrtSc</button>
        <div id=arrowrow>
          <button id=tpad2 aria-label="segurar = 2 dedos (rolar/botão direito)">✌</button>
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
    </div>
    <div id=padrow>
      <div id=tpad>trackpad<br><small>arraste move · toque clica · 2 dedos rolar</small></div>
    </div>
  </div>
  <input id=kbin type=text inputmode=email autocomplete=off autocapitalize=off autocorrect=off spellcheck=false>
  <div id=kbecho><span id=kbechot></span><span id=kbcaret></span></div>
</div>

<script>
const buzz=(ms=12)=>{try{navigator.vibrate&&navigator.vibrate(ms)}catch(_){}};

function goFullscreen(){
  const el=document.documentElement;
  const fn=el.requestFullscreen||el.webkitRequestFullscreen;
  if(fn && !document.fullscreenElement){ try{fn.call(el);}catch(_){} }
}
window.addEventListener('touchend', goFullscreen, {passive:true});
window.addEventListener('click', goFullscreen);

// tela ao vivo (grim), atualiza enquanto ligada
let deskT=null;
function deskTick(){const img=document.getElementById('deskimg');if(!img)return;
  const u='/api/screen?'+Date.now();const pre=new Image();pre.onload=()=>{img.src=u;};pre.src=u;}
function startDeskScreen(){deskTick();if(!deskT)deskT=setInterval(deskTick,700);}
function stopDeskScreen(){if(deskT){clearInterval(deskT);deskT=null;}}

// ---------- usar o celular como mouse + teclado do PC ----------
(function(){
  const tpad=document.getElementById('tpad'); if(!tpad) return;
  const ptr=o=>fetch('/ptr',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o),keepalive:true}).catch(()=>{});
  const key=o=>fetch('/key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o),keepalive:true}).catch(()=>{});
  const mods={ctrl:false,alt:false,super:false};
  const activeMods=()=>Object.keys(mods).filter(k=>mods[k]);
  const clearMods=()=>{for(const k in mods)mods[k]=false;
    for(const b of document.querySelectorAll('#deskkeys button[data-mod]'))b.classList.remove('on');};
  // --- trackpad ---
  let lx=0,ly=0,moved=false,t0=0,startFingers=1,lsy=0;
  // --- "2o dedo": segura o botao com o outro polegar pra simular 2 dedos no trackpad ---
  let held2=false;
  const tpad2=document.getElementById('tpad2');
  if(tpad2){
    const on2=ev=>{ev.preventDefault();held2=true;tpad2.classList.add('on');buzz(8);};
    const off2=()=>{held2=false;tpad2.classList.remove('on');};
    tpad2.addEventListener('touchstart',on2,{passive:false});
    tpad2.addEventListener('touchend',off2); tpad2.addEventListener('touchcancel',off2);
    tpad2.addEventListener('mousedown',on2); tpad2.addEventListener('mouseup',off2); tpad2.addEventListener('mouseleave',off2);
  }
  tpad.addEventListener('touchstart',ev=>{ev.preventDefault();startFingers=held2?2:ev.targetTouches.length;
    const t=ev.targetTouches[0];lx=t.clientX;ly=t.clientY;lsy=t.clientY;moved=false;t0=Date.now();},{passive:false});
  tpad.addEventListener('touchmove',ev=>{ev.preventDefault();const t=ev.targetTouches[0];
    if(held2||ev.targetTouches.length>=2){const dy=t.clientY-lsy;if(Math.abs(dy)>7){ptr({scroll:dy>0?-1:1});lsy=t.clientY;moved=true;}return;}
    const dx=t.clientX-lx,dy=t.clientY-ly;
    if(Math.abs(dx)>1||Math.abs(dy)>1){moved=true;ptr({dx:Math.round(dx*1.7),dy:Math.round(dy*1.7)});lx=t.clientX;ly=t.clientY;}},{passive:false});
  tpad.addEventListener('touchend',ev=>{ev.preventDefault();
    if(!moved&&Date.now()-t0<220){const c=startFingers>=2?'right':'left';buzz(10);ptr({click:c,state:1});setTimeout(()=>ptr({click:c,state:0}),25);}},{passive:false});
  // --- atalhos fixos (adicionar aqui = so isso, sem mexer no HTML) ---
  const SHORTCUTS=[{lbl:'⊞C',char:'c',mods:['super']}];
  const shortgrp=document.getElementById('shortgrp');
  for(const s of SHORTCUTS){
    const b=document.createElement('button'); b.textContent=s.lbl;
    b.onclick=()=>{buzz(15); key(s.char?{char:s.char,mods:s.mods}:{key:s.key,mods:s.mods});
      b.classList.add('flash'); setTimeout(()=>b.classList.remove('flash'),120);};
    shortgrp.appendChild(b);}
  // --- segurar seta = repete (navegar/scroll longo sem ficar tocando) ---
  for(const b of document.querySelectorAll('#arrows button.rep')){
    const k=b.dataset.key; let iv=null;
    const fire=()=>{buzz();key({key:k});};
    const start=ev=>{ev.preventDefault(); fire(); iv=setTimeout(function rep(){fire();iv=setTimeout(rep,70);},300);};
    const stop=()=>{clearTimeout(iv);iv=null;};
    b.addEventListener('touchstart',start,{passive:false});
    b.addEventListener('touchend',stop); b.addEventListener('touchcancel',stop);
    b.addEventListener('mousedown',start); b.addEventListener('mouseup',stop); b.addEventListener('mouseleave',stop);}
  // --- mostrar/esconder a tela do PC (grim so roda quando ligado) ---
  const st=document.getElementById('screentoggle');
  st.onclick=()=>{const on=document.body.toggleAttribute('data-screen');st.classList.toggle('on',on);buzz();
    if(on)startDeskScreen();else stopDeskScreen();};
  // --- teclado nativo do celular ---
  const kbin=document.getElementById('kbin');
  const kbt=document.getElementById('kbtoggle');
  const kbecho=document.getElementById('kbecho'), kbechot=document.getElementById('kbechot');
  // eco = o proprio value do campo (sem buffer paralelo que desincroniza)
  const showEcho=()=>{ kbechot.textContent=(kbin.value||'').slice(-48); };
  // visibilidade da barra controlada SO aqui (nao em focus/blur: no Android eles
  // disparam espurios com a predicao e faziam a barra piscar).
  const openKb=()=>{ kbin.value=''; kblast=''; showEcho();
    kbecho.classList.add('on'); kbt.classList.add('on'); kbin.focus(); };
  const closeKb=()=>{ kbin.blur(); kbecho.classList.remove('on'); kbt.classList.remove('on'); };
  kbt.onclick=()=>{ kbt.classList.contains('on')?closeKb():openKb(); };
  tpad.addEventListener('dblclick',openKb);
  // captura por DIFF no evento 'input' (todo teclado Android dispara input;
  // beforeinput/keydown nem sempre, ex.: teclado AOSP do LineageOS)
  // DIFF no value: inputmode=email da teclado sem predicao (letras caem na hora),
  // type=text permite espaco, e o diff pega insercao E backspace (value encolhe).
  let kblast='';
  kbin.addEventListener('input',()=>{
    const cur=kbin.value||'';
    let p=0; const m=Math.min(cur.length,kblast.length);
    while(p<m && cur[p]===kblast[p]) p++;
    const dele=kblast.length-p, ins=cur.slice(p);
    for(let i=0;i<dele;i++) key({key:'backspace'});             // apagados
    let sent=false;
    for(const ch of ins){ key({char:ch,mods:activeMods()}); sent=true; } // inseridos
    if(sent) clearMods();
    kblast=cur;
    if(cur.length>160){ kbin.value=''; kblast=''; }              // nao cresce sem fim
    showEcho();                                                  // eco = value atual
  });
  kbin.addEventListener('keydown',ev=>{const m={Enter:'enter',Backspace:'backspace',Tab:'tab',Escape:'esc',ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right'};
    if(m[ev.key]){ev.preventDefault();key({key:m[ev.key],mods:activeMods()});clearMods();
      if(ev.key==='Enter'){ kbin.value=''; kblast=''; }          // Enter = manda e limpa a barra
      showEcho();}});
  // --- teclas especiais + modificadores ---
  for(const b of document.querySelectorAll('#deskkeys button[data-key]:not(.rep)')){const k=b.dataset.key;
    b.onclick=()=>{buzz();key({key:k,mods:activeMods()});clearMods();};}
  for(const b of document.querySelectorAll('#deskkeys button[data-char]')){const ch=b.dataset.char;
    b.onclick=()=>{buzz();key({char:ch,mods:activeMods()});clearMods();};}
  for(const b of document.querySelectorAll('#deskkeys button[data-mod]')){const mo=b.dataset.mod;
    b.onclick=()=>{mods[mo]=!mods[mo];b.classList.toggle('on',mods[mo]);buzz();};}
})();

// teclado nativo aberto -> encolhe a area do app pra acima do teclado (trackpad sobe)
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
html,body{height:100%;margin:0;background:#14161b;color:#e6e8ec;
  font:16px/1.4 -apple-system,system-ui,sans-serif;display:flex;align-items:center;justify-content:center}
form{display:flex;flex-direction:column;gap:10px;width:min(280px,86vw)}
input{padding:14px;border-radius:10px;border:1px solid #ffffff22;background:#1c1f26;color:#e6e8ec;font:inherit}
button{padding:14px;border-radius:10px;border:0;background:#5b8def;color:#fff;font:inherit;font-weight:600}
.err{color:#ef5b5b;font-size:14px;text-align:center}
</style></head><body>
<form method=post action=/login>
  <div class=err><!--err--></div>
  <input type=password name=password placeholder="senha" autofocus>
  <button type=submit>Entrar</button>
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
            self._login_page("Senha errada")

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authed():
            if PASSWORD and (path == "/" or path == ""):
                return self._login_page()
            return self._deny()
        if path == "/" or path == "":
            body = PAGE.encode()
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
        else:
            self.send_response(404); self.end_headers()


if __name__ == "__main__":
    print(f"hyprpad em http://0.0.0.0:{PORT}"
          + (f" (token: {TOKEN})" if TOKEN else ""))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
