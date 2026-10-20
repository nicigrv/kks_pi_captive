#!/usr/bin/env python3
"""
wifi-portal – WLAN-Fallback mit Captive Portal für den Raspberry Pi.

Hat der Pi länger als WP_GRACE Sekunden kein Netzwerk, öffnet er einen
Hotspot. Jedes Handy, das sich verbindet, bekommt automatisch ein Portal,
über das sich der Pi mit einem WLAN verbinden lässt.

Parallelbetrieb (z. B. Pi 4): Der Hotspot läuft auf der virtuellen
Schnittstelle uap0, die Verbindung zum WLAN auf wlan0. Dadurch bleibt der
Hotspot beim Verbinden an, die WLAN-Liste bleibt aktuell und das Portal
zeigt am Ende IP-Adresse, Hostname und WLAN an. Klappt der Parallelbetrieb
nicht, wird automatisch auf den Einzelbetrieb (Hotspot auf wlan0) gewechselt.

Benötigt: NetworkManager (Standard ab Raspberry Pi OS Bookworm), dnsmasq-base, iw.
Nur Python-Standardbibliothek.
"""
import html
import ipaddress
import json
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# --- Einstellungen (überschreibbar über /etc/default/wifi-portal) -----------
def load_conf(path="/etc/default/wifi-portal"):
    """Liest KEY='Wert'-Zeilen wie die Shell (Quotes, Sonderzeichen bleiben erhalten)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                try:
                    parts = shlex.split(val, comments=True)
                except ValueError:
                    continue
                os.environ.setdefault(key.strip(), parts[0] if parts else "")
    except OSError:
        pass


load_conf()
IFACE = os.environ.get("WP_IFACE", "wlan0")
AP_VIF = os.environ.get("WP_AP_IFACE", "uap0")
AP_SSID = os.environ.get("WP_AP_SSID", "Pi-Setup")
AP_PASSWORD = os.environ.get("WP_AP_PASSWORD", "")
AP_IP = os.environ.get("WP_AP_IP", "10.42.0.1")
AP_NET = str(ipaddress.ip_network(f"{AP_IP}/24", strict=False))
# Interner Port des Portals. Anfragen an Port 80 aus dem Hotspot-Netz werden per
# nftables hierher umgeleitet – so stört ein anderer Webserver (z. B. Traefik/Docker) nicht.
PORTAL_PORT = int(os.environ.get("WP_PORT", "8099"))
GRACE = int(os.environ.get("WP_GRACE", "60"))
RETRY = int(os.environ.get("WP_RETRY", "300"))
HOLD = int(os.environ.get("WP_HOLD", "120"))
SCAN_INTERVAL = int(os.environ.get("WP_SCAN_INTERVAL", "90"))
CONCURRENT = os.environ.get("WP_CONCURRENT", "auto").strip().lower() not in ("off", "no", "0", "false", "aus")
ONLINE_TYPES = [t.strip() for t in os.environ.get("WP_ONLINE_TYPES", "wifi,ethernet").split(",") if t.strip()]

AP_CON = "wifi-portal-ap"   # Name der Hotspot-Verbindung in NetworkManager
NFT_TABLE = "wifi_portal"   # eigene nftables-Tabelle für die Port-Umleitung
CON_PREFIX = "wlan-"        # Präfix für über das Portal angelegte WLANs
HOSTNAME = socket.gethostname()
AP_5G_CHANNELS = (36, 40, 44, 48)   # 5-GHz-Kanäle ohne Radarerkennung (für AP erlaubt)

state = {
    "networks": [],        # [(ssid, (signal, security, channel))]
    "message": "",         # Fehlermeldung fürs Portal
    "ap": False,           # Hotspot aktiv?
    "ap_iface": None,      # uap0 (Parallelbetrieb) oder wlan0 (Einzelbetrieb)
    "ap_channel": None,    # (kanal, band)
    "concurrent": CONCURRENT,
    "job": None,           # {"ssid", "status": connecting|connected|legacy, "ip", "until"}
    "online_since": None,
}
connect_requests = queue.Queue()
ev_done = threading.Event()
ev_rescan = threading.Event()
httpd = None


def log(*args):
    print(*args, flush=True)


# --- Hilfsfunktionen ---------------------------------------------------------
def run(args, timeout=60, stdin=None):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=stdin)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, "", "timeout")
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 1, "", f"{args[0]} nicht gefunden")


def nmcli(*args, timeout=60):
    return run(["nmcli", *args], timeout=timeout)


def terse(line):
    """Zerlegt eine `nmcli -t`-Zeile (Doppelpunkt-getrennt, \\: escaped)."""
    fields, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            fields.append(cur)
            cur = ""
        else:
            cur += ch
    fields.append(cur)
    return fields


def devices():
    out = nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device").stdout
    return [f for f in map(terse, out.splitlines()) if len(f) >= 4]


# Virtuelle Schnittstellen (Docker, Bridges, VPN …) zählen nicht als Netzwerkverbindung
VIRTUAL_PREFIXES = ("veth", "docker", "br-", "virbr", "vnet", "tun", "tap", "wg", "lo")


def online():
    """True, wenn eine echte WLAN- oder LAN-Verbindung besteht.

    Gezählt wird nur der Zustand "connected" von NetworkManager selbst verwalteten
    Geräten – nicht "connected (externally)" (z. B. Docker-veth-Schnittstellen).
    """
    for d, t, s, c, *_ in devices():
        if t not in ONLINE_TYPES or c == AP_CON or d == AP_VIF:
            continue
        if s.strip() != "connected" or d.startswith(VIRTUAL_PREFIXES):
            continue
        return True
    return False


def ap_running():
    return any(c == AP_CON and s.startswith("connected") for d, t, s, c, *_ in devices())


def wait_online(seconds):
    end = time.time() + seconds
    while time.time() < end:
        if online():
            return True
        time.sleep(2)
    return False


def saved_wifi():
    out = nmcli("-t", "-f", "NAME,TYPE", "connection", "show").stdout
    return [f[0] for f in map(terse, out.splitlines())
            if len(f) >= 2 and f[1] == "802-11-wireless" and f[0] != AP_CON]


def has_clients(iface):
    return "Station" in run(["iw", "dev", iface, "station", "dump"]).stdout


def sta_info():
    """(SSID, IP) der Client-Verbindung auf wlan0."""
    ip = nmcli("-g", "IP4.ADDRESS", "device", "show", IFACE).stdout.strip()
    ip = ip.split("|")[0].strip().split("/")[0] if ip else ""
    ssid = ""
    out = nmcli("-t", "-f", "IN-USE,SSID", "device", "wifi", "list", "ifname", IFACE, "--rescan", "no").stdout
    for f in map(terse, out.splitlines()):
        if len(f) >= 2 and f[0] == "*":
            ssid = f[1]
            break
    return ssid, ip


def sta_channel():
    """Aktueller Kanal von wlan0 als (kanal, band) oder None."""
    m = re.search(r"channel (\d+) \((\d+) MHz", run(["iw", "dev", IFACE, "info"]).stdout)
    if not m:
        return None
    return ap_channel_for(int(m.group(1))) or None


def ap_channel_for(chan):
    """Kanal, auf dem der Hotspot laufen muss. None = unbekannt, False = nicht möglich."""
    if not chan:
        return None
    if 1 <= chan <= 14:
        return (chan, "bg")
    if chan in AP_5G_CHANNELS:
        return (chan, "a")
    return False


def parse_ap_sta_support(iw_list):
    """Prüft in `iw list`, ob eine Kombination aus AP und managed (Client) erlaubt ist."""
    if "valid interface combinations" not in iw_list:
        return False
    lines, collecting = [], False
    for line in iw_list.splitlines():
        if "valid interface combinations" in line:
            collecting = True
            continue
        if collecting:
            s = line.strip()
            if s.startswith(("*", "#", "total")):
                lines.append(s)
            elif lines:
                break
    for combo in " ".join(lines).split("*"):
        if re.search(r"\bAP\b", combo) and "managed" in combo:
            return True
    return False


def scan():
    """WLANs über wlan0 scannen (im Einzelbetrieb nur ohne laufenden Hotspot möglich)."""
    if state["ap"] and state["ap_iface"] == IFACE:
        return
    out = nmcli("-t", "-f", "SSID,SIGNAL,SECURITY,CHAN", "device", "wifi", "list",
                "ifname", IFACE, "--rescan", "yes", timeout=45).stdout
    best = {}
    for f in map(terse, out.splitlines()):
        if len(f) < 4 or not f[0] or f[0] == AP_SSID:
            continue
        try:
            sig = int(f[1])
        except ValueError:
            sig = 0
        try:
            chan = int(f[3])
        except ValueError:
            chan = 0
        if f[0] not in best or sig > best[f[0]][0]:
            best[f[0]] = (sig, f[2], chan)
    nets = sorted(best.items(), key=lambda x: -x[1][0])
    if nets:
        state["networks"] = nets
    log(f"Scan: {len(nets)} Netzwerke gefunden")


def lookup_channel(ssid):
    for s, (_sig, _sec, chan) in state["networks"]:
        if s == ssid:
            return chan
    return None


# --- Virtuelle Hotspot-Schnittstelle (Parallelbetrieb) -----------------------
def vif_mac():
    """Eigene MAC-Adresse für uap0, abgeleitet von wlan0 (lokal administriert)."""
    try:
        with open(f"/sys/class/net/{IFACE}/address") as f:
            b = [int(x, 16) for x in f.read().strip().split(":")]
    except (OSError, ValueError):
        return None
    b[0] |= 0x02
    b[5] ^= 0x01
    return ":".join(f"{x:02x}" for x in b)


def create_vif():
    if not os.path.exists(f"/sys/class/net/{AP_VIF}"):
        r = run(["iw", "dev", IFACE, "interface", "add", AP_VIF, "type", "__ap"])
        if r.returncode:
            log(f"{AP_VIF} anlegen fehlgeschlagen:", r.stderr.strip())
            return False
    for _ in range(30):  # warten, bis NetworkManager das Gerät übernommen hat
        for d, t, s, c, *_ in devices():
            if d == AP_VIF:
                if s == "unmanaged":
                    nmcli("device", "set", AP_VIF, "managed", "yes")
                elif s != "unavailable":
                    return True
        time.sleep(0.5)
    log(f"NetworkManager übernimmt {AP_VIF} nicht")
    return False


def delete_vif():
    if os.path.exists(f"/sys/class/net/{AP_VIF}"):
        run(["iw", "dev", AP_VIF, "del"])


# --- Hotspot -----------------------------------------------------------------
def ap_args(iface, channel, band):
    args = ["connection", "add", "type", "wifi", "ifname", iface, "con-name", AP_CON,
            "ssid", AP_SSID, "connection.autoconnect", "no",
            "802-11-wireless.mode", "ap", "802-11-wireless.band", band,
            "802-11-wireless.channel", str(channel),
            "ipv4.method", "shared", "ipv4.addresses", f"{AP_IP}/24",
            "ipv6.method", "ignore"]
    if iface == AP_VIF:
        mac = vif_mac()
        if mac:
            args += ["802-11-wireless.cloned-mac-address", mac]
    if len(AP_PASSWORD) >= 8:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", AP_PASSWORD,
                 "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
                 "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable"]
    return args


def bring_up_ap(iface, channel=6, band="bg"):
    """Startet (oder startet neu) die Hotspot-Verbindung auf iface."""
    nmcli("connection", "down", "id", AP_CON)
    nmcli("connection", "delete", "id", AP_CON)
    if iface == AP_VIF and not create_vif():
        delete_vif()
        return False
    r = nmcli(*ap_args(iface, channel, band))
    if r.returncode == 0:
        r = nmcli("--wait", "30", "connection", "up", "id", AP_CON, timeout=40)
    if r.returncode:
        log(f"Hotspot auf {iface} (Kanal {channel}) fehlgeschlagen:", r.stderr.strip())
        nmcli("connection", "delete", "id", AP_CON)
        if iface == AP_VIF:
            delete_vif()
        return False
    if not start_http():
        nmcli("connection", "down", "id", AP_CON)
        if iface == AP_VIF:
            delete_vif()
        return False
    state.update(ap=True, ap_iface=iface, ap_channel=(channel, band))
    mode = "Parallelbetrieb" if iface == AP_VIF else "Einzelbetrieb"
    log(f"Hotspot '{AP_SSID}' aktiv auf {iface}, Kanal {channel} ({mode}) – Portal unter http://{AP_IP}/")
    return True


def start_ap():
    """Frischer Hotspot-Start: Parallelbetrieb versuchen, sonst Einzelbetrieb."""
    while not connect_requests.empty():
        connect_requests.get_nowait()
    ev_done.clear()
    state["job"] = None
    state["online_since"] = None
    scan()  # wlan0 ist hier noch frei
    if state["concurrent"]:
        ch = sta_channel() or (6, "bg")
        if bring_up_ap(AP_VIF, *ch):
            return True
        disable_concurrent("Hotspot auf der virtuellen Schnittstelle ließ sich nicht starten")
    return bring_up_ap(IFACE, 6, "bg")


def stop_ap():
    stop_http()
    nmcli("connection", "down", "id", AP_CON)
    delete_vif()
    state.update(ap=False, ap_iface=None, ap_channel=None, job=None, online_since=None)


def disable_concurrent(reason):
    if state["concurrent"]:
        log(f"Parallelbetrieb deaktiviert ({reason}) – verwende Einzelbetrieb")
    state["concurrent"] = False
    nmcli("connection", "down", "id", AP_CON)
    delete_vif()


# --- Webserver ---------------------------------------------------------------
class PortalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # IP_FREEBIND: Server bleibt gebunden, auch wenn die Hotspot-IP beim
        # Kanalwechsel kurz verschwindet.
        try:
            self.socket.setsockopt(socket.SOL_IP, getattr(socket, "IP_FREEBIND", 15), 1)
        except OSError:
            pass
        super().server_bind()


IPT_RULE = ["PREROUTING", "-s", AP_NET, "-p", "tcp", "--dport", "80",
            "-m", "comment", "--comment", "wifi-portal",
            "-j", "REDIRECT", "--to-ports", str(PORTAL_PORT)]


def redirect_on():
    """Port 80 aus dem Hotspot-Netz auf den Portal-Port umleiten (vor Docker-Regeln)."""
    redirect_off()
    if shutil.which("nft"):
        rules = (f"table ip {NFT_TABLE} {{\n"
                 f"  chain prerouting {{\n"
                 f"    type nat hook prerouting priority -110; policy accept;\n"
                 f"    ip saddr {AP_NET} tcp dport 80 redirect to :{PORTAL_PORT}\n"
                 f"  }}\n}}\n")
        r = run(["nft", "-f", "-"], stdin=rules)
        if r.returncode == 0:
            return True
        log("nftables-Umleitung fehlgeschlagen:", r.stderr.strip())
    if shutil.which("iptables"):
        r = run(["iptables", "-t", "nat", "-I", *IPT_RULE[:1], "1", *IPT_RULE[1:]])
        if r.returncode == 0:
            return True
        log("iptables-Umleitung fehlgeschlagen:", r.stderr.strip())
    log("Keine Port-Umleitung möglich – nftables installieren: sudo apt install nftables")
    return False


def redirect_off():
    if shutil.which("nft"):
        run(["nft", "delete", "table", "ip", NFT_TABLE])
    if shutil.which("iptables"):
        for _ in range(5):
            if run(["iptables", "-t", "nat", "-D", *IPT_RULE]).returncode:
                break


def start_http():
    global httpd
    if httpd:
        return True
    for _ in range(10):
        try:
            httpd = PortalServer((AP_IP, PORTAL_PORT), Portal)
            break
        except OSError as e:
            err = e
            time.sleep(1)
    else:
        log(f"Webserver konnte nicht auf Port {PORTAL_PORT} starten: {err}")
        return False
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if not redirect_on():
        stop_http()
        return False
    return True


def stop_http():
    global httpd
    redirect_off()
    if httpd:
        httpd.shutdown()
        httpd.server_close()
        httpd = None


# --- Verbinden ---------------------------------------------------------------
def add_and_activate(ssid, psk, hidden):
    name = CON_PREFIX + ssid
    nmcli("connection", "delete", "id", name)
    args = ["connection", "add", "type", "wifi", "ifname", IFACE, "con-name", name,
            "ssid", ssid, "connection.autoconnect", "yes"]
    if psk:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
    if hidden:
        args += ["802-11-wireless.hidden", "yes"]
    r = nmcli(*args)
    if r.returncode == 0:
        r = nmcli("--wait", "45", "connection", "up", "id", name, timeout=60)
        if r.returncode == 0 and wait_online(15):
            return True
    log(f"Verbindung mit '{ssid}' fehlgeschlagen: {r.stderr.strip()}")
    nmcli("connection", "delete", "id", name)
    return False


def fail_message(ssid):
    return f"Verbindung mit „{ssid}“ fehlgeschlagen. Bitte Passwort prüfen."


def connect_concurrent(ssid, psk, hidden):
    """Hotspot bleibt an, Portal zeigt danach IP, Hostname und WLAN."""
    log(f"Verbinde mit '{ssid}' (Parallelbetrieb) …")
    state["job"] = {"ssid": ssid, "status": "connecting"}
    target = ap_channel_for(lookup_channel(ssid))
    if target is False:
        log(f"'{ssid}' funkt auf einem Kanal, den der Hotspot nicht nutzen darf – Einzelbetrieb für diese Verbindung")
        return connect_legacy(ssid, psk, hidden)
    if target and target != state["ap_channel"]:
        log(f"Hotspot wechselt auf Kanal {target[0]}")
        if not bring_up_ap(AP_VIF, *target):
            log("Kanalwechsel fehlgeschlagen – Einzelbetrieb für diese Verbindung")
            return connect_legacy(ssid, psk, hidden)
        time.sleep(2)

    if add_and_activate(ssid, psk, hidden):
        _ssid, ip = sta_info()
        state["message"] = ""
        state["job"] = {"ssid": ssid, "status": "connected", "ip": ip, "until": time.time() + HOLD}
        log(f"Verbunden mit '{ssid}', IP {ip} – Hotspot bleibt noch {HOLD}s an")
        if not ap_running():  # Kanal hat sich doch geändert -> Hotspot nachziehen
            bring_up_ap(AP_VIF, *(sta_channel() or (6, "bg")))
    else:
        state["message"] = fail_message(ssid)
        state["job"] = None


def connect_legacy(ssid, psk, hidden):
    """Einzelbetrieb: Hotspot aus, verbinden, bei Fehler Hotspot wieder an."""
    state["job"] = {"ssid": ssid, "status": "legacy"}
    time.sleep(3)  # Antwortseite noch ans Handy ausliefern
    log(f"Verbinde mit '{ssid}' (Einzelbetrieb) …")
    stop_ap()
    scan()
    if add_and_activate(ssid, psk, hidden):
        log(f"Verbunden mit '{ssid}'")
        state["message"] = ""
        return
    msg = fail_message(ssid)
    start_ap()
    state["message"] = msg


# --- Captive Portal ----------------------------------------------------------
HEAD = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WLAN einrichten</title><style>
body{font-family:system-ui,-apple-system,sans-serif;background:#f2f4f7;margin:0;padding:24px;color:#1d2433}
.card{max-width:420px;margin:auto;background:#fff;border-radius:14px;padding:24px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
h1{font-size:1.4em;margin:0 0 6px}p{color:#5b6475;line-height:1.45}
label{display:block;margin:16px 0 6px;font-weight:600}
select,input{width:100%;box-sizing:border-box;padding:12px;font-size:16px;border:1px solid #cfd5df;border-radius:10px;background:#fff}
button{width:100%;margin-top:22px;padding:14px;font-size:16px;border:0;border-radius:10px;background:#2563eb;color:#fff;font-weight:600}
button.sec{background:#e8edf5;color:#1d2433;margin-top:10px}
.err{background:#fde8e8;color:#9b1c1c;padding:10px 12px;border-radius:8px}
.ok{background:#e7f8ef;border-radius:10px;padding:6px 14px;margin:14px 0}
.ok p{color:#14532d;margin:8px 0}.big{font-size:1.35em;font-weight:700;color:#14532d}
.info{background:#eef4ff;color:#1e3a8a;padding:10px 12px;border-radius:8px;font-size:.92em}
.hint{font-size:.85em;font-weight:400;color:#5b6475}
.spin{display:inline-block;width:16px;height:16px;border:3px solid #cfd5df;border-top-color:#2563eb;border-radius:50%;animation:s 1s linear infinite;vertical-align:middle;margin-right:8px}
@keyframes s{to{transform:rotate(360deg)}}
</style></head><body><div class="card">"""
FOOT = "</div></body></html>"


