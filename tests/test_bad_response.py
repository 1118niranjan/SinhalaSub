"""A failed request must never be written into a subtitle as a translation."""
import pysrt

from sinhalasub import providers
from sinhalasub import app as sinhalasub


ERROR_PAGE = ("Error 500 (Server Error)!!1500.That's an error.There was an error. "
              "Please try again later.That's all we know.")


class ErrorPageTranslator:
    """Mimics the endpoint answering with an HTML error page body."""

    def __init__(self, source=None, target=None):
        pass

    def translate_batch(self, texts):
        return [ERROR_PAGE for _ in texts]


class HtmlTranslator(ErrorPageTranslator):
    def translate_batch(self, texts):
        return ["<html><head><title>502 Bad Gateway</title></head></html>"
                for _ in texts]


class GoodTranslator(ErrorPageTranslator):
    def translate_batch(self, texts):
        return ["හොඳ" for _ in texts]


def _prov(monkeypatch, cls):
    monkeypatch.setattr(providers, "_google_translator_cls", lambda: cls)
    return providers.GoogleTranslateProvider()


# ----- the response guard ----------------------------------------------------

def test_error_page_text_is_detected():
    assert providers.looks_like_error_page(ERROR_PAGE) is True


def test_html_body_is_detected():
    assert providers.looks_like_error_page("<html><body>oops</body></html>") is True


def test_common_gateway_errors_are_detected():
    for bad in ("502 Bad Gateway", "Service Unavailable",
                "Too Many Requests", "That's an error."):
        assert providers.looks_like_error_page(bad) is True, bad


def test_real_sinhala_is_not_flagged():
    assert providers.looks_like_error_page("මම ඔබට ඇත්ත කියන්නයි හිටියේ") is False


def test_short_plain_text_is_not_flagged():
    assert providers.looks_like_error_page("ඔව්") is False


# ----- the provider refuses to return it -------------------------------------

def test_provider_raises_on_an_error_page(monkeypatch):
    prov = _prov(monkeypatch, ErrorPageTranslator)
    try:
        prov.translate("P", "TRANSLATE:\n1|||Hello there\n", 60)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "error page" in str(exc).lower()


def test_provider_raises_on_html(monkeypatch):
    prov = _prov(monkeypatch, HtmlTranslator)
    try:
        prov.translate("P", "TRANSLATE:\n1|||Hello there\n", 60)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_good_translation_still_passes(monkeypatch):
    prov = _prov(monkeypatch, GoodTranslator)
    out = prov.translate("P", "TRANSLATE:\n1|||Hello there\n", 60)
    assert "හොඳ" in out


# ----- end to end: the cue keeps its original text ---------------------------

def _subs(texts):
    f = pysrt.SubRipFile()
    for i, t in enumerate(texts):
        f.append(pysrt.SubRipItem(index=i + 1,
                                  start=pysrt.SubRipTime(0, 0, i),
                                  end=pysrt.SubRipTime(0, 0, i + 1), text=t))
    return f


def test_a_total_failure_reports_instead_of_saving_an_error_page(monkeypatch):
    """Better to tell the user the service failed than to hand back junk."""
    monkeypatch.setattr(providers, "_google_translator_cls",
                        lambda: ErrorPageTranslator)
    prov = providers.GoogleTranslateProvider()
    try:
        sinhalasub.translate_all(_subs(["♪ Some song words here ♪"]),
                                 prov, workers=1)
        assert False, "expected the run to report the failure"
    except RuntimeError as exc:
        assert "error page" in str(exc).lower()


def test_only_the_failing_cue_keeps_its_original_text(monkeypatch):
    """A bad line must not take the good ones down with it."""

    class Flaky(ErrorPageTranslator):
        def translate_batch(self, texts):
            return [ERROR_PAGE if "poison" in t else "හොඳ" for t in texts]

    monkeypatch.setattr(providers, "_google_translator_cls", lambda: Flaky)
    prov = providers.GoogleTranslateProvider()
    # separate batches so one failure cannot discard the other line
    texts = sinhalasub.translate_all(
        _subs(["A clean line here.", "A poison line here."]),
        prov, workers=1, batch_size=1)
    assert texts[0] == "හොඳ"
    assert texts[1] == "A poison line here."   # kept, not overwritten
    assert "Error 500" not in " ".join(texts)
