#!/bin/bash
# Installiert wifi-portal (WLAN-Fallback-Hotspot mit Captive Portal)
# Aufruf im Projektordner:
#   sudo bash install.sh            – fragt Name und Passwort des Hotspots ab
#   sudo bash install.sh --update   – ohne Abfragen, Einstellungen bleiben (für das Auto-Update)
set -e
cd "$(dirname "$0")"
REPO_DIR=$(pwd -P)

CONF=/etc/default/wifi-portal
UPDATE=0
[ "${1:-}" = "--update" ] && UPDATE=1

if [ "$EUID" -ne 0 ]; then
  echo "Bitte mit sudo ausführen: sudo bash install.sh"; exit 1
fi

if ! systemctl is-active --quiet NetworkManager; then
  echo "NetworkManager läuft nicht. Unter Raspberry Pi OS Bullseye zuerst per"
  echo "'sudo raspi-config' -> Advanced Options -> Network Config -> NetworkManager umstellen."
  exit 1
fi

# --- Bisherige Werte (bei erneuter Installation) -----------------------------
OLD_SSID="Pi-Setup"
OLD_PW=""
if [ -f "$CONF" ]; then
  OLD_SSID=$(. "$CONF" 2>/dev/null; echo "${WP_AP_SSID:-Pi-Setup}")
  OLD_PW=$(. "$CONF" 2>/dev/null; echo "${WP_AP_PASSWORD:-}")
fi

if [ "$UPDATE" -eq 1 ]; then
  echo "=== wifi-portal – Update (Einstellungen bleiben erhalten) ==="
  AP_SSID=$OLD_SSID
  AP_PW=$OLD_PW
else
echo
echo "=== wifi-portal – Installation ==="
echo
echo "Wenn der Raspberry Pi kein Netzwerk findet, öffnet er ein eigenes WLAN (Hotspot)."
echo "Lege jetzt den Namen und das Passwort für dieses WLAN fest."
echo

# --- Hotspot-Name (SSID) abfragen --------------------------------------------
while true; do
  read -r -p "Name des WLANs (SSID) [$OLD_SSID]: " AP_SSID </dev/tty
  AP_SSID=${AP_SSID:-$OLD_SSID}
  LEN=$(printf %s "$AP_SSID" | wc -c)
  if [ "$LEN" -lt 1 ] || [ "$LEN" -gt 32 ]; then
    echo "  -> Der Name muss 1 bis 32 Zeichen lang sein."; continue
  fi
  case "$AP_SSID" in *"'"*) echo "  -> Das Zeichen ' ist nicht erlaubt."; continue;; esac
  break
done

# --- Hotspot-Passwort abfragen -----------------------------------------------
while true; do
  read -r -s -p "Passwort für das WLAN (8-63 Zeichen, leer = ohne Passwort): " AP_PW </dev/tty; echo
  if [ -z "$AP_PW" ]; then
    read -r -p "  Ohne Passwort kann sich jeder verbinden. Wirklich ohne Passwort? [j/N]: " YN </dev/tty
    case "$YN" in j|J|ja|Ja|JA) break;; *) continue;; esac
  fi
  if [ "${#AP_PW}" -lt 8 ] || [ "${#AP_PW}" -gt 63 ]; then
    echo "  -> Das Passwort muss 8 bis 63 Zeichen lang sein."; continue
  fi
  case "$AP_PW" in *"'"*) echo "  -> Das Zeichen ' ist nicht erlaubt."; continue;; esac
  read -r -s -p "Passwort wiederholen: " AP_PW2 </dev/tty; echo
  if [ "$AP_PW" != "$AP_PW2" ]; then
    echo "  -> Die Passwörter stimmen nicht überein. Bitte erneut eingeben."; continue
  fi
  break
done
echo
fi

# --- Abhängigkeiten (nur wenn sie fehlen; KEIN Vollpaket 'dnsmasq'!) ---------
MISSING=""
dpkg -s dnsmasq-base >/dev/null 2>&1 || MISSING="$MISSING dnsmasq-base"
command -v iw >/dev/null 2>&1        || MISSING="$MISSING iw"
command -v nft >/dev/null 2>&1       || MISSING="$MISSING nftables"
if [ -n "$MISSING" ]; then
  echo "Installiere:$MISSING"
  apt-get update && apt-get install -y $MISSING
fi