def render_form():
    opts = "".join(
        f'<option value="{html.escape(s, quote=True)}">{html.escape(s)} · {sig}%'
        f'{" &#128274;" if sec and sec != "--" else ""}</option>'
        for s, (sig, sec, _chan) in state["networks"])
    msg = f'<p class="err">{html.escape(state["message"])}</p>' if state["message"] else ""
    live = state["ap_iface"] == AP_VIF
    current = ""
    if live and online():
        ssid, ip = sta_info()
        if ip:
            current = (f'<p class="info">Der Pi ist bereits verbunden'
                       f'{" mit <b>" + html.escape(ssid) + "</b>" if ssid else ""} – IP <b>{html.escape(ip)}</b></p>')
    rescan = ('<form method="post" action="/rescan"><button class="sec" type="submit">'
              'Liste aktualisieren</button></form>') if live else ""
    hint = ("Die Liste wird regelmäßig aktualisiert." if live
            else "Die Liste zeigt die Netze vom Start des Hotspots.")
    return HEAD + f"""
<h1>WLAN einrichten</h1>
<p>Wähle das Netzwerk, mit dem sich <b>{html.escape(HOSTNAME)}</b> verbinden soll.</p>{current}{msg}
<form method="post" action="/connect">
<label for="ssid">Netzwerk</label>
<select id="ssid" name="ssid" onchange="document.getElementById('m').style.display=this.value=='__manual__'?'block':'none'">
{opts}<option value="__manual__">Anderes / verstecktes Netzwerk …</option></select>
<div id="m" style="display:none"><label for="manual">Netzwerkname (SSID)</label>
<input id="manual" name="manual" autocapitalize="none" autocorrect="off" spellcheck="false"></div>
<label for="psk">Passwort</label>
<input id="psk" name="psk" type="password" autocomplete="off" placeholder="leer lassen bei offenem WLAN">
<label class="hint"><input type="checkbox" style="width:auto"
 onchange="document.getElementById('psk').type=this.checked?'text':'password'"> Passwort anzeigen</label>
<button type="submit">Verbinden</button>
</form>{rescan}
<p class="hint">{hint}</p>
<script>document.getElementById('ssid').onchange()</script>""" + FOOT


