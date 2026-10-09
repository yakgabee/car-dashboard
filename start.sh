#!/usr/bin/env bash
# Starts the car dashboard and opens it full screen in Chromium.
# install_autostart.sh makes the Pi run this when the desktop starts.
# Run it by hand too:  ./start.sh
#
# - Uses .venv/bin/python if there is a .venv folder here, otherwise python3.
# - Restarts dashboard.py if it ever stops, and reopens Chromium if it closes.
# - Only one copy runs at a time.
# - The Exit button on the screen writes logs/exit-requested: then both loops stop,
#   Chromium closes and nothing restarts until start.sh runs again (e.g. next boot).
# - Output goes to logs/dashboard.log (the previous run is kept as dashboard.log.old).

cd "$(dirname "$(readlink -f "$0")")" || exit 1
mkdir -p logs

exec 9>logs/start.lock
if ! flock -n 9; then
    echo "The dashboard is already running."
    exit 0
fi

PYTHON=python3
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python
PORT=$("$PYTHON" -c 'import json; print(json.load(open("config.json")).get("port", 5000))' 2>/dev/null || echo 5000)
URL="http://localhost:$PORT"
BROWSER=$(command -v chromium-browser || command -v chromium)

[ -f logs/dashboard.log ] && mv -f logs/dashboard.log logs/dashboard.log.old
STOP=logs/exit-requested
rm -f "$STOP"           # a fresh start: an Exit from last time no longer applies

# dashboard.py, restarted if it stops
(
    while true; do
        echo "=== dashboard.py starting $(date) ===" >>logs/dashboard.log
        "$PYTHON" -u dashboard.py "$@" >>logs/dashboard.log 2>&1
        code=$?
        if [ -f "$STOP" ]; then
            echo "=== dashboard.py exited from the screen's Exit button $(date): not restarting ===" >>logs/dashboard.log
            break
        fi
        echo "=== dashboard.py stopped (exit $code), restarting in 5 s ===" >>logs/dashboard.log
        sleep 5
    done
) &

# wait until the page answers (first start can take a while: models load, Spotify connects)
for _ in $(seq 1 120); do
    curl -s -o /dev/null "$URL" && break
    sleep 1
done

if [ -z "$BROWSER" ]; then
    echo "Chromium not found. Open $URL yourself." | tee -a logs/dashboard.log
    wait
    exit 0
fi

# Chromium full screen, reopened if it closes, closed for good after the Exit button
while [ ! -f "$STOP" ]; do
    "$BROWSER" --kiosk "$URL" \
        --noerrdialogs --disable-infobars --no-first-run \
        --disable-session-crashed-bubble --disable-features=Translate \
        --check-for-update-interval=31536000 --password-store=basic \
        --autoplay-policy=no-user-gesture-required \
        >>logs/chromium.log 2>&1 &
    browser=$!
    while kill -0 "$browser" 2>/dev/null; do
        if [ -f "$STOP" ]; then
            kill "$browser" 2>/dev/null
            pkill -f -- "--kiosk $URL" 2>/dev/null     # the launcher may have handed off to a child
            break
        fi
        sleep 1
    done
    wait "$browser" 2>/dev/null
    [ -f "$STOP" ] || sleep 2
done
wait        # until the dashboard loop has finished too