# --- WLAN-Land prüfen (ohne Land bleibt WLAN per rfkill gesperrt) ------------
if command -v raspi-config >/dev/null 2>&1; then
  if [ -z "$(raspi-config nonint get_wifi_country 2>/dev/null)" ]; then
    echo "Setze WLAN-Land auf DE"
    raspi-config nonint do_wifi_country DE || true
  fi
fi

# --- Dateien installieren ----------------------------------------------------
install -m 755 wifi-portal.py /usr/local/bin/wifi-portal.py
install -d /etc/NetworkManager/dnsmasq-shared.d
install -m 644 captive-portal.conf /etc/NetworkManager/dnsmasq-shared.d/captive-portal.conf
[ -f "$CONF" ] || install -m 600 wifi-portal.default "$CONF"
install -m 644 wifi-portal.service /etc/systemd/system/wifi-portal.service
install -m 755 wifi-portal-update.sh /usr/local/bin/wifi-portal-update
install -m 644 wifi-portal-update.service /etc/systemd/system/wifi-portal-update.service
install -m 644 wifi-portal-update.timer /etc/systemd/system/wifi-portal-update.timer

# --- Eingaben in die Einstellungen schreiben ---------------------------------
set_conf() {
  if grep -q "^$1=" "$CONF"; then
    KEY="$1" VAL="$2" awk 'BEGIN{k=ENVIRON["KEY"]; v=ENVIRON["VAL"]}
      $0 ~ "^"k"=" {print k"='\''"v"'\''"; next} {print}' "$CONF" > "$CONF.tmp"
    mv "$CONF.tmp" "$CONF"
  else
    printf "%s='%s'\n" "$1" "$2" >> "$CONF"
  fi
}
set_conf WP_AP_SSID "$AP_SSID"
set_conf WP_AP_PASSWORD "$AP_PW"
if [ "$UPDATE" -eq 0 ]; then
  set_conf WP_REPO_DIR "$REPO_DIR"   # von hier holt das Auto-Update neue Versionen
fi
grep -q "^WP_UPDATE_CHANNEL=" "$CONF" || set_conf WP_UPDATE_CHANNEL auto
chown root:root "$CONF"
chmod 600 "$CONF"

# --- Dienst starten ----------------------------------------------------------
systemctl daemon-reload
systemctl enable wifi-portal.service
systemctl enable --now wifi-portal-update.timer
systemctl restart wifi-portal.service

if [ "$UPDATE" -eq 1 ]; then
  echo "Update installiert, Dienst neu gestartet."
  exit 0
fi

# --- Installierte Version merken (für Auto-Update und Rollback) --------------
STATE=/var/lib/wifi-portal
mkdir -p "$STATE"
if [ -d "$REPO_DIR/.git" ]; then
  rgit() { git -c "safe.directory=$REPO_DIR" -C "$REPO_DIR" "$@"; }
  rgit rev-parse HEAD > "$STATE/commit" 2>/dev/null || true
  rgit describe --tags --always > "$STATE/version" 2>/dev/null || true
fi
rm -rf "$STATE/current"; mkdir -p "$STATE/current"
tar -C "$REPO_DIR" --exclude=.git -cf - . | tar -C "$STATE/current" -xf -

echo
echo "=== Fertig ==="
echo "WLAN-Name (SSID): $AP_SSID"
if [ -n "$AP_PW" ]; then echo "Passwort:         gesetzt (WPA2)"; else echo "Passwort:         keins (offenes WLAN)"; fi
echo "Portal-Adresse:   http://10.42.0.1"
if iw list 2>/dev/null \
   | awk '/valid interface combinations/{f=1;next} f&&/^[ \t]*(\*|#|total)/{print;next} f{exit}' \
   | tr '\n' ' ' | tr '*' '\n' | grep -E '\bAP\b' | grep -q managed; then
  echo "Parallelbetrieb: ja – das Portal zeigt nach dem Verbinden IP und Hostname an"
else
  echo "Parallelbetrieb: nein – Einzelbetrieb (Hotspot geht beim Verbinden aus)"
fi
echo
TZ_NAME=$(timedatectl show -p Timezone --value 2>/dev/null || echo "?")
CHANNEL=$(. "$CONF" 2>/dev/null; echo "${WP_UPDATE_CHANNEL:-auto}")
echo "Version:          $(cat /var/lib/wifi-portal/version 2>/dev/null || echo unbekannt)"
echo "Auto-Update:      täglich um 03:00 Uhr von GitHub (Zeitzone: $TZ_NAME, Kanal: $CHANNEL)"
echo "Einstellungen:    $CONF"
echo "Log ansehen:      journalctl -u wifi-portal -f"
