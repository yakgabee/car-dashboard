#!/usr/bin/env python3
"""Voice commands for the car dashboard.

Tap the mic button on the screen, wait for the beep, then say:
    "play Road Trips by Drake"  or just  "Road Trips by Drake"
    "pause", "resume", "next", "skip", "previous", "go back"
    "take me to Square One" / "navigate to ..." / "directions to ..."
    "show lyrics" / "show the map"   switch the screen ("go home" stays free for navigation)
    Anything else ("find me coffee", "what's the weather?", "something chill")
    goes to Claude (assistant.py) when an API key is set.

Or, hands-free, start with the wake phrase ("wake_phrase" in config.json,
set it to "" to turn this off):
    "Hey Bitch, play Road Trips by Drake"    search Spotify and play it
    "Hey Bitch"  ...  "Yes?"  ...  "play Road Trips"   same, in two steps
    "Hey Bitch, pause" / "stop"              pause
    "Hey Bitch, resume" / "play"             play
    "Hey Bitch, next" / "skip"               next song
    "Hey Bitch, previous" / "go back"        previous song

Speech to text: Vosk, offline. Text to speech: Piper, offline.
Spotify search needs internet (the phone's hotspot in the car).

Files (in models/ next to this file):
    vosk-model-small-en-us-0.15/   https://alphacephei.com/vosk/models
    en_US-lessac-medium.onnx and en_US-lessac-medium.onnx.json
                                   https://huggingface.co/rhasspy/piper-voices

dashboard.py starts this by calling run(...). To test on its own, without Spotify:
    python3 voice.py                         listen and answer, prints what it hears
    python3 voice.py --say "Hello there"     just speak
    python3 voice.py --file command.wav      recognise a 16-bit mono WAV file instead of the mic
"""

import argparse
import json
import queue
import re
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
import vosk
from piper import PiperVoice

HERE = Path(__file__).resolve().parent
VOSK_MODEL = HERE / "models" / "vosk-model-small-en-us-0.15"
VOICES = HERE / "models"
DEFAULT_VOICE = "en_US-lessac-medium"   # config.json "piper_voice" picks another one in models/

COMMAND_WAIT_S = 8      # after the button or a bare "Hey Bitch", how long to wait for the command

# Words the small Vosk model often hears instead of the real ones.
SOUNDS_LIKE = {
    "hey": ["hay", "hi", "a", "say", "the"],
    "bitch": ["beach", "bitches", "pitch", "britch"],   # not "bit": "a bit" is too common
}

# Spoken command -> dashboard action. Checked after "play <song>".
COMMANDS = {
    "pause": "pause", "stop": "pause",
    "resume": "play", "play": "play", "continue": "play",
    "next": "next", "skip": "next", "next song": "next", "skip this": "next",
    "previous": "previous", "go back": "previous", "last song": "previous", "back": "previous",
    "lyrics": "lyrics", "show lyrics": "lyrics", "show the lyrics": "lyrics", "show me the lyrics": "lyrics",
    "show the map": "home", "show map": "home", "home screen": "home", "show home": "home",
}


def wake_pattern(phrase):
    """Regex that matches the wake phrase and its usual mishearings. None when the phrase is empty."""
    if not phrase.strip():
        return None
    parts = []
    for word in phrase.lower().split():
        options = [word] + SOUNDS_LIKE.get(word, [])
        parts.append("(?:" + "|".join(map(re.escape, options)) + ")")
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b")


