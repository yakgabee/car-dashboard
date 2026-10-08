#!/usr/bin/env python3
"""Hand gestures for the car dashboard, from spotify_gesture.py.

    Open hand (all fingers out)         -> play
    Fist                                -> pause
    2 fingers (index/middle) right      -> next
    2 fingers (index/middle) left       -> previous

Each gesture fires once. To repeat it (for example, to skip twice), drop your
hand or change gestures, then make it again.

dashboard.py starts this by calling run(trigger). It has no window there.
To test the camera on its own, with a preview window and no Spotify:
    python3 gestures.py              # press q in the window to quit
    python3 gestures.py --camera 1
"""

import argparse
import math
import os
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

STABLE_FRAMES = 6  # frames a gesture must be held before it fires
COOLDOWN_S = 1.0  # minimum time between actions
MAX_FAILED_READS = 30  # camera frames in a row that may fail before giving up


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

    if ext["index"] and ext["middle"] and not ext["ring"] and not ext["pinky"]:
        # Direction the fingers point: from the knuckles to the middle fingertip.
        knuckles_x = (lm[INDEX_MCP].x + lm[PINKY_MCP].x) / 2
        knuckles_y = (lm[INDEX_MCP].y + lm[PINKY_MCP].y) / 2
        dx = lm[12].x - knuckles_x
        dy = lm[12].y - knuckles_y
        if abs(dx) > abs(dy) * 1.2:  # mostly horizontal
            return "next" if dx > 0 else "previous"

    return None


def open_camera(index):
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise RuntimeError(f"camera {index} not found")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_SIZE[0])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_SIZE[1])
    return cap


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

    candidate, count = None, 0
    last_fired, last_fired_at = None, 0.0
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
                on_gesture(candidate)
                last_fired, last_fired_at = candidate, now

            if show:
                cv2.putText(frame, gesture or "-", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 2)
                cv2.imshow("Gesture test", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        cap.release()
        landmarker.close()
        if show:
            cv2.destroyAllWindows()


def run(trigger):
    """Called by dashboard.py in a background thread. Gesture names match the dashboard's actions."""
    print(f"[gestures] watching camera {CAMERA_INDEX}")
    watch(trigger)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--camera", type=int, default=CAMERA_INDEX, help="camera index (default 0)")
    args = parser.parse_args()
    print("Gesture test: no Spotify. Press q in the window (or Ctrl+C) to quit.")
    try:
        watch(lambda name: print(f"-> {name}"), camera=args.camera, show=True)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
