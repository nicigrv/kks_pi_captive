#!/bin/bash
# Diagnose für wifi-portal
# Trennt das WLAN, wartet, sammelt alle wichtigen Infos und verbindet den Pi danach wieder.
# Läuft im Hintergrund weiter, auch wenn die SSH-Verbindung abbricht.
#
# Aufruf:   sudo bash diagnose.sh          (Standard: 150 Sekunden Testzeit)
#           sudo bash diagnose.sh 240      (eigene Testzeit in Sekunden)
# Ergebnis: cat /tmp/portal-diagnose.txt

[ "$EUID" -eq 0 ] || { echo "Bitte mit sudo ausführen: sudo bash diagnose.sh"; exit 1; }
WAIT=${1:-150}
OUT=/tmp/portal-diagnose.txt

if [ "$2" != "--run" ]; then
  systemctl reset-failed portal-diagnose >/dev/null 2>&1
  systemd-run --unit=portal-diagnose --collect bash "$(readlink -f "$0")" "$WAIT" --run >/dev/null || exit 1
  echo "Diagnose läuft im Hintergrund (${WAIT} s)."
  echo "Gleich wird das WLAN getrennt – die SSH-Verbindung bricht dann ab. Das ist normal."
  echo "Schau in der Zeit auf dem Handy, ob der Hotspot erscheint, und versuche dich zu verbinden."
  echo "Danach verbindet sich der Pi von selbst wieder. Dann erneut per SSH anmelden und ausgeben:"
  echo "  cat $OUT"
  exit 0
fi

{
  echo "=== wifi-portal Diagnose – $(date) ==="
  tr -d '\0' </proc/device-tree/model 2>/dev/null; echo
  grep PRETTY_NAME /etc/os-release; uname -r
  echo "--- Dienst ---"
  echo "aktiv: $(systemctl is-active wifi-portal)  autostart: $(systemctl is-enabled wifi-portal)"
  echo "neue Version (Port-Umleitung): $(grep -c PORTAL_PORT /usr/local/bin/wifi-portal.py 2>/dev/null || echo 0)"
  echo "--- Einstellungen (ohne Passwort) ---"
  grep -vE '^#|^$|PASSWORD' /etc/default/wifi-portal
  echo "Hotspot-Passwort gesetzt: $(grep -qE "^WP_AP_PASSWORD='.+'" /etc/default/wifi-portal && echo ja || echo nein)"
  echo "--- rfkill ---"; rfkill list
  echo "--- WLAN-Land ---"; iw reg get | head -3; raspi-config nonint get_wifi_country 2>/dev/null
  echo "--- Interface-Kombinationen ---"; iw list | grep -A8 "valid interface combinations"
  echo "--- Geräte vorher ---"; nmcli device status
  echo "--- Ports 80 / 8099 ---"; ss -ltnp | grep -E ':(80|8099)[[:space:]]'
} > "$OUT" 2>&1

sleep 2
echo "--- WLAN trennen ---" >> "$OUT"
nmcli device disconnect wlan0 >> "$OUT" 2>&1
sleep "$WAIT"

{
  echo
  echo "=== nach ${WAIT} s ==="
  nmcli device status
  echo "--- iw dev ---"; iw dev
  echo "--- Hotspot-Verbindung ---"
  nmcli -f GENERAL.STATE,connection.interface-name,802-11-wireless.mode,802-11-wireless.band,802-11-wireless.channel \
    connection show wifi-portal-ap 2>&1 | head -8
  echo "--- verbundene Handys ---"
  for i in uap0 wlan0; do iw dev "$i" station dump 2>/dev/null | grep Station; done
  echo "--- nftables ---"; nft list table ip wifi_portal 2>&1
  echo "--- Log wifi-portal ---"
  journalctl -u wifi-portal --since "-$((WAIT + 60))s" --no-pager
  echo "--- NetworkManager-Log (Auszug) ---"
  journalctl -u NetworkManager --since "-$((WAIT + 60))s" --no-pager \
    | grep -iE "uap0|wifi-portal-ap|mode.?ap|error|fail|warn" | tail -40
  echo "--- Kernel (WLAN-Treiber) ---"
  dmesg | grep -iE "brcmf|uap0" | tail -20
} >> "$OUT" 2>&1

echo "--- WLAN wieder verbinden ---" >> "$OUT"
nmcli device connect wlan0 >> "$OUT" 2>&1
chmod 644 "$OUT"
