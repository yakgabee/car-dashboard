#!/usr/bin/env python3
"""Hand gestures for the car dashboard, from spotify_gesture.py.

    Open hand (all fingers out)         -> play
    Fist                                -> pause
    2 fingers (index/middle) right      -> next
    2 fingers (index/middle) left       -> previous
    Peace sign (2 fingers up)           -> switch hands-free / on-screen buttons

Each gesture is carried out one second after it is recognised. Each gesture
fires once. To repeat it (for example, to skip twice), drop your
hand or change gestures, then make it again.

dashboard.py starts this by calling run(trigger). It has no window there.
To test the camera on its own, with a preview window and no Spotify
(on the Pi use the .venv Python, from a terminal on the Pi's desktop):
    .venv/bin/python gestures.py              # press q in the window to quit
    .venv/bin/python gestures.py --camera 1
    .venv/bin/python gestures.py --check      # does MediaPipe run on this computer? (no camera needed)
Without --camera it uses "camera" from config.json, like the dashboard.
"""

import argparse
import json
import math
import os
import sys
import time

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "hand_landmarker.task")

CAMERA_INDEX = 0
CAMERA_SIZE = (640, 480)    # low on purpose: the Pi also runs the dashboard and, later, voice

# MediaPipe hand landmark indices
WRIST = 0
FINGERS = {  # name: (pip, tip)
    "index": (6, 8),
    "middle": (10, 12),
    "ring": (14, 16),
    "pinky": (18, 20),
}
INDEX_MCP, PINKY_MCP = 5, 17

STABLE_FRAMES = 6  # frames a gesture must be held before it counts as recognised
DELAY_S = 1.0  # a recognised gesture is carried out this many seconds later
COOLDOWN_S = 1.0  # minimum time between recognised gestures
MAX_FAILED_READS = 30  # camera frames in a row that may fail before giving up


def dist(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def finger_extended(lm, name):
    """A finger is extended if its tip is clearly farther from the wrist than its PIP joint."""
    pip, tip = FINGERS[name]
    return dist(lm[tip], lm[WRIST]) > dist(lm[pip], lm[WRIST]) * 1.15


def classify(lm):
    """Return 'play', 'pause', 'next', 'previous', 'peace' or None for a list of 21 landmarks."""
    ext = {name: finger_extended(lm, name) for name in FINGERS}

    if not any(ext.values()):
        return "pause"

    if all(ext.values()):
        return "play"

    if ext["index"] and ext["middle"] and not ext["ring"] and not ext["pinky"]:
        # Direction the fingers point: from the knuckles to the middle fingertip.
        knuckles_x = (lm[INDEX_MCP].x + lm[PINKY_MCP].x) / 2
        knuckles_y = (lm[INDEX_MCP].y + lm[PINKY_MCP].y) / 2
        dx = lm[12].x - knuckles_x
        dy = lm[12].y - knuckles_y
        if abs(dx) > abs(dy) * 1.2:  # mostly horizontal
            return "next" if dx > 0 else "previous"
        if -dy > abs(dx) * 1.2:      # mostly up (image y grows downward): a peace sign
            return "peace"

    return None


class Gestures:
    """Turns per-frame readings into actions: recognise a held gesture, carry it out DELAY_S later."""

    def __init__(self):
        self.candidate, self.count = None, 0
        self.last_fired, self.last_fired_at = None, float("-inf")
        self.waiting = []   # (due time, gesture), oldest first

    def update(self, gesture, now):
        """Feed one frame's reading. Returns the gestures whose second is up (usually none)."""
        if gesture == self.candidate:
            self.count += 1
        else:
            self.candidate, self.count = gesture, 1
        if self.candidate is None and self.count >= STABLE_FRAMES:
            self.last_fired = None  # hand gone / neutral: allow the same gesture to fire again
        elif (self.candidate is not None and self.count >= STABLE_FRAMES
              and self.candidate != self.last_fired and now - self.last_fired_at > COOLDOWN_S):
            self.last_fired, self.last_fired_at = self.candidate, now
            self.waiting.append((now + DELAY_S, self.candidate))
        due = [g for t, g in self.waiting if t <= now]
        self.waiting = [(t, g) for t, g in self.waiting if t > now]
        return due


def try_camera(index):
    """The camera at index if it opens and sends a picture, else None."""
    # Linux (the Pi): ask V4L2 directly; the default may pick a backend that can't open USB cameras
    cap = cv2.VideoCapture(index, cv2.CAP_V4L2) if sys.platform.startswith("linux") else cv2.VideoCapture(index)
    if cap.isOpened():
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_SIZE[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_SIZE[1])
        for _ in range(10):     # some cameras send a few empty frames while starting
            if cap.read()[0]:
                return cap
            time.sleep(0.05)
    cap.release()
    return None


def dashboard_running():
    """True if start.sh (the dashboard) is running; it holds the camera then."""
    try:
        import fcntl
        with open(os.path.join(HERE, "logs", "start.lock")) as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
    except BlockingIOError:
        return True
    except (OSError, ImportError):
        return False


def open_camera(index):
    """The configured camera, or else the first one that works (a config.json copied
    from the PC may name the PC's camera, e.g. OBS Virtual Camera = 1)."""
    cap = try_camera(index)
    if cap:
        return cap
    for other in range(10):
        if other != index and (cap := try_camera(other)):
            print(f"[gestures] camera {index} didn't work, using camera {other}. "
                  f'Put "camera": {other} in config.json.')
            return cap
    if dashboard_running():
        raise RuntimeError(f"camera {index} is busy: the dashboard is running and using it. Stop it first")
    raise RuntimeError(f"camera {index} not found (no working camera; is it plugged in?)")


def make_landmarker():
    if not os.path.exists(MODEL_PATH):
        raise RuntimeError("hand_landmarker.task is missing next to gestures.py")
    return vision.HandLandmarker.create_from_options(
        vision.HandLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(model_asset_path=MODEL_PATH),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=0.6,
            min_tracking_confidence=0.5,
        )
    )


