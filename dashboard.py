#!/usr/bin/env python3
"""
Car dashboard: greeting, clock, weather, now playing, album art, map and
next-direction placeholders, hand gestures and voice commands.

SETUP (on the Pi)
    pip install -r requirements.txt
    python3 dashboard.py --demo      # test the screen with fake songs first
    python3 dashboard.py             # first run writes config.json, fill it in
    python3 dashboard.py --kiosk     # also opens Chromium full screen

SCREEN
    config.json sets "name" (the greeting) and "theme" ("dark" or "light").
    Add ?theme=light to the address to try the other theme.
    The layout is drawn at 1024x600 and scaled to fit any screen.

SPOTIFY
    1. Create an app at https://developer.spotify.com/dashboard (needs Premium).
    2. Add this exact redirect URI to the app:  http://127.0.0.1:8888/callback
    3. Put the client ID and secret into config.json.
    4. First run opens a browser on the Pi to log in. After that it remembers.
    Music plays on your phone. The Pi only shows and controls it.

GESTURES
    gestures.py (next to this file) watches the camera and the dashboard
    starts it for you. It needs:  pip install mediapipe opencv-python
    and hand_landmarker.task in the same folder.
        Open hand -> play          Fist -> pause
        2 fingers right -> next    2 fingers left -> previous
    Test the camera on its own, with a preview window:  python3 gestures.py

    Any gesture code works if it has a function named run:

        def run(trigger):
            while True:
                gesture = detect()          # your existing code
                if gesture == "swipe_right":
                    trigger("next")

    Actions: play, pause, play_pause, next, previous, volume_up, volume_down.
    Repeats of the same action are ignored for a moment, so holding a pose
    does not skip five songs. Remove any cv2.imshow preview window: it does
    not work from a background thread.

    If you would rather keep your gesture script separate, it can send
        POST http://localhost:5000/api/action/next
    instead.

LYRICS
    Tap the song picture for the lyrics page, tap it again to go back. Lyrics
    come from LRCLIB (free, no key); time-synced ones follow the song, plain
    ones scroll along with it. Voice: "show lyrics", "show the map". Apps and
    gestures: POST /api/action/lyrics, /api/action/home, /api/action/toggle_view.

WEATHER
    Put an OpenWeather API key in config.json ("openweather_api_key") and set
    "weather_city", e.g. "Toronto,CA". It refreshes every 10 minutes.

VOICE
    voice.py (next to this file) listens on the microphone. Say
        "Hey Bitch, play Road Trips by Drake"
    or "Hey Bitch" alone, wait for "Yes?", then say the command. Also:
    pause, resume, next, skip, previous. Replies are spoken with Piper.
    It needs:  pip install vosk piper-tts sounddevice
    and the speech models in models/ (see voice.py). Test it on its own,
    without Spotify:  python3 voice.py
    "wake_phrase" in config.json changes what wakes it up.

CLAUDE
    With "anthropic_api_key" in config.json, voice requests the simple phrases
    don't cover go to Claude (assistant.py, model "claude_model", default
    claude-haiku-5-5): "find me coffee", "something chill", "what's the weather?".

MAP
    With "mapbox_token" (a public pk. token from mapbox.com) in config.json,
    navigation.py finds the destination, gets a driving route with traffic
    from the phone's shared position, shows the next turn, reroutes, speaks
    the turns, and the screen draws the map with Leaflet (static/leaflet).

PHONE
    The phone page also shares the phone's location with the dashboard. Phones
    only allow that on https, so it is served at https://<pi-address>:5443/phone
    too, with a self-signed certificate (accept the warning once).
    Open http://<pi-address>:5000/phone in the phone's browser to set or
    clear the destination. The phone must be on the same network.
    Apps can also send  POST http://<pi-address>:5000/api/destination   {"name": "123 Main St"}
"""

import argparse
import importlib.util
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
TOKEN_PATH = HERE / ".spotify_token"
CERT_DIR = HERE / "certs"               # self-signed https certificate for the phone page
FONTS_PATH = HERE / "fonts"
COLORS_PATH = HERE / "colors.json"      # colors picked on the phone page

# Colors the phone page can change. Unset ones use the theme's own color.
COLOR_DEFAULTS = {
    "dark": {"background": "#0c0f12", "panel": "#151a20", "border": "#252c35",
             "bar": "#ffb14a", "text": "#f3f5f7", "route": "#5aa9ff", "car": "#5aa9ff",
             "lyric": "#ffb14a", "lyrics": "#d3d9e1"},
    "light": {"background": "#eef1f4", "panel": "#ffffff", "border": "#d5dae1",
              "bar": "#a35400", "text": "#12161b", "route": "#1d6fd1", "car": "#1d6fd1",
              "lyric": "#a35400", "lyrics": "#2a313a"},
}

DEFAULT_CONFIG = {
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "spotify_redirect_uri": "http://127.0.0.1:8888/callback",
    "port": 5000,
    "poll_seconds": 2,
    "volume_step": 10,
    "name": "Gabriel",
    "theme": "dark",
    "show_cursor": True,        # false hides the mouse pointer (nice on a touchscreen)
    "openweather_api_key": "",
    "weather_city": "Toronto,CA",
    "weather_units": "metric",
    "wake_phrase": "hey bitch",
    "anthropic_api_key": "",
    "claude_model": "claude-haiku-5-5",
    "timezone": "America/Toronto",
    "piper_voice": "en_US-lessac-medium",   # any Piper voice in models/ (.onnx + .onnx.json)
    "https_port": 5443,
    "mapbox_token": "",
}

SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"
ACTIONS = ("play", "pause", "play_pause", "next", "previous", "volume_up", "volume_down")
VIEWS = ("home", "lyrics", "toggle_view")   # screen switches; trigger() handles them without Spotify
COOLDOWN = {"volume_up": 0.3, "volume_down": 0.3, "home": 0.3, "lyrics": 0.3}   # seconds; others use 1.0
LABELS = {
    "play": "Play",
    "pause": "Pause",
    "play_pause": "Play / pause",
    "next": "Next",
    "previous": "Previous",
    "volume_up": "Volume up",
    "volume_down": "Volume down",
}


# ---------------------------------------------------------------- shared state

