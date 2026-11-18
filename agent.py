#!/usr/bin/env python3
"""ATLAS TV - agent v0.3 (prototype)
Se connecte tout seul au serveur MQTT, trouve la TV sur le réseau local,
gère l'appairage et exécute les commandes envoyées par l'app.
"""
import asyncio, base64, html, io, json, logging, os, random, re, secrets, shutil, socket, ssl, subprocess, sys, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import xml.etree.ElementTree as ET
from pathlib import Path

import paho.mqtt.client as mqtt
from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth
from samsungtvws import SamsungTVWS
from wakeonlan import send_magic_packet

VERSION = "0.3.2"
AGENT_FILE = Path(__file__).resolve()
CONF = Path("/etc/atlas/config.json")
DATA = Path("/var/lib/atlas")
log = logging.getLogger("atlas")

# --------------------------------------------------------------- touches
KEYS = {
    "androidtv": {
        "power": "POWER", "vol_up": "VOLUME_UP", "vol_down": "VOLUME_DOWN", "mute": "VOLUME_MUTE",
        "ch_up": "CHANNEL_UP", "ch_down": "CHANNEL_DOWN", "ok": "DPAD_CENTER", "back": "BACK",
        "home": "HOME", "up": "DPAD_UP", "down": "DPAD_DOWN", "left": "DPAD_LEFT", "right": "DPAD_RIGHT",
        "guide": "GUIDE", "info": "INFO", "menu": "MENU", "tv": "TV",
        **{str(i): str(i) for i in range(10)},
    },
    "samsung": {
        "power": "KEY_POWER", "vol_up": "KEY_VOLUP", "vol_down": "KEY_VOLDOWN", "mute": "KEY_MUTE",
        "ch_up": "KEY_CHUP", "ch_down": "KEY_CHDOWN", "ok": "KEY_ENTER", "back": "KEY_RETURN",
        "home": "KEY_HOME", "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
        "guide": "KEY_GUIDE", "info": "KEY_INFO", "menu": "KEY_MENU", "tv": "KEY_TV", "exit": "KEY_EXIT",
        **{str(i): f"KEY_{i}" for i in range(10)},
    },
}
# séquence "sortir des menus" avant de remettre la chaîne favorite
REPAIR_EXIT = {"androidtv": ["home"], "samsung": ["exit", "back", "back", "tv"]}


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    finally:
        s.close()


async def port_open(ip, port, timeout=0.7):
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        w.close(); return True
    except Exception:
        return False


def http_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


