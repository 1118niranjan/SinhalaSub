"""Text shaping that makes machine translation of subtitles far more accurate.

Machine translators see one cue at a time with no scene context, so three things
wreck their output: markup they translate as if it were words, SHOUTED LINES they
mangle, and sentences that a subtitle file splits across two or three cues. This
module normalises all three before translation and restores them afterwards.
"""

import re

TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
LEAD_DASH_RE = re.compile(r"^\s*[-–—]\s*")
NOTE_CHARS = "♪♫"
# A sentence is finished if it ends with terminal punctuation (allowing a
# closing quote/bracket after it).
TERMINAL_RE = re.compile(r"[.!?…:](['\"”’)\]]*)$")
BRACKET_RE = re.compile(r"^\[[^\]]*\]$")
LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

# Two cues is the sweet spot. Merging three means the translation has to be cut
# into three pieces by word count, and Sinhala word order differs enough from
# English that the middle piece often lands on the wrong cue.
MAX_GROUP = 2


def unwrap(text):
    """Split a cue into its translatable core plus a rebuild function.

    Returns (core, rebuild) where rebuild(translated_core) puts the original
    markup - speaker dash, italics, music notes - back around the translation.
    """
    raw = " ".join((text or "").split())

    dash = ""
    m = LEAD_DASH_RE.match(raw)
    if m:
        dash = "- "
        raw = raw[m.end():]

    notes = False
    stripped = raw.strip(NOTE_CHARS + " ")
    if stripped != raw.strip():
        notes = True
        raw = stripped

    tags = TAG_RE.findall(raw)
    open_tag = tags[0] if tags and not tags[0].startswith("</") else ""
    close_tag = tags[-1] if tags and tags[-1].startswith("</") else ""
    core = TAG_RE.sub("", raw).strip()

    def rebuild(translated):
        out = (translated or "").strip()
        if open_tag or close_tag:
            out = "%s%s%s" % (open_tag, out, close_tag)
        if notes:
            out = "♪ %s ♪" % out
        if dash:
            out = dash + out
        return out

    return core, rebuild


def normalise_caps(text):
    """Turn SHOUTED LINES into sentence case; translators handle caps poorly.

    Short all-caps tokens (FBI, OK) are acronyms and are left alone.
    """
    t = text or ""
    letters = LETTER_RE.findall(t)
    if len(letters) < 4:
        return t
    if t.isupper():
        return t.capitalize()
    return t


def _is_continuation(prev_text, next_text):
    """True if next_text continues the sentence started in prev_text."""
    p = (prev_text or "").strip()
    n = (next_text or "").strip()
    if not p or not n:
        return False
    if BRACKET_RE.match(p) or BRACKET_RE.match(n):
        return False
    if not LETTER_RE.search(p) or not LETTER_RE.search(n):
        return False
    if TERMINAL_RE.search(p):
        return False           # previous cue completed its sentence
    if LEAD_DASH_RE.match(n):
        return False           # a dash starts a new speaker
    first = n.lstrip("<i>").lstrip()
    if first[:1].isupper():
        return False           # a capital letter starts a new sentence
    return True


def group_sentences(texts):
    """Group cue indexes whose text forms one sentence spanning several cues.

    Returns a list of index groups covering every cue exactly once, in order.
    """
    groups = []
    current = []
    for i, t in enumerate(texts):
        if current and len(current) < MAX_GROUP and _is_continuation(texts[current[-1]], t):
            current.append(i)
            continue
        if current:
            groups.append(current)
        current = [i]
    if current:
        groups.append(current)
    return groups


def split_translation(translated, word_counts):
    """Split one translation back across cues, in proportion to their lengths.

    word_counts holds the original word count of each cue in the group, so a
    long cue receives a longer share. Always returns exactly one string per cue.
    """
    n = len(word_counts)
    if n <= 1:
        return [(translated or "").strip()]
    words = (translated or "").split()
    if not words:
        return [""] * n

    # Prefer a natural break. If the translation contains a comma near the point
    # the proportional split would fall, cut there instead - a clause boundary
    # reads far better on screen than a cut in the middle of a phrase.
    if n == 2:
        target = max(1, int(round(len(words) * (word_counts[0] / (sum(word_counts) or 2)))))
        for offset in (0, 1, -1, 2, -2):
            k = target + offset
            if 1 <= k < len(words) and words[k - 1].endswith((",", ";", ":", "…")):
                return [" ".join(words[:k]), " ".join(words[k:])]
    total = sum(word_counts) or n
    parts, start = [], 0
    for k, wc in enumerate(word_counts):
        if k == n - 1:
            take = len(words) - start
        else:
            take = int(round(len(words) * (wc / total)))
            # leave at least one word for every remaining cue
            take = max(1, min(take, len(words) - start - (n - k - 1)))
        parts.append(" ".join(words[start:start + take]))
        start += take
    return parts


def apply_glossary(text, glossary):
    """Replace preferred terms before translation so they stay consistent."""
    if not glossary:
        return text
    out = text
    for term, replacement in glossary.items():
        if not term:
            continue
        out = re.sub(r"\b%s\b" % re.escape(term), replacement, out,
                     flags=re.IGNORECASE)
    return out