class Hub:
    """Everything the screen shows. Workers write to it, the web page reads it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {
            "connected": False,
            "playing": False,
            "title": None,
            "artist": None,
            "album": None,
            "art": None,
            "progress_ms": 0,
            "duration_ms": 0,
            "device": None,
            "volume": None,
            "supports_volume": False,
            "error": None,
            # plug-in and weather problems, kept apart from "error", which every Spotify poll clears
            "gestures_error": None,
            "voice_error": None,
            "weather_error": None,
            "weather": None,        # {"temp", "main", "night"} from OpenWeather
            "voice_state": "off",   # off, idle, listening, working: drives the mic button
            "position": None,       # {"lat", "lon", "accuracy", "speed", "heading", "at"} from the phone
            "colors": {},           # {"background": "#rrggbb", ...} from colors.json
            "last_action": None,
            "destination": None,
            "next_turn": None,      # {"instruction", "street", "distance", "icon"} from navigation.py
            "nav": None,            # {"name", "remaining", "minutes"} while following a route
            "nav_status": None,     # "Finding ...", "Waiting for your location ..."
            "nav_error": None,
            "route_id": 0,          # changes when the route changes; the line itself is at /api/route
            "view": "home",         # "home" or "lyrics"
            "lyrics_id": 0,         # changes when the lyrics change; the lines themselves are at /api/lyrics
            "lyrics_state": "none", # loading, synced, plain, instrumental, none, error
        }
        self.fetched_at = time.time()
        self.action_count = 0

    def update(self, **changes):
        with self.lock:
            self.data.update(changes)

    def set_playback(self, **changes):
        with self.lock:
            self.data.update(changes)
            self.fetched_at = time.time()

    def get(self, key):
        with self.lock:
            return self.data[key]

    def note_action(self, name):
        self.show(LABELS[name])

    def show(self, label):
        """Flash a short message on the screen (the toast)."""
        with self.lock:
            self.action_count += 1
            self.data["last_action"] = {"id": self.action_count, "label": label}

    def snapshot(self):
        with self.lock:
            snap = dict(self.data)
            if snap["playing"] and snap["duration_ms"]:
                elapsed = (time.time() - self.fetched_at) * 1000
                snap["progress_ms"] = int(min(snap["duration_ms"], snap["progress_ms"] + elapsed))
            return snap


hub = Hub()
actions = queue.Queue()
refresh_now = threading.Event()
_last_trigger = {}
_trigger_lock = threading.Lock()


def trigger(action):
    """Call this from gesture code. Returns right away; never blocks the camera loop."""
    if action not in ACTIONS and action not in VIEWS:
        print(f"[dashboard] unknown action: {action!r}")
        return False
    now = time.time()
    with _trigger_lock:
        if now - _last_trigger.get(action, 0) < COOLDOWN.get(action, 1.0):
            return False
        _last_trigger[action] = now
    if action in VIEWS:
        with hub.lock:
            if action == "toggle_view":
                action = "home" if hub.data["view"] == "lyrics" else "lyrics"
            hub.data["view"] = action
        return True
    actions.put(action)
    return True


# ---------------------------------------------------------------- spotify

class DemoPlayer:
    """Fake player so the screen can be tested without Spotify."""

    TRACKS = [
        ("Night Drive", "The Example Band", "Demo Album", 204000),
        ("A Much Longer Song Title To Check How Wrapping Looks", "Somebody feat. Somebody Else", "Demo Album", 187000),
        ("Third Track", "Placeholder", "Demo Album", 231000),
    ]

    def __init__(self):
        self.i, self.playing, self.vol = 0, True, 50
        self.pos, self.stamp = 0.0, time.time()

    def _advance(self):
        now = time.time()
        if self.playing:
            self.pos += (now - self.stamp) * 1000
        self.stamp = now
        if self.pos >= self.TRACKS[self.i][3]:
            self.next_track()

    def current_playback(self, **_):
        self._advance()
        title, artist, album, dur = self.TRACKS[self.i]
        return {
            "is_playing": self.playing,
            "progress_ms": int(self.pos),
            "device": {"name": "Demo phone", "volume_percent": self.vol, "supports_volume": True},
            "item": {"name": title, "duration_ms": dur, "artists": [{"name": artist}],
                     "album": {"name": album, "images": []}},
        }

    def pause_playback(self):
        self._advance(); self.playing = False

    def start_playback(self, uris=None):
        self._advance(); self.playing = True
        if uris:
            self.i = int(uris[0].split(":")[-1]); self.pos = 0.0

    def search(self, q, type="track", limit=10):
        words = set(re.findall(r"\w+", q.lower())) - {"track", "artist"}
        items = [{"uri": f"demo:{i}", "name": t[0], "artists": [{"name": t[1]}]}
                 for i, t in enumerate(self.TRACKS) if words & set(re.findall(r"\w+", t[0].lower()))]
        return {"tracks": {"items": items[:limit]}}

    def next_track(self):
        self.i = (self.i + 1) % len(self.TRACKS); self.pos = 0.0; self.stamp = time.time()

    def previous_track(self):
        self.i = (self.i - 1) % len(self.TRACKS); self.pos = 0.0; self.stamp = time.time()

    def volume(self, percent):
        self.vol = percent


def connect_spotify(cfg):
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    auth = SpotifyOAuth(
        client_id=cfg["spotify_client_id"],
        client_secret=cfg["spotify_client_secret"],
        redirect_uri=cfg["spotify_redirect_uri"],
        scope=SCOPES,
        cache_path=str(TOKEN_PATH),
        open_browser=True,
    )
    # retries=0 so a slow or failing call returns quickly instead of hanging the worker
    sp = spotipy.Spotify(auth_manager=auth, requests_timeout=5, retries=0)
    sp.current_playback()   # forces the one-time login now, while a person is at the keyboard
    return sp


def explain(err):
    """Turn an exception into a short message for the screen, plus seconds to wait."""
    status = getattr(err, "http_status", None)
    if status == 429:
        headers = getattr(err, "headers", None) or {}
        try:
            wait = int(headers.get("Retry-After", 10))
        except (TypeError, ValueError):
            wait = 10
        return "Spotify is rate limiting. Retrying shortly.", max(wait, 5)
    if status == 404:
        return "Open Spotify on your phone and start a song.", 5
    if status in (401, 403):
        return "Spotify refused the request. Check login and Premium.", 15
    if status:
        return f"Spotify error {status}.", 5
    return "No connection to Spotify.", 5


def read_playback(pb):
    if not pb or not pb.get("item"):
        device = (pb or {}).get("device") or {}
        hub.set_playback(connected=True, playing=False, title=None, artist=None, album=None,
                         art=None, progress_ms=0, duration_ms=0, device=device.get("name"),
                         volume=device.get("volume_percent"),
                         supports_volume=bool(device.get("supports_volume")), error=None)
        return
    item = pb["item"]
    device = pb.get("device") or {}
    if "album" in item:                                    # a song
        artist = ", ".join(a["name"] for a in item.get("artists", []))
        album = item["album"].get("name")
        images = item["album"].get("images") or []
    else:                                                  # a podcast episode
        artist = (item.get("show") or {}).get("name")
        album = None
        images = item.get("images") or []
    hub.set_playback(
        connected=True,
        playing=bool(pb.get("is_playing")),
        title=item.get("name"),
        artist=artist,
        album=album,
        art=images[0]["url"] if images else None,
        progress_ms=pb.get("progress_ms") or 0,
        duration_ms=item.get("duration_ms") or 0,
        device=device.get("name"),
        volume=device.get("volume_percent"),
        supports_volume=bool(device.get("supports_volume")),
        error=None,
    )


def poll_loop(sp, sp_lock, poll_seconds):
    while True:
        wait = poll_seconds
        try:
            with sp_lock:
                pb = sp.current_playback(additional_types="episode")
            read_playback(pb)
        except Exception as err:                           # keep the dashboard alive no matter what
            message, wait = explain(err)
            hub.update(connected=False, error=message)
        refresh_now.wait(wait)
        refresh_now.clear()


def action_loop(sp, sp_lock, volume_step):
    while True:
        action = actions.get()
        hub.note_action(action)
        try:
            with sp_lock:
                if action in ("play", "pause", "play_pause"):
                    playing = hub.get("playing")
                    want = not playing if action == "play_pause" else action == "play"
                    if want != playing:     # Spotify refuses "play" while already playing
                        sp.start_playback() if want else sp.pause_playback()
                elif action == "next":
                    sp.next_track()
                elif action == "previous":
                    sp.previous_track()
                else:
                    if not hub.get("supports_volume"):
                        hub.update(error="This device does not allow remote volume.")
                        continue
                    current = hub.get("volume") or 0
                    step = volume_step if action == "volume_up" else -volume_step
                    target = max(0, min(100, current + step))
                    sp.volume(target)
                    hub.update(volume=target)
                    hub.show(f"Volume {target}%")
            # optimistic update so the screen reacts before Spotify confirms
            if action in ("play", "pause", "play_pause"):
                hub.set_playback(playing=want, progress_ms=hub.snapshot()["progress_ms"])
        except Exception as err:
            hub.update(error=explain(err)[0])
        time.sleep(0.4)          # give Spotify a moment, then fetch the real state
        refresh_now.set()


player = {"sp": None, "lock": None}     # set in main(); voice commands use it


def play_song(query):
    """Search Spotify and play the best match. Returns a sentence to speak. Called from the voice thread."""
    sp, sp_lock = player["sp"], player["lock"]
    if sp is None:
        return "Spotify is not connected."
    title, _, artist = query.partition(" by ")
    try:
        with sp_lock:
            items = []
            if artist:      # "road trips by drake" -> search the title and artist fields
                items = sp.search(f"track:{title} artist:{artist}", type="track", limit=5)["tracks"]["items"]
            if not items:
                items = sp.search(query, type="track", limit=5)["tracks"]["items"]
            if not items and artist:    # the artist name was probably misheard
                items = sp.search(title, type="track", limit=5)["tracks"]["items"]
            if not items:
                return f"I couldn't find {query}."
            track = items[0]
            sp.start_playback(uris=[track["uri"]])
    except Exception as err:
        message = explain(err)[0]
        hub.update(error=message)
        return message
    name, by = track["name"], track["artists"][0]["name"] if track["artists"] else ""
    hub.show(f"Playing {name}")
    time.sleep(0.4)
    refresh_now.set()
    return f"Playing {name} by {by}." if by else f"Playing {name}."


lyrics = {"source": None}       # lyrics.Lyrics, set in main()
navigator = {"nav": None}       # navigation.Navigator, set in main() when there is a Mapbox token


def navigate(place):
    """Set the destination by voice. Returns a sentence to speak. Routing comes with the map stage."""
    place = place.strip(" .")[:120]
    hub.update(destination=place)
    hub.show(f"Destination: {place}")
    return f"Finding {place}." if navigator["nav"] else f"Destination set to {place}."


assistant = {"claude": None, "error": None}   # set in main() when there is an API key


def car_state():
    """What Claude may know about the car right now. Kept small: it goes with every request."""
    s = hub.snapshot()
    w = s["weather"]
    return {
        "time": time.strftime("%A %B %d %Y, %H:%M"),     # full date: Claude doesn't know today's date
        "now_playing": f"{s['title']} by {s['artist']}" if s["title"] else None,
        "music_playing": s["playing"],
        "song_position": (f"{s['progress_ms'] // 60000}:{s['progress_ms'] // 1000 % 60:02d} of "
                          f"{s['duration_ms'] // 60000}:{s['duration_ms'] // 1000 % 60:02d}") if s["duration_ms"] else None,
        "destination": s["destination"],
        "weather": f"{w['temp']} degrees, {w['main'].lower()}" if w else None,
    }


def assist(words, from_button):
    """Let Claude handle a voice request. Returns a sentence to speak, "" to stay quiet,
    or None when Claude is not available (voice.py then falls back to its simple rules)."""
    claude = assistant["claude"]
    if claude is None:
        return None
    try:
        d = claude.decide(words, car_state())
    except assistant["error"] as err:
        hub.update(voice_error=str(err))
        return None
    hub.update(voice_error=None)
    action = d.get("action")
    if action == "play_song" and d.get("query"):
        hub.show(f"Finding {d['query']}")
        return play_song(d["query"])
    if action == "control" and d.get("control"):
        trigger(d["control"])
        return ""
    if action == "navigate" and d.get("destination"):
        return navigate(d["destination"])
    if action == "search" and d.get("query"):
        hub.show("Searching the web…")
        try:
            return claude.search(words, d["query"], car_state())
        except assistant["error"] as err:
            hub.update(voice_error=str(err))
            return str(err)
    if action == "answer" and d.get("reply"):
        return d["reply"]
    # "none": background talk. After the button press, say so; otherwise stay quiet.
    return "Sorry, I didn't catch that." if from_button else ""


# ---------------------------------------------------------------- weather

WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"


def weather_loop(cfg):
    import requests

    params = {"q": cfg["weather_city"], "units": cfg["weather_units"], "appid": cfg["openweather_api_key"]}
    while True:
        wait = 600
        try:
            r = requests.get(WEATHER_URL, params=params, timeout=8)
            if r.status_code == 401:
                raise RuntimeError("Weather key refused. New keys can take a couple of hours to start working.")
            if r.status_code == 404:
                raise RuntimeError(f"Weather: city {cfg['weather_city']!r} not found.")
            r.raise_for_status()
            d = r.json()
            hub.update(weather={"temp": round(d["main"]["temp"]), "main": d["weather"][0]["main"],
                                "night": d["weather"][0]["icon"].endswith("n")},
                       weather_error=None)
        except RuntimeError as err:
            hub.update(weather_error=str(err))
            wait = 120
        except Exception:
            hub.update(weather_error="No weather connection.")
            wait = 60
        time.sleep(wait)


# ---------------------------------------------------------------- plug-ins (gestures.py, voice.py)

def start_plugin(name, *args):
    """Run <name>.py's run(*args) in a thread. Problems show on screen as <name>_error."""
    path = HERE / f"{name}.py"
    if not path.exists():
        print(f"[dashboard] no {name}.py found, {name} off")
        return
    title = name.capitalize()

    def runner():
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.run(*args)
            hub.update(**{f"{name}_error": f"{title} stopped."})
            if name == "voice":
                hub.update(voice_state="off")
        except ImportError as err:
            print(f"[dashboard] {name} off: {err}")
            hub.update(**{f"{name}_error": f"{title} off: {err.name or 'a library'} is not installed."})
            if name == "voice":
                hub.update(voice_state="off")
        except Exception as err:
            traceback.print_exc()
            hub.update(**{f"{name}_error": f"{title} stopped: {str(err)[:80]}"})
            if name == "voice":
                hub.update(voice_state="off")

    threading.Thread(target=runner, name=name, daemon=True).start()
    print(f"[dashboard] {name}.py started")


