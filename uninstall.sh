#!/bin/bash
# Entfernt wifi-portal wieder
if [ "$EUID" -ne 0 ]; then echo "Bitte mit sudo ausführen"; exit 1; fi
systemctl disable --now wifi-portal.service 2>/dev/null
systemctl disable --now wifi-portal-update.timer 2>/dev/null
nmcli connection delete id wifi-portal-ap 2>/dev/null
iw dev uap0 del 2>/dev/null
nft delete table ip wifi_portal 2>/dev/null
rm -f /usr/local/bin/wifi-portal.py \
      /etc/systemd/system/wifi-portal.service \
      /etc/NetworkManager/dnsmasq-shared.d/captive-portal.conf \
      /etc/default/wifi-portal \
      /usr/local/bin/wifi-portal-update \
      /etc/systemd/system/wifi-portal-update.service \
      /etc/systemd/system/wifi-portal-update.timer
rm -rf /var/lib/wifi-portal
systemctl daemon-reload
echo "wifi-portal entfernt. Über das Portal angelegte WLANs heißen 'wlan-<SSID>' und bleiben erhalten."