def watch(on_gesture, camera=CAMERA_INDEX, show=False):
    """Read the camera and call on_gesture(name) once per held gesture. Runs until the camera fails or q."""
    landmarker = make_landmarker()
    cap = open_camera(camera)

    gestures = Gestures()
    last_ts = 0
    failed = 0

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                failed += 1
                if failed >= MAX_FAILED_READS:
                    raise RuntimeError("camera stopped sending pictures")
                time.sleep(0.05)
                continue
            failed = 0
            frame = cv2.flip(frame, 1)  # mirror so "right" means your right
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            ts = max(int(time.monotonic() * 1000), last_ts + 1)
            last_ts = ts
            result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)

            gesture = None
            if result.hand_landmarks:
                lm = result.hand_landmarks[0]
                gesture = classify(lm)
                if show:
                    h, w = frame.shape[:2]
                    for p in lm:
                        cv2.circle(frame, (int(p.x * w), int(p.y * h)), 4, (0, 255, 0), -1)

            now = time.monotonic()
            for name in gestures.update(gesture, now):
                on_gesture(name)

            if show:
                label = gesture or "-"
                if gestures.waiting:
                    label += f"  ({gestures.waiting[0][1]} in {gestures.waiting[0][0] - now:.1f}s)"
                cv2.putText(frame, label, (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 2)
                cv2.imshow("Gesture test", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        cap.release()
        landmarker.close()
        if show:
            cv2.destroyAllWindows()


def configured_camera():
    """The "camera" number from config.json next to this file, or 0."""
    try:
        with open(os.path.join(HERE, "config.json")) as f:
            return int(json.load(f).get("camera", CAMERA_INDEX))
    except (OSError, ValueError, TypeError):
        return CAMERA_INDEX


def run(trigger, camera=CAMERA_INDEX):
    """Called by dashboard.py in a background thread. Gesture names match the dashboard's actions."""
    print(f"[gestures] watching camera {camera}")
    watch(trigger, camera=camera)


def check():
    """Run the hand model once on a blank picture. dashboard.py runs this in a separate process
    first, because a MediaPipe built for newer CPUs kills the whole program ("Illegal instruction")."""
    import numpy as np
    landmarker = make_landmarker()
    blank = np.zeros((96, 96, 3), dtype=np.uint8)
    landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=blank), 1)
    landmarker.close()
    print(f"MediaPipe {mp.__version__} works.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--camera", type=int, default=None, help='camera index (default: "camera" in config.json, else 0)')
    parser.add_argument("--check", action="store_true", help="only check that MediaPipe runs on this computer (no camera)")
    args = parser.parse_args()
    if args.check:
        check()
        return
    if args.camera is None:
        args.camera = configured_camera()
    if sys.platform.startswith("linux"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            sys.exit("No screen to show the window on. Run this in a terminal on the Pi's desktop, not over SSH.")
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")     # OpenCV's window only knows X11; the Pi desktop runs it via XWayland
    print(f"Using camera {args.camera}.")
    print("Gesture test: no Spotify. Press q in the window (or Ctrl+C) to quit.")
    try:
        watch(lambda name: print(f"-> {name}"), camera=args.camera, show=True)
    except KeyboardInterrupt:
        pass
    except RuntimeError as err:
        sys.exit(f"Problem: {err}.")


if __name__ == "__main__":
    main()
