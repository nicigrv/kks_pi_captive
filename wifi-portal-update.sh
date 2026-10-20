#!/bin/bash
# Abgesichertes Auto-Update von wifi-portal aus GitHub.
# Wird jede Nacht um 3:00 Uhr vom Timer wifi-portal-update.timer gestartet.
#
# Update-Kanal (WP_UPDATE_CHANNEL in /etc/default/wifi-portal):
#   auto    – neuester Release (Git-Tag v1.2.3), solange es keinen gibt: neuester Stand von main
#   release – nur Releases
#   main    – immer der neueste Stand von main
#
# Ablauf: neue Version in einen eigenen Ordner entpacken -> Syntax prüfen -> installieren ->
# Funktion prüfen. Läuft der Dienst danach nicht sauber, wird automatisch die vorherige
# Version wiederhergestellt und die fehlerhafte Version künftig übersprungen.
# Der Projektordner (git clone) wird dabei nicht verändert, nur aktualisiert (fetch).
#
# Manuell auslösen:  sudo systemctl start wifi-portal-update
# Log ansehen:       journalctl -u wifi-portal-update --no-pager

CONF=/etc/default/wifi-portal
STATE=/var/lib/wifi-portal

conf_get() { ( . "$CONF" 2>/dev/null; eval "echo \"\${$1:-$2}\"" ); }

conf_set() {  # Wert in der Einstellungsdatei setzen (gleiches Format wie install.sh)
  if grep -q "^$1=" "$CONF"; then
    KEY="$1" VAL="$2" awk 'BEGIN{k=ENVIRON["KEY"]; v=ENVIRON["VAL"]}
      $0 ~ "^"k"=" {print k"='\''"v"'\''"; next} {print}' "$CONF" > "$CONF.tmp" && mv "$CONF.tmp" "$CONF"
  else
    printf "%s='%s'\n" "$1" "$2" >> "$CONF"
  fi
  chmod 600 "$CONF"
}

healthy() {  # Läuft der Dienst nach der Installation sauber? $1 = Startzeit (Unix-Zeit)
  sleep "${WP_HEALTH_WAIT:-20}"
  local log
  log=$(journalctl -u wifi-portal --since "@$1" --no-pager -o cat 2>/dev/null)
  if ! systemctl is-active --quiet wifi-portal; then
    echo "  Prüfung: Dienst läuft nicht."; return 1
  fi
  if [ "$(systemctl show -p NRestarts --value wifi-portal)" != "0" ]; then
    echo "  Prüfung: Dienst ist abgestürzt und wurde neu gestartet."; return 1
  fi
  if echo "$log" | grep -q "Traceback"; then
    echo "  Prüfung: Programmfehler im Log:"; echo "$log" | tail -8 | sed 's/^/    /'; return 1
  fi
  if ! echo "$log" | grep -q "wifi-portal gestartet"; then
    echo "  Prüfung: Startmeldung fehlt."; return 1
  fi
  echo "  Prüfung: Dienst läuft sauber."
  return 0
}

