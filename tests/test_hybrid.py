import providers


class Fast:
    """Stands in for Google Translate."""

    def available(self):
        return True

    def translate(self, prompt, stdin_text, timeout):
        out = []
        for line in stdin_text.splitlines():
            if "|||" not in line or line.startswith(("CONTEXT", "TRANSLATE", "ALSO")):
                continue
            num, _, src = line.partition("|||")
            if num.strip().isdigit():
                out.append("%s|||FAST(%s)" % (num.strip(), src.strip()))
        return "\n".join(out) + "\n"


class Good:
    """Stands in for the Claude CLI polish pass."""

    seen = None

    def available(self):
        return True

    def translate(self, prompt, stdin_text, timeout):
        Good.seen = []
        out = []
        for line in stdin_text.splitlines():
            if "|||" not in line or line.startswith(("CONTEXT", "TRANSLATE", "ALSO")):
                continue
            num, _, src = line.partition("|||")
            if num.strip().isdigit():
                Good.seen.append(src.strip())
                out.append("%s|||GOOD(%s)" % (num.strip(), src.strip()))
        return "\n".join(out) + "\n"


def test_short_lines_keep_the_fast_translation():
    prov = providers.HybridProvider(Fast(), Good(), min_words=6)
    out = prov.translate("P", "TRANSLATE:\n1|||Yeah okay\n", 60)
    assert "FAST(Yeah okay)" in out
    assert "GOOD(" not in out


def test_long_lines_are_polished_by_the_better_engine():
    long_line = "He told me the whole story before anyone else arrived that night"
    prov = providers.HybridProvider(Fast(), Good(), min_words=6)
    out = prov.translate("P", "TRANSLATE:\n1|||%s\n" % long_line, 60)
    assert "GOOD(" in out
    assert Good.seen == [long_line]


def test_mixed_batch_splits_between_engines():
    prov = providers.HybridProvider(Fast(), Good(), min_words=6)
    stdin = ("TRANSLATE:\n"
             "1|||Yes\n"
             "2|||This is a much longer sentence that deserves careful treatment\n")
    out = prov.translate("P", stdin, 60)
    body = dict(l.split("|||", 1) for l in out.strip().splitlines())
    assert body["1"].startswith("FAST(")
    assert body["2"].startswith("GOOD(")


def test_polish_failure_falls_back_to_the_fast_result():
    class Broken(Good):
        def translate(self, prompt, stdin_text, timeout):
            raise RuntimeError("usage limit reached")

    long_line = "He told me the whole story before anyone else arrived that night"
    prov = providers.HybridProvider(Fast(), Broken(), min_words=6)
    out = prov.translate("P", "TRANSLATE:\n1|||%s\n" % long_line, 60)
    assert "FAST(" in out  # never lose the line just because polish failed


def test_available_requires_only_the_fast_engine():
    class Down:
        def available(self):
            return False

        def translate(self, *a):
            raise RuntimeError

    assert providers.HybridProvider(Fast(), Down()).available() is True