STATUS_JS = """<script>
var box=document.getElementById('box'),t=document.getElementById('t'),net=document.getElementById('net');
var stop=false,shown=false;
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function poll(){
  if(stop)return;
  fetch('/status',{cache:'no-store'}).then(function(r){return r.json()}).then(function(s){
    net.style.display='none';
    if(s.status==='connected'){
      if(!shown){
        shown=true;t.textContent='Verbunden \\u2714';
        box.innerHTML='<div class="ok"><p>WLAN: <b>'+esc(s.ssid)+'</b></p>'+
          '<p>IP-Adresse:<br><span class="big">'+esc(s.ip||'wird ermittelt')+'</span></p>'+
          '<p>Hostname: <b>'+esc(s.host)+'</b></p></div>'+
          '<p class="hint">Der Hotspot schaltet sich in <b id="rem"></b> Sekunden ab.</p>'+
          '<button onclick="done()">Fertig</button>';
      }
      document.getElementById('rem').textContent=s.remaining;
    }else if(s.status==='legacy'){
      stop=true;t.textContent='Verbinde mit \\u201e'+s.ssid+'\\u201c \\u2026';
      box.innerHTML='<p>Der Pi kann sich mit diesem WLAN nur verbinden, wenn der Hotspot ausgeht. '+
        'Die IP-Adresse kann deshalb hier nicht angezeigt werden.</p>'+
        '<p>Nach dem Verbinden ist der Pi meist unter <b>'+esc(s.host)+'</b> erreichbar.</p>'+
        '<p class="hint">Klappt die Verbindung nicht, erscheint \\u201e'+esc(s.ap_ssid)+
        '\\u201c in ca. einer Minute wieder.</p>';
      return;
    }else if(s.status==='idle'){location.href='/';return;}
    setTimeout(poll,2000);
  }).catch(function(){net.style.display='block';setTimeout(poll,2000);});
}
function done(){
  stop=true;fetch('/done',{method:'POST'}).catch(function(){});
  t.textContent='Fertig';
  box.innerHTML='<p>Der Hotspot wird ausgeschaltet. Du kannst dieses Fenster schlie\\u00dfen.</p>';
}
poll();
</script>"""