main() {
  REPO=$(conf_get WP_REPO_DIR "")
  CHANNEL=$(conf_get WP_UPDATE_CHANNEL auto)

  if [ -z "$REPO" ] || [ ! -d "$REPO/.git" ]; then
    echo "Kein Projektordner gefunden (WP_REPO_DIR in $CONF) – Update übersprungen."
    return 0
  fi

  # git als Besitzer des Ordners ausführen (sonst meldet git "dubious ownership")
  OWNER=$(stat -c %U "$REPO")
  OWNER_HOME=$(getent passwd "$OWNER" | cut -d: -f6)
  g() { runuser -u "$OWNER" -- env HOME="$OWNER_HOME" git -C "$REPO" "$@"; }

  if ! g fetch --quiet --tags --prune --prune-tags origin; then
    echo "GitHub nicht erreichbar (kein Internet?) – Update übersprungen."
    return 0
  fi

  # --- Ziel bestimmen ---
  BRANCH=$(g rev-parse --abbrev-ref '@{u}' 2>/dev/null || echo origin/main)
  TAG=$(g tag -l 'v[0-9]*' --sort=-v:refname | head -1)
  case "$CHANNEL" in
    main)    REF=$BRANCH ;;
    release) [ -n "$TAG" ] || { echo "Kanal 'release', aber noch kein Release vorhanden – übersprungen."; return 0; }
             REF=$TAG ;;
    *)       REF=${TAG:-$BRANCH} ;;
  esac
  TARGET=$(g rev-parse --verify --quiet "$REF^{commit}")
  [ -n "$TARGET" ] || { echo "Ziel '$REF' nicht gefunden – übersprungen."; return 0; }
  LABEL=$REF; [ "$REF" = "$BRANCH" ] && LABEL="main (${TARGET:0:7})"

  mkdir -p "$STATE"
  CURRENT=$(cat "$STATE/commit" 2>/dev/null)
  echo "Kanal: $CHANNEL – Ziel: $LABEL – installiert: ${CURRENT:0:7}${CURRENT:+ ($(cat "$STATE/version" 2>/dev/null))}"

  if [ "$TARGET" = "$CURRENT" ]; then
    echo "Kein Update vorhanden."
    return 0
  fi
  if [ -n "$CURRENT" ] && g merge-base --is-ancestor "$TARGET" "$CURRENT" 2>/dev/null; then
    echo "Installierte Version ist neuer als das Ziel – kein Update."
    return 0
  fi
  if grep -qx "$TARGET" "$STATE/bad" 2>/dev/null; then
    echo "Version ${TARGET:0:7} ist als fehlerhaft markiert – übersprungen (wartet auf eine neuere)."
    return 0
  fi

  echo "Update gefunden: ${CURRENT:0:7} -> $LABEL"
  [ -n "$CURRENT" ] && g log --oneline "$CURRENT..$TARGET" 2>/dev/null | head -20 | sed 's/^/  /'

  # --- Neue Version entpacken und vorab prüfen ---
  NEW="$STATE/new"
  rm -rf "$NEW"; mkdir -p "$NEW"
  if ! g archive "$TARGET" | tar -x -C "$NEW"; then
    echo "Entpacken fehlgeschlagen – Update abgebrochen."; rm -rf "$NEW"; return 1
  fi
  if ! python3 -m py_compile "$NEW/wifi-portal.py" || ! bash -n "$NEW/install.sh" || ! bash -n "$NEW/wifi-portal-update.sh"; then
    echo "Neue Version enthält Syntaxfehler – Update abgebrochen und Version gesperrt."
    echo "$TARGET" >> "$STATE/bad"; rm -rf "$NEW"; return 1
  fi
  rm -rf "$NEW/__pycache__"

  # --- Sicherung der laufenden Version (für den Rollback) ---
  if [ ! -f "$STATE/current/install.sh" ]; then
    rm -rf "$STATE/current"; mkdir -p "$STATE/current"
    if [ -n "$CURRENT" ] && g cat-file -e "$CURRENT^{commit}" 2>/dev/null; then
      g archive "$CURRENT" | tar -x -C "$STATE/current"
    else
      g archive HEAD | tar -x -C "$STATE/current"   # beste Schätzung der installierten Version
    fi
  fi

  # --- Installieren und prüfen ---
  echo "Installiere $LABEL …"
  T0=$(date +%s)
  if bash "$NEW/install.sh" --update; then OK=1; else OK=0; fi
  conf_set WP_REPO_DIR "$REPO"   # Updates kommen weiterhin aus dem Projektordner
  if [ "$OK" = 1 ] && healthy "$T0"; then
    rm -rf "$STATE/previous"
    mv "$STATE/current" "$STATE/previous"
    mv "$NEW" "$STATE/current"
    echo "$TARGET" > "$STATE/commit"
    echo "$LABEL" > "$STATE/version"
    echo "Update erfolgreich: $LABEL"
    return 0
  fi

  # --- Rollback ---
  echo "FEHLER: Neue Version läuft nicht sauber – stelle vorherige Version wieder her."
  echo "$TARGET" >> "$STATE/bad"
  T0=$(date +%s)
  bash "$STATE/current/install.sh" --update
  conf_set WP_REPO_DIR "$REPO"
  if healthy "$T0"; then
    echo "Vorherige Version wiederhergestellt. Version ${TARGET:0:7} wird künftig übersprungen."
  else
    echo "ACHTUNG: Auch die vorherige Version läuft nicht sauber – bitte von Hand prüfen:"
    echo "  journalctl -u wifi-portal -n 50 --no-pager"
  fi
  rm -rf "$NEW"
  return 1
}

# Alles steht in Funktionen: bash liest so das ganze Skript ein, bevor es läuft –
# das Skript wird während des Updates selbst ersetzt.
main "$@"
exit $?
