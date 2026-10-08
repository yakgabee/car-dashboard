# CarPi – project context for Claude

## What's in this repo vs. what isn't
- This repo only contains the webcam hand-gesture Spotify controller (`spotify_gesture.py`).
- The **car dashboard** (web server, phone page, HTTPS certs, voice models, `config.json`)
  lives only on the user's PC and is **not in GitHub**. A cloud session can't see or run it.
  Ask the user before assuming any dashboard file exists here.

## Dashboard status (as of 2026-10-08, from a local session on the PC)
- Runs on the PC at `172.20.10.5`: HTTP on port 5000, HTTPS on port 5443. The phone page is `https://<host>:5443/phone`.
- **iPhone location sharing works.** The dashboard has its own small local certificate authority
  ("Car Dashboard local CA"). The iPhone installs it from `http://<host>:5000/ca.crt` (Safari only),
  then: install the profile → turn on full trust under General → About → Certificate Trust Settings
  → set Safari Websites location to "While Using the App". These steps are also shown on the phone page.
- The CA private key is `certs/ca-key.pem` on the PC. Keep it private.
- Moving to the Pi: the Pi generates its own CA unless the `certs/` folder is copied over.
  Without the copy, the iPhone trust steps have to be repeated with the Pi's address.
- iPhone stops sharing location when the screen locks, so the page must stay open while driving.
- A destination can be set (Claude turns requests like "find coffee on the way home" into a destination).
  The phone's position arrives (about 19 m accuracy seen).

## Next step: map/navigation stage (not built yet; UI shows "Map not connected yet")
Planned features: map with route + car position, next turn (street + distance), automatic rerouting,
spoken turns ("In 300 metres, turn right onto…"), arrival.

Decision pending between:
- **Mapbox**: needs a free account and a public token (`pk.…`). Has live traffic and nicer maps.
- **OpenStreetMap, no key**: Nominatim (geocoding), OSRM public server (routing + steps), OSM tiles.
  No traffic. These are community servers that ask for light use. Switching to Mapbox later is a small change.

Claude must **not** generate turn-by-turn directions itself. It has no map data, and wrong directions are unsafe while driving.

## Working across machines
- Preferred: **Remote Control**. The local session on the PC keeps running and is opened from
  claude.ai/code or the phone app. This keeps access to the webcam, mic, speakers, Spotify and the running dashboard.
- Moving the dashboard to a cloud session would require:
  - putting it in a private GitHub repo;
  - keeping secrets out (config.json, the Spotify login, certs);
  - dropping the 120 MB "Ryan high" voice model, which is over GitHub's 100 MB limit.
  A cloud session also has no hardware, so it can only edit code.