# --------------------------------------------------------------- dépendances (auto-réparation)
def ensure_deps():
    """Installe ce qui manque (utile après une mise à jour à distance)."""
    try:
        need = [p for p, b in (("adb", "adb"), ("mpv", "mpv"), ("v4l-utils", "cec-ctl")) if not shutil.which(b)]
        if not Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf").exists():
            need.append("fonts-dejavu-core")
        if not shutil.which("dnsmasq"):
            need.append("dnsmasq-base")  # nécessaire au Wi-Fi « ATLAS-Setup »
        if need:
            log.info("installation de %s", need)
            subprocess.run(["apt-get", "install", "-y", "-q", *need], timeout=1200,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for mod, pkg in (("PIL", "pillow"), ("qrcode", "qrcode")):
            try:
                __import__(mod)
            except ImportError:
                subprocess.run([sys.executable, "-m", "pip", "install", "-q", pkg], timeout=900)
        if shutil.which("raspi-config"):  # le Wi-Fi du Pi reste bloqué tant que le pays n'est pas réglé
            subprocess.run(["raspi-config", "nonint", "do_wifi_country", "CH"], timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["rfkill", "unblock", "wifi"], timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cfg = Path("/boot/firmware/config.txt")  # le Pi ne doit pas voler l'entrée HDMI au démarrage
        if cfg.exists() and "hdmi_ignore_cec_init" not in cfg.read_text():
            with cfg.open("a") as f:
                f.write("\n# ATLAS\nhdmi_ignore_cec_init=1\nhdmi_force_hotplug=1\n")
    except Exception as e:
        log.warning("installation des dépendances: %s", e)


# --------------------------------------------------------------- HDMI : CEC (bascule d'entrée)
class CEC:
    DEV = "/dev/cec0"

    def available(self):
        return Path(self.DEV).exists() and shutil.which("cec-ctl") is not None

    def _c(self, *a, timeout=8):
        return run(["cec-ctl", "-d", self.DEV, *a], timeout)

    def init(self):
        if self.available():
            self._c("--playback", "-o", "ATLAS")

    def info(self):
        """Adresse HDMI du boîtier + autres appareils (box) vus sur le bus."""
        out = self._c("--show-topology", timeout=25)
        head = out.split("System Information", 1)[0]
        m = re.search(r"Physical Address\s*:\s*([0-9a-f.]+)", head)
        me = m.group(1) if m else None
        devs = []
        for sec in out.split("System Information for device")[1:]:
            pa = re.search(r"Physical Address\s*:\s*([0-9a-f.]+)", sec)
            nm = re.search(r"OSD Name\s*:\s*'?([^'\n]*)'?", sec)
            la = re.match(r"\s*(\d+)\s*\(([^)]*)\)", sec)
            if pa:
                devs.append({"la": int(la.group(1)) if la else None, "type": la.group(2) if la else "",
                             "phys": pa.group(1), "name": (nm.group(1).strip() if nm else "")})
        return me, devs, out[-1500:]

    def show_me(self, phys):
        self._c("--to", "0", "--image-view-on")
        time.sleep(0.5)
        if phys and phys != "f.f.f.f":
            self._c("--active-source", f"phys-addr={phys}")

    def give_back(self, phys, target=None):
        if target:
            self._c("--set-stream-path", f"phys-addr={target}")
        elif phys:
            self._c("--inactive-source", f"phys-addr={phys}")


# --------------------------------------------------------------- HDMI : affichage (photos, écrans d'installation)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONTB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


class Display:
    def __init__(self):
        self.proc = None
        self.what = None

    def _start(self, args):
        self.stop()
        if not shutil.which("mpv"):
            raise RuntimeError("affichage HDMI pas encore prêt (installation de mpv en cours)")
        logf = open(DATA / "mpv.log", "w")
        self.proc = subprocess.Popen(["mpv", "--vo=drm", "--fs", "--no-audio", "--no-osc", "--no-terminal",
                                      "--osd-level=0", *args], stdout=logf, stderr=logf)
        time.sleep(1.5)
        if self.proc.poll() is not None:
            err = (DATA / "mpv.log").read_text()[-300:]
            self.proc = None
            raise RuntimeError("affichage HDMI impossible (câble HDMI branché ?) " + err.strip().splitlines()[-1:][0] if err.strip() else "affichage HDMI impossible (câble HDMI branché ?)")

    def image(self, path, what="screen"):
        self._start(["--image-display-duration=inf", "--loop-file=inf", str(path)]); self.what = what

    def slideshow(self, files, seconds, shuffle):
        files = list(files)
        if shuffle:
            random.shuffle(files)
        lst = DATA / "playlist.txt"; lst.write_text("\n".join(str(f) for f in files))
        self._start([f"--image-display-duration={seconds}", "--loop-playlist=inf", f"--playlist={lst}"])
        self.what = "slideshow"

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except Exception:
                self.proc.kill()
        self.proc = None; self.what = None


def render_screen(title, lines=(), qrs=(), color=(31, 111, 139)):
    """Fabrique une image 1920x1080 (titre, textes à gauche, QR codes à droite) à afficher sur la TV."""
    from PIL import Image, ImageDraw, ImageFont
    import qrcode
    W, H = 1920, 1080
    im = Image.new("RGB", (W, H), (14, 26, 34)); d = ImageDraw.Draw(im)
    d.rectangle((0, 0, W, 14), fill=color)
    fb = ImageFont.truetype(FONTB, 72); f = ImageFont.truetype(FONT, 40); fs = ImageFont.truetype(FONT, 32)
    d.text((110, 80), "ATLAS TV", font=ImageFont.truetype(FONTB, 38), fill=(120, 190, 215))
    d.text((110, 140), title, font=fb, fill=(255, 255, 255))
    qrs = list(qrs)
    maxw = (W - 220) if not qrs else (W - 110 - len(qrs) * 440 - 60)
    y = 300
    for ln in lines:
        words, cur = ln.split(" "), ""
        indent = len(ln) - len(ln.lstrip(" "))
        for w in words:
            t = (cur + " " + w).strip() if cur else w
            if d.textlength(t, font=f) > maxw and cur:
                d.text((110, y), cur, font=f, fill=(215, 228, 236)); y += 58; cur = " " * indent + w
            else:
                cur = t if not cur else cur + " " + w
        d.text((110, y), cur, font=f, fill=(215, 228, 236)); y += 62
    x = W - 110
    for label, data in reversed(qrs):
        q = qrcode.QRCode(border=2, box_size=10); q.add_data(data); q.make(fit=True)
        img = q.make_image(fill_color="black", back_color="white").convert("RGB").resize((380, 380))
        x -= 380
        im.paste(img, (x, 300))
        d.text((x + 190 - d.textlength(label, font=fs) / 2, 700), label, font=fs, fill=(255, 255, 255))
        x -= 60
    p = DATA / "screen.png"; im.save(p); return p


# --------------------------------------------------------------- Wi-Fi : mode installation « ATLAS-Setup »
class WiFi:
    CON = "atlas-setup"

    def nm(self, *a, timeout=30):
        r = subprocess.run(["nmcli", *a], capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()

    def current(self):
        rc, out = self.nm("-t", "-f", "TYPE,STATE,CONNECTION", "device")
        res = {}
        for ln in out.splitlines():
            t, st, con = (ln.split(":") + ["", "", ""])[:3]
            if st == "connected" and con != self.CON and t in ("ethernet", "wifi"):
                res[t] = con
        return res  # ex. {"ethernet": "Wired connection 1", "wifi": "MaMaison"}

    def scan(self):
        self.nm("device", "wifi", "rescan", timeout=20); time.sleep(3)
        rc, out = self.nm("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list")
        nets = {}
        for ln in out.splitlines():
            parts = re.split(r"(?<!\\):", ln)
            if len(parts) >= 3 and parts[0]:
                ssid = parts[0].replace("\\:", ":")
                sig = int(parts[1] or 0)
                if ssid not in nets or nets[ssid]["signal"] < sig:
                    nets[ssid] = {"ssid": ssid, "signal": sig, "secure": parts[2] not in ("", "--")}
        return sorted(nets.values(), key=lambda n: -n["signal"])

    def start_hotspot(self, ssid, pw):
        self.nm("connection", "delete", self.CON)
        rc, out = self.nm("device", "wifi", "hotspot", "ifname", "wlan0", "con-name", self.CON, "ssid", ssid, "password", pw)
        self.nm("connection", "modify", self.CON, "connection.autoconnect", "no")
        if rc:
            raise RuntimeError(out)

    def stop_hotspot(self):
        self.nm("connection", "down", self.CON)

    def connect(self, ssid, pw):
        self.stop_hotspot(); time.sleep(3)
        self.nm("connection", "delete", "id", ssid)  # efface un ancien essai (mot de passe périmé)
        for _ in range(4):  # attendre que le réseau réapparaisse dans la liste après l'arrêt du hotspot
            self.nm("device", "wifi", "rescan", timeout=20); time.sleep(4)
            rc, out = self.nm("-t", "-f", "SSID", "device", "wifi", "list")
            if ssid in [l.replace("\\:", ":") for l in out.splitlines()]:
                break
        args = ["device", "wifi", "connect", ssid, "ifname", "wlan0"] + (["password", pw] if pw else [])
        rc, out = self.nm(*args, timeout=90)
        return rc == 0, out


def wifi_error(out):
    o = out.lower()
    if "secrets were required" in o or "802-1x" in o or "psk" in o or "authentication" in o:
        return "Mot de passe refusé par le Wi-Fi"
    if "no network with ssid" in o or "not found" in o:
        return "Réseau introuvable (boîtier trop loin du Wi-Fi ?)"
    if "timeout" in o:
        return "Le Wi-Fi ne répond pas (trop loin ?)"
    return "Connexion impossible : " + out.strip().splitlines()[-1][:70] if out.strip() else "Connexion impossible"


PORTAL = """<!doctype html><html lang=fr><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>ATLAS - Wi-Fi</title><style>body{{font:17px -apple-system,Segoe UI,sans-serif;margin:0;padding:24px 18px;background:#f3f6f8;color:#14212b}}
h1{{font-size:24px}}.c{{background:#fff;border-radius:16px;padding:18px;border:1px solid #dde5ea}}label{{display:block;margin:14px 0 6px;color:#61717d}}
select,input{{font:inherit;width:100%;padding:14px;border-radius:12px;border:1px solid #cdd8df;box-sizing:border-box}}
button{{font:inherit;font-weight:700;width:100%;padding:16px;border:0;border-radius:14px;background:#1f6f8b;color:#fff;margin-top:18px}}</style>
<h1>Connecter ATLAS au Wi-Fi</h1><div class=c>{body}</div></html>"""


class Portal(BaseHTTPRequestHandler):
    agent = None

    def log_message(self, *a):
        pass

    def _send(self, body):
        b = PORTAL.format(body=body).encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        nets = self.agent.wifi_nets
        opts = "".join(f'<option value="{html.escape(n["ssid"], True)}">{html.escape(n["ssid"])} '
                       f'({n["signal"]}%)</option>' for n in nets)
        self._send(f"""<form method=post action=/connect><label>Réseau Wi-Fi de la maison</label>
<select name=ssid>{opts}<option value="">Autre (taper le nom)…</option></select>
<label>…ou nom du réseau</label><input name=ssid2 autocapitalize=none autocomplete=off placeholder="si absent de la liste">
<label>Mot de passe Wi-Fi</label><input name=pw type=password autocomplete=off>
<button>Connecter</button></form>""")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        q = urllib.parse.parse_qs(self.rfile.read(n).decode())
        ssid = (q.get("ssid2", [""])[0] or q.get("ssid", [""])[0]).strip()
        pw = q.get("pw", [""])[0]
        if not ssid:
            return self.do_GET()
        self._send("<p><b>Connexion en cours…</b></p><p>Regardez la TV : elle affichera le résultat dans quelques secondes.</p>"
                   "<p>Votre téléphone va se déconnecter du Wi-Fi ATLAS, c'est normal.</p>")
        self.agent.loop.call_soon_threadsafe(self.agent.queue.put_nowait,
                                              json.dumps({"action": "_wifi_join", "ssid": ssid, "pw": pw,
                                                          "k": self.agent.conf["device_key"]}).encode())


# --------------------------------------------------------------- vue écran (ADB, box Android)
def run(cmd, timeout=10, binary=False):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.stdout if binary else r.stdout.decode(errors="replace")


class Screen:
    """Capture de l'interface (menus) et texte affiché, via le débogage ADB de la box.
    La vidéo des chaînes est masquée par Android lui-même (couche protégée) : rien n'est contourné."""

    def __init__(self, ip):
        self.ip = ip
        self.serial = f"{ip}:5555"

    def available(self):
        return shutil.which("adb") is not None

    def state(self):
        out = run(["adb", "devices"], 8)
        for line in out.splitlines():
            if line.startswith(self.serial):
                return line.split()[-1]  # device / unauthorized / offline
        return "absent"

    def connect(self):
        run(["adb", "connect", self.serial], 10)
        return self.state()

    def capture(self):
        png = run(["adb", "-s", self.serial, "exec-out", "screencap", "-p"], 15, binary=True)
        if not png.startswith(b"\x89PNG"):
            raise RuntimeError("capture impossible")
        from PIL import Image
        im = Image.open(io.BytesIO(png)).convert("RGB")
        im.thumbnail((720, 405))
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=60)
        dark = sum(im.convert("L").resize((32, 18)).getdata()) / (32 * 18) < 8
        return base64.b64encode(buf.getvalue()).decode(), dark

    def ui_text(self):
        xml = run(["adb", "-s", self.serial, "exec-out", "sh", "-c",
                   "uiautomator dump /sdcard/atlas_ui.xml >/dev/null 2>&1; cat /sdcard/atlas_ui.xml"], 15)
        i = xml.find("<hierarchy")
        if i < 0:
            return {"focus": None, "texts": []}
        root = ET.fromstring(xml[i:xml.rfind(">") + 1])
        texts, focus = [], None
        for n in root.iter("node"):
            t = (n.get("text") or n.get("content-desc") or "").strip()
            if not t or len(t) > 80:
                continue
            if n.get("focused") == "true" or n.get("selected") == "true":
                focus = focus or t
            if t not in texts:
                texts.append(t)
        if not focus:  # l'élément sélectionné porte souvent son texte dans un enfant
            for n in root.iter("node"):
                if n.get("focused") == "true":
                    for c in n.iter("node"):
                        t = (c.get("text") or c.get("content-desc") or "").strip()
                        if t:
                            focus = t; break
                    break
        return {"focus": focus, "texts": texts[:12]}


# --------------------------------------------------------------- pilotes
class AndroidTV:
    kind = "androidtv"
    notify = staticmethod(lambda **kw: None)

    def __init__(self, info, on_change):
        self.info = info
        self.on_change = on_change
        self.r = AndroidTVRemote("ATLAS", str(DATA / "atv_cert.pem"), str(DATA / "atv_key.pem"), info["ip"])
        self.connected = False

    @property
    def is_on(self):
        return bool(self.r.is_on) if self.connected else None

    def status(self):
        v = self.r.volume_info if self.connected else None
        return {"is_on": self.is_on, "app": self.r.current_app if self.connected else None,
                "volume": (v or {}).get("level"), "volume_max": (v or {}).get("max"),
                "reachable": self.connected}

    async def start_pairing(self):
        await self.r.async_generate_cert_if_missing()
        try:  # déjà autorisée ? (la box se souvient d'ATLAS)
            await asyncio.wait_for(self.r.async_connect(), 6)
            self.r.disconnect()
            return "done"
        except Exception:
            pass
        await self.r.async_start_pairing()
        return "code"  # un code s'affiche sur la TV

    async def finish_pairing(self, code):
        await self.r.async_finish_pairing(code.strip().upper())

    async def connect(self):
        await self.r.async_generate_cert_if_missing()
        await self.r.async_connect()
        self.connected = True
        for add in (self.r.add_is_on_updated_callback, self.r.add_current_app_updated_callback,
                    self.r.add_volume_info_updated_callback):
            add(lambda *_: self.on_change())
        self.r.add_is_available_updated_callback(self._avail)
        self.r.keep_reconnecting()

    def _avail(self, ok):
        self.connected = ok; self.on_change()

    async def press(self, key):
        self.r.send_key_command(KEYS[self.kind][key])

    async def power(self, on):
        if self.is_on is None or self.is_on != on:
            await self.press("power")

    async def set_volume(self, level):
        v = self.r.volume_info if self.connected else None
        if not v or v.get("level") is None:
            return False
        mx = v.get("max") or 100
        target = round(int(level) * mx / 100)
        diff = target - int(v["level"])
        for _ in range(min(abs(diff), 60)):
            await self.press("vol_up" if diff > 0 else "vol_down"); await asyncio.sleep(0.12)
        return True


class SamsungTV:
    kind = "samsung"

    def __init__(self, info, on_change):
        self.info = info
        self.on_change = on_change
        self.tv = SamsungTVWS(host=info["ip"], port=8002, token_file=str(DATA / f"samsung_{info['mac'] or info['ip']}.token"),
                              name="ATLAS", timeout=8)
        self._on = None
        self._vol = None
        self.notify = lambda **kw: None

    @property
    def is_on(self):
        return self._on

    def status(self):
        return {"is_on": self._on, "app": "Samsung TV", "volume": self._vol, "volume_max": 100 if self._vol is not None else None,
                "reachable": self._on is not None}

    # volume exact via UPnP (port 9197), comme l'app SmartThings
    def _upnp(self, action, args):
        body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1"><InstanceID>0</InstanceID>'
                f'<Channel>Master</Channel>{args}</u:{action}></s:Body></s:Envelope>').encode()
        req = urllib.request.Request(f"http://{self.info['ip']}:9197/upnp/control/RenderingControl1", data=body, headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:RenderingControl:1#{action}"'})
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.read().decode(errors="replace")

    def get_volume(self):
        try:
            m = re.search(r"<CurrentVolume>(\d+)</CurrentVolume>", self._upnp("GetVolume", ""))
            self._vol = int(m.group(1)) if m else None
        except Exception:
            self._vol = None
        return self._vol

    async def set_volume(self, level):
        try:
            await asyncio.to_thread(self._upnp, "SetVolume", f"<DesiredVolume>{int(level)}</DesiredVolume>")
            self._vol = int(level); self.on_change(); return True
        except Exception as e:
            log.info("volume UPnP indisponible (%s), méthode par touches", e)
            return False

    async def refresh(self):
        def q():
            try:
                d = http_json(f"http://{self.info['ip']}:8001/api/v2/", 2)["device"]
                return d.get("PowerState", "on") == "on"
            except Exception:
                return False
        new = await asyncio.to_thread(q)
        old_vol = self._vol
        if new:
            await asyncio.to_thread(self.get_volume)
        if new != self._on or self._vol != old_vol:
            self._on = new; self.on_change()

    def has_token(self):
        p = Path(self.tv.token_file)
        return p.exists() and p.stat().st_size > 0

    async def start_pairing(self):
        def op():
            t = SamsungTVWS(host=self.info["ip"], port=8002, token_file=self.tv.token_file, name="ATLAS", timeout=40)
            t.open(); t.close()
        await asyncio.to_thread(op)  # attend que l'utilisateur appuie sur "Autoriser" sur la TV
        return "done"

    async def finish_pairing(self, code):
        pass

    async def connect(self):
        await self.refresh()

    async def press(self, key):
        k = KEYS[self.kind][key]
        def op():
            try:
                self.tv.send_key(k); return
            except Exception:
                self.tv.close()
            try:
                self.tv.send_key(k); return
            except Exception:
                self.tv.close()
            # la TV redemande peut-être l'autorisation (ex. le boîtier a changé de réseau) : on laisse le temps d'accepter
            self.notify(type="tv_auth", step="accept")
            t = SamsungTVWS(host=self.info["ip"], port=8002, token_file=self.tv.token_file, name="ATLAS", timeout=45)
            t.open(); self.tv = t
            self.notify(type="tv_auth", step="ok")
            t.send_key(k)
        await asyncio.to_thread(op)

    async def power(self, on):
        await self.refresh()
        if on and not self._on:
            mac = self.info.get("mac")
            if mac:
                for addr in ("255.255.255.255", self.info["ip"].rsplit(".", 1)[0] + ".255", self.info["ip"]):
                    send_magic_packet(mac, ip_address=addr)
        elif not on and self._on:
            await self.press("power")


DRIVERS = {"androidtv": AndroidTV, "samsung": SamsungTV}


# --------------------------------------------------------------- découverte
async def discover():
    base = local_ip().rsplit(".", 1)[0]
    ips = [f"{base}.{i}" for i in range(1, 255)]
    found = []

    async def check(ip):
        if await port_open(ip, 6466):  # Android TV (Swisscom, Salt, Google TV...)
            name, mac = f"Android TV {ip}", None
            try:
                r = AndroidTVRemote("ATLAS", str(DATA / "atv_cert.pem"), str(DATA / "atv_key.pem"), ip)
                await r.async_generate_cert_if_missing()
                name, mac = await asyncio.wait_for(r.async_get_name_and_mac(), 5)
            except Exception:
                pass
            found.append({"kind": "androidtv", "ip": ip, "name": name, "mac": mac})
        elif await port_open(ip, 8001):  # Samsung Tizen
            try:
                d = (await asyncio.to_thread(http_json, f"http://{ip}:8001/api/v2/"))["device"]
                found.append({"kind": "samsung", "ip": ip, "name": d.get("name", "Samsung TV"),
                              "mac": d.get("wifiMac"), "model": d.get("modelName")})
            except Exception:
                pass

    sem = asyncio.Semaphore(64)
    async def guarded(ip):
        async with sem:
            await check(ip)
    await asyncio.gather(*(guarded(ip) for ip in ips))
    for d in found:
        d["id"] = f"{d['kind']}-{(d.get('mac') or d['ip']).replace(':', '').lower()}"
    return found


# --------------------------------------------------------------- agent
class Agent:
    def __init__(self):
        self.conf = json.loads(CONF.read_text())
        self.base = f"atlas/{self.conf['device_id']}"
        self.state_file = DATA / "state.json"
        self.st = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.st.setdefault("devices", {}); self.st.setdefault("active", None)
        self.st.setdefault("settings", {"fav_channel": 1, "volume_steps": 12, "favorites": []})
        self.st.setdefault("last_channel", None)
        self.tv = None
        self.cec = CEC(); self.display = Display(); self.wifi = WiFi()
        self.hdmi = {"cec": False, "phys": None, "devices": []}
        self.wifi_nets = []; self.setup_mode = False; self.portal = None
        self.slide_until = 0
        self.photos_dir = DATA / "photos"
        self.st.setdefault("slideshow", {"seconds": 8, "shuffle": True, "minutes": 30})
        self.mq_ok = False
        self.net_info = {}
        self.screen = None
        self.watch_until = 0
        self._shot_task = None
        self.pairing = None
        self.discovered = []
        self.queue = asyncio.Queue()
        self.loop = asyncio.get_running_loop()
        self._dirty = asyncio.Event()

    def save(self):
        self.state_file.write_text(json.dumps(self.st, indent=1))

    # ---- MQTT
    def mqtt_start(self):
        c = self.conf
        cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"agent-{c['device_id']}")
        cl.username_pw_set(c["mqtt_user"], c["mqtt_pass"])
        if c.get("mqtt_tls", True):
            cl.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        cl.will_set(f"{self.base}/online", "0", qos=1, retain=True)
        cl.reconnect_delay_set(2, 10)  # se reconnecte vite après un changement de réseau
        cl.on_connect = self._on_connect
        cl.on_disconnect = lambda *_a: setattr(self, "mq_ok", False)
        cl.on_message = lambda _c, _u, m: self.loop.call_soon_threadsafe(self.queue.put_nowait, m.payload)
        cl.connect_async(c["mqtt_host"], int(c.get("mqtt_port", 8883)), keepalive=30)
        cl.loop_start()
        self.mq = cl

    def _on_connect(self, cl, _u, _f, rc, _p):
        log.info("MQTT connecté (%s)", rc)
        self.mq_ok = not rc.is_failure
        cl.subscribe(f"{self.base}/cmd", qos=1)
        cl.publish(f"{self.base}/online", "1", qos=1, retain=True)
        self.loop.call_soon_threadsafe(self._dirty.set)

    def event(self, **kw):
        self.mq.publish(f"{self.base}/event", json.dumps(kw), qos=1)

    def changed(self):
        self.loop.call_soon_threadsafe(self._dirty.set)

    async def state_publisher(self):
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), 30)
            except asyncio.TimeoutError:
                pass
            self._dirty.clear()
            if isinstance(self.tv, SamsungTV):
                await self.tv.refresh()
            st = {"agent": VERSION, "ts": int(time.time()), "devices": list(self.st["devices"].values()),
                  "active": self.st["active"], "settings": self.st["settings"],
                  "last_channel": self.st["last_channel"], "tv": self.tv.status() if self.tv else None,
                  "screen": {"supported": bool(self.tv and self.tv.kind == "androidtv"), "enabled": bool(self.st.get("screen"))},
                  "hdmi": {"cec": self.hdmi["cec"], "phys": self.hdmi["phys"], "box": self.st.get("box_phys"),
                           "devices": self.hdmi["devices"], "showing": self.display.what if self.display.running() else None},
                  "slideshow": {**self.st["slideshow"], "running": self.display.what == "slideshow" and self.display.running(),
                                "until": int(self.slide_until)},
                  "net": self.net_info,
                  "pairing": self.pairing and {"id": self.pairing["info"]["id"], "step": self.pairing["step"]}}
            self.mq.publish(f"{self.base}/state", json.dumps(st), qos=1, retain=True)

    # ---- TV
    async def activate(self, dev_id):
        info = self.st["devices"][dev_id]
        drv = DRIVERS[info["kind"]](info, self.changed)
        try:
            await drv.connect()
        except (CannotConnect, ConnectionClosed, OSError) as e:
            log.warning("TV injoignable (%s), nouvelle recherche...", e)
            for d in await discover():  # l'adresse IP a peut-être changé
                if d["id"] == dev_id:
                    info["ip"] = d["ip"]; self.save()
                    drv = DRIVERS[info["kind"]](info, self.changed)
                    await drv.connect()
                    break
        drv.notify = lambda **kw: self.event(**kw)
        self.tv = drv
        self.st["active"] = dev_id; self.save(); self.changed()

    async def wait_on(self, timeout=30):
        for _ in range(timeout):
            if isinstance(self.tv, SamsungTV):
                await self.tv.refresh()
            if self.tv.is_on:
                return True
            await asyncio.sleep(1)
        return False

    async def channel(self, n):
        for ch in str(int(n)):
            await self.tv.press(ch); await asyncio.sleep(0.35)
        if self.tv.kind == "samsung":
            await asyncio.sleep(0.3); await self.tv.press("ok")
        self.st["last_channel"] = int(n); self.save(); self.changed()

    async def repair(self):
        s = self.st["settings"]
        self.event(type="repair", step="start")
        if self.display.running() or (self.cec.available() and self.st.get("box_phys")):
            await self.tv_release()  # remet la TV sur l'entrée de la box (si photos ou mauvaise entrée HDMI)
        await self.tv.power(True)
        if not await self.wait_on():
            self.event(type="repair", step="error", message="La TV ne s'allume pas"); return
        await asyncio.sleep(8)
        for k in REPAIR_EXIT[self.tv.kind]:
            await self.tv.press(k); await asyncio.sleep(1.2)
        await self.channel(s.get("fav_channel", 1))
        await asyncio.sleep(2)
        level = int(s.get("volume", s.get("volume_steps", 15)))
        if not await self.tv.set_volume(level):  # sinon : on descend puis on remonte (volume exact inconnu)
            for _ in range(25):
                await self.tv.press("vol_down"); await asyncio.sleep(0.12)
            for _ in range(level):
                await self.tv.press("vol_up"); await asyncio.sleep(0.15)
        self.event(type="repair", step="done")

    # ---- commandes
    async def handle(self, raw):
        try:
            cmd = json.loads(raw)
        except Exception:
            return
        if cmd.get("k") != self.conf["device_key"]:
            log.warning("commande refusée (clé invalide)"); return
        a = cmd.get("action"); cid = cmd.get("cid")
        try:
            if a == "scan":
                self.event(type="scan", step="start")
                self.discovered = await discover()
                self.event(type="scan", step="done", devices=self.discovered)
            elif a == "pair_start":
                info = next(d for d in self.discovered if d["id"] == cmd["device"])
                drv = DRIVERS[info["kind"]](info, self.changed)
                self.pairing = {"info": info, "drv": drv, "step": "starting"}; self.changed()
                already = info["kind"] == "samsung" and drv.has_token()
                self.event(type="pairing", step="starting" if already or info["kind"] != "samsung" else "accept_on_tv")
                res = await drv.start_pairing()
                if res == "code":
                    self.pairing["step"] = "code_needed"; self.event(type="pairing", step="code_needed")
                else:
                    await self._paired(already=already or info["kind"] == "androidtv")
            elif a == "pair_finish":
                await self.pairing["drv"].finish_pairing(cmd["code"])
                await self._paired()
            elif a == "forget":
                self.st["devices"].pop(cmd.get("device") or self.st["active"], None)
                self.st["active"] = None; self.tv = None; self.save(); self.changed()
            elif a == "_wifi_join":
                await self.wifi_join(cmd["ssid"], cmd.get("pw", ""))
            elif a == "wifi_setup":
                self.event(type="ack", cid=cid, ok=True)
                await asyncio.sleep(2); await self.setup_start(); return
            elif a == "show_qr":
                await self.show_pair_qr(int(cmd.get("minutes", 5)))
            elif a == "photo_add":
                await self.photo_add(cmd["data"])
            elif a == "photo_del":
                self.photo_del(cmd["id"])
            elif a == "slideshow_start":
                await self.slideshow_start(cmd)
            elif a in ("slideshow_stop", "tv_release"):
                await self.tv_release()
            elif a == "hdmi_refresh":
                await self.hdmi_refresh()
                self.event(type="hdmi", raw=self.hdmi.get("raw", ""))
            elif a == "set_box_input":
                self.st["box_phys"] = cmd.get("phys") or None; self.save()
            elif a == "update":
                await self.update()
            elif a == "screen_enable":
                await self.screen_enable()
            elif a == "screen":
                self.watch_until = time.time() + int(cmd.get("watch", 0))
                await self.screen_capture()
            elif a == "settings":
                self.st["settings"].update({k: v for k, v in cmd.items() if k in ("fav_channel", "volume_steps", "volume", "favorites")})
                self.save(); self.changed()
            elif not self.tv:
                raise RuntimeError("Aucune TV associée")
            elif a == "key":
                await self.tv.press(cmd["key"]); self.screen_after_key()
            elif a == "channel":
                await self.channel(cmd["number"]); self.screen_after_key()
            elif a == "power":
                await self.tv.power(bool(cmd.get("on")))
            elif a == "repair":
                await self.repair(); self.screen_after_key()
            self.event(type="ack", cid=cid, ok=True)
        except InvalidAuth:
            self.event(type="ack", cid=cid, ok=False, message="Code refusé, réessayez")
            if self.pairing:
                self.pairing["step"] = "code_needed"; self.event(type="pairing", step="code_needed", error="Code refusé")
        except Exception as e:
            log.exception("erreur commande %s", a)
            self.event(type="ack", cid=cid, ok=False, message=str(e) or e.__class__.__name__)
            if a in ("pair_start", "pair_finish"):
                self.pairing = None; self.event(type="pairing", step="error", message=str(e))
        self.changed()

    async def _paired(self, already=False):
        info = self.pairing["info"]
        self.st["devices"][info["id"]] = info; self.save()
        self.pairing = None
        await self.activate(info["id"])
        self.event(type="pairing", step="done", device=info, already=already)

    # ---- vue écran
    def screen_obj(self):
        if not self.tv or self.tv.kind != "androidtv":
            return None
        if not self.screen or self.screen.ip != self.tv.info["ip"]:
            self.screen = Screen(self.tv.info["ip"])
        return self.screen

    async def screen_enable(self):
        sc = self.screen_obj()
        if not sc:
            raise RuntimeError("Vue écran disponible seulement sur les box Android (Swisscom, Salt…)")
        if not sc.available():
            await asyncio.to_thread(ensure_deps)
        self.event(type="screen", step="connecting")
        st = await asyncio.to_thread(sc.connect)
        for _ in range(45):  # l'utilisateur doit accepter "Autoriser le débogage" sur la TV
            if st == "device":
                break
            if st == "unauthorized":
                self.event(type="screen", step="authorize")
            await asyncio.sleep(2)
            st = await asyncio.to_thread(sc.state)
            if st in ("absent", "offline"):
                st = await asyncio.to_thread(sc.connect)
        if st != "device":
            self.st["screen"] = False; self.save()
            self.event(type="screen", step="error", message="Débogage ADB non activé ou refusé sur la box"); return
        self.st["screen"] = True; self.save(); self.changed()
        self.event(type="screen", step="ok")
        await self.screen_capture()

    async def screen_capture(self):
        sc = self.screen_obj()
        if not sc or not self.st.get("screen"):
            return
        try:
            if await asyncio.to_thread(sc.state) != "device":
                await asyncio.to_thread(sc.connect)
            img, dark = await asyncio.to_thread(sc.capture)
            try:
                ui = await asyncio.to_thread(sc.ui_text)
            except Exception:
                ui = {"focus": None, "texts": []}
            self.mq.publish(f"{self.base}/screen", json.dumps({"ts": int(time.time()), "img": img, "dark": dark,
                            "app": self.tv.status().get("app"), **ui}), qos=1, retain=True)
        except Exception as e:
            log.warning("capture écran: %s", e)
            self.event(type="screen", step="error", message=f"Capture impossible ({e})")

    def screen_after_key(self):
        if self.st.get("screen") and time.time() < self.watch_until:
            if self._shot_task and not self._shot_task.done():
                self._shot_task.cancel()
            async def later():
                await asyncio.sleep(1.3); await self.screen_capture()
            self._shot_task = asyncio.create_task(later())

    # ---- HDMI / CEC
    async def hdmi_refresh(self):
        if not self.cec.available():
            self.hdmi["cec"] = False; return
        await asyncio.to_thread(self.cec.init)
        me, devs, raw = await asyncio.to_thread(self.cec.info)
        others = [d for d in devs if d["phys"] not in (me, "0.0.0.0") and d["phys"] != "f.f.f.f"]
        self.hdmi.update(cec=True, phys=me, devices=others, raw=raw)
        if not self.st.get("box_phys") and len(others) == 1:
            self.st["box_phys"] = others[0]["phys"]; self.save()
        self.changed()

    async def tv_show(self, path_or_none=None, what="screen"):
        if self.cec.available() and not self.hdmi["phys"]:
            await self.hdmi_refresh()
        if self.cec.available():
            await asyncio.to_thread(self.cec.show_me, self.hdmi["phys"])
        if path_or_none:
            self.display.image(path_or_none, what)

    async def tv_release(self):
        self.display.stop(); self.slide_until = 0
        if self.cec.available():
            await asyncio.to_thread(self.cec.give_back, self.hdmi["phys"], self.st.get("box_phys"))
        self.changed()

    def app_link(self):
        c = self.conf
        q = urllib.parse.urlencode({"h": c["mqtt_host"], "port": c.get("ws_port", 8084), "u": c["mqtt_user"],
                                    "p": c["mqtt_pass"], "d": c["device_id"], "k": c["device_key"]})
        return c["app_url"].split("#")[0] + "#" + q

    async def show_pair_qr(self, minutes=5):
        p = await asyncio.to_thread(render_screen, "Installez la télécommande ATLAS",
                                    ["1. Ouvrez l'appareil photo de votre téléphone", "2. Visez le QR code à droite",
                                     "3. Touchez le lien qui apparaît", "", "Cet écran se fermera tout seul."],
                                    [("Télécommande ATLAS", self.app_link())])
        await self.tv_show(p, "pair_qr")
        async def close():
            await asyncio.sleep(minutes * 60)
            if self.display.what == "pair_qr":
                await self.tv_release()
        asyncio.create_task(close())

    # ---- photos / diaporama
    def photos(self):
        self.photos_dir.mkdir(parents=True, exist_ok=True)
        return sorted(self.photos_dir.glob("*.jpg"))

    def publish_photos(self):
        items = []
        for f in self.photos():
            t = f.with_suffix(".thumb")
            items.append({"id": f.stem, "thumb": t.read_text() if t.exists() else ""})
        self.mq.publish(f"{self.base}/photos", json.dumps({"items": items}), qos=1, retain=True)

    async def photo_add(self, data):
        from PIL import Image, ImageOps
        raw = base64.b64decode(data)
        def work():
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
            im.thumbnail((1920, 1080))
            pid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
            f = self.photos_dir / f"{pid}.jpg"; self.photos_dir.mkdir(parents=True, exist_ok=True)
            im.save(f, "JPEG", quality=88)
            th = im.copy(); th.thumbnail((160, 160)); b = io.BytesIO(); th.save(b, "JPEG", quality=55)
            f.with_suffix(".thumb").write_text(base64.b64encode(b.getvalue()).decode())
        if len(self.photos()) >= 100:
            raise RuntimeError("Album plein (100 photos max) : supprimez-en quelques-unes")
        await asyncio.to_thread(work)
        self.publish_photos()

    def photo_del(self, pid):
        for ext in (".jpg", ".thumb"):
            (self.photos_dir / f"{Path(pid).name}{ext}").unlink(missing_ok=True)
        self.publish_photos()

    async def slideshow_start(self, opts):
        s = self.st["slideshow"]; s.update({k: opts[k] for k in ("seconds", "shuffle", "minutes") if k in opts}); self.save()
        files = self.photos()
        if not files:
            raise RuntimeError("Ajoutez d'abord des photos")
        await self.tv_show()
        self.display.slideshow(files, int(s["seconds"]), bool(s["shuffle"]))
        mins = int(s.get("minutes") or 0)
        self.slide_until = time.time() + mins * 60 if mins else 0
        self.changed()

    async def slideshow_guard(self):
        while True:
            await asyncio.sleep(15)
            if self.display.what == "slideshow" and self.slide_until and time.time() > self.slide_until:
                log.info("fin du diaporama (durée atteinte)")
                await self.tv_release()
                self.event(type="slideshow", step="ended")

    # ---- réseau / Wi-Fi
    async def net_watch(self):
        await asyncio.sleep(50)  # laisse le temps au câble / Wi-Fi connu de se connecter
        while True:
          try:
            if not self.mq_ok and not self.setup_mode:
                cur = await asyncio.to_thread(self.wifi.current)
                if not cur:
                    log.warning("pas d'Internet : passage en mode installation Wi-Fi")
                    await self.setup_start()
            if self.mq_ok:
                self.net_info = await asyncio.to_thread(self.wifi.current)
          except FileNotFoundError:
            return  # pas de NetworkManager
          except Exception as e:
            log.warning("surveillance réseau: %s", e)
          await asyncio.sleep(30)

    async def setup_start(self, reason=""):
        self.setup_mode = True
        self.wifi_nets = await asyncio.to_thread(self.wifi.scan)
        ssid = "ATLAS-Setup-" + self.conf["device_id"][-4:]
        pw = self.st.setdefault("setup_pw", "".join(random.choice("23456789") for _ in range(8))); self.save()
        await asyncio.to_thread(self.wifi.start_hotspot, ssid, pw)
        if not self.portal:
            Portal.agent = self
            self.portal = ThreadingHTTPServer(("0.0.0.0", 80), Portal)
            threading.Thread(target=self.portal.serve_forever, daemon=True).start()
        lines = ["1. Scannez le QR code ① : votre téléphone rejoint le Wi-Fi ATLAS.",
                 f"Ou : Réglages Wi-Fi → {ssid} (mot de passe {pw}).", "",
                 "2. Scannez le QR code ② : choisissez le Wi-Fi de la maison et tapez son mot de passe."]
        if reason:
            lines = [f"⚠ {reason}", ""] + lines
        p = await asyncio.to_thread(render_screen, "Connexion au Wi-Fi", lines,
                                    [("① Wi-Fi ATLAS", f"WIFI:T:WPA;S:{ssid};P:{pw};;"), ("② Choisir le Wi-Fi", "http://10.42.0.1")],
                                    (200, 140, 30))
        await self.tv_show(p, "setup")

    async def wifi_join(self, ssid, pw):
        p = await asyncio.to_thread(render_screen, "Connexion en cours…", [f"Réseau : {ssid}", "Merci de patienter (30 s environ)."])
        self.display.image(p, "setup")
        ok, out = await asyncio.to_thread(self.wifi.connect, ssid, pw)
        log.info("Wi-Fi %s : %s", ssid, out[-200:])
        if ok:
            p = await asyncio.to_thread(render_screen, "Wi-Fi connecté", [f"Réseau : {ssid}", "Connexion au serveur ATLAS…"])
            self.display.image(p, "setup")
            for _ in range(45):
                if self.mq_ok:
                    break
                await asyncio.sleep(2)
        if ok and not self.mq_ok:
            self.setup_mode = False  # le Wi-Fi est bon : on le garde, Internet reviendra
            p = await asyncio.to_thread(render_screen, "Wi-Fi connecté, Internet absent",
                                        [f"Réseau : {ssid}", "La box internet n'a pas l'air connectée à Internet.",
                                         "ATLAS réessaiera tout seul."], (), (200, 140, 30))
            self.display.image(p, "setup"); await asyncio.sleep(30); await self.tv_release()
            return
        if ok and self.mq_ok:
            self.setup_mode = False
            if self.portal:
                self.portal.shutdown(); self.portal.server_close(); self.portal = None
            log.info("Wi-Fi %s connecté", ssid)
            if not self.st["devices"]:
                await self.show_pair_qr()
            else:
                p = await asyncio.to_thread(render_screen, "✓ Connecté !", [f"Wi-Fi : {ssid}", "ATLAS est prêt."], (), (30, 142, 90))
                self.display.image(p, "setup"); await asyncio.sleep(8); await self.tv_release()
        else:
            log.warning("échec Wi-Fi %s : %s", ssid, out)
            await self.setup_start(wifi_error(out))

    # ---- mise à jour à distance
    def update_url(self):
        if self.conf.get("update_url"):
            return self.conf["update_url"]
        u = urllib.parse.urlparse(self.conf["app_url"])
        owner = u.netloc.split(".")[0]; repo = u.path.strip("/").split("/")[0]
        return f"https://raw.githubusercontent.com/{owner}/{repo}/main/agent.py"

    async def update(self):
        url = self.update_url() + f"?t={int(time.time())}"
        self.event(type="update", step="download", url=url.split("?")[0])
        code = await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=30).read())
        m = re.search(rb'VERSION = "([^"]+)"', code)
        if not m or b"class Agent" not in code:
            raise RuntimeError("fichier agent.py invalide")
        new = AGENT_FILE.with_suffix(".new.py"); new.write_bytes(code)
        chk = subprocess.run([sys.executable, "-m", "py_compile", str(new)], capture_output=True)
        if chk.returncode:
            new.unlink(); raise RuntimeError("erreur dans le nouveau code, mise à jour annulée")
        shutil.copy2(AGENT_FILE, AGENT_FILE.with_suffix(".prev.py"))
        os.replace(new, AGENT_FILE)
        self.event(type="update", step="restart", version=m.group(1).decode())
        await asyncio.sleep(1.5)
        os._exit(0)  # systemd relance l'agent avec la nouvelle version

    async def run(self):
        DATA.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HOME", "/root")
        asyncio.get_running_loop().run_in_executor(None, ensure_deps)
        self.mqtt_start()
        asyncio.create_task(self.state_publisher())
        asyncio.create_task(self.slideshow_guard())
        asyncio.create_task(self.net_watch())
        async def hdmi_boot():
            await asyncio.sleep(20)
            try:
                await self.hdmi_refresh()
            except Exception as e:
                log.warning("HDMI/CEC: %s", e)
            await asyncio.sleep(25)
            if self.mq_ok:
                self.publish_photos()
        asyncio.create_task(hdmi_boot())
        if self.st["active"] in self.st["devices"]:
            try:
                await self.activate(self.st["active"])
            except Exception as e:
                log.warning("TV non disponible au démarrage: %s", e)
        log.info("ATLAS %s prêt - %s", VERSION, self.conf["device_id"])
        while True:
            await self.handle(await self.queue.get())


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await Agent().run()

if __name__ == "__main__":
    asyncio.run(main())
