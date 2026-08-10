"""Subtitle quality checks, using the conventions professional subtitlers use.

A translation can be word-perfect and still be a bad subtitle: on screen too
briefly to read, wrapped onto three lines, overlapping the next cue, or quietly
left in English because a batch failed. These checks catch all of that before
you ship the file.

Thresholds follow common industry practice (Netflix-style guidelines):
17 characters per second, 42 characters per line, at most 2 lines per cue.
"""

import re
from collections import Counter

MAX_CPS = 17          # characters per second a viewer can comfortably read
MAX_LINE_CHARS = 42   # per displayed line
MAX_LINES = 2         # displayed lines per cue
MIN_DURATION = 0.5    # seconds a cue must stay on screen

TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
BRACKET_RE = re.compile(r"^\[[^\]]*\]$")
SINHALA_RE = re.compile(u"[඀-෿]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


class Issue:
    """One problem found in one cue."""

    __slots__ = ("index", "kind", "detail", "value")

    def __init__(self, index, kind, detail, value=0):
        self.index = index
        self.kind = kind
        self.detail = detail
        self.value = value

    def __repr__(self):
        return "Issue(cue=%s, %s, %s)" % (self.index, self.kind, self.detail)


def _plain(text):
    return TAG_RE.sub("", text or "").strip()


def check(subs):
    """Return a list of Issue objects for a parsed subtitle file."""
    issues = []
    prev_end = None
    prev_index = None
    for cue in subs:
        raw = cue.text or ""
        body = _plain(raw)
        idx = cue.index
        duration = max(0.0, (cue.end.ordinal - cue.start.ordinal) / 1000.0)

        if not body:
            issues.append(Issue(idx, "empty", "cue has no text"))
        else:
            display_lines = [l for l in body.splitlines() if l.strip()]
            if len(display_lines) > MAX_LINES:
                issues.append(Issue(idx, "line_count",
                                    "%d lines on screen (max %d)"
                                    % (len(display_lines), MAX_LINES),
                                    len(display_lines)))
            for line in display_lines:
                if len(line.strip()) > MAX_LINE_CHARS:
                    issues.append(Issue(idx, "line_length",
                                        "%d characters in one line (max %d)"
                                        % (len(line.strip()), MAX_LINE_CHARS),
                                        len(line.strip())))
                    break

            # Reading speed: only meaningful for real words, not sound cues.
            if duration > 0 and not BRACKET_RE.match(body):
                chars = len(body.replace("\n", " "))
                cps = chars / duration
                if cps > MAX_CPS:
                    issues.append(Issue(idx, "reading_speed",
                                        "%.0f characters/second (max %d)"
                                        % (cps, MAX_CPS), cps))

            # Untranslated: latin words with no Sinhala anywhere in the cue.
            if (not BRACKET_RE.match(body)
                    and LATIN_WORD_RE.search(body)
                    and not SINHALA_RE.search(body)):
                issues.append(Issue(idx, "untranslated",
                                    "still in English: %s" % body[:50]))

        if duration < MIN_DURATION and LETTER_RE.search(body):
            issues.append(Issue(idx, "zero_duration",
                                "on screen only %.2fs" % duration, duration))

        if prev_end is not None and cue.start.ordinal < prev_end:
            issues.append(Issue(idx, "overlap",
                                "starts before cue %s ends" % prev_index))
        prev_end = cue.end.ordinal
        prev_index = idx
    return issues


LABELS = {
    "reading_speed": "too fast to read",
    "line_length": "line too long",
    "line_count": "too many lines",
    "untranslated": "left in English",
    "empty": "empty cue",
    "overlap": "overlapping timing",
    "zero_duration": "on screen too briefly",
}


def summarise(issues, total_cues):
    """One-line human summary for the status bar."""
    if not issues:
        return "No problems found in %d cues." % total_cues
    counts = Counter(i.kind for i in issues)
    parts = ["%d %s [%s]" % (n, LABELS.get(k, k), k) for k, n in counts.most_common()]
    return "%d issue(s) in %d cues: %s" % (len(issues), total_cues, ", ".join(parts))
