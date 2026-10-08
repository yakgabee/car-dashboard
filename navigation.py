"""Turn-by-turn navigation for the car dashboard, with Mapbox.

    destination (phone page, voice, Claude)  ->  Search Box API finds the place
    phone's position + place                 ->  Directions API (driving-traffic) gives the route
    each new position                        ->  where we are on the route, the next turn,
                                                 rerouting when off the route, arrival

The position comes from the phone page ("Share my location"). Nothing here
blocks the screen: dashboard.py runs Navigator.loop() in its own thread.
Needs "mapbox_token" (a public pk. token) in config.json.
"""

import math
import queue
import time

import requests

SEARCH_URL = "https://api.mapbox.com/search/searchbox/v1/forward"
DIRECTIONS_URL = "https://api.mapbox.com/directions/v5/mapbox/driving-traffic/{start};{end}"

POSITION_STALE_S = 60   # older positions are not used
OFF_ROUTE_M = 60        # farther than this from the route line counts as off route...
OFF_ROUTE_TIMES = 3     # ...this many updates in a row -> reroute
REROUTE_GAP_S = 20      # at most one reroute per this many seconds
RETRY_S = 30            # after a failed search or route, wait before trying again
ARRIVED_M = 35          # this close to the end of the route = arrived
ANNOUNCE_FAR_M = 500    # "In 500 metres, turn right onto ..."
ANNOUNCE_NEAR_M = 120   # "Turn right onto ..."


class NavError(Exception):
    """A navigation problem, worded for the screen and the speaker."""


# ---------------------------------------------------------------- geometry

def metres(a, b):
    """Distance between two (lon, lat) points. Flat-earth is plenty at city scale."""
    lat = math.radians((a[1] + b[1]) / 2)
    dx = math.radians(b[0] - a[0]) * math.cos(lat) * 6371000
    dy = math.radians(b[1] - a[1]) * 6371000
    return math.hypot(dx, dy)


def project(point, line, cum):
    """Closest spot on the route to point: (metres off the route, metres along the route)."""
    lat = math.radians(point[1])
    kx, ky = math.cos(lat) * 6371000 * math.pi / 180, 6371000 * math.pi / 180

    def xy(p):
        return (p[0] - point[0]) * kx, (p[1] - point[1]) * ky

    best_off, best_along = float("inf"), 0.0
    for i in range(len(line) - 1):
        ax, ay = xy(line[i])
        bx, by = xy(line[i + 1])
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length2))
        off = math.hypot(ax + t * dx, ay + t * dy)
        if off < best_off:
            best_off, best_along = off, cum[i] + t * (cum[i + 1] - cum[i])
    return best_off, best_along


def say_distance(m):
    """Distance for the screen: 350 m, 1.2 km."""
    if m < 1000:
        return f"{max(10, round(m / 10) * 10):.0f} m"
    return f"{m / 1000:.1f} km"


def speak_distance(m):
    """Distance for the speaker: 500 metres, 1.5 kilometres."""
    if m < 1000:
        return f"{max(10, round(m / 50) * 50):.0f} metres"
    return f"{m / 1000:.1f} kilometres".replace(".0 ", " ")


# ---------------------------------------------------------------- Mapbox calls

def search(query, near, token):
    """Find a place or address near (lon, lat). Returns {"name", "address", "lon", "lat"}."""
    params = {"q": query, "limit": 1, "access_token": token}
    if near:
        params["proximity"] = f"{near[0]},{near[1]}"
    r = requests.get(SEARCH_URL, params=params, timeout=8)
    if r.status_code == 401:
        raise NavError("Mapbox refused the token.")
    r.raise_for_status()
    features = r.json().get("features") or []
    if not features:
        raise NavError(f"Couldn't find {query}.")
    f = features[0]
    lon, lat = f["geometry"]["coordinates"]
    p = f.get("properties") or {}
    return {"name": p.get("name") or query, "address": p.get("full_address") or p.get("place_formatted") or "",
            "lon": lon, "lat": lat}


def short_instruction(step):
    """A few words for the direction strip, from Mapbox's maneuver type and modifier."""
    m = step["maneuver"]
    kind, side = m.get("type", ""), m.get("modifier", "")
    if kind == "arrive":
        return "Arrive"
    if kind == "depart":
        return "Head out"
    if kind in ("roundabout", "rotary", "roundabout turn"):
        return "Roundabout"
    if kind == "fork":
        return f"Keep {side.replace('slight ', '')}" if side else "Keep going"
    if kind == "merge":
        return "Merge"
    if kind == "off ramp":
        return "Take the exit"
    if kind == "on ramp":
        return "Take the ramp"
    if side == "uturn":
        return "U-turn"
    if side and side != "straight":
        return f"Turn {side}"
    return "Continue"


def icon_for(step):
    m = step["maneuver"]
    side = m.get("modifier", "")
    if m.get("type") == "arrive":
        return "arrive"
    if side == "uturn":
        return "uturn"
    if "left" in side:
        return "left"
    if "right" in side:
        return "right"
    return "straight"


def directions(start, end, token):
    """Driving route with traffic from start to end, both (lon, lat)."""
    url = DIRECTIONS_URL.format(start=f"{start[0]},{start[1]}", end=f"{end[0]},{end[1]}")
    r = requests.get(url, params={"steps": "true", "geometries": "geojson", "overview": "full",
                                  "access_token": token}, timeout=10)
    if r.status_code == 401:
        raise NavError("Mapbox refused the token.")
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise NavError("No driving route found.")
    route = data["routes"][0]
    line = route["geometry"]["coordinates"]
    cum = [0.0]
    for a, b in zip(line, line[1:]):
        cum.append(cum[-1] + metres(a, b))
    steps = route["legs"][0]["steps"]
    # where each step starts along the line (step lengths, scaled to our own line length)
    scale = cum[-1] / max(1.0, sum(s["distance"] for s in steps))
    starts, total = [], 0.0
    for s in steps:
        starts.append(total)
        total += s["distance"] * scale
    return {"line": line, "cum": cum, "length": cum[-1], "duration": route["duration"],
            "steps": steps, "starts": starts}


