"""Claude as the brain behind voice commands the simple phrase matcher can't handle.

voice.py first tries fixed phrases ("pause", "play <song>", "take me to <place>").
Anything else goes here: Claude reads what the speech recogniser heard, plus a
little car state (song, destination, weather, time, IP address), and picks ONE action:

    play_song  query        -> Spotify search and play
    control    control      -> play / pause / next / previous / lyrics / home screen
    navigate   destination  -> set the destination (Mapbox does the routing)
    answer     reply        -> a short spoken answer
    search     query        -> live information: a second request searches the web
    none                    -> background talk, ignore

Memory: the conversation is kept while you keep talking, so follow-ups work
("who scored?" after "who won the Leafs game?"). It starts fresh after
MEMORY_IDLE_S of silence or MEMORY_TURNS exchanges. History is only ever
appended to, never edited, as the API expects.

Claude never makes up directions; routes always come from the map service.

Needs "anthropic_api_key" in config.json (from console.anthropic.com) and
pip install anthropic. "claude_model" picks the model (default claude-haiku-5-5).
"""

import json
import re
import time

import anthropic

MEMORY_IDLE_S = 300     # 5 minutes without a question -> forget the conversation
MEMORY_TURNS = 8        # after this many exchanges start a fresh conversation
WEB_SEARCH_TOOL = "web_search_20250305"     # basic search: about twice as fast as the newer one in our test

SYSTEM = """You are the voice assistant built into a car dashboard. The driver says a short request out loud.
It was transcribed by a small offline speech recogniser, so words are often misheard: work out what
the driver most likely meant (for example "lick lie" is probably the artist Lykke Li).
Earlier turns of this conversation are included, so follow-up questions refer to them.

Pick exactly one action:
- play_song: the driver wants music. Put the song and artist, spelled as on Spotify, in "query".
  For a mood or genre ("something chill"), pick one fitting, well-known song.
- control: playback control. "control" is one of play, pause, next, previous, lyrics (show the lyrics page), home (back to the map and home screen; "take me home" is navigate, not this).
- navigate: the driver wants to go somewhere. Put a place name or address a map search would
  understand in "destination". Never give directions yourself; the map service does the routing.
- answer: a question you can answer from general knowledge, the conversation so far, or the car
  information. "reply" is one or two short spoken sentences, no lists or formatting.
  "IP address" means ip_address in the car information (this computer on the local network), not a web search.
- search: the answer needs live or recent information: scores, news, weather elsewhere or later,
  opening hours, prices, events, anything that may have changed. Put a short web search query in
  "query" (include the city when it matters). Someone else will search and answer.
- none: the words are not a request for you (people talking, noise, half a sentence).

The reply is read aloud while the driver is driving, so keep it short. Leave unused fields empty."""

SEARCH_SYSTEM = """You are the voice assistant built into a car dashboard. Search the web to answer the driver's
question, then answer in one or two short sentences that will be read aloud while driving.
Say each fact once. No lists, no links, no markdown, no source names unless they matter.
If the search doesn't answer it, say so briefly."""

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string",
                   "enum": ["play_song", "control", "navigate", "answer", "search", "none"]},
        "query": {"type": "string"},
        "control": {"type": "string",
                    "enum": ["", "play", "pause", "next", "previous", "lyrics", "home"]},
        "destination": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["action", "query", "control", "destination", "reply"],
    "additionalProperties": False,
}


class AssistantError(Exception):
    """Something went wrong talking to Claude. The message is short enough to speak."""


def _call(fn):
    """Run an API call, turning failures into AssistantError with a speakable message."""
    try:
        return fn()
    except anthropic.AuthenticationError:
        raise AssistantError("Claude refused the API key.")
    except anthropic.RateLimitError:
        raise AssistantError("Claude is busy. Try again in a moment.")
    except anthropic.APIConnectionError:
        raise AssistantError("No internet connection for Claude.")
    except anthropic.APIStatusError as err:
        raise AssistantError(f"Claude error {err.status_code}.")


def drop_repeats(text):
    """Search answers sometimes state a fact twice (once per source). Keep the first telling."""
    kept, seen = [], set()
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        words = set(re.findall(r"[a-z0-9']+", sentence.lower()))
        if kept and words and len(words & seen) / len(words) > 0.6:
            continue
        kept.append(sentence)
        seen |= words
    return " ".join(kept)


class Assistant:
    def __init__(self, api_key, model="claude-haiku-5-5", location=None):
        # short timeouts and one retry: a driver won't wait longer than that
        self.client = anthropic.Anthropic(api_key=api_key, timeout=20.0, max_retries=1)
        self.model = model
        self.location = location        # {"city", "region", "country", "timezone"} for local search results
        self.history = []               # this conversation's messages, appended to only
        self.last_at = 0.0
        self.note = None                # what the web search told the driver, for the next turn

    def forget(self):
        self.history, self.note = [], None

    def decide(self, heard, car):
        """heard: the transcribed words. car: dict of current state. Returns the decision dict."""
        if time.time() - self.last_at > MEMORY_IDLE_S or len(self.history) >= MEMORY_TURNS * 2:
            self.forget()
        content = f"Car information: {json.dumps(car)}\n\nThe driver said: {heard}"
        if self.note:
            content = f"(After your last turn you searched the web and told the driver: \"{self.note}\")\n\n{content}"
        messages = self.history + [{"role": "user", "content": content}]
        response = _call(lambda: self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
            messages=messages,
        ))
        if response.stop_reason == "refusal":
            self.forget()
            return {"action": "answer", "query": "", "control": "", "destination": "",
                    "reply": "Sorry, I can't help with that."}
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            decision = json.loads(text)
        except json.JSONDecodeError:
            raise AssistantError("Claude's answer was cut off.")
        self.history = messages + [{"role": "assistant", "content": response.content}]
        self.last_at, self.note = time.time(), None
        return decision

    def search(self, question, query, car):
        """Answer a question that needs live information, with Claude's web search tool."""
        tool = {"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": 2}
        if self.location:
            tool["user_location"] = {"type": "approximate", **self.location}
        messages = [{"role": "user", "content":
                     f"Car information: {json.dumps(car)}\n\nThe driver asked: {question}\n"
                     f"Suggested search: {query}"}]
        for _ in range(3):      # the server may pause a long search turn; resume it a couple of times
            response = _call(lambda: self.client.messages.create(
                model=self.model, max_tokens=2000, system=SEARCH_SYSTEM,
                output_config={"effort": "low"}, tools=[tool], messages=messages,
            ))
            if response.stop_reason != "pause_turn":
                break
            messages = messages[:1] + [{"role": "assistant", "content": response.content}]
        if response.stop_reason == "refusal":
            return "Sorry, I can't help with that."
        # cited answers arrive in pieces; the pieces carry their own spacing
        answer = "".join(b.text for b in response.content if b.type == "text")
        answer = re.sub(r"\s+([.,!?])", r"\1", re.sub(r"\s+", " ", answer)).strip()
        answer = drop_repeats(answer)
        answer = answer or "I couldn't find an answer to that."
        self.note, self.last_at = answer, time.time()
        return answer