class Speaker:
    """Text to speech with Piper. While it talks, the microphone is ignored."""

    def __init__(self, voice_name=DEFAULT_VOICE):
        path = VOICES / f"{voice_name}.onnx"
        if not path.exists():
            raise RuntimeError(f"voice file missing: models/{path.name}")
        self.voice = PiperVoice.load(path)
        self.talking = threading.Event()
        self.times_spoken = 0

    def say(self, text):
        print(f"[voice] says: {text}")
        chunks = list(self.voice.synthesize(text))
        if not chunks:
            return
        audio = np.concatenate([c.audio_int16_array for c in chunks])
        self.talking.set()
        try:
            sd.play(audio, chunks[0].sample_rate)
            sd.wait()
            time.sleep(0.2)     # let the room echo die down before listening again
        finally:
            self.talking.clear()
            self.times_spoken += 1

    def beep(self):
        """Short rising two-tone that means "go ahead, I'm listening"."""
        rate = 22050
        t = np.arange(int(rate * 0.09)) / rate
        tone = np.concatenate([np.sin(2 * np.pi * 660 * t), np.sin(2 * np.pi * 880 * t)])
        ramp = np.arange(tone.size)
        fade = np.minimum(1, np.minimum(ramp, ramp[::-1]) / 200)      # no clicks at the ends
        self.talking.set()
        try:
            sd.play((tone * fade * 0.3 * 32767).astype(np.int16), rate)
            sd.wait()
        finally:
            self.talking.clear()
            self.times_spoken += 1


class Listener:
    """Turns recognised sentences into actions."""

    def __init__(self, wake_phrase, speaker, trigger, play_song, show, set_state=lambda state: None,
                 navigate=None, assist=None):
        self.wake = wake_pattern(wake_phrase)
        self.speaker, self.trigger, self.play_song, self.show = speaker, trigger, play_song, show
        self.set_state = set_state
        self.navigate = navigate    # navigate(place) -> sentence to speak
        self.assist = assist        # assist(words, from_button) -> sentence, "" (stay quiet) or None (no Claude)
        self.awaiting_until = 0.0
        self.from_button = False

    @property
    def awaiting(self):
        return time.monotonic() < self.awaiting_until

    def start_listening(self, from_button):
        """Wait up to COMMAND_WAIT_S for a command, after the button or a bare wake phrase."""
        self.from_button = from_button
        self.set_state("listening")
        self.show("Listening…")
        if from_button:
            self.speaker.beep()
        else:
            self.speaker.say("Yes?")
        self.awaiting_until = time.monotonic() + COMMAND_WAIT_S

    def stop_listening(self, why=None):
        self.awaiting_until = 0.0
        self.set_state("idle")
        if why:
            self.show(why)

    def heard(self, text):
        text = text.lower().strip()
        if not text:
            return
        print(f"[voice] heard: {text}")
        match = self.wake.search(text) if self.wake else None
        if self.awaiting:
            rest = text[match.end():].strip(" ,.") if match else text
        elif match:
            rest = text[match.end():].strip(" ,.")
            if not rest:
                self.start_listening(from_button=False)
                return
            self.from_button = False
        else:
            return
        self.awaiting_until = 0.0
        self.set_state("working")
        try:
            self.command(rest)
        finally:
            self.set_state("idle")

    def command(self, rest):
        rest = re.sub(r"^(?:can you |could you |please )", "", rest)
        action = COMMANDS.get(re.sub(r"\s+(?:please|the song|song)$", "", rest))
        if action:
            self.trigger(action)
            return
        place = re.match(r"(?:take me to|navigate to|directions to|drive to|get me to) (?:the )?(.+)", rest)
        if place and self.navigate:
            self.speaker.say(self.navigate(place.group(1)))
            return
        song = re.match(r"play (?:the song |me )?(.+)", rest)
        if song and song.group(1) not in ("music", "it", "again", "something"):
            query = song.group(1)
        else:
            if self.assist:
                self.show("Thinking…")
                reply = self.assist(rest, self.from_button)
                if reply is not None:       # Claude handled it
                    if reply:
                        self.speaker.say(reply)
                    return
            if self.from_button:
                query = rest        # no Claude: after the button, anything else is taken as a song name
            else:
                self.speaker.say("Sorry, I didn't catch that.")
                return
        query = re.split(r"\s+play\s+", query)[0].strip()   # background talk can run on: keep the first request
        self.show(f"Finding {query}")
        self.speaker.say(self.play_song(query))


