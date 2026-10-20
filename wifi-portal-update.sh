#!/bin/bash
# Automatisches Update von wifi-portal aus GitHub.
# Wird jede Nacht um 3:00 Uhr vom Timer wifi-portal-update.timer gestartet.
# Manuell auslösen:  sudo systemctl start wifi-portal-update
# Log ansehen:       journalctl -u wifi-portal-update

# Alles steht in main(): bash liest so das ganze Skript ein, bevor es läuft –
# das Skript wird während des Updates selbst ersetzt.
main() {
  CONF=/etc/default/wifi-portal
  REPO=$( . "$CONF" 2>/dev/null; echo "${WP_REPO_DIR:-}" )

  if [ -z "$REPO" ] || [ ! -d "$REPO/.git" ]; then
    echo "Kein Projektordner gefunden (WP_REPO_DIR in $CONF) – Update übersprungen."
    return 0
  fi

  # git als Besitzer des Ordners ausführen (sonst meldet git "dubious ownership")
  OWNER=$(stat -c %U "$REPO")
  OWNER_HOME=$(getent passwd "$OWNER" | cut -d: -f6)
  g() { runuser -u "$OWNER" -- env HOME="$OWNER_HOME" git -C "$REPO" "$@"; }

  if ! g fetch --quiet origin; then
    echo "GitHub nicht erreichbar (kein Internet?) – Update übersprungen."
    return 0
  fi

  LOCAL=$(g rev-parse HEAD)
  REMOTE=$(g rev-parse '@{u}' 2>/dev/null)
  if [ -z "$REMOTE" ]; then
    echo "Kein Upstream-Branch eingerichtet – Update übersprungen."
    return 0
  fi
  if [ "$LOCAL" = "$REMOTE" ]; then
    echo "Kein Update vorhanden (Stand ${LOCAL:0:7})."
    return 0
  fi

  echo "Update gefunden: ${LOCAL:0:7} -> ${REMOTE:0:7}"
  g log --oneline "$LOCAL..$REMOTE" | sed 's/^/  /'

  if ! g merge --ff-only --quiet '@{u}'; then
    echo "Update nicht möglich: Im Ordner $REPO wurden Dateien lokal geändert."
    echo "Prüfen mit: git -C $REPO status"
    return 1
  fi

  echo "Installiere neue Version …"
  bash "$REPO/install.sh" --update
  echo "Update abgeschlossen."
}

main "$@"
exit $?
