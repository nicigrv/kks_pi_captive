# kks_pi_captive – WLAN-Fallback mit Captive Portal

Hat der Raspberry Pi 60 Sekunden lang kein Netzwerk, öffnet er einen eigenen Hotspot.
Verbindet sich ein Handy, öffnet sich automatisch ein Portal: WLAN auswählen, Passwort
eingeben, fertig. Klappt die Verbindung nicht, kommt der Hotspot mit Fehlermeldung zurück.

## Voraussetzungen
- Raspberry Pi OS (auch Lite) **Bookworm oder neuer** (nutzt NetworkManager). Prüfen: `nmcli general status`
- Pi mit WLAN (Pi 3/4/5/Zero 2 W)

## Schnellinstallation (ein Befehl)
Auf dem Pi (per SSH, als normaler Benutzer, **ohne** sudo):
```bash
curl -fsSL https://raw.githubusercontent.com/nicigrv/kks_pi_captive/main/setup.sh | bash
```
Lädt das Projekt nach `~/kks_pi_captive` (oder aktualisiert es) und startet die Installation.
Ohne erneute Abfrage von Name und Passwort: `… | bash -s -- --update`

## Installation Schritt für Schritt (per SSH, z. B. aus der Windows-PowerShell)
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

## Gespeicherte WLANs verwalten
Im Portal unter „Gespeicherte WLANs verwalten“: alle Netze sehen, mit denen sich der Pi
automatisch verbindet, die Reihenfolge ändern (▲ ▼, das oberste wird bevorzugt) und alte
Netze löschen.

## Automatische Updates
Jede Nacht um **3:00 Uhr** sucht der Pi auf GitHub nach einer neuen Version und installiert sie
(Einstellungen bleiben erhalten). Ist der Pi offline, wird das Update übersprungen.

**Update-Kanal** (`WP_UPDATE_CHANNEL` in `/etc/default/wifi-portal`):
- `auto` (Standard) – neuester **Release** (Git-Tag `v1.2.3`); solange es keinen gibt, neuester Stand von `main`
- `release` – nur Releases
- `main` – immer der neueste Stand von `main`

**Abgesichert:**
1. Die neue Version wird in einen eigenen Ordner entpackt und vorab auf Syntaxfehler geprüft.
2. Nach der Installation wird geprüft, ob der Dienst sauber läuft (aktiv, kein Absturz, kein Programmfehler).
3. Wenn nicht: automatischer **Rollback** auf die vorherige Version. Die fehlerhafte Version wird
   gesperrt und erst wieder versucht, wenn es eine neuere gibt.

Der Projektordner `~/kks_pi_captive` wird dabei nur aktualisiert (`git fetch`), nicht verändert.

```bash
systemctl list-timers wifi-portal-update      # nächster Termin
sudo systemctl start wifi-portal-update       # jetzt sofort prüfen
journalctl -u wifi-portal-update --no-pager   # Log
cat /var/lib/wifi-portal/version              # installierte Version
sudo systemctl disable --now wifi-portal-update.timer   # Auto-Update abschalten
```

### Neue Version veröffentlichen
Änderungen auf `main` pushen und testen. Wenn alles passt, einen Release anlegen –
erst dann holen sich die Pis die neue Version:
```bash
git tag v1.0.1
git push origin v1.0.1
```
(oder auf GitHub: *Releases → Draft a new release → Tag `v1.0.1` → Publish*)

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
