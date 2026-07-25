"""Hard lines translated by an LLM must be reusable on later free runs."""
import pysrt

import memory_db
import providers
import sinhalasub


HARD = ("He told me that the entire arrangement had collapsed, long before "
        "anyone thought to ask")


def _subs(texts):
    f = pysrt.SubRipFile()
    for i, t in enumerate(texts):
        f.append(pysrt.SubRipItem(index=i + 1,
                                  start=pysrt.SubRipTime(0, 0, i),
                                  end=pysrt.SubRipTime(0, 0, i + 1), text=t))
    return f


# ----- the reuse rule --------------------------------------------------------

def test_short_lines_are_reusable_at_any_quality():
    assert sinhalasub.memory_reusable("Yeah.", "machine") is True
    assert sinhalasub.memory_reusable("[door slams]", "machine") is True


def test_long_machine_line_is_not_reused():
    # a long line from a cheap engine is context-dependent - do not trust it
    assert sinhalasub.memory_reusable(HARD, "machine") is False


def test_long_llm_line_is_reused():
    # the whole point: a hard line an LLM already solved should come back free
    assert sinhalasub.memory_reusable(HARD, "llm") is True


def test_long_corrected_line_is_reused():
    assert sinhalasub.memory_reusable(HARD, "correction") is True


# ----- end to end: LLM result reused by a later free run ---------------------

def test_llm_polished_hard_line_is_reused_by_a_later_google_run(tmp_path, monkeypatch):
    db_path = str(tmp_path / "m.db")
    monkeypatch.setattr(sinhalasub, "DB_PATH", db_path)

    # A polish pass solved the hard line and stored it at llm quality.
    mem = memory_db.MemoryDB(db_path)
    mem.store({HARD: "හොඳ පරිවර්තනය"}, engine="claude", tier="llm")

    # Later, a plain (free) Google run meets the same line.
    prefill = sinhalasub.memory_prefill(
        _subs([HARD]), min_tier=memory_db.engine_tier("google"))
    assert prefill == {0: "හොඳ පරිවර්තනය"}


def test_machine_line_is_not_reused_by_a_better_run(tmp_path, monkeypatch):
    db_path = str(tmp_path / "m.db")
    monkeypatch.setattr(sinhalasub, "DB_PATH", db_path)
    memory_db.MemoryDB(db_path).store({HARD: "යන්ත්‍ර"}, engine="google",
                                      tier="machine")
    prefill = sinhalasub.memory_prefill(
        _subs([HARD]), min_tier=memory_db.engine_tier("hybrid"))
    assert prefill == {}


# ----- per-line provenance from the hybrid engine ---------------------------

class _Fast:
    def available(self):
        return True

    def translate(self, prompt, stdin_text, timeout):
        out = []
        for line in stdin_text.splitlines():
            num, _, src = line.partition("|||")
            if num.strip().isdigit():
                out.append("%s|||fast" % num.strip())
        return "\n".join(out) + "\n"


class _Good(_Fast):
    def translate(self, prompt, stdin_text, timeout):
        out = []
        for line in stdin_text.splitlines():
            num, _, src = line.partition("|||")
            if num.strip().isdigit():
                out.append("%s|||polished" % num.strip())
        return "\n".join(out) + "\n"


def test_hybrid_records_which_sources_it_polished():
    prov = providers.HybridProvider(_Fast(), _Good(), min_words=6)
    stdin = "TRANSLATE:\n1|||Yes\n2|||%s\n" % HARD
    prov.translate("P", stdin, 60)
    # only the hard line went to the better engine, and we can prove which
    assert HARD in prov.polished_sources
    assert "Yes" not in prov.polished_sources


def test_hybrid_polished_set_is_empty_when_nothing_is_hard():
    prov = providers.HybridProvider(_Fast(), _Good(), min_words=6)
    prov.translate("P", "TRANSLATE:\n1|||Yes\n", 60)
    assert prov.polished_sources == set()
