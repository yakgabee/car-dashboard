# Car Pi dashboard

A DIY car dashboard on a Raspberry Pi: map with the next direction, the song
that is playing, hands-free gesture control of the music, and a local Ollama
voice assistant. This file is the handoff from the planning conversation. It
records what was decided, what exists, and what is still open.

## How the owner wants to work

- Build one stage at a time. Do not write the whole project in one go.
- Plan in lists first, then build the stage that is agreed.
- Keep it a single Python program that is simple to run on the Pi.
- Always push finished work to `main` too (the owner pulls from `main`), not
  only to the working branch. No need to ask first.

## Hardware

- Have: Raspberry Pi 4 (8GB), camera with a built-in microphone.
- Ordered: 7-inch display. Resolution and whether it is a touchscreen are not
  confirmed. The mockups assume 1024x600.
- Still needed: 5V/3A USB-C car power supply, good microSD card, case with fan
  or heatsink, speaker or aux cable for spoken replies, mounts.
- Maybe: USB GPS dongle, power board for clean shutdown with the ignition.

## What exists

### dashboard.py

One process. Flask serves a full-screen page shown in Chromium kiosk mode, and
background threads do the work so a slow network call never freezes the screen.

- `Hub`: shared state the page reads from `/api/state` once a second.
- `poll_loop`: polls Spotify playback, backs off on errors and rate limits.
- `action_loop`: runs playback actions from a queue.
- `trigger(action)`: the entry point for gestures. Actions are `play`, `pause`,
  `play_pause`, `next`, `previous`, `volume_up`, `volume_down`. Repeats are
  ignored for a moment (1s, or 0.3s for volume). `play`/`pause` skip the
  Spotify call when it is already in that state.
- `gestures.py` (stage 2, done): the owner's `spotify_gesture.py` from
  `Documents\CarPi\HAND GESTURE`, wrapped as `run(trigger)` and started in a
  thread. MediaPipe hand landmarker (`hand_landmarker.task` sits next to it),
  camera `"camera"` from config.json (default 0; the owner's OBS Virtual
  Camera on the PC is 1) at 640x480. Open hand = play, fist = pause, two fingers
  (index + middle) pointing right/left = next/previous. A gesture must hold 6 frames to be
  recognised, then is carried out 1 s later (`DELAY_S`, owner asked: a
  delay, not a 1 s hold), and fires once until the hand drops or changes
  (`Gestures` class). The preview window shows what is waiting. Problems (library missing, no camera) show
  on screen via `Hub.gestures_error`. `python3 gestures.py` is a stand-alone
  test with a preview window. Needs mediapipe (install.sh), 64-bit Raspberry
  Pi OS, and the libegl1 + libgles2 system libraries (mediapipe 1.x loads
  libEGL.so.1 and libGLESv2.so.2; found in a fresh-install smoke test). Not yet tested on the Pi.
  No volume gestures yet.
- Weather (stage 3, done): `weather_loop` polls OpenWeather current weather
  every 10 min for `weather_city` (Toronto,CA). Key in config.json as
  `openweather_api_key`. Header shows an icon and the temperature; problems go
  to `Hub.weather_error`. Sunrise/sunset not used yet.
- `voice.py` (owner asked for it ahead of stage 6, done for music only):
  Vosk (`models/vosk-model-small-en-us-0.15`) listens on the default mic;
  wake phrase from config `wake_phrase` ("hey bitch"), with sound-alikes in
  `SOUNDS_LIKE`. "<wake> play <song> [by <artist>]" calls dashboard
  `play_song(query)` (Spotify search: title+artist fields, then plain query,
  then title alone; first hit). Also pause/stop, resume, next/skip,
  previous/go back. Replies spoken with Piper (`models/en_US-lessac-medium`).
  Mic audio is ignored while it speaks. `python3 voice.py` tests it without
  Spotify; `--say` and `--file` too. No Ollama questions yet.
  The small Vosk model mishears unusual names; Spotify's fuzzy search covers
  a lot of it. Loud music in the car will hurt recognition.
