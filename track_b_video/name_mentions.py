"""
Phase 3: catching the favourite person's NAME being said.

A lower-confidence second signal alongside the face matching: at a
pre-release event the camera may be on the stage while a host is
announcing who's coming up next, and the name lands before the face
does.

Two findings from measuring this on a real Telugu news clip, both of
which decide the design (see implementation_plan.txt Phase 3):

1. ASK WHISPER TO TRANSLATE, NOT TRANSCRIBE. On the test clip,
   transcribing Telugu took 565s for 113s of audio - five times slower
   than real time, and it returned no usable segments at all.
   Translating the same clip to English took 90s, i.e. 1.3x FASTER
   than real time, and read cleanly. English output also means names
   come out romanised, the way someone would type them.

2. NAMES ARE ONLY EVER APPROXIMATE, so match them fuzzily. Whisper
   wrote "Ramcharan" as one word, so a plain search for "ram charan"
   found nothing while "charan" hit. The smaller models are worse:
   base produced "Sucumar" for Sukumar. Hence normalise (case, spaces,
   punctuation all removed) and then allow near-misses.

Model size is a real tradeoff, measured on this CPU-only machine:

    tiny   2.9x real time   text barely coherent, missed most names
    base   2.6x real time   "Sucumar", missed Sukumar on exact match
    small  1.3x real time   clean, caught every name tried

small is the default because a missed name is a missed alert, and 1.3x
still keeps ahead of a live stream.
"""

from dataclasses import dataclass
from difflib import SequenceMatcher

# How close a run of words has to be to the name before it counts.
# 1.0 is character-identical after normalising. 0.82 accepts
# "sucumar" for "sukumar" (0.86) while still rejecting unrelated
# words of similar length - the tradeoff the Phase 3 "done when"
# cares about, since a false alert is worse than a missed one for a
# signal that's only meant to be corroborating.
DEFAULT_FUZZY_THRESHOLD = 0.82

DEFAULT_MODEL_SIZE = "small"


@dataclass
class Mention:
    start: float
    end: float
    text: str
    matched: str
    score: float  # 1.0 for an exact hit after normalising


def normalize(text: str) -> str:
    """Lowercase, keep only letters and digits.

    Spaces go too, so "Ram Charan" and Whisper's "Ramcharan" collapse
    to the same string.
    """
    return "".join(c for c in text.lower() if c.isalnum())


def _words(text: str) -> list[str]:
    return [w for w in (normalize(part) for part in text.split()) if w]


def find_mention(
    text: str,
    name: str,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> tuple[str, float] | None:
    """Look for `name` in `text`, allowing for how STT mangles names.

    Returns (matched text, score) or None.
    """
    target = normalize(name)
    if not target:
        return None

    words = _words(text)
    if not words:
        return None

    # Compare against runs of WHOLE words, never a free substring of
    # the whole text. Normalising strips spaces, so a plain substring
    # search let a short name match inside an unrelated word: "NTR"
    # hit "country" and "entry", and "to" hit "tomorrow". Whisper
    # still splits and joins names unpredictably, which is why runs of
    # several words are joined back up and compared as one.
    name_word_count = max(1, len(name.split()))
    max_span = name_word_count + 1

    for span in range(1, max_span + 1):
        for i in range(len(words) - span + 1):
            if "".join(words[i : i + span]) == target:
                return name, 1.0

    for span in range(max(1, name_word_count - 1), max_span + 1):
        for i in range(len(words) - span + 1):
            candidate = "".join(words[i : i + span])
            if not candidate:
                continue
            score = SequenceMatcher(None, target, candidate).ratio()
            if score >= fuzzy_threshold:
                return candidate, score
    return None


def transcribe(
    source,
    model,
    task: str = "translate",
    beam_size: int = 5,
):
    """Transcribe a file path or a numpy array of 16kHz float32 audio."""
    segments, info = model.transcribe(source, task=task, beam_size=beam_size)
    return list(segments), info


def load_model(model_size: str = DEFAULT_MODEL_SIZE):
    # int8 on CPU: this machine has no usable GPU (see
    # watch_local_video.py), and int8 is what makes small viable at
    # faster-than-real-time.
    from faster_whisper import WhisperModel

    return WhisperModel(model_size, device="cpu", compute_type="int8")


def scan_segments(
    segments,
    name: str,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> list[Mention]:
    mentions = []
    for seg in segments:
        hit = find_mention(seg.text, name, fuzzy_threshold)
        if hit:
            matched, score = hit
            mentions.append(
                Mention(start=seg.start, end=seg.end, text=seg.text.strip(), matched=matched, score=score)
            )
    return mentions
