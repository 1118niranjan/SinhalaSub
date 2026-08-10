"""Save a finished translation in whichever format the target player wants.

Different players want different things. SubRip (.srt) is the universal choice
and what almost every TV, set-top box and media player reads. Very old
DivX-era hardware often only understands frame-based MicroDVD (.sub). Smart TVs
and browsers use WebVTT (.vtt). ASS/SSA carries real styling. Plain text is for
reading the script.

Encoding matters as much as format on older hardware: some devices only detect
UTF-8 when a byte-order mark is present, so that is offered as its own choice.
"""

import re

TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

FORMATS = [
    {"key": "srt", "ext": ".srt", "label": "SubRip (.srt) - works on almost everything"},
    {"key": "sub", "ext": ".sub", "label": "MicroDVD (.sub) - very old DivX players"},
    {"key": "vtt", "ext": ".vtt", "label": "WebVTT (.vtt) - smart TVs, web players"},
    {"key": "ass", "ext": ".ass", "label": "ASS/SSA (.ass) - keeps colour and styling"},
    {"key": "txt", "ext": ".txt", "label": "Plain text (.txt) - dialogue only"},
]

ENCODINGS = [
    {"key": "utf-8", "label": "UTF-8 (recommended)"},
    {"key": "utf-8-sig", "label": "UTF-8 with BOM (older TVs that need it)"},
    {"key": "utf-16", "label": "UTF-16"},
]

DEFAULT_FPS = 23.976


def format_by_key(key):
    for f in FORMATS:
        if f["key"] == key:
            return f
    raise ValueError("Unknown subtitle format: %r" % key)


def _plain(text):
    """Cue text with HTML-ish markup removed, whitespace tidied per line."""
    stripped = TAG_RE.sub("", text or "")
    return "\n".join(" ".join(l.split()) for l in stripped.splitlines() if l.strip())


def _ms(t):
    return t.ordinal


def _hhmmss(ordinal, sep=","):
    ms = ordinal % 1000
    total = ordinal // 1000
    s, m, h = total % 60, (total // 60) % 60, total // 3600
    return "%02d:%02d:%02d%s%03d" % (h, m, s, sep, ms)


def _ass_colour(hex_colour):
    """#RRGGBB -> ASS &HAABBGGRR (alpha 00 = opaque, channels reversed)."""
    h = (hex_colour or "").lstrip("#")
    if len(h) != 6:
        return "&H00FFFFFF"
    return "&H00%s%s%s" % (h[4:6].upper(), h[2:4].upper(), h[0:2].upper())


# ----- writers ---------------------------------------------------------------

def _write_srt(subs, path, encoding, **_):
    subs.save(path, encoding=encoding)


def _write_vtt(subs, path, encoding, **_):
    parts = ["WEBVTT", ""]
    for cue in subs:
        parts.append("%s --> %s" % (_hhmmss(_ms(cue.start), "."),
                                    _hhmmss(_ms(cue.end), ".")))
        parts.append(_plain(cue.text))
        parts.append("")
    _put(path, "\n".join(parts), encoding)


def _write_sub(subs, path, encoding, fps=DEFAULT_FPS, **_):
    fps = float(fps or DEFAULT_FPS)
    lines = []
    for cue in subs:
        start = int(round(_ms(cue.start) / 1000.0 * fps))
        end = int(round(_ms(cue.end) / 1000.0 * fps))
        body = _plain(cue.text).replace("\n", "|")
        lines.append("{%d}{%d}%s" % (start, end, body))
    _put(path, "\n".join(lines) + "\n", encoding)


def _write_ass(subs, path, encoding, colour="", font="Iskoola Pota", size=48, **_):
    primary = _ass_colour(colour) if colour else "&H00FFFFFF"
    head = [
        "[Script Info]",
        "Title: SinhalaSub",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        "Style: Default,%s,%d,%s,&H000000FF,&H00000000,&H80000000,0,0,0,0,"
        "100,100,0,0,1,2.5,1,2,60,60,40,1" % (font, int(size), primary),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    ]
    for cue in subs:
        body = _plain(cue.text).replace("\n", "\\N")
        head.append("Dialogue: 0,%s,%s,Default,,0,0,0,,%s"
                    % (_ass_time(_ms(cue.start)), _ass_time(_ms(cue.end)), body))
    _put(path, "\n".join(head) + "\n", encoding)


def _ass_time(ordinal):
    """ASS uses h:mm:ss.cc (centiseconds, single-digit hour)."""
    cs = (ordinal % 1000) // 10
    total = ordinal // 1000
    s, m, h = total % 60, (total // 60) % 60, total // 3600
    return "%d:%02d:%02d.%02d" % (h, m, s, cs)


def _write_txt(subs, path, encoding, **_):
    _put(path, "\n".join(_plain(c.text) for c in subs) + "\n", encoding)


def _put(path, body, encoding):
    with open(path, "w", encoding=encoding or "utf-8", newline="\r\n") as f:
        f.write(body)


_WRITERS = {"srt": _write_srt, "vtt": _write_vtt, "sub": _write_sub,
            "ass": _write_ass, "txt": _write_txt}


def write(subs, path, fmt, encoding="utf-8", **options):
    """Write `subs` to `path` in `fmt`. Raises ValueError on an unknown format."""
    format_by_key(fmt)  # validates
    _WRITERS[fmt](subs, path, encoding, **options)
    return path
