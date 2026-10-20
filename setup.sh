#!/bin/bash
# Schnellinstallation von kks_pi_captive (wifi-portal) mit einem Befehl:
#
#   curl -fsSL https://raw.githubusercontent.com/nicigrv/kks_pi_captive/main/setup.sh | bash
#
# Lädt das Projekt nach ~/kks_pi_captive (oder aktualisiert es) und startet die Installation.
# Als normaler Benutzer ausführen (nicht mit sudo) – das Passwort wird bei Bedarf abgefragt.

main() {
  REPO_URL="https://github.com/nicigrv/kks_pi_captive.git"
  DIR="$HOME/kks_pi_captive"

  if [ "$EUID" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
    echo "Bitte ohne sudo starten:"
    echo "  curl -fsSL https://raw.githubusercontent.com/nicigrv/kks_pi_captive/main/setup.sh | bash"
    return 1
  fi

  if ! command -v git >/dev/null 2>&1; then
    echo "Installiere git …"
    sudo apt-get update && sudo apt-get install -y git || return 1
  fi

  if [ -d "$DIR/.git" ]; then
    echo "Projekt gefunden – hole neueste Version …"
    git -C "$DIR" pull --ff-only || { echo "Aktualisieren fehlgeschlagen (lokale Änderungen?)"; return 1; }
  else
    echo "Lade Projekt nach $DIR …"
    git clone "$REPO_URL" "$DIR" || return 1
  fi

  # install.sh liest die Eingaben direkt vom Terminal (/dev/tty) – klappt auch über curl | bash
  sudo bash "$DIR/install.sh" "$@" </dev/tty
}

main "$@"