- Mic button, now the Claude page's talk button (bottom center of the screen, in a footer row with the error
  text on the left and a voice hint on the right): `POST /api/voice/listen`
  sets `listen_now`; voice.py beeps and takes the next sentence (8 s) as the
  command. After the button, anything that is not a command is treated as a
  song name. A second tap cancels. `Hub.voice_state` (off/idle/listening/
  working) drives the button. Added because the wake phrase was unreliable
  in a noisy room. `"wake_phrase": ""` in config.json turns the wake phrase
  off. To fit the footer the map is 310px tall and the song picture 300x300.
- Claude assistant (`assistant.py`, stage 5 part D, done early): voice
  requests the fixed phrases don't cover go to Claude via the Anthropic SDK
  (`anthropic_api_key`, `claude_model` = `claude-haiku-5-5` as the owner chose,
  effort low, JSON-schema output). Claude picks one action: play_song,
  control, navigate, answer, none, using `car_state()` (time, song and
  position in it, destination, weather). Errors go to `Hub.voice_error` and
  voice falls back to the simple rules. Claude never routes; Mapbox will.
  Tested live: fixes misheard names ("lick lie" -> Lykke Li), ignores chatter.
- Claude web search + memory (owner asked): decision action `search` ->
  `Assistant.search()` makes a second request with the `web_search_20250305`
  server tool (max 2 searches, user_location from config city/timezone;
  chosen over web_search_20260209 because it answered in ~3 s vs ~6.5 s in
  one test), then cleans the cited pieces and drops repeated sentences.
  About a cent per searched question. Memory: `Assistant.history` is
  append-only (full response.content kept); it restarts after 5 min of
  silence or 8 exchanges rather than trimming. A search answer is passed to
  the next turn as a note. `car_state()` includes the full date.
  One test run once hung for minutes on the network with no error and one
  call hit a native TLS "access violation" on Python 3.12; neither repeated.
- Voice choice: `piper_voice` in config.json (any Piper voice in models/).
  Default now `en_US-ryan-medium` (owner chose US male). `en_US-ryan-high`
  is also downloaded but took ~4.7 s per sentence on the PC (too slow for a
  Pi); medium takes ~1 s.
