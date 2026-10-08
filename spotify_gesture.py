#!/usr/bin/env python3
"""Control Spotify playback with hand gestures seen by your webcam.

Gestures:
    Open hand (all fingers out)        -> Play
    Fist                               -> Pause
    3 fingers (index/middle/ring) right -> Next track
    3 fingers (index/middle/ring) left  -> Previous track

Press 'q' in the preview window to quit.
"""

import argparse
import math
import os
import time
import urllib.request

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision
from dotenv import load_dotenv

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/latest/hand_landmarker.task"
)
HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "hand_landmarker.task")

# Load SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET / SPOTIPY_REDIRECT_URI from .env next to this script.
load_dotenv(os.path.join(HERE, ".env"))

# MediaPipe hand landmark indices
WRIST = 0
FINGERS = {  # name: (pip, tip)
    "index": (6, 8),
    "middle": (10, 12),
    "ring": (14, 16),
    "pinky": (18, 20),
}
INDEX_MCP, PINKY_MCP = 5, 17

STABLE_FRAMES = 6  # frames a gesture must be held before it fires
COOLDOWN_S = 1.0  # minimum time between actions


def dist(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def finger_extended(lm, name):
    """A finger is extended if its tip is clearly farther from the wrist than its PIP joint."""
    pip, tip = FINGERS[name]
    return dist(lm[tip], lm[WRIST]) > dist(lm[pip], lm[WRIST]) * 1.15


def classify(lm):
    """Return 'play', 'pause', 'next', 'previous' or None for a list of 21 landmarks."""
    ext = {name: finger_extended(lm, name) for name in FINGERS}

    if not any(ext.values()):
        return "pause"

    if all(ext.values()):
        return "play"

    if ext["index"] and ext["middle"] and ext["ring"] and not ext["pinky"]:
        # Direction the fingers point: from the knuckles to the middle fingertip.
        knuckles_x = (lm[INDEX_MCP].x + lm[PINKY_MCP].x) / 2
        knuckles_y = (lm[INDEX_MCP].y + lm[PINKY_MCP].y) / 2
        dx = lm[12].x - knuckles_x
        dy = lm[12].y - knuckles_y
        if abs(dx) > abs(dy) * 1.2:  # mostly horizontal
            return "next" if dx > 0 else "previous"

    return None


class Spotify:
    def __init__(self, dry_run=False):
        self.sp = None
        if dry_run:
            return
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth

        self.sp = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                scope="user-modify-playback-state user-read-playback-state",
                redirect_uri=os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
            )
        )

    def do(self, action):
        print(f"-> {action}")
        if self.sp is None:
            return
        try:
            if action == "play":
                self.sp.start_playback()
            elif action == "pause":
                self.sp.pause_playback()
            elif action == "next":
                self.sp.next_track()
            elif action == "previous":
                self.sp.previous_track()
        except Exception as e:  # no active device, already paused, etc.
            print(f"   Spotify error: {e}")


def ensure_model():
    if not os.path.exists(MODEL_PATH):
        print("Downloading hand landmark model...")
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--camera", type=int, default=0, help="camera index (default 0)")
    parser.add_argument("--dry-run", action="store_true", help="detect gestures without calling Spotify")
    parser.add_argument("--no-window", action="store_true", help="don't show the preview window")
    args = parser.parse_args()

    ensure_model()
    spotify = Spotify(dry_run=args.dry_run)

    landmarker = vision.HandLandmarker.create_from_options(
        vision.HandLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(model_asset_path=MODEL_PATH),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=0.6,
            min_tracking_confidence=0.5,
        )
    )

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        raise SystemExit(f"Could not open camera {args.camera}")

    candidate, count = None, 0
    last_fired, last_fired_at = None, 0.0
    last_ts = 0
    print("Running. Press 'q' in the window (or Ctrl+C) to quit.")

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame = cv2.flip(frame, 1)  # mirror so "right" means your right
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            ts = max(int(time.monotonic() * 1000), last_ts + 1)
            last_ts = ts
            result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)

            gesture = None
            if result.hand_landmarks:
                lm = result.hand_landmarks[0]
                gesture = classify(lm)
                h, w = frame.shape[:2]
                for p in lm:
                    cv2.circle(frame, (int(p.x * w), int(p.y * h)), 4, (0, 255, 0), -1)

            # Debounce: gesture must be stable for a few frames.
            if gesture == candidate:
                count += 1
            else:
                candidate, count = gesture, 1

            now = time.monotonic()
            if candidate is None and count >= STABLE_FRAMES:
                last_fired = None  # hand gone / neutral: allow the same gesture to fire again
            elif (
                candidate is not None
                and count >= STABLE_FRAMES
                and candidate != last_fired
                and now - last_fired_at > COOLDOWN_S
            ):
                spotify.do(candidate)
                last_fired, last_fired_at = candidate, now

            if not args.no_window:
                cv2.putText(frame, gesture or "-", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 2)
                cv2.imshow("Spotify Gestures", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        landmarker.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
