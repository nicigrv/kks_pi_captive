# kks_pi_captive – WLAN-Fallback mit Captive Portal

Hat der Raspberry Pi 60 Sekunden lang kein Netzwerk, öffnet er einen eigenen Hotspot.
Verbindet sich ein Handy, öffnet sich automatisch ein Portal: WLAN auswählen, Passwort
eingeben, fertig. Klappt die Verbindung nicht, kommt der Hotspot mit Fehlermeldung zurück.

## Voraussetzungen
- Raspberry Pi OS (auch Lite) **Bookworm oder neuer** (nutzt NetworkManager). Prüfen: `nmcli general status`
- Pi mit WLAN (Pi 3/4/5/Zero 2 W)

## Installation (per SSH, z. B. aus der Windows-PowerShell)
```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/nicigrv/kks_pi_captive.git
cd kks_pi_captive
sudo bash install.sh
```
Das Skript fragt den **Namen (SSID)** und das **Passwort** für den Hotspot des Pi ab.
Erneut ausführen, um beides zu ändern.

## Testen
Achtung: Wenn du per SSH **über WLAN** verbunden bist, fliegst du dabei raus.
```bash
sudo nmcli device disconnect wlan0
```
Nach ca. 60 s erscheint der Hotspot. Handy verbinden → Portal öffnet sich.
(Hängt ein LAN-Kabel dran, zählt das als „online“ → in `/etc/default/wifi-portal`
`WP_ONLINE_TYPES='wifi'` setzen und `sudo systemctl restart wifi-portal`.)

Öffnet sich das Portal nicht von allein: im Handy-Browser `http://10.42.0.1` aufrufen.

## Ablauf
1. Pi bootet, NetworkManager versucht bekannte WLANs.
2. Nach `WP_GRACE` Sekunden ohne Verbindung: WLANs scannen, Hotspot starten.
3. DNS aller Clients zeigt auf den Pi, jede HTTP-Anfrage wird aufs Portal umgeleitet.
4. Nach Eingabe wird das WLAN-Profil `wlan-<SSID>` angelegt und verbunden.
   Erfolg → bleibt gespeichert (autoconnect). Fehler → Profil löschen, Meldung im Portal.

### Parallelbetrieb (z. B. Pi 4)
Kann der WLAN-Chip gleichzeitig Access Point und Client sein, läuft der Hotspot auf der
virtuellen Schnittstelle `uap0` und die Verbindung auf `wlan0`:
- Die WLAN-Liste wird auch bei laufendem Hotspot aktualisiert (Knopf „Liste aktualisieren“).
- Nach „Verbinden“ bleibt das Portal offen und fragt alle 2 s den Status ab.
- Bei Erfolg zeigt es **WLAN, IP-Adresse und Hostname** an. Der Hotspot bleibt noch
  `WP_HOLD` Sekunden (Standard 120) an oder bis „Fertig“ gedrückt wird.
- Ein Funkmodul = ein Kanal: Funkt das Ziel-WLAN auf einem anderen Kanal, wechselt der
  Hotspot vorher dorthin. Das Handy verliert dabei kurz die Verbindung und verbindet sich neu.
- Klappt der Parallelbetrieb nicht (Chip kann es nicht, 5-GHz-Kanal mit Radarerkennung,
  wiederholte Ausfälle), wechselt das Programm automatisch in den **Einzelbetrieb**:
  Hotspot geht beim Verbinden aus, bei Fehler kommt er wieder; ohne verbundenes Handy
  werden alle `WP_RETRY` Sekunden die bekannten WLANs erneut probiert.

Parallelbetrieb abschalten: `WP_CONCURRENT='off'` in `/etc/default/wifi-portal`.

## Hotspot-Name und -Passwort ändern
- **Im Portal:** Link „Hotspot-Name oder -Passwort ändern“ unten auf der Seite. Zum Ändern
  muss das aktuelle Hotspot-Passwort eingegeben werden. Danach startet der Hotspot neu.
- **Im Terminal:** `cd ~/kks_pi_captive && sudo bash install.sh` erneut ausführen.

## Automatische Updates
Jede Nacht um **3:00 Uhr** prüft der Pi, ob es auf GitHub eine neue Version gibt, lädt sie
herunter (`git pull`) und installiert sie (`install.sh --update`, Einstellungen bleiben).
Ist der Pi gerade offline, wird das Update übersprungen.
```bash
systemctl list-timers wifi-portal-update      # nächster Termin
sudo systemctl start wifi-portal-update       # jetzt sofort prüfen
journalctl -u wifi-portal-update              # Log
sudo systemctl disable --now wifi-portal-update.timer   # Auto-Update abschalten
```
Lokal geänderte Dateien im Projektordner verhindern das Update (wird im Log gemeldet).

## Einstellungen
`/etc/default/wifi-portal` – Hotspot-Name, Hotspot-Passwort, Parallelbetrieb, Wartezeiten.
Danach: `sudo systemctl restart wifi-portal`

## Logs
```bash
journalctl -u wifi-portal -f
```

## Diagnose
Erscheint kein Hotspot oder klappt etwas nicht, erstellt dieses Skript einen Bericht.
Es trennt das WLAN, wartet 150 s, sammelt alle Infos und verbindet den Pi danach selbst wieder
(die SSH-Verbindung bricht dabei ab, das ist normal):
```bash
cd ~/kks_pi_captive
sudo bash diagnose.sh
# nach ca. 3 Minuten wieder anmelden:
cat /tmp/portal-diagnose.txt
```

## Grenzen
- Einzelbetrieb: Während der Hotspot läuft, kann nicht gescannt werden. Die Liste
  stammt vom Hotspot-Start; nicht gelistete Netze über „Anderes Netzwerk“ eintragen.
- Unterstützt offene und WPA2/WPA3-Personal-Netze. WPA-Enterprise (eduroam,
  Schul-/Firmen-WLAN mit Benutzername) nicht.

## Deinstallation
```bash
cd ~/kks_pi_captive
sudo bash uninstall.sh
```