- Spotify device (owner hit it: "Playing Quebec by Drake" shown, nothing
  played): `play_song` and the play action now name the device. `pick_device`
  takes the active device, else one whose name contains config
  `"spotify_device"` (e.g. "iPhone"), else the first; none at all ->
  "Open Spotify on your phone or computer first." (`NoDevice`). After
  `start_playback`, `play_song` checks with `is_playing()` (8 x 0.75 s; the
  same URI, the `linked_from` URI or the same name counts, since Spotify may
  play a relinked copy) and only then says "Playing X by Y on <device>.".
  Second round (owner's PC still ignored it): an idle device first gets
  `transfer_playback(force_play=False)` + 1 s, and if the song still hasn't
  started the play command is sent once more. On failure it prints what
  Spotify reported to the log and says "Press play once in Spotify there,
  then ask again." The resume action uses `transfer_playback(force_play=True)`
  for an idle device. Tested against fakes (ok, needs transfer, relinked,
  slow 4 s, needs two commands, never) and demo; NOT yet confirmed on the
  owner's PC.
- Voice navigation: "take me to / navigate to / directions to <place>" sets
  the destination (`navigate()`).
- Map and turn-by-turn (stage 5, done): `navigation.py` `Navigator` thread
  watches `Hub.destination` and `Hub.position`. Mapbox Search Box API finds
  the place near the car, Directions API `driving-traffic` gives the route.
  Each position is projected onto the route line: next maneuver, distance to
  it, remaining distance/time -> `Hub.next_turn` (`instruction`, `street`,
  `distance`, `icon`) and `Hub.nav`. Off route (>60 m, 3 updates) reroutes,
  <35 m from the end announces arrival and clears the destination. Turns are
  spoken at ~500 m and ~120 m through `Navigator.speech`, which voice.py
  drains between commands. The screen draws it with Leaflet (bundled in
  `static/leaflet`, served by our own `/static` route; Flask's built-in static
  route is off) over Mapbox raster tiles (dark-v11/light-v11), route line from
  `/api/route` when `Hub.route_id` changes, car arrow rotated by heading,
  "N min · X km" chip. Token: `mapbox_token` in config.json (public pk.,
  also given to the page). Tested with a simulated drive; not yet in a car.
- Phone location (owner chose the website over the Expo app): the phone page
  has "Share my location" (watchPosition + screen wake lock) posting to
  `POST /api/position` -> `Hub.position`. Phones only allow geolocation on
  https, so `start_https()` serves the same app on `https_port` 5443.
  iPhone refused location (PERMISSION_DENIED) with a plain self-signed cert,
  so `ensure_certificate()` now keeps a local CA (`certs/ca.pem`, made once)
  and signs a fresh server cert at every start for this machine's current
  IPv4 addresses (397 days, serverAuth, chain sent). The phone installs the
  CA once from `http://<ip>:5000/ca.crt` and enables full trust (steps are on
  the phone page). Keep `certs/ca-key.pem` private: a phone that trusts the CA
  would trust anything signed with it. Not yet confirmed on the owner's
  iPhone. Sharing stops when the phone locks.
- Volume control (owner asked for on-screen buttons here): a 44px strip right
  of the 300px cover art with +, a level bar (`Hub.volume`) and -, posting
  `volume_up`/`volume_down`. Dimmed when the device reports no remote volume
  (iPhones usually). Each change flashes "Volume N%".
- Lyrics page (stage 4, done): `lyrics.py` `Lyrics` thread watches the song
  in `Hub` and asks LRCLIB (`/api/get` with title, first artist, album and
  length, then `/api/search` with the title as is and without " - Remastered",
  "(feat. ...)" etc., preferring synced lyrics within 4 s of the song length).
  Remembers the last 50 songs. `Hub.lyrics_id`/`lyrics_state` (loading,
  synced, plain, instrumental, none, error; network errors retry after 30 s);
  the lines are at `GET /api/lyrics`, fetched by the page only when the id
  changes. The page follows `design/lyrics-*.html`: current line large in the
  accent color, two lines before and three after, positions estimated between
  polls (200 ms tick, lines shown 300 ms early). Plain lyrics scroll with the
  song's progress and say "Lyrics without timing". `Hub.view` ("home" or
  "lyrics") is kept on the server so a reload, voice or a gesture all agree:
  tap the song picture to switch, `POST /api/action/lyrics|home|toggle_view`,
  voice "show lyrics" / "show the map" (not "go home", which sounds like
  navigation), Claude's `control` can pick `lyrics`/`home`. While a route is
  active a slim next-turn strip (icon, street, distance) sits under the
  progress bar and the picture shrinks to 270px. `--demo` has fake synced
  lyrics for "Night Drive", plain for "Third Track", none for the other.
  Tested in demo mode at 800x480, 1024x600, 1280x720 and the lookup against
  canned LRCLIB replies; lrclib.net itself was blocked in the build sandbox,
  so NOT yet tested against the real service.
- Claude page (owner asked): the footer button (now a mic again, see below) opens it; Claude's colour is an
  orange spark, `--claude` #d97757 dark / #c15f3c light) and opens a third
  view, `Hub.view` = "assistant" (`#aiPage`: Back, title, the last 3
  questions and answers, newest large at the bottom, and a big talk button).
  The talk button posts `/api/voice/listen` as the mic button did. Requests
  started from the button are answered as TEXT: voice.py `Listener.reply()`
  sends (question, answer) to dashboard `chat_reply()` -> `Hub.chat` (last 6)
  instead of speaking; controls like "pause" show "Done.", "Didn't hear
  anything" shows too. The wake phrase still answers out loud. Toasts are
  not shown on the Claude page. Tested with a stand-in mic/speaker and
  screenshots with injected answers; NOT tested with a real microphone.
- Spotify button / search mode on the Claude page: built, then removed (owner:
  "forget about the whole spotify thing, just keep to Claude"). The Claude
  page has one button again and `play_song` is one function as before.
  Kept: the home footer button is a microphone (owner asked) that opens the
  Claude page.
- Auto-reload after a restart: `screen["boot"]` (start time) goes into the
  page's SCREEN and into every `/api/state`; when they differ the page calls
  `location.reload()`. Added because the owner restarted dashboard.py with
  the browser tab still open and kept running the old page. Tested: one reload, no loop.
- Plug-ins load through `start_plugin(name, *args)`; `--no-gestures` and
  `--no-voice` turn them off.