# ---------------------------------------------------------------- web page

app = Flask(__name__, static_folder=None)   # /static is served below, from this folder
screen = {"name": DEFAULT_CONFIG["name"], "theme": DEFAULT_CONFIG["theme"], "cursor": True,
          "mapbox": ""}   # set in main(); the Mapbox token is a public one, made for web pages


@app.get("/")
def page():
    return Response(PAGE.replace("__SCREEN__", json.dumps(screen).replace("</", "<\\/")), mimetype="text/html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(HERE / "static", name, max_age=86400)


@app.get("/api/route")
def api_route():
    nav = navigator["nav"]
    return jsonify(nav.geometry() if nav else {"id": 0, "line": []})


@app.get("/api/lyrics")
def api_lyrics():
    source = lyrics["source"]
    return jsonify(source.data() if source else {"id": 0, "song": None, "state": "none", "lines": []})


@app.get("/fonts/<path:name>")
def fonts(name):
    return send_from_directory(FONTS_PATH, name, max_age=86400)


@app.get("/api/state")
def api_state():
    return jsonify(hub.snapshot())


@app.post("/api/action/<name>")
def api_action(name):
    if name not in ACTIONS and name not in VIEWS:
        return jsonify(ok=False, error="unknown action"), 404
    return jsonify(ok=True, accepted=trigger(name))


@app.route("/api/destination", methods=["POST", "DELETE"])
def api_destination():
    if request.method == "DELETE":
        hub.update(destination=None)
        return jsonify(ok=True)
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()[:120]
    if not name:
        return jsonify(ok=False, error='send {"name": "..."}'), 400
    hub.update(destination=name)
    return jsonify(ok=True)


_colors_lock = threading.Lock()


def clean_colors(raw):
    """Keep only known names with #rrggbb values."""
    if not isinstance(raw, dict):
        return {}
    return {name: value.lower() for name, value in raw.items()
            if name in COLOR_DEFAULTS["dark"] and isinstance(value, str)
            and re.fullmatch(r"#[0-9a-fA-F]{6}", value)}


def load_colors():
    try:
        hub.update(colors=clean_colors(json.loads(COLORS_PATH.read_text())))
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as err:
        print(f"[dashboard] ignoring colors.json: {err}")


@app.route("/api/colors", methods=["GET", "POST", "DELETE"])
def api_colors():
    with _colors_lock:
        if request.method == "POST":
            changes = clean_colors(request.get_json(silent=True))
            if not changes:
                return jsonify(ok=False, error='send {"background": "#112233"}'), 400
            hub.update(colors={**hub.get("colors"), **changes})
        elif request.method == "DELETE":
            hub.update(colors={})
        if request.method != "GET":
            tmp = COLORS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(hub.get("colors"), indent=2) + "\n")
            tmp.replace(COLORS_PATH)
    return jsonify(ok=True, colors=hub.get("colors"), defaults=COLOR_DEFAULTS[screen["theme"]])


listen_now = threading.Event()     # the mic button sets it, voice.py picks it up


@app.post("/api/voice/listen")
def api_voice_listen():
    """The mic button: start listening for a command (or cancel, if already listening)."""
    if hub.get("voice_state") == "off":
        return jsonify(ok=False, error=hub.get("voice_error") or "Voice is off."), 409
    listen_now.set()
    return jsonify(ok=True)


@app.post("/api/position")
def api_position():
    """The phone page sends its GPS position here while location sharing is on."""
    body = request.get_json(silent=True) or {}
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error='send {"lat": 43.6, "lon": -79.4}'), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify(ok=False, error="lat/lon out of range"), 400

    def num(key):
        try:
            return round(float(body[key]), 1)
        except (KeyError, TypeError, ValueError):
            return None
    hub.update(position={"lat": lat, "lon": lon, "accuracy": num("accuracy"), "speed": num("speed"),
                         "heading": num("heading"), "at": time.time()})
    return jsonify(ok=True)


def local_addresses():
    """This computer's IPv4 addresses, so the certificate matches whichever one the phone uses."""
    import socket
    found = {"127.0.0.1"}
    try:
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    try:    # the address used to reach the internet (no packet is actually sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            found.add(probe.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith("169.254."))


def ensure_certificate():
    """https for the phone page, from a small certificate authority (CA) of our own.

    The CA is made once (certs/ca.pem). Install it on the phone once and the phone
    then fully trusts the dashboard; iPhones refuse location to untrusted pages.
    The server certificate is remade at every start, for this computer's current
    addresses, signed by that CA."""
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    CERT_DIR.mkdir(exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_cert_path, ca_key_path = CERT_DIR / "ca.pem", CERT_DIR / "ca-key.pem"
    if ca_cert_path.exists() and ca_key_path.exists():
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Car Dashboard local CA")])
        ca_cert = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
                   .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=3650))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                                content_commitment=False, key_encipherment=False,
                                                data_encipherment=False, key_agreement=False,
                                                encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
                   .sign(ca_key, hashes.SHA256()))
        ca_key_path.write_bytes(ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
        ca_cert_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(a)) for a in local_addresses()]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "car-dashboard")]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=397))     # Apple rejects longer server certificates
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    cert_path, key_path = CERT_DIR / "server.pem", CERT_DIR / "server-key.pem"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM)
                          + ca_cert.public_bytes(serialization.Encoding.PEM))   # send the chain
    return cert_path, key_path


@app.get("/ca.crt")
def ca_certificate():
    """The phone downloads this once to trust the dashboard's https page."""
    path = CERT_DIR / "ca.pem"
    if not path.exists():
        return Response("No certificate yet: start the dashboard with https on.", status=404)
    return Response(path.read_bytes(), mimetype="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="car-dashboard-ca.crt"'})


def start_https(port):
    """Second server, same app, on https: phones only share location with secure pages."""
    from werkzeug.serving import make_server
    try:
        cert, key = ensure_certificate()
        server = make_server("0.0.0.0", port, app, threaded=True, ssl_context=(str(cert), str(key)))
    except ImportError:
        print("[dashboard] no https: pip install cryptography")
        return
    except OSError as err:
        print(f"[dashboard] no https on port {port}: {err}")
        return
    threading.Thread(target=server.serve_forever, name="https", daemon=True).start()
    for address in local_addresses():
        if address != "127.0.0.1":
            print(f"[dashboard] phone page with location: https://{address}:{port}/phone")


@app.get("/phone")
def phone_page():
    return Response(PHONE_PAGE, mimetype="text/html")


PHONE_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Car controls</title>
<style>
  :root { --bg: #eef1f4; --panel: #ffffff; --line: #d5dae1; --text: #12161b; --muted: #556070; --route: #1d6fd1; --on-route: #ffffff; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0c0f12; --panel: #151a20; --line: #252c35; --text: #f3f5f7; --muted: #9aa4b2; --route: #5aa9ff; --on-route: #0c0f12; }
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--bg); color: var(--text); padding: 24px 16px;
         font-family: Barlow, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  main { max-width: 480px; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }
  h1 { font-size: 28px; }
  #current { color: var(--muted); font-size: 18px; }
  #current b { color: var(--text); }
  input, button { font: inherit; font-size: 20px; border-radius: 14px; padding: 14px 16px; width: 100%; }
  input { background: var(--panel); color: var(--text); border: 1px solid var(--line); }
  button { border: 0; font-weight: 600; }
  #set { background: var(--route); color: var(--on-route); }
  #clear { background: var(--panel); color: var(--text); border: 1px solid var(--line); }
  #status, #colorStatus { min-height: 24px; color: var(--muted); }
  h2 { font-size: 24px; margin-top: 18px; }
  .hint { color: var(--muted); font-size: 16px; }
  #colors { display: flex; flex-direction: column; border: 1px solid var(--line); border-radius: 14px;
            background: var(--panel); overflow: hidden; }
  #colors label { display: flex; align-items: center; justify-content: space-between; gap: 12px;
                  padding: 10px 12px 10px 16px; font-size: 20px; min-height: 64px; }
  #colors label + label { border-top: 1px solid var(--line); }
  #colors .name { display: flex; flex-direction: column; }
  #colors .name small { color: var(--muted); font-size: 14px; font-variant-numeric: tabular-nums; }
  #colors input[type="color"] { width: 64px; height: 44px; padding: 0; border: 1px solid var(--line);
                                border-radius: 10px; background: none; flex-shrink: 0; cursor: pointer; }
  #colors input[type="color"]::-webkit-color-swatch-wrapper { padding: 3px; }
  #colors input[type="color"]::-webkit-color-swatch { border: 0; border-radius: 7px; }
  #reset { background: var(--panel); color: var(--text); border: 1px solid var(--line); }
  #share { background: var(--route); color: var(--on-route); }
  #share.on { background: var(--panel); color: var(--text); border: 1px solid var(--line); }
  #locStatus { min-height: 24px; color: var(--muted); }
  #locStatus a { color: var(--route); word-break: break-all; }
  #caHelp { color: var(--muted); font-size: 16px; line-height: 1.4; }
  #caHelp summary { cursor: pointer; color: var(--text); }
  #caHelp ol { margin: 8px 0 0 20px; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  #caHelp a { color: var(--route); }
