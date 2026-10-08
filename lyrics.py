"""Song lyrics for the car dashboard, from LRCLIB (https://lrclib.net).

    song changes (Hub title/artist/album/duration)  ->  LRCLIB /api/get, then /api/search
    time-synced lyrics ("[01:02.34] line")           ->  list of (ms, text) the page follows
    plain lyrics only                                ->  lines without times
    nothing found                                    ->  "No lyrics for this song"

Free, no key. Nothing here blocks the screen: dashboard.py runs Lyrics.loop()
in its own thread and the page reads the result from /api/lyrics.
"""

import re
import threading
import time
from collections import OrderedDict

import requests

GET_URL = "https://lrclib.net/api/get"
SEARCH_URL = "https://lrclib.net/api/search"
HEADERS = {"User-Agent": "CarPiDashboard/1.0 (https://github.com/yakgabee/car-dashboard)"}
TIMEOUT_S = 6
RETRY_S = 30            # after a network problem, try the same song again this much later
CACHE_SONGS = 50        # songs remembered, so going back to a song needs no new request
DURATION_SLACK_S = 4    # a search result this close in length counts as the same recording

LRC_TIME = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
# " - Remastered 2011", " (feat. X)", " [Live]": LRCLIB often lists the plain title
TITLE_EXTRAS = re.compile(r"\s+-\s+.*$|\s*[\(\[](?:feat|ft|with|remaster|live|radio|explicit)[^\)\]]*[\)\]]", re.I)

# Fake lyrics for --demo, keyed by title. Times are in ms.
DEMO_LYRICS = {
    "Night Drive": {"state": "synced", "lines": [
        [0, ""], [4000, "Headlights on the empty road"], [9000, "City fading in the mirror"],
        [14000, "Radio low, the window down"], [19000, "Every mile a little clearer"],
        [25000, ""], [30000, "We don't need to know the way"], [35000, "Just the hum beneath the wheels"],
        [40000, "Keep on driving through the night"], [45000, "Until the morning feels real"],
        [52000, ""], [60000, "Headlights on the empty road"], [65000, "City fading in the mirror"],
        [70000, "Radio low, the window down"], [75000, "Every mile a little clearer"],
    ]},
    "Third Track": {"state": "plain", "lines": [
        "This song only has plain lyrics", "so there are no times on the lines", "",
        "The page scrolls through them slowly", "as the song plays", "",
        "That is the best guess we can make", "without timing from LRCLIB",
    ]},
}


def parse_lrc(text):
    """'[00:12.34] words' lines -> [[ms, words], ...] sorted by time. A line can carry several times."""
    lines = []
    for raw in text.splitlines():
        stamps = LRC_TIME.findall(raw)
        if not stamps:
            continue
        words = LRC_TIME.sub("", raw).strip()
        for minutes, seconds in stamps:
            lines.append([int(minutes) * 60000 + round(float(seconds) * 1000), words])
    lines.sort(key=lambda line: line[0])
    return lines


def from_record(record):
    """An LRCLIB record -> {"state", "lines"}."""
    if record.get("instrumental"):
        return {"state": "instrumental", "lines": []}
    if record.get("syncedLyrics"):
        lines = parse_lrc(record["syncedLyrics"])
        if lines:
            return {"state": "synced", "lines": lines}
    if record.get("plainLyrics"):
        return {"state": "plain", "lines": record["plainLyrics"].strip().splitlines()}
    return {"state": "none", "lines": []}


def lookup(title, artist, album, duration_s):
    """Ask LRCLIB. Returns {"state", "lines"}. Raises requests exceptions on network problems."""
    artist = (artist or "").split(", ")[0]          # the Hub joins several artists with ", "
    if album and duration_s:                        # exact match: fastest and most reliable
        r = requests.get(GET_URL, headers=HEADERS, timeout=TIMEOUT_S, params={
            "track_name": title, "artist_name": artist, "album_name": album, "duration": duration_s})
        if r.status_code == 200:
            found = from_record(r.json())
            if found["state"] != "none":
                return found
        elif r.status_code != 404:
            r.raise_for_status()
    best = None
    for name in dict.fromkeys([title, TITLE_EXTRAS.sub("", title).strip()]):     # as is, then cleaned
        if not name:
            continue
        r = requests.get(SEARCH_URL, headers=HEADERS, timeout=TIMEOUT_S,
                         params={"track_name": name, "artist_name": artist})
        r.raise_for_status()
        records = r.json() or []
        # synced first, then the length closest to the song's
        records.sort(key=lambda rec: (not rec.get("syncedLyrics"),
                                      abs((rec.get("duration") or 0) - duration_s) if duration_s else 0))
        for rec in records:
            if duration_s and rec.get("duration") and abs(rec["duration"] - duration_s) > DURATION_SLACK_S:
                continue
            found = from_record(rec)
            if found["state"] in ("synced", "plain", "instrumental"):
                if found["state"] == "synced":
                    return found
                best = best or found
        if best:
            return best
    return {"state": "none", "lines": []}


class Lyrics:
    """Watches the Hub for a new song and fetches its lyrics. Writes lyrics_id/lyrics_state;
    the page fetches the lines from /api/lyrics when lyrics_id changes."""

    def __init__(self, hub, demo=False):
        self.hub, self.demo = hub, demo
        self.lock = threading.Lock()
        self.current = {"id": 0, "song": None, "state": "none", "lines": []}
        self.cache = OrderedDict()
        self.changed = threading.Event()

    def data(self):
        """What /api/lyrics returns."""
        with self.lock:
            return dict(self.current)

    def _publish(self, song, state, lines):
        with self.lock:
            self.current = {"id": self.current["id"] + 1, "song": song, "state": state, "lines": lines}
            lyrics_id = self.current["id"]
        self.hub.update(lyrics_id=lyrics_id, lyrics_state=state)

    def _song(self):
        s = self.hub.snapshot()
        if not s["title"] or not s["album"]:        # nothing playing, or a podcast episode
            return None
        return (s["title"], s["artist"] or "", s["album"], round((s["duration_ms"] or 0) / 1000))

    def loop(self):
        shown, retry_at = None, 0.0
        while True:
            song = self._song()
            if song != shown or (retry_at and time.time() >= retry_at):
                shown, retry_at = song, 0.0
                if song is None:
                    self._publish(None, "none", [])
                elif song in self.cache:
                    self._publish(song[0], **self.cache[song])
                else:
                    self._publish(song[0], "loading", [])
                    try:
                        found = DEMO_LYRICS.get(song[0], {"state": "none", "lines": []}) if self.demo \
                            else lookup(*song)
                        self.cache[song] = found
                        while len(self.cache) > CACHE_SONGS:
                            self.cache.popitem(last=False)
                        if self._song() == song:            # still the same song after the wait
                            self._publish(song[0], **found)
                    except Exception:
                        if self._song() == song:
                            self._publish(song[0], "error", [])
                            retry_at = time.time() + RETRY_S
            time.sleep(1)