- HTTP: `POST /api/action/<name>`, `POST|DELETE /api/destination`.
- `/phone`: a small page for the phone's browser to set or clear the
  destination until the Expo app exists. Follows the phone's light/dark mode.
  It also has "Screen colors": pickers for background, panels, borders, song
  bar, text, directions, map arrow (`car`, follows directions until set) and
  lyrics (`lyric` = current line, follows the song bar; `lyrics` = other
  lines, the next one in that color and farther ones faded toward the
  background with CSS color-mix). `GET|POST|DELETE /api/colors` (`#rrggbb` only),
  saved to `colors.json` and sent to the screen in `/api/state` as `colors`.
  Unset colors fall back to the theme; "Reset colors" clears them all.
- Start on boot (owner asked): `install_autostart.sh` (run once on the Pi;
  `--remove` undoes it) writes `~/.config/autostart/car-dashboard.desktop`
  (XDG autostart, honoured by the Pi desktop on labwc, wayfire and X11) and
  turns off screen blanking with `raspi-config nonint do_blanking 1`.
  `start.sh` holds a `flock` on `logs/start.lock` (one copy only), runs
  dashboard.py with `.venv/bin/python` if present (Pi OS blocks plain pip),
  restarts it 5 s after it stops, waits up to 120 s for the page, then runs
  Chromium `--kiosk` (chromium-browser or chromium; `--password-store=basic`
  avoids the keyring prompt) and reopens it if closed. Logs in `logs/`.
  Needs desktop autologin, an existing config.json and a cached Spotify
  login. Tested in the sandbox with a stand-in Chromium (launch, single
  copy, restart); NOT tested on a Pi.
- `requirements.txt` lists every pip package (`pip install -r requirements.txt`).
  `install.sh` (owner asked) does the whole setup: apt (python3-venv,
  python3-dev, libportaudio2, libgl1, libegl1, libgles2, curl, Chromium if missing), `.venv`,
  pip in groups so one failure (usually mediapipe) doesn't stop the rest,
  then an import check with a summary. Its package list mirrors
  requirements.txt; keep them in step. No `opencv-python`: mediapipe pulls
  `opencv-contrib-python`, and two cv2 packages clash. Tested in the sandbox
  (x86_64, apt faked): all groups installed and imported except sounddevice,
  which needs the apt PortAudio library. NOT tested on a Pi.
- `--demo` runs with fake songs and no Spotify. `--kiosk` opens Chromium.
- First run writes `config.json` (Spotify client ID and secret go there).
- `.gitattributes` keeps `*.sh` at LF so the scripts still run if the repo
  passes through the owner's Windows PC.
- Pi readiness smoke test (sandbox, x86_64, no config.json, real modules from
  install.sh): every page and endpoint answers on 5000 and https 5443; with
  the apt libraries present, gestures stops only at "camera 0 not found" and
  voice at "Error querying device -1" (no mic), both shown on screen.

Tested: demo mode, the HTTP endpoints, the gesture plug-in, error handling
against a stand-in for Spotipy, layout at 800x480, 1024x600 and 1280x720.
NOT tested: the real Spotify login, real album art, anything on the Pi.

Stage 1 (home page layout) is done. The page matches `design/home-*.html`:
greeting (`"name"` in config.json), date and time, weather placeholder `--°`,
a "No route" map panel showing the destination name, a next-direction strip,
song picture, title, artist and progress bar. No on-screen buttons; the toast
still confirms each gesture. Spotify errors show in red under the progress bar.
- Theme: `"theme": "dark" | "light"` in config.json, or `?theme=light`.
- Layout is drawn at 1024x600 and scaled to fit the screen.
- Barlow is bundled in `fonts/` (OFL license included) and served at `/fonts/`.
- `Hub` has `next_turn` (`{"instruction", "street", "distance"}`), unused until
  stage 5. The strip already renders it and picks a left/right/straight icon.
- Tapping the song picture opens the lyrics page (stage 4).

### design/

Static HTML copies of the agreed mockups, at 1024x600. Open them in a browser.
Treat them as the visual target.

- `home-dark.html`, `home-light.html`
- `lyrics-dark.html`, `lyrics-light.html`
- `sketch.jpg`: the owner's original pencil sketch and notes.

Text in [square brackets] is a placeholder for live data.

## Target UI

