# CarPi – Spotify Gesture Control

Control Spotify playback with hand gestures using your webcam.

| Gesture | Action |
|---|---|
| ✋ Open hand (all fingers out) | Play |
| ✊ Fist | Pause |
| 👉 Index + middle + ring fingers pointing **right** | Next track |
| 👈 Index + middle + ring fingers pointing **left** | Previous track |

Each gesture fires once. To repeat it (for example, to skip twice), drop your hand or change gestures, then make it again.

## Setup

1. Install dependencies (Python 3.9–3.12 recommended):
   ```bash
   pip install -r requirements.txt
   ```
2. Create an app at <https://developer.spotify.com/dashboard> and add the redirect URI `http://127.0.0.1:8888/callback`.
3. Copy `.env.example` to `.env` and fill in the **Client ID** and **Client secret** from your app's Settings page:
   ```
   SPOTIPY_CLIENT_ID=your_client_id_here
   SPOTIPY_CLIENT_SECRET=your_client_secret_here
   SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
   ```
   `.env` is in `.gitignore`, so your secret won't be committed.

Controlling playback through the Spotify API requires **Spotify Premium**. Spotify must already be open and playing (or recently played) on a device.

## Run

```bash
python spotify_gesture.py            # webcam 0, with preview window
python spotify_gesture.py --dry-run  # test gestures without touching Spotify
python spotify_gesture.py --camera 1 --no-window
```

On the first run, a browser opens to log in to Spotify, and the hand-tracking model (~8 MB) is downloaded. Press `q` in the preview window to quit.
