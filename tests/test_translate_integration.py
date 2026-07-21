import pysrt

import sinhalasub


class FakeProvider:
    """Echoes each TRANSLATE line back as 'N|||SI-<n>' so alignment is testable."""

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def translate(self, prompt, stdin_text, timeout):
        self.calls += 1
        out = []
        for line in stdin_text.splitlines():
            if "|||" not in line:
                continue
            num, _, _ = line.partition("|||")
            num = num.strip()
            if num.isdigit():
                out.append("%s|||SI-%s" % (num, num))
        # build_batch_input includes CONTEXT lines too; the parser keeps only
        # the numbers this batch expects, so echoing everything is fine.
        return "\n".join(out) + "\n"


def _subs(n):
    subs = pysrt.SubRipFile()
    for i in range(n):
        subs.append(pysrt.SubRipItem(
            index=i + 1,
            start=pysrt.SubRipTime(0, 0, i),
            end=pysrt.SubRipTime(0, 0, i + 1),
            text="line %d" % (i + 1)))
    return subs


def test_translate_all_uses_provider_and_aligns(monkeypatch):
    monkeypatch.setattr(sinhalasub, "BATCH_SIZE", 3)
    subs = _subs(7)
    prov = FakeProvider()
    texts = sinhalasub.translate_all(subs, prov, workers=2)
    assert len(texts) == 7
    assert texts[0] == "SI-1"
    assert texts[6] == "SI-7"
    assert prov.calls >= 3  # 7 cues / batch size 3 => 3 batches


def test_translate_all_respects_batch_size():
    subs = _subs(10)
    prov = FakeProvider()
    texts = sinhalasub.translate_all(subs, prov, workers=1, batch_size=5)
    assert prov.calls == 2  # 10 cues / batch size 5 => 2 batches
    assert len(texts) == 10
    assert texts[9] == "SI-10"


def test_translate_batch_keeps_english_when_provider_returns_nothing():
    subs = _subs(2)

    class Empty:
        def translate(self, prompt, stdin_text, timeout):
            return ""

    result = sinhalasub.translate_batch(subs, [0, 1], Empty())
    assert result[0] == "line 1"  # kept English rather than corrupt alignment
    assert result[1] == "line 2"