Home page:
- Top left: greeting, "Hello, Gabriel".
- Top right: date, time, weather.
- Left: map with the route and current position, and a "next direction" strip
  under it (turn icon, instruction, street, distance).
- Right: song picture, song name, artist, thin progress bar.
- No on-screen playback buttons. Gestures control playback.

Lyrics page:
- Left: song picture, song name, artist, progress bar.
- Right: lyrics, current line large, lines before and after dimmer.

Navigation: tapping the song picture opens the lyrics page, tapping it again
returns home. If the display is not a touchscreen, use a gesture instead.

Suggested but not decided: a slim next-direction strip on the lyrics page so a
turn is not missed, and automatic light/dark switching at sunrise and sunset
(OpenWeather returns both times).

Design tokens (font: Barlow, fall back to system sans; bundle it locally
because the car may be offline):

| Token            | Dark      | Light     |
|------------------|-----------|-----------|
| Background       | `#0c0f12` | `#eef1f4` |
| Panel            | `#151a20` | `#ffffff` |
| Panel border     | `#252c35` | `#d5dae1` |
| Text             | `#f3f5f7` | `#12161b` |
| Muted text       | `#9aa4b2` | `#556070` |
| Accent (music)   | `#ffb14a` | `#a35400` |
| Route / turn     | `#5aa9ff` | `#1d6fd1` |
| Map ground       | `#151a20` | `#dde2e8` |
| Map streets      | `#222932` | `#ffffff` |

## Build order

1. Rearrange the home page to match the design.
2. Plug the owner's gesture code in through `gestures.py`.
3. Weather (OpenWeather) and the greeting. Done.
4. Lyrics page and the tap to switch. Done.
5. Map: done (phone page for destination and position, Mapbox route, Leaflet map).
6. Voice: music by voice is done (`voice.py`). Questions to Ollama next.
7. Car install: power and clean shutdown are handled by the owner (don't
   plan them). Start on boot: done (`start.sh`, `install_autostart.sh`).
   The dash mounting plate is still open.

## Constraints already researched

- Spotify (rules changed in February 2026): the app owner needs Premium or the
  app stops working. Redirect URI must be `http://127.0.0.1:8888/callback`
  (not `localhost`). All Player endpoints are still available. Search is
  capped at 10 results. Many older Spotipy tutorials use removed endpoints.
  Changelog: https://developer.spotify.com/documentation/web-api/references/changes/february-2026
- Spotipy only remote-controls a player. Music plays on the phone, which must
  have Spotify open. iPhones usually refuse remote volume changes, so the
  volume gestures may do nothing there. The code checks `supports_volume`.
- Lyrics: Spotify's API has none. LRCLIB is the planned source (free,
  time-synced for many songs, not all). The page needs a "no lyrics" state.
- Google Maps cannot be embedded as turn-by-turn navigation. Use a routing
  API (OpenRouteService or Mapbox) and draw the route ourselves.
- The Pi has no GPS. Either a USB GPS dongle (reliable) or the Expo app
  streaming the phone's location (background location is flaky).
- Internet in the car comes from the phone's hotspot.
- Ollama on a Pi 4 runs small models only and answers take several seconds.
  Hand tracking and Ollama compete for CPU, so keep camera resolution low.
- Voice needs speech-to-text (Vosk or Whisper tiny) and text-to-speech
  (Piper). The camera's microphone is the input.
- Pi 4 needs a proper 5V/3A supply and a clean shutdown, or the SD card will
  eventually corrupt.

## Open decisions

- Is the 7-inch display a touchscreen, and what is its resolution?
- Location source: the phone page (chosen). A USB GPS dongle stays the fallback if it proves flaky.
- Gestures for volume up/down (play, pause, next, previous are decided).
- Assistant start: wake phrase chosen ("hey bitch").
- Direction strip on the lyrics page: added, only while a route is active
  (chosen while building; easy to drop).
- Whether light/dark switches automatically.

## Conventions

- Keep secrets in `config.json` and never commit it (see `.gitignore`).
- Every feature gets its own worker thread and writes to `Hub`. Nothing slow
  runs in a request handler or in the gesture loop.
- Every network call needs a timeout and a readable on-screen error state.
- The screen must stay readable at a glance: large type, few elements.
