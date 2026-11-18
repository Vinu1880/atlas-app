#!/usr/bin/env python3
"""ATLAS TV - agent v0.2 (prototype)
Se connecte tout seul au serveur MQTT, trouve la TV sur le réseau local,
gère l'appairage et exécute les commandes envoyées par l'app.
"""
import asyncio, base64, io, json, logging, os, re, shutil, socket, ssl, subprocess, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import paho.mqtt.client as mqtt
from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth
from samsungtvws import SamsungTVWS
from wakeonlan import send_magic_packet

VERSION = "0.2"
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
        if not shutil.which("adb"):
            subprocess.run(["apt-get", "install", "-y", "-q", "adb"], timeout=600,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            import PIL  # noqa
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pillow"], timeout=600)
    except Exception as e:
        log.warning("installation des dépendances: %s", e)


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


class SamsungTV:
    kind = "samsung"

    def __init__(self, info, on_change):
        self.info = info
        self.on_change = on_change
        self.tv = SamsungTVWS(host=info["ip"], port=8002, token_file=str(DATA / f"samsung_{info['mac'] or info['ip']}.token"),
                              name="ATLAS", timeout=8)
        self._on = None

    @property
    def is_on(self):
        return self._on

    def status(self):
        return {"is_on": self._on, "app": "Samsung TV", "volume": None, "reachable": self._on is not None}

    async def refresh(self):
        def q():
            try:
                d = http_json(f"http://{self.info['ip']}:8001/api/v2/", 2)["device"]
                return d.get("PowerState", "on") == "on"
            except Exception:
                return False
        new = await asyncio.to_thread(q)
        if new != self._on:
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
                self.tv.send_key(k)
            except Exception:
                self.tv.close(); self.tv.send_key(k)
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
        cl.reconnect_delay_set(2, 60)
        cl.on_connect = self._on_connect
        cl.on_message = lambda _c, _u, m: self.loop.call_soon_threadsafe(self.queue.put_nowait, m.payload)
        cl.connect_async(c["mqtt_host"], int(c.get("mqtt_port", 8883)), keepalive=30)
        cl.loop_start()
        self.mq = cl

    def _on_connect(self, cl, _u, _f, rc, _p):
        log.info("MQTT connecté (%s)", rc)
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
        await self.tv.power(True)
        if not await self.wait_on():
            self.event(type="repair", step="error", message="La TV ne s'allume pas"); return
        await asyncio.sleep(8)
        for k in REPAIR_EXIT[self.tv.kind]:
            await self.tv.press(k); await asyncio.sleep(1.2)
        await self.channel(s.get("fav_channel", 1))
        await asyncio.sleep(2)
        for _ in range(25):
            await self.tv.press("vol_down"); await asyncio.sleep(0.12)
        for _ in range(int(s.get("volume_steps", 12))):
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
            elif a == "update":
                await self.update()
            elif a == "screen_enable":
                await self.screen_enable()
            elif a == "screen":
                self.watch_until = time.time() + int(cmd.get("watch", 0))
                await self.screen_capture()
            elif a == "settings":
                self.st["settings"].update({k: v for k, v in cmd.items() if k in ("fav_channel", "volume_steps", "favorites")})
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