def recognizer(rate):
    if not VOSK_MODEL.exists():
        raise RuntimeError(f"speech model missing: models/{VOSK_MODEL.name}")
    vosk.SetLogLevel(-1)
    return vosk.KaldiRecognizer(vosk.Model(str(VOSK_MODEL)), rate)


def listen(listener, speaker, listen_now=None, announcements=None):
    """Feed the microphone to Vosk forever. listen_now is set by the screen's mic button;
    announcements is a queue of sentences to speak (turn-by-turn directions)."""
    rate = int(sd.query_devices(kind="input")["default_samplerate"])
    rec = recognizer(rate)
    chunks = queue.Queue()

    def on_audio(indata, frames, when, status):
        if not speaker.talking.is_set():
            chunks.put(bytes(indata))

    with sd.RawInputStream(samplerate=rate, blocksize=rate // 4, dtype="int16", channels=1, callback=on_audio):
        print(f"[voice] listening at {rate} Hz")
        while True:
            if listen_now is not None and listen_now.is_set():
                listen_now.clear()
                if listener.awaiting and listener.from_button:
                    listener.stop_listening("Cancelled")        # a second tap cancels
                else:
                    listener.start_listening(from_button=True)
                    rec.Reset()                                 # only what comes after the beep counts
                    while not chunks.empty():
                        chunks.get_nowait()
                continue
            if announcements is not None and not listener.awaiting and not announcements.empty():
                speaker.say(announcements.get_nowait())
                rec.Reset()                                     # don't hear our own directions
                while not chunks.empty():
                    chunks.get_nowait()
                continue
            if listener.awaiting_until and not listener.awaiting:
                listener.stop_listening("Didn't hear anything")
                rec.Reset()
            try:
                data = chunks.get(timeout=0.1)
            except queue.Empty:
                continue
            if rec.AcceptWaveform(data):
                spoken = speaker.times_spoken
                listener.heard(json.loads(rec.Result())["text"])
                if speaker.times_spoken != spoken:
                    # it answered: drop audio queued meanwhile so it doesn't hear itself
                    rec.Reset()
                    while not chunks.empty():
                        chunks.get_nowait()


def run(trigger, play_song, show, wake_phrase="hey bitch", listen_now=None, set_state=lambda state: None,
        navigate=None, assist=None, announcements=None, voice_name=DEFAULT_VOICE):
    """Called by dashboard.py in a background thread. listen_now: the mic button's Event."""
    speaker = Speaker(voice_name)
    listener = Listener(wake_phrase, speaker, trigger, play_song, show, set_state, navigate, assist)
    set_state("idle")
    listen(listener, speaker, listen_now, announcements)


def recognise_file(path):
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit("Use a 16-bit mono WAV file.")
        rec = recognizer(w.getframerate())
        while data := w.readframes(4000):
            rec.AcceptWaveform(data)
        return json.loads(rec.FinalResult())["text"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--say", help="speak this text and exit")
    parser.add_argument("--file", help="recognise a WAV file instead of the microphone")
    parser.add_argument("--wake", default="hey bitch", help="wake phrase (default: hey bitch)")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="Piper voice name in models/")
    args = parser.parse_args()

    if args.say:
        Speaker(args.voice).say(args.say)
        return

    speaker = Speaker(args.voice)
    listener = Listener(args.wake, speaker,
                        trigger=lambda action: print(f"-> {action}"),
                        play_song=lambda query: f"I would play {query}.",
                        show=lambda text: print(f"[screen] {text}"))
    if args.file:
        listener.heard(recognise_file(args.file))
        return
    print(f'Voice test: no Spotify. Say "{args.wake}, play <song>". Ctrl+C to quit.')
    try:
        listen(listener, speaker)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
