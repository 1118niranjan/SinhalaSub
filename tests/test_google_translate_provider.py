from sinhalasub import providers


class FakeBatchTranslator:
    """Stands in for deep_translator.GoogleTranslator."""

    last_batch = None

    def __init__(self, source=None, target=None):
        self.source = source
        self.target = target

    def translate_batch(self, texts):
        FakeBatchTranslator.last_batch = list(texts)
        return ["SI<%s>" % t for t in texts]


def test_registered_as_keyless_provider():
    desc = providers.provider_by_key("google")
    assert desc["needs_key"] is False
    assert desc["key"] == "google"


def test_translates_only_the_translate_section(monkeypatch):
    monkeypatch.setattr(providers, "_google_translator_cls", lambda: FakeBatchTranslator)
    prov = providers.GoogleTranslateProvider()
    stdin = (
        "CONTEXT (for understanding only - do not translate, do not output):\n"
        "7|||Earlier context line\n"
        "\n"
        "TRANSLATE (2 lines - output exactly these numbers):\n"
        "8|||Hello there\n"
        "9|||Goodbye now\n"
    )
    out = prov.translate("PROMPT-IGNORED", stdin, timeout=60)
    # context line must NOT be sent to the translator
    assert FakeBatchTranslator.last_batch == ["Hello there", "Goodbye now"]
    lines = out.strip().splitlines()
    assert lines == ["8|||SI<Hello there>", "9|||SI<Goodbye now>"]


def test_output_parses_with_the_existing_response_parser(monkeypatch):
    monkeypatch.setattr(providers, "_google_translator_cls", lambda: FakeBatchTranslator)
    from sinhalasub import app as sinhalasub
    prov = providers.GoogleTranslateProvider()
    stdin = "TRANSLATE (1 lines):\n5|||Some line\n"
    out = prov.translate("P", stdin, 60)
    parsed = sinhalasub.parse_response(out, {5})
    assert parsed == {5: "SI<Some line>"}


def test_empty_translate_section_returns_empty(monkeypatch):
    monkeypatch.setattr(providers, "_google_translator_cls", lambda: FakeBatchTranslator)
    prov = providers.GoogleTranslateProvider()
    assert prov.translate("P", "CONTEXT:\n1|||only context\n", 60).strip() == ""


def test_falls_back_to_source_when_translator_returns_none(monkeypatch):
    class NoneTranslator(FakeBatchTranslator):
        def translate_batch(self, texts):
            return [None] * len(texts)

    monkeypatch.setattr(providers, "_google_translator_cls", lambda: NoneTranslator)
    prov = providers.GoogleTranslateProvider()
    out = prov.translate("P", "TRANSLATE:\n3|||Keep me\n", 60)
    assert out.strip() == "3|||Keep me"  # never corrupt alignment
