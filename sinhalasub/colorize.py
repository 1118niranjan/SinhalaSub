"""Colouring rules for subtitle cues.

Players render <font color="#RRGGBB"> tags inside .srt text, so colouring is a
pure text transform. Auto-colour classifies each cue - sound effect, emphasis,
two-speaker dialogue, plain line - and highlights character and place names, so
a viewer can tell at a glance who is speaking and what is a sound rather than
speech.
"""

import re

BRACKET_RE = re.compile(r"^\[[^\]]*\]$")
TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
NOTE_CHARS = "♪♫"
LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
WORD_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")

# Capitalised words that are almost never character names.
_STOPWORDS = {
    "The", "This", "That", "These", "Those", "There", "Then", "They", "Their",
    "You", "Your", "We", "Our", "He", "She", "His", "Her", "It", "Its", "But",
    "And", "For", "Not", "Yes", "No", "Okay", "Well", "What", "When", "Where",
    "Who", "Why", "How", "All", "Any", "One", "Two", "Now", "Just", "Like",
    "Come", "Get", "Let", "Look", "Listen", "Please", "Sorry", "Thank", "Thanks",
    "Hey", "Oh", "Sir", "Maam", "God", "Yeah", "Nope", "Stop", "Wait", "Good",
    "Right", "Sure", "Never", "Every", "Some", "Something", "Nothing", "Because",
    "Before", "After", "Down", "Over", "Here", "Have", "Has", "Had", "Are", "Was",
    "Were", "Will", "Would", "Could", "Should", "Can", "Did", "Does", "Don",
    "Mister", "Doctor", "Captain",
}


def colour_line(text, hex_colour):
    """Wrap a whole cue in one colour. Empty colour leaves the text alone."""
    if not hex_colour:
        return text
    return '<font color="%s">%s</font>' % (hex_colour, text)


def _plain(text):
    """Cue text with markup and music symbols removed, for classification."""
    return TAG_RE.sub("", text or "").strip(NOTE_CHARS + " \n").strip()


def classify(text):
    """Return 'sound', 'emphasis', 'dialogue' or 'normal' for a cue."""
    raw = (text or "").strip()
    if not raw:
        return "normal"
    core = _plain(raw)
    # sound effects and pure music cues
    if BRACKET_RE.match(core) or not LETTER_RE.search(core):
        return "sound"
    if raw.strip(NOTE_CHARS + " ") != raw.strip() and any(
            c in raw for c in NOTE_CHARS):
        return "sound"
    # two or more dash-prefixed lines = different speakers in one cue
    dash_lines = [l for l in raw.splitlines() if l.strip().startswith("-")]
    if len(dash_lines) >= 2:
        return "dialogue"
    letters = LETTER_RE.findall(core)
    if TAG_RE.search(raw):
        return "emphasis"
    if len(letters) >= 4 and core.isupper():
        return "emphasis"
    return "normal"


def find_names(texts):
    """Guess character and place names from a list of English cue texts.

    A capitalised word that appears somewhere other than the start of a line is
    almost always a proper noun, which is what we want to highlight.
    """
    names = set()
    for text in texts or []:
        for line in (text or "").splitlines():
            line = TAG_RE.sub("", line).strip().lstrip("-").strip()
            words = line.split()
            for i, w in enumerate(words):
                token = w.strip(".,!?;:\"'()[]")
                if i == 0:
                    continue  # sentence-initial capital proves nothing
                if not WORD_RE.fullmatch(token):
                    continue
                if token in _STOPWORDS:
                    continue
                names.add(token)
    return names


def auto_colour_line(text, scheme, names=None):
    """Apply the colour scheme to one cue.

    scheme keys: 'sound', 'emphasis', 'speaker1', 'speaker2', 'name', 'normal'.
    Any key left out simply does not colour that category.
    """
    scheme = scheme or {}
    names = names or set()
    raw = text or ""
    kind = classify(raw)

    if kind == "sound" and scheme.get("sound"):
        return colour_line(raw, scheme["sound"])
    if kind == "emphasis" and scheme.get("emphasis"):
        return colour_line(raw, scheme["emphasis"])

    if kind == "dialogue" and (scheme.get("speaker1") or scheme.get("speaker2")):
        out, turn = [], 0
        for line in raw.splitlines():
            if line.strip().startswith("-"):
                key = "speaker1" if turn % 2 == 0 else "speaker2"
                turn += 1
                out.append(colour_line(line, scheme.get(key, "")))
            else:
                out.append(line)
        return "\n".join(out)

    if names and scheme.get("name"):
        highlighted = raw
        for name in sorted(names, key=len, reverse=True):
            highlighted = re.sub(
                r"\b%s\b" % re.escape(name),
                colour_line(name, scheme["name"]),
                highlighted)
        if highlighted != raw:
            return highlighted

    if scheme.get("normal"):
        return colour_line(raw, scheme["normal"])
    return raw
