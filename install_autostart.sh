#!/usr/bin/env bash
# Makes the dashboard open by itself when the Raspberry Pi starts.
# Run once on the Pi:            ./install_autostart.sh
# To turn it off again:          ./install_autostart.sh --remove
#
# It adds a desktop autostart entry (works with the Pi's desktop: labwc, wayfire
# or X11) that runs start.sh, and turns off screen blanking. The Pi must log in
# to the desktop by itself: raspi-config > System Options > Boot / Auto Login >
# Desktop Autologin (the default on Raspberry Pi OS with desktop).

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ENTRY="$HOME/.config/autostart/car-dashboard.desktop"

if [ "$1" = "--remove" ]; then
    rm -f "$ENTRY"
    echo "Removed. The dashboard no longer starts by itself."
    exit 0
fi

chmod +x "$HERE/start.sh"
mkdir -p "$(dirname "$ENTRY")"
cat >"$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=Car dashboard
Comment=Starts dashboard.py and opens it full screen
Exec=$HERE/start.sh
X-GNOME-Autostart-enabled=true
EOF
echo "Added $ENTRY"

if command -v raspi-config >/dev/null; then
    sudo raspi-config nonint do_blanking 1 && echo "Screen blanking turned off."
else
    echo "raspi-config not found: turn off screen blanking yourself if the screen goes dark."
fi

[ -f "$HERE/config.json" ] || echo "Note: no config.json yet. Run python3 dashboard.py once to make it and fill it in."
[ -f "$HERE/.spotify_token" ] || echo "Note: log in to Spotify once first: run python3 dashboard.py with a keyboard and mouse attached."
echo "Done. Restart the Pi (sudo reboot) to try it. Logs: $HERE/logs/"
