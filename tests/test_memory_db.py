import memory_db as db


def _open(tmp_path):
    return db.MemoryDB(str(tmp_path / "t.db"))


# ----- quality tiers ---------------------------------------------------------

def test_tier_ranking_order():
    assert db.tier_rank("correction") > db.tier_rank("llm")
    assert db.tier_rank("llm") > db.tier_rank("hybrid")
    assert db.tier_rank("hybrid") > db.tier_rank("machine")
    assert db.tier_rank("unknown-engine") == db.tier_rank("machine")


def test_store_and_lookup(tmp_path):
    m = _open(tmp_path)
    m.store({"Hello": "හලෝ"}, engine="google", tier="machine")
    assert m.lookup(["Hello"]) == {"Hello": "හලෝ"}


def test_better_tier_overwrites_worse(tmp_path):
    m = _open(tmp_path)
    m.store({"Hello": "machine version"}, engine="google", tier="machine")
    m.store({"Hello": "llm version"}, engine="claude", tier="llm")
    assert m.lookup(["Hello"])["Hello"] == "llm version"


def test_worse_tier_never_overwrites_better(tmp_path):
    m = _open(tmp_path)
    m.store({"Hello": "llm version"}, engine="claude", tier="llm")
    m.store({"Hello": "machine version"}, engine="google", tier="machine")
    assert m.lookup(["Hello"])["Hello"] == "llm version"


def test_lookup_can_require_a_minimum_tier(tmp_path):
    m = _open(tmp_path)
    m.store({"Hello": "machine version"}, engine="google", tier="machine")
    # asking for llm-or-better must not return the machine entry
    assert m.lookup(["Hello"], min_tier="llm") == {}


# ----- corrections always win -------------------------------------------------

def test_correction_beats_everything(tmp_path):
    m = _open(tmp_path)
    m.store({"Hello": "llm version"}, engine="claude", tier="llm")
    m.save_correction("Hello", "my fix")
    assert m.lookup(["Hello"])["Hello"] == "my fix"


def test_correction_is_not_overwritten_by_later_runs(tmp_path):
    m = _open(tmp_path)
    m.save_correction("Hello", "my fix")
    m.store({"Hello": "llm version"}, engine="claude", tier="llm")
    assert m.lookup(["Hello"])["Hello"] == "my fix"


def test_corrections_count(tmp_path):
    m = _open(tmp_path)
    m.save_correction("a", "1")
    m.save_correction("b", "2")
    assert m.stats()["corrections"] == 2


# ----- learned names ----------------------------------------------------------

def test_names_round_trip(tmp_path):
    m = _open(tmp_path)
    m.learn_name("Marseille", "මාර්සෙයි")
    assert m.names()["Marseille"] == "මාර්සෙයි"


def test_learned_names_feed_the_glossary(tmp_path):
    m = _open(tmp_path)
    m.learn_name("John", "ජෝන්")
    merged = m.glossary_with_names({"Paris": "පැරිස්"})
    assert merged == {"Paris": "පැරිස්", "John": "ජෝන්"}


def test_explicit_glossary_wins_over_learned_name(tmp_path):
    m = _open(tmp_path)
    m.learn_name("John", "learned")
    assert m.glossary_with_names({"John": "explicit"})["John"] == "explicit"


# ----- history ---------------------------------------------------------------

def test_history_records_a_run(tmp_path):
    m = _open(tmp_path)
    m.record_run("Movie.srt", engine="google", cues=1200, seconds=210.5)
    rows = m.history()
    assert len(rows) == 1
    assert rows[0]["file"] == "Movie.srt"
    assert rows[0]["cues"] == 1200


def test_history_newest_first(tmp_path):
    m = _open(tmp_path)
    m.record_run("a.srt", engine="google", cues=1, seconds=1)
    m.record_run("b.srt", engine="google", cues=2, seconds=1)
    assert [r["file"] for r in m.history()][0] == "b.srt"


# ----- migration from the old schema -----------------------------------------

def test_migrates_legacy_memory_table(tmp_path):
    import sqlite3
    path = str(tmp_path / "legacy.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE memory (source TEXT PRIMARY KEY, sinhala TEXT NOT NULL,"
                " model TEXT, created TEXT)")
    con.execute("INSERT INTO memory VALUES ('Old line','පැරණි','haiku','2026-01-01')")
    con.commit()
    con.close()

    m = db.MemoryDB(path)
    # legacy rows survive and are readable through the new API
    assert m.lookup(["Old line"])["Old line"] == "පැරණි"
    # they came from an LLM, so they are treated as llm tier, not machine
    assert m.lookup(["Old line"], min_tier="llm") != {}


def test_stats_reports_totals(tmp_path):
    m = _open(tmp_path)
    m.store({"a": "1", "b": "2"}, engine="google", tier="machine")
    s = m.stats()
    assert s["lines"] == 2
    assert s["corrections"] == 0
