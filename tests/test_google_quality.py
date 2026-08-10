"""Quality behaviour of the Google Translate provider."""
from sinhalasub import providers


class RecordingTranslator:
    """Echoes back what it was asked to translate so we can inspect the calls."""

    last_batch = None

    def __init__(self, source=None, target=None):
        pass

    def translate_batch(self, texts):
        RecordingTranslator.last_batch = list(texts)
        # pretend Sinhala: one token per source word keeps splitting testable
        return [" ".join("ස%d" % i for i, _ in enumerate(t.split())) for t in texts]


def _prov(monkeypatch, **kw):
    monkeypatch.setattr(providers, "_google_translator_cls", lambda: RecordingTranslator)
    return providers.GoogleTranslateProvider(**kw)


def _stdin(pairs):
    lines = ["TRANSLATE (%d lines):" % len(pairs)]
    lines += ["%d|||%s" % (n, t) for n, t in pairs]
    return "\n".join(lines) + "\n"


def test_split_sentence_is_translated_as_one_unit(monkeypatch):
    prov = _prov(monkeypatch)
    out = prov.translate("P", _stdin([(1, "I was going to"), (2, "tell you the truth.")]), 60)
    # both cues joined into a single translation request
    assert RecordingTranslator.last_batch == ["I was going to tell you the truth."]
    # ...but both cues still come back, in order
    nums = [l.split("|||")[0] for l in out.strip().splitlines()]
    assert nums == ["1", "2"]
    assert all(l.split("|||")[1].strip() for l in out.strip().splitlines())


def test_non_adjacent_cues_are_never_joined(monkeypatch):
    prov = _prov(monkeypatch)
    # cue 1 and cue 5 are not neighbours - joining them would be wrong
    prov.translate("P", _stdin([(1, "I was going to"), (5, "tell you the truth.")]), 60)
    assert RecordingTranslator.last_batch == ["I was going to", "tell you the truth."]


def test_complete_sentences_stay_separate(monkeypatch):
    prov = _prov(monkeypatch)
    prov.translate("P", _stdin([(1, "Stop right there."), (2, "Put it down.")]), 60)
    assert RecordingTranslator.last_batch == ["Stop right there.", "Put it down."]


def test_shouting_is_normalised_before_translation(monkeypatch):
    prov = _prov(monkeypatch)
    prov.translate("P", _stdin([(1, "GET OUT OF HERE!")]), 60)
    assert RecordingTranslator.last_batch == ["Get out of here!"]


def test_markup_is_not_translated_and_is_restored(monkeypatch):
    prov = _prov(monkeypatch)
    out = prov.translate("P", _stdin([(1, "- <i>Run now!</i>")]), 60)
    assert RecordingTranslator.last_batch == ["Run now!"]
    body = out.split("|||", 1)[1].strip()
    assert body.startswith("- <i>") and body.endswith("</i>")


def test_glossary_terms_are_applied(monkeypatch):
    prov = _prov(monkeypatch, glossary={"Marseille": "මාර්සෙයි"})
    prov.translate("P", _stdin([(1, "We reach Marseille tonight.")]), 60)
    assert "මාර්සෙයි" in RecordingTranslator.last_batch[0]


def test_every_requested_number_is_returned(monkeypatch):
    prov = _prov(monkeypatch)
    pairs = [(1, "He said"), (2, "it would rain."), (3, "Then he left."), (4, "OK.")]
    out = prov.translate("P", _stdin(pairs), 60)
    nums = sorted(int(l.split("|||")[0]) for l in out.strip().splitlines())
    assert nums == [1, 2, 3, 4]