def render_status():
    job = state["job"] or {}
    return HEAD + f"""
<h1 id="t">Verbinde mit „{html.escape(job.get("ssid", ""))}“ …</h1>
<div id="box"><p><span class="spin"></span>Der Pi verbindet sich. Das kann bis zu einer Minute dauern.</p>
<p class="hint">Bricht die Verbindung zum Hotspot kurz ab, verbinde dich wieder mit
„{html.escape(AP_SSID)}“ – das Ergebnis erscheint dann hier.</p></div>
<p class="err" id="net" style="display:none">Verbindung zum Pi kurz unterbrochen – warte …</p>
""" + STATUS_JS + FOOT


def status_payload():
    job = state["job"] or {}
    status = job.get("status", "idle")
    return {
        "status": status,
        "ssid": job.get("ssid", ""),
        "ip": job.get("ip", ""),
        "host": HOSTNAME + ".local",
        "remaining": max(0, int(job.get("until", 0) - time.time())) if status == "connected" else None,
        "message": state["message"],
        "ap_ssid": AP_SSID,
    }


class Portal(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body="", headers=None, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def redirect(self, code=302):
        self.send(code, "", {"Location": f"http://{AP_IP}/"})

    def do_GET(self):
        # Alles, was nicht direkt das Portal ist (z. B. die Captive-Checks von
        # Android/iOS), wird umgeleitet -> Handy öffnet das Portal automatisch.
        host = self.headers.get("Host", "").split(":")[0]
        path = self.path.split("?")[0]
        if host != AP_IP:
            return self.redirect()
        if path == "/status":
            return self.send(200, json.dumps(status_payload()), ctype="application/json")
        if path != "/":
            return self.redirect()
        job = state["job"]
        if job and job.get("status") in ("connecting", "connected", "legacy"):
            return self.send(200, render_status())
        self.send(200, render_form())

    do_HEAD = do_GET

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, 4096)).decode(errors="replace")
        if self.path == "/done":
            ev_done.set()
            return self.send(200, "{}", ctype="application/json")
        if self.path == "/rescan":
            ev_rescan.set()
            return self.send(200, HEAD + '<meta http-equiv="refresh" content="8;url=/">'
                             '<h1>Suche nach WLANs …</h1><p><span class="spin"></span>'
                             'Die Liste wird gleich neu geladen.</p>' + FOOT)
        if self.path != "/connect":
            return self.redirect(303)
        job = state["job"]
        if job and job.get("status") in ("connecting", "connected", "legacy"):
            return self.redirect(303)  # läuft bereits
        form = urllib.parse.parse_qs(body)

        def get(key):
            return form.get(key, [""])[0]

        manual = get("ssid") == "__manual__"
        ssid = get("manual").strip() if manual else get("ssid")
        psk = get("psk")
        if not ssid or len(ssid.encode()) > 32:
            state["message"] = "Bitte ein gültiges Netzwerk auswählen."
            return self.redirect(303)
        if psk and not 8 <= len(psk) <= 63:
            state["message"] = "Das WLAN-Passwort muss 8–63 Zeichen lang sein."
            return self.redirect(303)
        live = state["ap_iface"] == AP_VIF
        state["job"] = {"ssid": ssid, "status": "connecting" if live else "legacy"}
        connect_requests.put((ssid, psk, manual))
        self.send(200, render_status())