# ---------------------------------------------------------------- the worker

class Navigator:
    """Watches Hub's destination and position, keeps Hub's route and next turn up to date."""

    def __init__(self, hub, token):
        self.hub, self.token = hub, token
        self.speech = queue.Queue(maxsize=5)    # sentences for voice.py to speak
        self.query = None                       # the destination text we are working on
        self.place = None                       # what the search found
        self.route = None
        self.route_id = 0
        self.failed_at = 0.0
        self.planned_at = 0.0
        self.off_count = 0
        self.announced = set()                  # (step index, "far"/"near")

    def say(self, text):
        try:
            self.speech.put_nowait(text)
        except queue.Full:
            pass

    def geometry(self):
        """For the screen's /api/route: the line to draw."""
        route = self.route
        return {"id": self.route_id, "line": route["line"] if route else []}

    def loop(self):
        while True:
            try:
                self.tick()
            except Exception as err:     # keep navigating no matter what
                self.hub.update(nav_error=f"Navigation problem: {str(err)[:80]}")
                self.failed_at = time.time()
            time.sleep(1)

    def fresh_position(self):
        p = self.hub.get("position")
        if p and time.time() - p["at"] < POSITION_STALE_S:
            return p
        return None

    def clear(self):
        self.place, self.route, self.off_count = None, None, 0
        self.announced = set()
        self.route_id += 1
        self.hub.update(next_turn=None, nav=None, nav_status=None, nav_error=None, route_id=self.route_id)

    def tick(self):
        destination = self.hub.get("destination")
        if destination != self.query:
            self.query = destination
            self.failed_at = 0.0
            self.clear()
        if not destination:
            return
        position = self.fresh_position()
        if self.route is None:
            if position is None:
                self.hub.update(nav_status="Waiting for your location. Share it from the phone page.")
                return
            if time.time() - self.failed_at > RETRY_S:
                self.plan(position)
            return
        if position is not None:
            self.follow(position)

    def plan(self, position, rerouting=False):
        here = (position["lon"], position["lat"])
        self.hub.update(nav_status="Rerouting…" if rerouting else f"Finding {self.query}…")
        try:
            if self.place is None:
                self.place = search(self.query, here, self.token)
            route = directions(here, (self.place["lon"], self.place["lat"]), self.token)
        except NavError as err:
            self.hub.update(nav_error=str(err), nav_status=None)
            self.failed_at = time.time()
            if not rerouting:
                self.say(str(err))
            return
        except requests.RequestException:
            self.hub.update(nav_error="No connection to Mapbox.", nav_status=None)
            self.failed_at = time.time()
            return
        self.route, self.planned_at, self.off_count = route, time.time(), 0
        self.announced = set()
        self.route_id += 1
        self.hub.update(route_id=self.route_id, nav_status=None, nav_error=None)
        minutes = max(1, round(route["duration"] / 60))
        if rerouting:
            self.say("Rerouting.")
        else:
            self.hub.show(f"Route to {self.place['name']}")
            self.say(f"Route to {self.place['name']}. {minutes} minute{'s' if minutes != 1 else ''}, "
                     f"{speak_distance(route['length'])}.")
        self.follow(position)

    def follow(self, position):
        route = self.route
        here = (position["lon"], position["lat"])
        off, along = project(here, route["line"], route["cum"])

        # off the route for a while: plan again from here
        limit = max(OFF_ROUTE_M, position.get("accuracy") or 0)
        self.off_count = self.off_count + 1 if off > limit else 0
        if self.off_count >= OFF_ROUTE_TIMES and time.time() - self.planned_at > REROUTE_GAP_S:
            self.plan(position, rerouting=True)
            return

        remaining = route["length"] - along
        if remaining < ARRIVED_M:
            name = self.place["name"]
            self.say(f"You have arrived at {name}.")
            self.hub.show("You have arrived")
            self.hub.update(destination=None)     # next tick clears the route
            return

        steps, starts = route["steps"], route["starts"]
        current = max(i for i, s in enumerate(starts) if s <= along + 1)
        upcoming = min(current + 1, len(steps) - 1)
        step = steps[upcoming]
        to_turn = max(0.0, starts[upcoming] - along)
        street = step.get("name") or ""
        if step["maneuver"].get("type") == "arrive":
            street = self.place["name"]

        sentence = step["maneuver"].get("instruction", "").rstrip(".")
        if to_turn <= ANNOUNCE_NEAR_M and (upcoming, "near") not in self.announced:
            self.announced.update({(upcoming, "near"), (upcoming, "far")})
            self.say(sentence + ".")
        elif (to_turn <= ANNOUNCE_FAR_M and (upcoming, "far") not in self.announced
              and to_turn > ANNOUNCE_NEAR_M + 150):
            self.announced.add((upcoming, "far"))
            self.say(f"In {speak_distance(to_turn)}, {sentence[:1].lower() + sentence[1:]}.")

        left = max(0.0, remaining / max(1.0, route["length"]))
        self.hub.update(
            next_turn={"instruction": short_instruction(step), "street": street,
                       "distance": say_distance(to_turn), "icon": icon_for(step)},
            nav={"name": self.place["name"], "remaining": say_distance(remaining),
                 "minutes": max(1, round(route["duration"] * left / 60))},
            nav_status=None,
        )