</style>
</head>
<body>
<main>
  <h1>Where to?</h1>
  <div id="current">Current: <b id="dest">checking…</b></div>
  <form id="form">
    <input id="name" maxlength="120" placeholder="Address or place" autocomplete="off" enterkeyhint="go">
  </form>
  <button id="set" form="form">Set destination</button>
  <button id="clear" type="button">Clear</button>
  <div id="status"></div>

  <h2>Location</h2>
  <div class="hint">Shares this phone's GPS with the dashboard for the map. Keep this page open
    while driving; most phones stop sharing when the screen locks.</div>
  <button id="share" type="button">Share my location</button>
  <div id="locStatus"></div>
  <details id="caHelp">
    <summary>iPhone says permission refused? Install the dashboard's certificate (once)</summary>
    <ol>
      <li>In <b>Safari</b>, open <a id="caLink">the certificate</a> and tap <b>Allow</b>.</li>
      <li>Settings, <b>Profile Downloaded</b> (or General, VPN &amp; Device Management): <b>Install</b>.</li>
      <li>Settings, General, About, <b>Certificate Trust Settings</b>: turn on
        <b>Car Dashboard local CA</b>.</li>
      <li>Settings, Privacy &amp; Security, Location Services, <b>Safari Websites</b>:
        <b>While Using the App</b>.</li>
      <li>Open <a id="secureLink2">the secure page</a> again and tap Share my location.</li>
    </ol>
  </details>

  <h2>Screen colors</h2>
  <div class="hint">Changes show on the dashboard within a second.</div>
  <div id="colors"></div>
  <button id="reset" type="button">Reset colors</button>
  <div id="colorStatus"></div>
</main>
<script>
const $ = id => document.getElementById(id);
async function refresh() {
  try {
    const s = await (await fetch("/api/state", {cache: "no-store"})).json();
    $("dest").textContent = s.destination || "none";
  } catch (e) { $("dest").textContent = "dashboard not reachable"; }
}
async function send(method, body) {
  $("status").textContent = "Sending…";
  try {
    const r = await fetch("/api/destination", {method, headers: {"Content-Type": "application/json"},
                                               body: body && JSON.stringify(body)});
    $("status").textContent = r.ok ? "Done. Check the dashboard." : "The dashboard refused it.";
  } catch (e) { $("status").textContent = "Could not reach the dashboard."; }
  refresh();
}
$("form").addEventListener("submit", e => {
  e.preventDefault();
  const name = $("name").value.trim();
  if (!name) { $("status").textContent = "Type a destination first."; return; }
  send("POST", {name}).then(() => { $("name").value = ""; $("name").blur(); });
});
$("clear").addEventListener("click", () => send("DELETE"));
refresh();

const COLOR_LABELS = {
  background: "Background", panel: "Panels", border: "Borders",
  bar: "Song bar", text: "Text", route: "Directions", car: "Map arrow",
  lyric: "Lyrics: current line", lyrics: "Lyrics: other lines",
};
const pending = {};
let sendTimer = null;

function fillColors(data) {
  for (const [name, label] of Object.entries(COLOR_LABELS)) {
    let row = $("row-" + name);
    if (!row) {
      row = document.createElement("label");
      row.id = "row-" + name;
      row.innerHTML = '<span class="name"><span></span><small></small></span><input type="color">';
      row.querySelector(".name span").textContent = label;
      row.querySelector("input").addEventListener("input", e => pickColor(name, e.target.value));
      $("colors").append(row);
    }
    const custom = data.colors[name];
    const value = custom || data.defaults[name];
    row.querySelector("input").value = value;
    row.querySelector("small").textContent = custom ? value : "default";
  }
}
async function loadColors() {
  try { fillColors(await (await fetch("/api/colors", {cache: "no-store"})).json()); }
  catch (e) { $("colorStatus").textContent = "Could not reach the dashboard."; }
}
function pickColor(name, value) {
  pending[name] = value;
  $("row-" + name).querySelector("small").textContent = value;
  clearTimeout(sendTimer);
  sendTimer = setTimeout(sendColors, 150);   // the picker fires many times while dragging
}
async function sendColors() {
  const body = {...pending};
  for (const k in pending) delete pending[k];
  try {
    const r = await fetch("/api/colors", {method: "POST", headers: {"Content-Type": "application/json"},
                                          body: JSON.stringify(body)});
    $("colorStatus").textContent = r.ok ? "Saved." : "The dashboard refused that color.";
  } catch (e) { $("colorStatus").textContent = "Could not reach the dashboard."; }
}
$("reset").addEventListener("click", async () => {
  try {
    fillColors(await (await fetch("/api/colors", {method: "DELETE"})).json());
    $("colorStatus").textContent = "Back to the default colors.";
  } catch (e) { $("colorStatus").textContent = "Could not reach the dashboard."; }
});
loadColors();