# --- Hauptschleife -----------------------------------------------------------
def main():
    log(f"wifi-portal gestartet (Interface {IFACE}, Hotspot '{AP_SSID}', Wartezeit {GRACE}s)")
    nmcli("radio", "wifi", "on")
    if state["concurrent"]:
        if parse_ap_sta_support(run(["iw", "list"]).stdout):
            log(f"Parallelbetrieb möglich – Hotspot läuft auf {AP_VIF}")
        else:
            disable_concurrent("vom WLAN-Chip nicht unterstützt")
    stop_ap()  # Reste nach Absturz/Neustart aufräumen
    redirect_off()
    nmcli("connection", "delete", "id", AP_CON)
    offline_since = None
    last_retry = last_scan = time.time()
    ap_failures = []

    while True:
        if not state["ap"]:
            if online():
                offline_since = None
            else:
                if offline_since is None:
                    offline_since = time.time()
                    log(f"Kein Netzwerk erkannt – Hotspot startet in {GRACE} s")
                if time.time() - offline_since >= GRACE:
                    log("Kein Netzwerk – starte Hotspot")
                    if start_ap():
                        last_retry = last_scan = time.time()
                    else:
                        offline_since = time.time()  # später erneut versuchen
            time.sleep(5)
            continue

        # --- Hotspot läuft ---
        try:
            ssid, psk, hidden = connect_requests.get(timeout=2)
            if state["ap_iface"] == AP_VIF:
                connect_concurrent(ssid, psk, hidden)
            else:
                connect_legacy(ssid, psk, hidden)
            offline_since = None
            last_retry = time.time()
            continue
        except queue.Empty:
            pass

        if ev_done.is_set():
            ev_done.clear()
            time.sleep(2)
            log("„Fertig“ gedrückt – Hotspot aus")
            stop_ap()
            continue

        now = time.time()
        if state["ap_iface"] == AP_VIF:
            # Parallelbetrieb: Hotspot überwachen
            if not ap_running():
                ap_failures = [t for t in ap_failures if now - t < 300] + [now]
                if len(ap_failures) > 3:
                    disable_concurrent("Hotspot fällt wiederholt aus")
                    stop_ap()
                    continue
                log("Hotspot ausgefallen – starte neu")
                if not bring_up_ap(AP_VIF, *(sta_channel() or (6, "bg"))):
                    disable_concurrent("Neustart des Hotspots fehlgeschlagen")
                    stop_ap()
                continue
            job = state["job"]
            if job and job.get("status") == "connected":
                if now >= job["until"]:
                    log("Anzeigezeit abgelaufen – Hotspot aus")
                    stop_ap()
                    continue
            elif online():
                # bekanntes WLAN/LAN ist (wieder) da
                if not has_clients(AP_VIF):
                    log("Wieder online – Hotspot aus")
                    stop_ap()
                    continue
                state["online_since"] = state["online_since"] or now
                if now - state["online_since"] > HOLD:
                    log("Wieder online – Hotspot aus")
                    stop_ap()
                    continue
            else:
                state["online_since"] = None
            if ev_rescan.is_set() or now - last_scan > SCAN_INTERVAL:
                ev_rescan.clear()
                scan()
                last_scan = time.time()
        else:
            # Einzelbetrieb: regelmäßig prüfen, ob ein bekanntes WLAN wieder da ist
            if now - last_retry > RETRY:
                last_retry = now
                if not has_clients(IFACE) and saved_wifi():
                    log("Kein Gerät am Hotspot – versuche bekannte WLANs …")
                    stop_ap()
                    if wait_online(45):
                        log("Wieder online")
                        offline_since = None
                    else:
                        start_ap()


if __name__ == "__main__":
    main()
