"""Token/time efficiency: skip untranslatable cues, translate duplicates once."""
import pysrt

import sinhalasub


def _subs(texts):
    subs = pysrt.SubRipFile()
    for i, t in enumerate(texts):
        subs.append(pysrt.SubRipItem(
            index=i + 1,
            start=pysrt.SubRipTime(0, 0, i),
            end=pysrt.SubRipTime(0, 0, i + 1),
            text=t))
    return subs


class CountingProvider:
    """Records every source line it was actually asked to translate."""

    def __init__(self):
        self.seen = []
        self.calls = 0

    def available(self):
        return True

    def translate(self, prompt, stdin_text, timeout):
        self.calls += 1
        out = []
        in_target = False
        for line in stdin_text.splitlines():
            if line.startswith("TRANSLATE"):
                in_target = True
                continue
            if not in_target or "|||" not in line:
                continue
            num, _, src = line.partition("|||")
            num = num.strip()
            if num.isdigit():
                self.seen.append(src)
                out.append("%s|||SI[%s]" % (num, src))
        return "\n".join(out) + "\n"


# ----- untranslatable cue filter -------------------------------------------

def test_needs_translation_skips_sound_and_music_cues():
    assert sinhalasub.needs_translation("Hello there") is True
    assert sinhalasub.needs_translation("[door slams]") is False
    assert sinhalasub.needs_translation("♪♪") is False
    assert sinhalasub.needs_translation("♪ ♫ ") is False
    assert sinhalasub.needs_translation("1985") is False
    assert sinhalasub.needs_translation("- ...") is False
    assert sinhalasub.needs_translation("") is False


def test_needs_translation_keeps_lyrics_with_words():
    # music note + real words still needs translating
    assert sinhalasub.needs_translation("♪ I will always love you ♪") is True


def test_untranslatable_cues_are_not_sent_to_the_model():
    subs = _subs(["Hello there", "[door slams]", "♪♪", "Goodbye now"])
    prov = CountingProvider()
    texts = sinhalasub.translate_all(subs, prov, workers=1)
    assert "[door slams]" not in prov.seen
    assert "♪♪" not in prov.seen
    # untranslatable cues keep their original text, alignment preserved
    assert texts[1] == "[door slams]"
    assert texts[2] == "♪♪"
    assert len(texts) == 4


# ----- duplicate deduplication ---------------------------------------------

def test_identical_lines_are_translated_only_once():
    subs = _subs(["Yeah.", "Something unique here.", "Yeah.", "Yeah.", "Okay."])
    prov = CountingProvider()
    texts = sinhalasub.translate_all(subs, prov, workers=1)
    # "Yeah." must be sent exactly once despite appearing three times
    assert prov.seen.count("Yeah.") == 1
    # ...but every occurrence still gets the translation
    assert texts[0] == texts[2] == texts[3] == "SI[Yeah.]"
    assert texts[1] == "SI[Something unique here.]"
    assert texts[4] == "SI[Okay.]"


def test_dedup_and_filter_together_preserve_full_alignment():
    subs = _subs(["Go!", "[music]", "Go!", "Stop.", "♪♪", "Go!"])
    prov = CountingProvider()
    texts = sinhalasub.translate_all(subs, prov, workers=1)
    assert len(texts) == 6
    assert all(t is not None for t in texts)
    assert prov.seen.count("Go!") == 1
    assert texts[0] == texts[2] == texts[5]
    assert texts[1] == "[music]"
    assert texts[4] == "♪♪"