// ---- location sharing (phones only allow it on https pages)
const HTTPS_PORT = 5443, HTTP_PORT = 5000;
let watchId = null, wakeLock = null, sent = 0;
function secureLink() {
  return "https://" + location.hostname + ":" + HTTPS_PORT + "/phone";
}
async function keepAwake() {
  try { if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen"); } catch (e) {}
}
async function sendPosition(pos) {
  if (watchId === null) return;     // stopped meanwhile
  const c = pos.coords;
  try {
    const r = await fetch("/api/position", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({lat: c.latitude, lon: c.longitude, accuracy: c.accuracy,
                            speed: c.speed, heading: c.heading})});
    if (!r.ok) { $("locStatus").textContent = "The dashboard refused the location (error " + r.status + ")."; return; }
    sent++;
    $("locStatus").textContent = "Sharing. Accurate to " + Math.round(c.accuracy) + " m. Updates sent: " + sent;
  } catch (e) {
    $("locStatus").textContent = "Got a location but could not reach the dashboard. Is it still running?";
  }
}
function stopSharing(note) {
  if (watchId !== null) navigator.geolocation.clearWatch(watchId);
  watchId = null;
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
  $("share").textContent = "Share my location";
  $("share").classList.remove("on");
  if (note !== undefined) $("locStatus").textContent = note;
}
function startSharing() {
  if (!window.isSecureContext) {
    $("locStatus").innerHTML = 'Phones only share location on a secure page. Open <a></a> and accept the warning once.';
    $("locStatus").querySelector("a").href = secureLink();
    $("locStatus").querySelector("a").textContent = secureLink();
    return;
  }
  if (!navigator.geolocation) { $("locStatus").textContent = "This browser can't share location."; return; }
  $("locStatus").textContent = "Asking for permission…";
  sent = 0;
  // A quick rough fix first (Wi-Fi / cell towers), then precise GPS updates as they come.
  navigator.geolocation.getCurrentPosition(sendPosition, () => {},
                                           {enableHighAccuracy: false, maximumAge: 60000, timeout: 15000});
  watchId = navigator.geolocation.watchPosition(sendPosition, err => {
    if (err.code === 1) {       // PERMISSION_DENIED: the only error worth giving up on
      stopSharing("Location permission was refused. On iPhone, follow the certificate steps below.");
      $("caHelp").open = true;
    } else if (sent === 0) {    // timeout / no fix yet: keep waiting, GPS can take a while indoors
      $("locStatus").textContent = "Waiting for a location fix… (" + err.message + ")";
    }
  }, {enableHighAccuracy: true, maximumAge: 2000, timeout: 30000});
  keepAwake();
  $("share").textContent = "Stop sharing";
  $("share").classList.add("on");
}
$("share").addEventListener("click", () => watchId === null ? startSharing() : stopSharing("Stopped."));
$("caLink").href = "http://" + location.hostname + ":" + HTTP_PORT + "/ca.crt";
$("secureLink2").href = secureLink();
document.addEventListener("visibilitychange", () => { if (watchId !== null && !document.hidden) keepAwake(); });
if (!window.isSecureContext) {
  $("locStatus").innerHTML = 'To share location, open <a></a>';
  $("locStatus").querySelector("a").href = secureLink();
  $("locStatus").querySelector("a").textContent = secureLink();
}
</script>
</body>
</html>
"""


PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard</title>
<link rel="stylesheet" href="/static/leaflet/leaflet.css">
<script src="/static/leaflet/leaflet.js"></script>
<style>
  @font-face { font-family: Barlow; font-weight: 400; src: url(/fonts/Barlow-400.woff2) format("woff2"); }
  @font-face { font-family: Barlow; font-weight: 500; src: url(/fonts/Barlow-500.woff2) format("woff2"); }
  @font-face { font-family: Barlow; font-weight: 600; src: url(/fonts/Barlow-600.woff2) format("woff2"); }
  @font-face { font-family: Barlow; font-weight: 700; src: url(/fonts/Barlow-700.woff2) format("woff2"); }

  :root, [data-theme="dark"] {
    --bg: #0c0f12; --panel: #151a20; --line: #252c35; --text: #f3f5f7; --muted: #9aa4b2;
    --accent: #ffb14a; --route: #5aa9ff; --on-route: #0c0f12; --map: #151a20; --map-line: #252c35;
    --art: #1a2028; --art-line: #2a323c; --art-icon: #5b6675; --track: #2a323c; --warn: #ff7a6e;
    --ly-far: #7f8998; --ly-next: #d3d9e1;
  }
  :root { --car: var(--route); --ly-current: var(--accent); --ly-mid: var(--muted); }
  [data-theme="light"] {
    --bg: #eef1f4; --panel: #ffffff; --line: #d5dae1; --text: #12161b; --muted: #556070;
    --accent: #a35400; --route: #1d6fd1; --on-route: #ffffff; --map: #dde2e8; --map-line: #c9d0d9;
    --art: #dfe4ea; --art-line: #c9d0d9; --art-icon: #7b8696; --track: #cfd5dd; --warn: #b3261e;
    --ly-far: #636b78; --ly-next: #2a313a;
  }
  * { box-sizing: border-box; margin: 0; }
  html, body { height: 100%; }
  body { background: var(--bg); color: var(--text); overflow: hidden; user-select: none;
         font-family: Barlow, system-ui, "Segoe UI", Roboto, "DejaVu Sans", sans-serif; }

  /* drawn at 1024x600 like the mockups, then scaled to fit the real screen */
  #screen, #lyricsPage { position: absolute; left: 50%; top: 50%; width: 1024px; height: 600px;
                         transform: translate(-50%, -50%) scale(var(--scale, 1)); }
  #screen { padding: 22px 32px 18px; display: flex; flex-direction: column; gap: 16px; }
  #art { cursor: pointer; -webkit-tap-highlight-color: transparent; }
  /* hidden, not display:none, so Leaflet keeps its size; !important beats the map's own visibility: visible */
  html.lyrics #screen, html.lyrics #screen * { visibility: hidden !important; }
  #lyricsPage { display: none; grid-template-columns: 404px 620px; }
  html.lyrics #lyricsPage { display: grid; }

  /* lyrics page: song on the left, lines on the right (design/lyrics-*.html) */
  .lyLeft { padding: 32px; display: flex; flex-direction: column; gap: 16px; border-right: 1px solid var(--line); min-width: 0; }
  #lyArt { width: 340px; height: 340px; flex-shrink: 0; border-radius: 18px; background: var(--art);
           border: 1px solid var(--art-line); display: grid; place-items: center; overflow: hidden;
           cursor: pointer; -webkit-tap-highlight-color: transparent; }
  #lyArt svg { width: 64px; height: 64px; stroke: var(--art-icon); }
  #lyArt img { width: 100%; height: 100%; object-fit: cover; display: none; }
  #lyArt.has-img svg { display: none; }
  #lyArt.has-img img { display: block; }
  #lyricsPage.nav #lyArt { width: 270px; height: 270px; }
  .lySong { display: flex; flex-direction: column; gap: 2px; }
  #lyTitle { font-size: 32px; font-weight: 700; line-height: 1.15; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #lyArtist { font-size: 22px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .lyProgress { display: flex; flex-direction: column; gap: 8px; }
  #lyBar { height: 6px; border-radius: 3px; background: var(--track); overflow: hidden; }
  #lyFill { height: 100%; width: 0; background: var(--accent); border-radius: 3px; }
  .lyTimes { display: flex; justify-content: space-between; font-size: 17px; color: var(--muted);
             font-variant-numeric: tabular-nums; }
  /* slim next-turn strip, only while following a route */
  #lyTurn { display: none; height: 64px; flex-shrink: 0; border-radius: 14px; background: var(--panel);
            border: 1px solid var(--line); padding: 0 14px; align-items: center; gap: 12px; margin-top: auto; }
  #lyricsPage.nav #lyTurn { display: flex; }
  #lyTurnIcon { width: 40px; height: 40px; border-radius: 10px; background: var(--route); flex-shrink: 0;
                display: grid; place-items: center; }
  #lyTurnIcon svg { width: 24px; height: 24px; stroke: var(--on-route); }
  #lyTurnMain { font-size: 22px; font-weight: 700; flex-grow: 1; min-width: 0;
                white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #lyTurnDist { font-size: 22px; font-weight: 600; flex-shrink: 0; }

  .lyRight { padding: 32px 40px; display: flex; flex-direction: column; justify-content: center; min-width: 0;
             position: relative; overflow: hidden; }
  #lySynced { display: flex; flex-direction: column; gap: 22px; }
  #lySynced div { font-size: 26px; font-weight: 500; color: var(--ly-far);
                  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-height: 1.2em; }
  #lySynced .l1, #lySynced .l4 { font-size: 28px; color: var(--ly-mid); }
  #lySynced .l2 { font-size: 46px; font-weight: 700; line-height: 1.12; letter-spacing: -0.01em; color: var(--ly-current);
                  white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  #lySynced .l3 { font-size: 30px; font-weight: 600; color: var(--ly-next); }
  #lyPlain { position: absolute; inset: 32px 40px; overflow: hidden; }
  #lyPlainText { font-size: 28px; font-weight: 500; line-height: 1.45; color: var(--ly-next); white-space: pre-line;
                 padding: 40% 0; transition: transform 1s linear; }
  #lyNote { position: absolute; right: 24px; top: 8px; font-size: 16px; color: var(--muted); }
  #lyMessage { display: flex; flex-direction: column; align-items: center; gap: 12px; text-align: center; }
  #lyMessage svg { width: 56px; height: 56px; stroke: var(--muted); }
  #lyMessageMain { font-size: 34px; font-weight: 700; }
  #lyMessageNote { font-size: 21px; color: var(--muted); }
  .lyRight > .hide { display: none !important; }

  header { display: flex; justify-content: space-between; align-items: center; height: 56px; flex-shrink: 0; }
  #hello { font-size: 36px; font-weight: 600; letter-spacing: -0.01em;
           white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #now { display: flex; align-items: center; gap: 22px; flex-shrink: 0; }
  #date, #weather { font-size: 21px; color: var(--muted); }
  #weather { display: flex; align-items: center; gap: 8px; }
  #weatherIcon { width: 26px; height: 26px; stroke: var(--muted); display: none; }
  #time { font-size: 38px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }

  main { display: grid; grid-template-columns: 580px 352px; gap: 28px; height: 408px; flex-shrink: 0; }
  .left { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  #map { height: 310px; border-radius: 18px; background: var(--map); border: 1px solid var(--map-line);
         position: relative; overflow: hidden; }
  #mapView { position: absolute; inset: 0; background: var(--map); visibility: hidden; }
  #map.live #mapView { visibility: visible; }
  #map.live #mapEmpty { display: none; }
  #mapEmpty { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center;
              justify-content: center; gap: 10px; text-align: center; padding: 0 40px; }
  #mapEmpty svg { width: 56px; height: 56px; stroke: var(--muted); margin-bottom: 6px; }
  #eta { position: absolute; left: 12px; top: 12px; z-index: 500; display: none; padding: 6px 12px;
         border-radius: 10px; background: var(--panel); border: 1px solid var(--line);
         font-size: 19px; font-weight: 600; font-variant-numeric: tabular-nums; }
  #eta.show { display: block; }
  .leaflet-container { background: var(--map); font-family: inherit; }
  .leaflet-control-attribution { font-size: 10px; background: rgba(0, 0, 0, 0.35) !important; color: #c8ced6; }
  .leaflet-control-attribution a { color: #c8ced6; }
  .car { width: 34px; height: 34px; }
  .car svg { width: 34px; height: 34px; filter: drop-shadow(0 1px 3px rgba(0, 0, 0, 0.5)); }
  #mapTitle { font-size: 30px; font-weight: 700; max-width: 100%;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #mapNote { font-size: 20px; color: var(--muted); }

  #turn { height: 84px; border-radius: 18px; background: var(--panel); border: 1px solid var(--line);
          padding: 0 22px; display: flex; align-items: center; gap: 18px; }
  #turnIcon { width: 52px; height: 52px; border-radius: 12px; background: var(--route); flex-shrink: 0;
              display: grid; place-items: center; }
  #turnIcon svg { width: 30px; height: 30px; stroke: var(--on-route); }
  #turnText { display: flex; flex-direction: column; gap: 2px; flex-grow: 1; min-width: 0; }
  #turnMain { font-size: 28px; font-weight: 700; line-height: 1.1;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #turnStreet { font-size: 19px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #turnDist { font-size: 26px; font-weight: 600; flex-shrink: 0; }

  .right { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  #art { height: 300px; width: 300px; border-radius: 18px; background: var(--art); border: 1px solid var(--art-line);
         display: grid; place-items: center; overflow: hidden; }
  #art svg { width: 64px; height: 64px; stroke: var(--art-icon); }
  #art img { width: 100%; height: 100%; object-fit: cover; display: none; }
  #art.has-img svg { display: none; }
  #art.has-img img { display: block; }
  .artRow { display: flex; gap: 8px; height: 300px; }
  #art { flex-shrink: 0; }
  /* volume: + / level / - in the strip right of the cover */
  #volume { width: 44px; display: flex; flex-direction: column; align-items: center; gap: 8px; }
  #volume button { width: 44px; height: 56px; flex-shrink: 0; border-radius: 12px; padding: 0;
                   border: 1px solid var(--line); background: var(--panel); display: grid; place-items: center;
                   cursor: pointer; -webkit-tap-highlight-color: transparent; }
  #volume button:active { transform: scale(0.94); background: var(--line); }
  #volume button svg { width: 22px; height: 22px; stroke: var(--text); }
  #volBar { flex-grow: 1; width: 8px; border-radius: 4px; background: var(--track); overflow: hidden;
            display: flex; flex-direction: column; justify-content: flex-end; }
  #volFill { width: 100%; height: 0; background: var(--accent); border-radius: 4px; transition: height 0.25s; }
  #volume.off { opacity: 0.35; }
  #song { display: flex; flex-direction: column; gap: 2px; }
  #title { font-size: 30px; font-weight: 700; line-height: 1.15; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #artist { font-size: 21px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #bar { height: 6px; border-radius: 3px; background: var(--track); overflow: hidden; flex-shrink: 0; }
  #fill { height: 100%; width: 0; background: var(--accent); border-radius: 3px; transition: width 1s linear; }

  footer { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 20px; height: 64px; }
  #error { font-size: 17px; color: var(--warn); line-height: 1.2; max-height: 2.4em; overflow: hidden; }
  #voiceHint { font-size: 18px; color: var(--muted); text-align: right; white-space: nowrap;
               overflow: hidden; text-overflow: ellipsis; }
  #mic { width: 64px; height: 64px; border-radius: 50%; border: 1px solid var(--line); background: var(--panel);
         display: grid; place-items: center; padding: 0; cursor: pointer; position: relative;
         -webkit-tap-highlight-color: transparent; transition: background 0.15s, border-color 0.15s; }
  #mic svg { width: 30px; height: 30px; stroke: var(--text); }
  #mic:active { transform: scale(0.95); }
  #mic.listening { background: var(--accent); border-color: var(--accent); }
  #mic.listening svg { stroke: var(--bg); }
  #mic.listening::after { content: ""; position: absolute; inset: -8px; border-radius: 50%;
                          border: 3px solid var(--accent); animation: ring 1.1s ease-out infinite; }
  #mic.working { border-color: var(--accent); }
  #mic.working svg { stroke: var(--accent); }
  #mic.off { opacity: 0.4; }
  @keyframes ring { from { transform: scale(0.9); opacity: 0.9; } to { transform: scale(1.35); opacity: 0; } }

  html.no-cursor, html.no-cursor * { cursor: none !important; }

  #toast { position: fixed; left: 50%; top: 46%; transform: translate(-50%, -50%) scale(0.96);
           background: var(--panel); border: 2px solid var(--accent); color: var(--text);
           font-size: calc(48px * var(--scale, 1)); font-weight: 650; padding: 3.5vh 5vw; border-radius: 3vh;
           opacity: 0; transition: opacity 0.18s, transform 0.18s; pointer-events: none; }
  #toast.show { opacity: 1; transform: translate(-50%, -50%) scale(1); }
</style>
</head>
<body>
<div id="screen">
  <header>
    <div id="hello">Hello</div>
    <div id="now">
      <div id="date"></div>
      <div id="time">--:--</div>
      <div id="weather">
        <svg id="weatherIcon" viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"></svg>
        <span id="temp">--&deg;</span>
      </div>
    </div>
  </header>

  <main>
    <div class="left">
      <div id="map">
        <div id="mapView"></div>
        <div id="eta"></div>
        <div id="mapEmpty">
          <svg viewBox="0 0 24 24" fill="none" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/>
          </svg>
          <div id="mapTitle">No route</div>
          <div id="mapNote">Send a destination from your phone</div>
        </div>
      </div>
      <div id="turn">
        <div id="turnIcon">
          <svg id="turnSvg" viewBox="0 0 24 24" fill="none" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"></svg>
        </div>
        <div id="turnText">
          <div id="turnMain">No destination</div>
          <div id="turnStreet">Set one on your phone</div>
        </div>
        <div id="turnDist"></div>
      </div>
    </div>

    <div class="right">
      <div class="artRow">
        <div id="art" role="button" aria-label="Song picture">
          <svg viewBox="0 0 24 24" fill="none" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>
          </svg>
          <img id="cover" alt="">
        </div>
        <div id="volume">
          <button id="volUp" type="button" aria-label="Volume up">
            <svg viewBox="0 0 24 24" fill="none" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
          </button>
          <div id="volBar"><div id="volFill"></div></div>
          <button id="volDown" type="button" aria-label="Volume down">
            <svg viewBox="0 0 24 24" fill="none" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M5 12h14"/></svg>
          </button>
        </div>
      </div>
      <div id="song">
        <div id="title">Nothing playing</div>
        <div id="artist">Start a song on your phone</div>
      </div>
      <div id="bar"><div id="fill"></div></div>
    </div>
  </main>

  <footer>
    <div id="error"></div>
    <button id="mic" type="button" aria-label="Voice command">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>
      </svg>
    </button>
    <div id="voiceHint"></div>
  </footer>
</div>

<div id="lyricsPage">
  <div class="lyLeft">
    <div id="lyArt" role="button" aria-label="Back to home">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>
      </svg>
      <img id="lyCover" alt="">
    </div>
    <div class="lySong">
      <div id="lyTitle">Nothing playing</div>
      <div id="lyArtist"></div>
    </div>
    <div class="lyProgress">
      <div id="lyBar"><div id="lyFill"></div></div>
      <div class="lyTimes"><span id="lyElapsed">0:00</span><span id="lyLength">0:00</span></div>
    </div>
    <div id="lyTurn">
      <div id="lyTurnIcon">
        <svg id="lyTurnSvg" viewBox="0 0 24 24" fill="none" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"></svg>
      </div>
      <div id="lyTurnMain"></div>
      <div id="lyTurnDist"></div>
    </div>
  </div>
  <div class="lyRight">
    <div id="lySynced" class="hide">
      <div class="l0"></div><div class="l1"></div><div class="l2"></div>
      <div class="l3"></div><div class="l4"></div><div class="l5"></div>
    </div>
    <div id="lyPlain" class="hide"><div id="lyPlainText"></div></div>
    <div id="lyNote" class="hide">Lyrics without timing</div>
    <div id="lyMessage">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>
      </svg>
      <div id="lyMessageMain">No lyrics</div>
      <div id="lyMessageNote"></div>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
const SCREEN = __SCREEN__;
const $ = id => document.getElementById(id);
const ICONS = {
  right: '<path d="M6 20v-7a4 4 0 0 1 4-4h9"/><path d="M15 5l4 4-4 4"/>',
  left: '<path d="M18 20v-7a4 4 0 0 0-4-4H5"/><path d="M9 5 5 9l4 4"/>',
  straight: '<path d="M12 20V4"/><path d="M6 10l6-6 6 6"/>',
  pin: '<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0C18.5 15.4 12 21 12 21z"/><circle cx="12" cy="10" r="2.3"/>',
  uturn: '<path d="M8 20V9a4 4 0 0 1 8 0v6"/><path d="M12 11l4 4 4-4"/>',
};
ICONS.arrive = ICONS.pin;
// Phone page color names -> the CSS variables they set
const COLOR_VARS = {
  background: ["--bg"], panel: ["--panel", "--map", "--art"], border: ["--line", "--map-line", "--art-line"],
  bar: ["--accent"], text: ["--text", "--ly-next"], route: ["--route"], car: ["--car"], lyric: ["--ly-current"],
};
// "Lyrics: other lines": the next line in that color, lines farther away fade into the background
const LYRICS_VARS = {"--ly-next": 100, "--ly-mid": 72, "--ly-far": 55};
let lastAction = null, toastTimer = null, misses = 0, shownIcon = null, shownColors = "";

const theme = new URLSearchParams(location.search).get("theme") || SCREEN.theme;
document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
const cursorParam = new URLSearchParams(location.search).get("cursor");   // ?cursor=off / ?cursor=on to try it
document.documentElement.classList.toggle("no-cursor", cursorParam ? cursorParam === "off" : !SCREEN.cursor);
$("hello").textContent = SCREEN.name ? "Hello, " + SCREEN.name : "Hello";

function fit() {
  const scale = Math.min(innerWidth / 1024, innerHeight / 600);
  document.documentElement.style.setProperty("--scale", scale);
}
function clock() {
  const now = new Date();
  $("time").textContent = now.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"}).replace(/\s?[AP]M/i, "");
  $("date").textContent = now.toLocaleDateString([], {weekday: "short", month: "short", day: "numeric"});
}
function toast(text) {
  $("toast").textContent = text;
  $("toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").classList.remove("show"), 1300);
}
const CLOUD_HIGH = '<path d="M7 14a4 4 0 0 1-.6-7.96A5.5 5.5 0 0 1 17 5.5a4.25 4.25 0 0 1 .5 8.5z"/>';
const WEATHER_ICONS = {
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  cloud: '<path d="M7 18a4 4 0 0 1-.6-7.96A5.5 5.5 0 0 1 17 9.5a4.25 4.25 0 0 1 .5 8.5z"/>',
  rain: CLOUD_HIGH + '<path d="M8 17l-1 3M12 17l-1 3M16 17l-1 3"/>',
  snow: CLOUD_HIGH + '<path d="M8 18h.01M12 20h.01M16 18h.01"/>',
  storm: CLOUD_HIGH + '<path d="M13 14l-3 4h4l-3 4"/>',
  fog: '<path d="M4 9h16M4 13h16M6 17h12"/>',
};
let shownWeather = null;
function renderWeather(w) {
  const icon = !w ? null
    : w.main === "Clear" ? (w.night ? "moon" : "sun")
    : w.main === "Clouds" ? "cloud"
    : (w.main === "Rain" || w.main === "Drizzle") ? "rain"
    : w.main === "Snow" ? "snow"
    : w.main === "Thunderstorm" ? "storm" : "fog";
  if (icon !== shownWeather) {
    $("weatherIcon").innerHTML = icon ? WEATHER_ICONS[icon] : "";
    $("weatherIcon").style.display = icon ? "block" : "none";
    shownWeather = icon;
  }
  $("temp").textContent = w ? w.temp + "°" : "--°";
}
function turnIconName(t) {
  const text = (t.instruction || "").toLowerCase();
  return t.icon || (text.includes("right") ? "right" : text.includes("left") ? "left" : "straight");
}
function turnIcon(name) {
  if (name !== shownIcon) { $("turnSvg").innerHTML = ICONS[name] || ICONS.straight; shownIcon = name; }
}
// ---- the map (Leaflet + Mapbox tiles), only when there is a Mapbox token
let map = null, routeLine = null, car = null, shownRouteId = 0, routeLoading = false;
const CAR_SVG = '<svg viewBox="0 0 34 34"><circle cx="17" cy="17" r="15" fill="#fff"/>' +
  '<circle cx="17" cy="17" r="12" style="fill: var(--car)"/><path d="M17 8l6 15-6-3.5-6 3.5z" fill="#fff"/></svg>';
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function setupMap() {
  if (map || !SCREEN.mapbox || !window.L) return;
  const style = document.documentElement.dataset.theme === "light" ? "light-v11" : "dark-v11";
  map = L.map("mapView", {zoomControl: false, dragging: false, scrollWheelZoom: false, doubleClickZoom: false,
                          boxZoom: false, keyboard: false, touchZoom: false, fadeAnimation: false});
  L.tileLayer("https://api.mapbox.com/styles/v1/mapbox/" + style + "/tiles/512/{z}/{x}/{y}@2x?access_token=" + SCREEN.mapbox,
              {tileSize: 512, zoomOffset: -1, maxZoom: 19,
               attribution: '© <a href="https://www.mapbox.com/about/maps/">Mapbox</a> © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'}).addTo(map);
  map.attributionControl.setPrefix(false);
  map.setView([43.6532, -79.3832], 12);
}
async function loadRoute(id) {
  if (routeLoading) return;
  routeLoading = true;
  try {
    const r = await (await fetch("/api/route", {cache: "no-store"})).json();
    if (routeLine) { routeLine.remove(); routeLine = null; }
    if (r.line && r.line.length) {
      const latlngs = r.line.map(([lon, lat]) => [lat, lon]);
      routeLine = L.polyline(latlngs, {color: cssVar("--route"), weight: 7, opacity: 0.95,
                                       lineCap: "round", lineJoin: "round"}).addTo(map);
      if (!car) map.fitBounds(routeLine.getBounds(), {padding: [30, 30]});
    }
    shownRouteId = r.id;
  } catch (e) {}
  routeLoading = false;
}
function renderMap(s) {
  setupMap();
  const fresh = s.position && (Date.now() / 1000 - s.position.at) < 60;
  const live = !!map && (fresh || s.route_id > 0 && !!s.nav);
  $("map").classList.toggle("live", live);
  if (!map) return;
  if (live && !$("map").dataset.sized) { map.invalidateSize(); $("map").dataset.sized = "1"; }
  if (s.route_id !== shownRouteId) loadRoute(s.route_id);
  if (fresh) {
    const at = [s.position.lat, s.position.lon];
    const heading = s.position.heading == null ? 0 : s.position.heading;
    if (!car) car = L.marker(at, {interactive: false, keyboard: false,
                                  icon: L.divIcon({className: "car", html: CAR_SVG, iconSize: [34, 34], iconAnchor: [17, 17]})}).addTo(map);
    car.setLatLng(at);
    const svg = car.getElement() && car.getElement().querySelector("svg");
    if (svg) svg.style.transform = "rotate(" + heading + "deg)";
    map.setView(at, s.nav ? 16 : 15, {animate: true});
  } else if (car) { car.remove(); car = null; }
  $("eta").classList.toggle("show", !!s.nav);
  if (s.nav) $("eta").textContent = s.nav.minutes + " min · " + s.nav.remaining;
}

function renderRoute(s) {
  renderMap(s);
  const t = s.next_turn;
  if (t) {
    turnIcon(turnIconName(t));
    $("turnMain").textContent = t.instruction || "Continue";
    $("turnStreet").textContent = t.street || "";
    $("turnDist").textContent = t.distance || "";
  } else {
    turnIcon("pin");
    $("turnMain").textContent = s.destination || "No destination";
    $("turnStreet").textContent = s.destination ? (s.nav_status || "Destination") : "Set one on your phone";
    $("turnDist").textContent = "";
  }
  $("mapTitle").textContent = s.destination || "No route";
  $("mapNote").textContent = s.nav_status || (s.destination
    ? (SCREEN.mapbox ? "Share your location from the phone page" : "Add a Mapbox token to config.json for the map")
    : "Send a destination from your phone");
}
function applyColors(colors) {
  const key = JSON.stringify(colors || {});
  if (key === shownColors) return;
  shownColors = key;
  const style = document.documentElement.style;
  for (const [name, vars] of Object.entries(COLOR_VARS))
    for (const v of vars) {
      if (colors && colors[name]) style.setProperty(v, colors[name]);
      else style.removeProperty(v);
    }
  for (const [v, pct] of Object.entries(LYRICS_VARS)) {
    if (colors && colors.lyrics) style.setProperty(v, pct === 100 ? colors.lyrics
      : "color-mix(in srgb, " + colors.lyrics + " " + pct + "%, var(--bg))");
    else if (v !== "--ly-next" || !(colors && colors.text)) style.removeProperty(v);
  }
}
const VOICE_HINTS = {
  off: "Voice is off", idle: "Tap and say a song", listening: "Listening…", working: "One moment…",
};
function renderVoice(state) {
  const mic = $("mic");
  for (const name of ["off", "listening", "working"]) mic.classList.toggle(name, state === name);
  $("voiceHint").textContent = VOICE_HINTS[state] || "";
}
// ---- the lyrics page
const LEAD_MS = 300;    // show a line a moment early: Spotify's position arrives a little late
let view = "home", viewHoldUntil = 0, lyricsId = -1, lyrics = null, lyIndex = null, lyIcon = null;
let play = {pos: 0, at: 0, playing: false, dur: 0}, hasSong = false, shownLayout = "";
const LY_MESSAGES = {
  loading: ["Finding lyrics…", ""],
  none: ["No lyrics for this song", "LRCLIB doesn't have them yet"],
  instrumental: ["Instrumental", "No words in this one"],
  error: ["No connection to the lyrics service", "Trying again shortly"],
};
function showView(v) {
  view = v === "lyrics" ? "lyrics" : "home";
  document.documentElement.classList.toggle("lyrics", view === "lyrics");
  lyIndex = null;
  lyTick();
}
function setView(v) {
  showView(v);
  viewHoldUntil = Date.now() + 2000;      // don't let a poll already on its way switch it back
  fetch("/api/action/" + v, {method: "POST"}).catch(() => toast("Dashboard not reachable"));
}
function mmss(ms) {
  const sec = Math.floor((ms || 0) / 1000);
  return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0");
}
function positionNow() {
  return play.playing ? Math.min(play.dur, play.pos + performance.now() - play.at) : play.pos;
}
function lyShow(part) {
  for (const id of ["lySynced", "lyPlain", "lyMessage"]) $(id).classList.toggle("hide", id !== part);
  $("lyNote").classList.toggle("hide", part !== "lyPlain");
}
function lyMessage(main, note) {
  lyShow("lyMessage");
  $("lyMessageMain").textContent = main;
  $("lyMessageNote").textContent = note;
}
function lyLayout() {
  shownLayout = hasSong + "|" + (lyrics ? lyrics.id : "");
  const state = lyrics ? lyrics.state : "loading";
  if (!hasSong) lyMessage("Nothing playing", "Start a song on your phone");
  else if (state === "synced") lyShow("lySynced");
  else if (state === "plain") { lyShow("lyPlain"); $("lyPlainText").textContent = lyrics.lines.join("\n"); }
  else lyMessage(...(LY_MESSAGES[state] || LY_MESSAGES.none));
  lyIndex = null;
  lyTick();
}
async function loadLyrics() {
  try {
    lyrics = await (await fetch("/api/lyrics", {cache: "no-store"})).json();
    lyLayout();
  } catch (e) { lyricsId = -1; }     // try again on the next poll
}
function lyTick() {
  if (view !== "lyrics") return;
  const pos = positionNow();
  $("lyFill").style.width = play.dur ? (100 * pos / play.dur) + "%" : "0";
  $("lyElapsed").textContent = mmss(pos);
  $("lyLength").textContent = mmss(play.dur);
  if (!hasSong || !lyrics) return;
  if (lyrics.state === "synced") {
    const lines = lyrics.lines, at = pos + LEAD_MS;
    let i = -1;
    while (i + 1 < lines.length && lines[i + 1][0] <= at) i++;
    if (i === lyIndex) return;
    lyIndex = i;
    const slots = $("lySynced").children;
    for (let k = 0; k < 6; k++) {
      const line = lines[i - 2 + k];
      slots[k].textContent = line ? (line[1] || "♪") : (k === 2 ? "♪" : "");
    }
  } else if (lyrics.state === "plain") {
    const text = $("lyPlainText"), room = $("lyPlain").clientHeight;
    const travel = Math.max(0, text.offsetHeight - room);
    text.style.transform = "translateY(" + (-travel * (play.dur ? pos / play.dur : 0)) + "px)";
  }
}
function renderLyricsPage(s) {
  hasSong = !!s.title;
  play = {pos: s.progress_ms || 0, at: performance.now(), playing: !!s.playing, dur: s.duration_ms || 0};
  $("lyTitle").textContent = s.title || "Nothing playing";
  $("lyArtist").textContent = s.title ? (s.artist || "") : "Start a song on your phone";
  const cover = $("lyCover");
  if (s.art) { if (cover.getAttribute("src") !== s.art) cover.src = s.art; }
  else cover.removeAttribute("src");
  $("lyArt").classList.toggle("has-img", !!s.art);
  const t = s.nav && s.next_turn;
  $("lyricsPage").classList.toggle("nav", !!t);
  if (t) {
    const name = turnIconName(t);
    if (name !== lyIcon) { $("lyTurnSvg").innerHTML = ICONS[name] || ICONS.straight; lyIcon = name; }
    $("lyTurnMain").textContent = t.street || t.instruction || "Continue";   // short: the strip is narrow
    $("lyTurnDist").textContent = t.distance || "";
  }
  if (s.lyrics_id !== lyricsId) { lyricsId = s.lyrics_id; loadLyrics(); }
  if (hasSong + "|" + (lyrics ? lyrics.id : "") !== shownLayout) lyLayout();
  if (Date.now() > viewHoldUntil && s.view !== view) showView(s.view);
  else lyTick();
}

function render(s) {
  applyColors(s.colors);
  renderLyricsPage(s);
  renderVoice(s.voice_state);
  $("volume").classList.toggle("off", !s.supports_volume);
  $("volFill").style.height = (s.volume == null ? 0 : s.volume) + "%";
  $("title").textContent = s.title || "Nothing playing";
  $("artist").textContent = s.title ? (s.artist || "") : "Start a song on your phone";
  const cover = $("cover");
  if (s.art) { if (cover.getAttribute("src") !== s.art) cover.src = s.art; }
  else cover.removeAttribute("src");
  $("art").classList.toggle("has-img", !!s.art);
  $("fill").style.width = s.duration_ms ? (100 * s.progress_ms / s.duration_ms) + "%" : "0";
  $("error").textContent = [s.error, s.gestures_error, s.voice_error, s.weather_error, s.nav_error].filter(Boolean).join(" · ");
  renderWeather(s.weather);
  renderRoute(s);
  if (s.last_action && s.last_action.id !== lastAction) {
    if (lastAction !== null) toast(s.last_action.label);
    lastAction = s.last_action.id;
  } else if (!s.last_action && lastAction === null) { lastAction = 0; }
}
async function tick() {
  try {
    const r = await fetch("/api/state", {cache: "no-store"});
    render(await r.json());
    misses = 0;
  } catch (e) {
    if (++misses > 2) $("error").textContent = "Dashboard stopped. Check the Pi.";
  }
}
$("art").addEventListener("click", () => setView("lyrics"));
$("lyArt").addEventListener("click", () => setView("home"));
$("lyCover").addEventListener("error", () => $("lyArt").classList.remove("has-img"));
for (const [id, action] of [["volUp", "volume_up"], ["volDown", "volume_down"]])
  $(id).addEventListener("click", () => {
    fetch("/api/action/" + action, {method: "POST"}).then(tick).catch(() => toast("Dashboard not reachable"));
  });
$("mic").addEventListener("click", async () => {
  try {
    const r = await fetch("/api/voice/listen", {method: "POST"});
    if (!r.ok) toast((await r.json()).error || "Voice is off");
    else if (!$("mic").classList.contains("listening")) renderVoice("listening");   // react before the next poll
  } catch (e) { toast("Dashboard not reachable"); }
});
$("cover").addEventListener("error", () => $("art").classList.remove("has-img"));

fit(); clock(); tick();
addEventListener("resize", fit);
setInterval(clock, 1000);
setInterval(tick, 1000);
setInterval(lyTick, 200);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- start-up

def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        return dict(DEFAULT_CONFIG), True
    try:
        cfg = {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
    except json.JSONDecodeError as err:
        sys.exit(f"config.json is not valid JSON: {err}")
    return cfg, False


def open_kiosk(port):
    browser = shutil.which("chromium-browser") or shutil.which("chromium")
    if not browser:
        print("[dashboard] Chromium not found, open the page yourself")
        return
    subprocess.Popen([browser, "--kiosk", "--noerrdialogs", "--disable-infobars",
                      f"http://localhost:{port}"])


def main():
    parser = argparse.ArgumentParser(description="Car dashboard")
    parser.add_argument("--demo", action="store_true", help="fake songs, no Spotify needed")
    parser.add_argument("--kiosk", action="store_true", help="open Chromium full screen")
    parser.add_argument("--no-gestures", action="store_true", help="don't start gestures.py (camera)")
    parser.add_argument("--no-voice", action="store_true", help="don't start voice.py (microphone)")
    args = parser.parse_args()

    cfg, created = load_config()
    screen.update(name=str(cfg["name"]), theme="light" if cfg["theme"] == "light" else "dark",
                  cursor=bool(cfg["show_cursor"]), mapbox=str(cfg["mapbox_token"]))
    load_colors()

    if args.demo:
        sp = DemoPlayer()
        print("[dashboard] demo mode, no Spotify")
    else:
        if created or not cfg["spotify_client_id"] or not cfg["spotify_client_secret"]:
            sys.exit(f"Fill in your Spotify client ID and secret in {CONFIG_PATH}, then run again.\n"
                     "To see the screen without Spotify: python3 dashboard.py --demo")
        try:
            sp = connect_spotify(cfg)
        except ImportError:
            sys.exit("Spotipy is not installed. Run: pip install spotipy")
        except Exception as err:
            sys.exit(f"Could not log in to Spotify: {err}")
        print("[dashboard] Spotify connected")

    sp_lock = threading.Lock()
    threading.Thread(target=poll_loop, args=(sp, sp_lock, cfg["poll_seconds"]),
                     name="spotify-poll", daemon=True).start()
    threading.Thread(target=action_loop, args=(sp, sp_lock, cfg["volume_step"]),
                     name="spotify-actions", daemon=True).start()
    player.update(sp=sp, lock=sp_lock)
    if cfg["openweather_api_key"]:
        threading.Thread(target=weather_loop, args=(cfg,), name="weather", daemon=True).start()
    else:
        hub.update(weather_error="Add an OpenWeather key to config.json for weather.")
    try:
        spec = importlib.util.spec_from_file_location("lyrics", HERE / "lyrics.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        lyrics["source"] = module.Lyrics(hub, demo=args.demo)
        threading.Thread(target=lyrics["source"].loop, name="lyrics", daemon=True).start()
    except ImportError:
        print("[dashboard] lyrics off: pip install requests")
    if not args.no_gestures:
        start_plugin("gestures", trigger)
    if cfg["anthropic_api_key"]:
        try:
            spec = importlib.util.spec_from_file_location("assistant", HERE / "assistant.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            city, _, country = str(cfg["weather_city"]).partition(",")
            location = {"city": city.strip(), "country": country.strip() or "CA", "timezone": cfg["timezone"]}
            assistant.update(claude=module.Assistant(cfg["anthropic_api_key"], cfg["claude_model"], location),
                             error=module.AssistantError)
            print(f"[dashboard] Claude assistant on ({cfg['claude_model']})")
        except ImportError:
            print("[dashboard] Claude off: pip install anthropic")
    if cfg["mapbox_token"]:
        spec = importlib.util.spec_from_file_location("navigation", HERE / "navigation.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        navigator["nav"] = module.Navigator(hub, cfg["mapbox_token"])
        threading.Thread(target=navigator["nav"].loop, name="navigation", daemon=True).start()
        print("[dashboard] navigation on (Mapbox)")
    if not args.no_voice:
        start_plugin("voice", trigger, play_song, hub.show, cfg["wake_phrase"], listen_now,
                     lambda state: hub.update(voice_state=state), navigate, assist,
                     navigator["nav"].speech if navigator["nav"] else None, cfg["piper_voice"])
    start_https(int(cfg["https_port"]))

    port = int(cfg["port"])
    if args.kiosk:
        threading.Timer(1.5, open_kiosk, args=(port,)).start()
    print(f"[dashboard] open http://localhost:{port}")
    # 0.0.0.0 so the phone app can reach it over the hotspot later
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
