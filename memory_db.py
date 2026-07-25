"""Translation memory database.

Everything the app learns lives here, so quality compounds instead of being
re-earned on every movie:

* **Lines** - every translated line, tagged with the engine that produced it and
  a quality tier. A machine translation can never overwrite an LLM one, and a
  run can ask for "llm quality or better" so a fast free pass does not drag a
  careful pass down.
* **Corrections** - lines you fixed by hand. These outrank every engine and are
  reused forever, so the same mistake is never made twice.
* **Names** - learned transliterations, so a character keeps the same Sinhala
  spelling across every movie you translate.
* **History** - what was translated, with which engine, how long it took.
"""

import os
import sqlite3

# Higher wins. Anything unrecognised is treated as raw machine output.
TIERS = {"machine": 10, "hybrid": 30, "llm": 60, "correction": 100}
DEFAULT_TIER = "machine"


def tier_rank(tier):
    return TIERS.get(tier, TIERS[DEFAULT_TIER])


def engine_tier(provider_key):
    """Map a provider key to the quality tier its output deserves."""
    return {"google": "machine", "hybrid": "hybrid", "openai": "llm"}.get(
        provider_key, DEFAULT_TIER)


class MemoryDB:
    """SQLite-backed memory. Safe to construct per operation; cheap to open."""

    def __init__(self, path):
        self.path = path
        self._migrate()

    def _con(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    # ----- schema ----------------------------------------------------------

    def _migrate(self):
        con = self._con()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS lines ("
                " source TEXT PRIMARY KEY,"
                " sinhala TEXT NOT NULL,"
                " engine TEXT,"
                " tier TEXT NOT NULL DEFAULT 'machine',"
                " rank INTEGER NOT NULL DEFAULT 10,"
                " updated TEXT DEFAULT CURRENT_TIMESTAMP)")
            con.execute(
                "CREATE TABLE IF NOT EXISTS corrections ("
                " source TEXT PRIMARY KEY,"
                " sinhala TEXT NOT NULL,"
                " created TEXT DEFAULT CURRENT_TIMESTAMP)")
            con.execute(
                "CREATE TABLE IF NOT EXISTS names ("
                " term TEXT PRIMARY KEY,"
                " sinhala TEXT NOT NULL,"
                " created TEXT DEFAULT CURRENT_TIMESTAMP)")
            con.execute(
                "CREATE TABLE IF NOT EXISTS history ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " file TEXT, engine TEXT, cues INTEGER, seconds REAL,"
                " created TEXT DEFAULT CURRENT_TIMESTAMP)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_lines_rank ON lines(rank)")

            # Carry over the original single-table format. Those rows came from
            # the Claude CLI, so they are LLM quality, not machine output.
            has_legacy = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory'"
            ).fetchone()
            if has_legacy:
                already = con.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
                if not already:
                    con.execute(
                        "INSERT OR IGNORE INTO lines"
                        " (source, sinhala, engine, tier, rank, updated)"
                        " SELECT source, sinhala, COALESCE(model,'claude'), 'llm', ?,"
                        " created FROM memory", (TIERS["llm"],))
            con.commit()
        finally:
            con.close()

    # ----- lines -----------------------------------------------------------

    def lookup(self, sources, min_tier=None):
        """Return {source: sinhala}. Corrections always win over engine output."""
        sources = [s for s in dict.fromkeys(sources) if s]
        if not sources:
            return {}
        floor = tier_rank(min_tier) if min_tier else 0
        found = {}
        con = self._con()
        try:
            for start in range(0, len(sources), 400):
                chunk = sources[start:start + 400]
                marks = ",".join("?" * len(chunk))
                for row in con.execute(
                        "SELECT source, sinhala FROM lines"
                        " WHERE source IN (%s) AND rank >= ?" % marks,
                        chunk + [floor]):
                    found[row["source"]] = row["sinhala"]
                # corrections outrank everything, so they are applied last
                for row in con.execute(
                        "SELECT source, sinhala FROM corrections"
                        " WHERE source IN (%s)" % marks, chunk):
                    found[row["source"]] = row["sinhala"]
        finally:
            con.close()
        return found

    def store(self, pairs, engine="", tier=DEFAULT_TIER):
        """Insert or upgrade lines; never downgrades a better existing entry."""
        if not pairs:
            return 0
        rank = tier_rank(tier)
        con = self._con()
        try:
            con.executemany(
                "INSERT INTO lines (source, sinhala, engine, tier, rank, updated)"
                " VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)"
                " ON CONFLICT(source) DO UPDATE SET"
                "  sinhala=excluded.sinhala, engine=excluded.engine,"
                "  tier=excluded.tier, rank=excluded.rank,"
                "  updated=CURRENT_TIMESTAMP"
                " WHERE excluded.rank >= lines.rank",
                [(s, t, engine, tier, rank) for s, t in pairs.items() if s and t])
            con.commit()
            return con.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
        finally:
            con.close()

    # ----- corrections -----------------------------------------------------

    def save_correction(self, source, sinhala):
        con = self._con()
        try:
            con.execute(
                "INSERT INTO corrections (source, sinhala) VALUES (?,?)"
                " ON CONFLICT(source) DO UPDATE SET sinhala=excluded.sinhala",
                (source, sinhala))
            con.commit()
        finally:
            con.close()

    def corrections(self):
        con = self._con()
        try:
            return {r["source"]: r["sinhala"]
                    for r in con.execute("SELECT source, sinhala FROM corrections")}
        finally:
            con.close()

    # ----- names -----------------------------------------------------------

    def learn_name(self, term, sinhala):
        con = self._con()
        try:
            con.execute(
                "INSERT INTO names (term, sinhala) VALUES (?,?)"
                " ON CONFLICT(term) DO UPDATE SET sinhala=excluded.sinhala",
                (term, sinhala))
            con.commit()
        finally:
            con.close()

    def names(self):
        con = self._con()
        try:
            return {r["term"]: r["sinhala"]
                    for r in con.execute("SELECT term, sinhala FROM names")}
        finally:
            con.close()

    def glossary_with_names(self, glossary):
        """Learned names, with the user's explicit glossary taking priority."""
        merged = dict(self.names())
        merged.update(glossary or {})
        return merged

    # ----- history / stats -------------------------------------------------

    def record_run(self, file, engine="", cues=0, seconds=0.0):
        con = self._con()
        try:
            con.execute(
                "INSERT INTO history (file, engine, cues, seconds) VALUES (?,?,?,?)",
                (os.path.basename(file or ""), engine, int(cues), float(seconds)))
            con.commit()
        finally:
            con.close()

    def history(self, limit=50):
        con = self._con()
        try:
            return [dict(r) for r in con.execute(
                "SELECT file, engine, cues, seconds, created FROM history"
                " ORDER BY id DESC LIMIT ?", (limit,))]
        finally:
            con.close()

    def stats(self):
        con = self._con()
        try:
            one = lambda q: con.execute(q).fetchone()[0]  # noqa: E731
            return {
                "lines": one("SELECT COUNT(*) FROM lines"),
                "corrections": one("SELECT COUNT(*) FROM corrections"),
                "names": one("SELECT COUNT(*) FROM names"),
                "runs": one("SELECT COUNT(*) FROM history"),
                "cues_total": one("SELECT COALESCE(SUM(cues),0) FROM history"),
            }
        finally:
            con.close()
