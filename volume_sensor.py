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
The same with the whole Pi board drawn, to find the pins: docs/volume-sensor-pi.png

dashboard.py starts this when "volume_sensor": true is in config.json.
Test it on its own (prints the distance and the volume it would set):
    .venv/bin/python volume_sensor.py
Always "no echo"? The low-level test pings the sensor itself and says what it sees:
    .venv/bin/python volume_sensor.py --raw
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


def run(set_volume, cfg, show=None):
    """Called by dashboard.py in a thread. set_volume(percent) changes Spotify's volume.
    show(cm), if given, gets every reading (cm, or None for no echo): the stand-alone test prints them."""
    sensor = open_sensor(int(cfg.get("sensor_trigger_pin", 23)), int(cfg.get("sensor_echo_pin", 24)))
    follower = Follower(float(cfg.get("volume_near_cm", 5)), float(cfg.get("volume_far_cm", 35)),
                        bool(cfg.get("volume_reverse", False)))
    print("[volume sensor] watching")
    while True:
        cm = sensor.distance * 100      # gpiozero gives metres, 1.0 when nothing is in range
        cm = cm if cm < 99 else None
        if show:
            show(cm)
        volume = follower.update(cm, time.monotonic())
        if volume is not None:
            set_volume(volume)
        time.sleep(READ_EVERY_S)


def raw_test(trigger_pin, echo_pin, pings=10):
    """Sends pings by hand (lgpio, no gpiozero) and reports what the Echo pin does."""
    import lgpio
    chip = None
    for number in (0, 4):           # Pi 4: gpiochip0 (Pi 5: gpiochip4)
        try:
            chip = lgpio.gpiochip_open(number)
            break
        except Exception:
            pass
    if chip is None:
        print("Could not open the Pi's GPIO chip.")
        return
    try:
        lgpio.gpio_claim_output(chip, trigger_pin, 0)
        lgpio.gpio_claim_input(chip, echo_pin)
        time.sleep(0.1)
        answered = stuck = 0
        for i in range(pings):
            if lgpio.gpio_read(chip, echo_pin):
                stuck += 1
                print(f"ping {i + 1}: Echo is already HIGH before the ping")
                time.sleep(0.2)
                continue
            lgpio.gpio_write(chip, trigger_pin, 1)
            time.sleep(0.00001)
            lgpio.gpio_write(chip, trigger_pin, 0)
            start = end = None
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 0.06:
                high = lgpio.gpio_read(chip, echo_pin)
                if high and start is None:
                    start = time.perf_counter()
                elif not high and start is not None:
                    end = time.perf_counter()
                    break
            if start is None:
                print(f"ping {i + 1}: no answer")
            else:
                answered += 1
                cm = ((end or time.perf_counter()) - start) * 34300 / 2
                print(f"ping {i + 1}: answered, about {cm:.0f} cm")
            time.sleep(0.2)
    finally:
        lgpio.gpiochip_close(chip)
    print()
    if answered:
        print(f"The sensor answered {answered} of {pings} pings: the wiring works.")
        print("Run .venv/bin/python volume_sensor.py again; if that still says no echo, tell Claude.")
    elif stuck:
        print("Echo stays HIGH all the time. GPIO 24 is seeing a voltage it shouldn't:")
        print("check that row 13 only has the Pi pin 18 wire and the two resistor legs,")
        print("and that the 2 kOhm resistor really goes to the GND row (17).")
    else:
        print("The sensor never answered. One of these is not connected:")
        print("  power: Pi pin 2 -> row 3 -> sensor VCC, and Pi pin 6 -> row 17 -> sensor GND")
        print("  ping:  Pi pin 16 -> row 6 -> sensor Trig")
        print("  echo:  sensor Echo -> row 9 -> 1 kOhm -> row 13 -> Pi pin 18")
        print("Push every wire end fully onto its pin; the sensor's pins are short.")


def main():
    import json
    import os
    import sys
    cfg = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
    if "--raw" in sys.argv:
        print("Low-level sensor test: hold your hand about 20 cm above the sensor.")
        print("Stop the dashboard first (it uses the same pins).")
        raw_test(int(cfg.get("sensor_trigger_pin", 23)), int(cfg.get("sensor_echo_pin", 24)))
        return
    print("Volume sensor test: move your hand over the sensor. Ctrl+C to quit.")
    print("Stop the dashboard first (it uses the same pins).")
    print("Nothing in front of the sensor shows 'no echo'; a hand should show its distance.")
    last = [0.0]

    def show(cm):     # twice a second is enough to read
        if time.monotonic() - last[0] >= 0.5:
            last[0] = time.monotonic()
            print(f"distance: {'no echo (nothing within 1 m)' if cm is None else f'{cm:.0f} cm'}")

    try:
        run(lambda v: print(f"   -> volume {v}%"), cfg, show)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
