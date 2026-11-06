#!/usr/bin/env bash
# Removes the car dashboard from this computer: stops it, removes the start-on-boot
# entry, turns screen blanking back on, and deletes this whole folder (code, config.json
# with your keys, the Spotify login, certificates, .venv, voice models and logs).
# Run from this folder:   ./uninstall.sh          (asks first)
#                         ./uninstall.sh --yes    (no question)
# It does not touch the GitHub repository, and the certificate installed on your
# phone stays there until you remove it (Settings > General > VPN & Device Management).

main() {
    HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
    ENTRY="$HOME/.config/autostart/car-dashboard.desktop"

    # never delete something that isn't the dashboard folder
    if [ ! -f "$HERE/dashboard.py" ] || [ "$HERE" = "/" ] || [ "$HERE" = "$HOME" ]; then
        echo "This doesn't look like the dashboard folder ($HERE). Nothing deleted."
        exit 1
    fi

    echo "This will:"
    echo "  - stop the dashboard and its full-screen browser"
    echo "  - remove the start-on-boot entry ($ENTRY)"
    echo "  - turn screen blanking back on"
    echo "  - DELETE the folder $HERE and everything in it,"
    echo "    including config.json (your API keys) and the Spotify login"
    if [ "$1" != "--yes" ]; then
        read -r -p "Type yes to continue: " answer
        if [ "$answer" != "yes" ]; then
            echo "Cancelled. Nothing changed."
            exit 0
        fi
    fi

    echo "== Stopping the dashboard"
    touch "$HERE/logs/exit-requested" 2>/dev/null        # tells start.sh not to restart anything
    pkill -f "start.sh" 2>/dev/null                      # its restart loop too
    pkill -f "dashboard.py" 2>/dev/null
    pkill -f -- "--kiosk http://localhost" 2>/dev/null
    sleep 1

    echo "== Removing start on boot"
    rm -f "$ENTRY"

    if command -v raspi-config >/dev/null; then
        echo "== Turning screen blanking back on (asks for your password)"
        sudo raspi-config nonint do_blanking 0 || echo "   Could not change screen blanking; skipped."
    fi

    echo "== Deleting $HERE"
    cd / || exit 1
    rm -rf -- "$HERE"

    echo
    echo "Done. The car dashboard is removed from this computer."
    echo "Left alone: system packages from install.sh (Chromium, PortAudio, ...) and uv's"
    echo "Python 3.12 in ~/.local, since other programs may use them."
}

main "$@"   # everything above is read before the folder (and this file) is deleted
exit
