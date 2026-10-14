#!/usr/bin/env python3
"""Spotify volume from the height of your hand over an ultrasonic sensor (HC-SR04).

    Hand about 5 cm above the sensor   ->  0 %
    Hand about 35 cm above             ->  100 %
    No hand (or farther away)          ->  volume stays where it is

The hand has to stay in range for a moment before the volume follows it, so a
hand passing over the sensor changes nothing. Change the range in config.json:
"volume_near_cm", "volume_far_cm"; "volume_reverse": true makes closer = louder.

Wiring (HC-SR04 runs on 5 V, the Pi's pins take 3.3 V, so the Echo line needs
a voltage divider or the Pi can be damaged):

    HC-SR04 VCC   -> Pi pin 2  (5 V)
    HC-SR04 GND   -> Pi pin 6  (GND)
    HC-SR04 Trig  -> Pi pin 16 (GPIO 23)          "sensor_trigger_pin": 23
    HC-SR04 Echo  -> 1 kOhm -> Pi pin 18 (GPIO 24) "sensor_echo_pin": 24
                     and from GPIO 24 -> 2 kOhm -> GND

Breadboard picture with step-by-step instructions: docs/volume-sensor-wiring.png

dashboard.py starts this when "volume_sensor": true is in config.json.
Test it on its own (prints the distance and the volume it would set):
    .venv/bin/python volume_sensor.py
"""

import statistics
import time

READ_EVERY_S = 0.1      # 10 readings a second
SMOOTH = 5              # median of the last 5 readings: one bad echo doesn't move the volume
SETTLE_READINGS = 4     # the hand must be in range this many readings in a row (~0.4 s)
MIN_STEP = 4            # only send a change of at least this many percent...
MIN_GAP_S = 0.4         # ...and at most this often (Spotify rate-limits)


def volume_for(cm, near, far, reverse=False):
    """Distance in cm -> volume 0-100. near and far are the ends of the range."""
    share = (cm - near) / (far - near)
    share = max(0.0, min(1.0, share))
    if reverse:
        share = 1 - share
    return round(share * 100)


class Follower:
    """Turns distance readings into volume changes. No hardware here, so it can be tested."""

    def __init__(self, near, far, reverse=False):
        self.near, self.far, self.reverse = near, far, reverse
        self.recent = []
        self.in_range = 0
        self.sent, self.sent_at = None, 0.0

    def update(self, cm, now):
        """Feed one reading (cm, or None for no echo). Returns a volume to set, or None."""
        self.recent = (self.recent + [cm if cm is not None else float("inf")])[-SMOOTH:]
        smooth = statistics.median(self.recent)
        # a little slack at both ends so the hand can reach 0 % and 100 %
        if not (self.near - 3 <= smooth <= self.far + 5):
            self.in_range = 0
            return None
        self.in_range += 1
        if self.in_range < SETTLE_READINGS:
            return None
        target = volume_for(smooth, self.near, self.far, self.reverse)
        if self.sent is not None and abs(target - self.sent) < MIN_STEP and target not in (0, 100):
            return None
        if target == self.sent or now - self.sent_at < MIN_GAP_S:
            return None
        self.sent, self.sent_at = target, now
        return target


def open_sensor(trigger_pin, echo_pin):
    from gpiozero import DistanceSensor      # ImportError -> "Volume sensor off: gpiozero is not installed"
    return DistanceSensor(echo=echo_pin, trigger=trigger_pin, max_distance=1.0)


def run(set_volume, cfg):
    """Called by dashboard.py in a thread. set_volume(percent) changes Spotify's volume."""
    sensor = open_sensor(int(cfg.get("sensor_trigger_pin", 23)), int(cfg.get("sensor_echo_pin", 24)))
    follower = Follower(float(cfg.get("volume_near_cm", 5)), float(cfg.get("volume_far_cm", 35)),
                        bool(cfg.get("volume_reverse", False)))
    print("[volume sensor] watching")
    while True:
        cm = sensor.distance * 100      # gpiozero gives metres, 1.0 when nothing is in range
        volume = follower.update(cm if cm < 99 else None, time.monotonic())
        if volume is not None:
            set_volume(volume)
        time.sleep(READ_EVERY_S)


def main():
    import json
    import os
    cfg = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
    print("Volume sensor test: move your hand over the sensor. Ctrl+C to quit.")
    try:
        run(lambda v: print(f"   -> volume {v}%"), cfg)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
