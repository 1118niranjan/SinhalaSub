"""SinhalaSub - English to Sinhala subtitle translator using the local claude CLI.

Parses an English .srt with pysrt, translates it batch-by-batch through
`claude -p` (headless, on the user's Claude subscription - no API key),
and writes a <basename>.si.srt next to the input with identical cue
indexes and timestamps.

Batches run in parallel workers: batch context comes from the previous
source-English cues, not from previous translations, so parallelism does
not change translation quality. Progress is checkpointed to a sidecar
.si.partial.json after every batch so an interrupted run can resume.
"""

import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import webbrowser
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import colorchooser, filedialog, messagebox, ttk

import pysrt

import colorize
import memory_db
import providers
import quality
import subtitle_export

_HERE = os.path.dirname(os.path.abspath(__file__))


def load_env_file(path=None):
    """Load KEY=VALUE lines from a .env file next to this script.

    Real environment variables always win over .env values, and the app
    works fine with no .env at all. No python-dotenv dependency needed.
    """
    path = path or os.path.join(_HERE, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_env_file()


def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


BATCH_SIZE = _env_int("SINHALASUB_BATCH_SIZE", 30)   # cues per claude call
MAX_WORKERS = _env_int("SINHALASUB_WORKERS", 3)      # parallel claude calls
CLAUDE_TIMEOUT = _env_int("SINHALASUB_TIMEOUT", 600) # seconds per claude call
# Empty/unset = "CLI default": the exact engine the first version used, kept as
# the default so translation quality never silently changes between updates.
DEFAULT_MODEL = os.environ.get("SINHALASUB_MODEL", "").strip() or "CLI default"
MODEL_CHOICES = ["CLI default", "opus", "sonnet", "haiku"]
DEFAULT_COLOR = os.environ.get("SINHALASUB_COLOR", "").strip()

# Remembered UI choices (model, colour, workers, memory). Saved whenever you
# change them in the app, so you pick once and it sticks - no code or .env
# editing needed. Not secret, but gitignored to keep the repo clean.
SETTINGS_PATH = os.path.join(_HERE, "settings.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

CONTEXT_SIZE = 3         # previous cues sent as read-only context
MAX_ATTEMPTS = 3         # 1 initial try + 2 retries per batch
DELIM = "|||"

# Single line, and no double quotes or percent signs: on Windows the claude
# CLI is a .cmd shim, so this string passes through cmd.exe argument parsing.
PROMPT = (
    "You are an expert English to Sinhala subtitle translator. "
    "The input has a CONTEXT section with earlier subtitle lines that are for "
    "understanding only - never translate or output them - and a TRANSLATE section. "
    "First read everything and understand the scene, the tone, and who is speaking to whom. "
    "Then translate each line of the TRANSLATE section into natural, fluent, spoken Sinhala "
    "that a native reader finds smooth. Capture the intended meaning, tone, idioms and jokes. "
    "Never produce a robotic word-for-word translation. "
    "Keep each translation short enough to read comfortably on screen. "
    "Transliterate proper names and place names into Sinhala phonetically from their native "
    "pronunciation, keeping every syllable and every final sound - for example the French city "
    "Marseille is pronounced maar-se-yi, so its Sinhala form must keep all three parts including "
    "the closing yi glide, never a shortened or anglicized form. "
    "Prefer punchy, living Sinhala idioms that a native speaker would actually say over flat "
    "literal phrasing. "
    "Translate profanity and vulgar slang faithfully by intent: render each swear word with a "
    "natural Sinhala expression of the same strength and function in that sentence - for example "
    "an intensifier stays an intensifier, and an idiom like being screwed means being in serious "
    "trouble. Pick the raw, crude everyday word a real speaker would use, never a polite or "
    "sanitized paraphrase. Never censor, soften, or skip profanity, and never translate it "
    "literally when it is used figuratively. "
    "Do not translate sound or music cues such as [music] or [door slams]; output them unchanged. "
    "Return ONLY the translations, one per line, each prefixed with its number and the "
    "delimiter ||| in the form NUMBER|||translation. "
    "Output exactly one line for every line in the TRANSLATE section, with the same "
    "numbers in the same order. No commentary, no blank lines, no extra text."
)

LINE_RE = re.compile(r"^\s*(\d+)\s*\|\|\|\s*(.*?)\s*$")


class TranslationCancelled(Exception):
    """Raised when the user cancels a run; partial work is kept in the checkpoint."""


def find_claude():
    """Return the full path of the claude CLI, or None if it is not on PATH."""
    return shutil.which("claude")


def load_srt(path):
    """Parse an .srt file. pysrt auto-detects the encoding via chardet."""
    subs = pysrt.open(path)
    if len(subs) == 0:
        raise ValueError("No subtitle cues found in file: %s" % path)
    return subs


def flatten(text):
    """Collapse a cue's internal line breaks and runs of whitespace to one line."""
    return " ".join(text.split())


# Cues that carry no translatable words: [door slams], bare music notes, and
# lines with no letters at all (timestamps, numbers, dashes). Sending these to
# the model wastes tokens and time - the model just echoes them back - so they
# are filtered out locally and copied through unchanged.
_NOTE_ONLY_RE = re.compile(r"^[\s\W\d_]*$", re.UNICODE)
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def needs_translation(text):
    """True if the cue contains real words that a model should translate."""
    t = flatten(text or "")
    if not t:
        return False
    if BRACKET_RE.match(t):          # [door slams], [music]
        return False
    if not _LETTER_RE.search(t):     # ♪♪, "1985", "- ...", punctuation only
        return False
    return True


def make_batches(total, batch_size=None):
    """Split cue positions 0..total-1 into consecutive batches."""
    batch_size = batch_size or BATCH_SIZE
    return [list(range(start, min(start + batch_size, total)))
            for start in range(0, total, batch_size)]


def build_batch_input(subs, batch, skip=None):
    """Build the stdin text for one batch: read-only context + numbered lines.

    Numbers are 1-based positions in the file, so they are unique across the
    whole run and map straight back onto cues. Positions in `skip` (already
    translated, e.g. from translation memory) are shown as extra context so
    the model still sees the full scene, but are not translated again.
    """
    skip = skip or set()
    targets = [i for i in batch if i not in skip]
    known = [i for i in batch if i in skip]
    lines = ["CONTEXT (for understanding only - do not translate, do not output):"]
    first = batch[0]
    if first == 0:
        lines.append("(none - this is the start of the subtitles)")
    else:
        for i in range(max(0, first - CONTEXT_SIZE), first):
            lines.append("%d%s%s" % (i + 1, DELIM, flatten(subs[i].text)))
    if known:
        lines.append("")
        lines.append("ALSO CONTEXT (handled elsewhere - do not translate, do not output):")
        for i in known:
            lines.append("%d%s%s" % (i + 1, DELIM, flatten(subs[i].text)))
    lines.append("")
    lines.append("TRANSLATE (%d lines - output exactly these numbers):" % len(targets))
    for i in targets:
        lines.append("%d%s%s" % (i + 1, DELIM, flatten(subs[i].text)))
    return "\n".join(lines) + "\n"


def parse_response(stdout, expected):
    """Extract NUMBER|||text lines from claude's output.

    Returns {number: text} for expected numbers only, ignoring commentary,
    code fences, or context lines the model was told not to output.
    """
    found = {}
    for raw in stdout.splitlines():
        m = LINE_RE.match(raw)
        if m:
            n = int(m.group(1))
            text = m.group(2).strip()
            if n in expected and text:
                found[n] = text
    return found


def translate_batch(subs, batch, provider, log=None, cancel=None, skip=None):
    """Translate one batch of cue positions; returns {position: sinhala_text}.

    Retries the whole batch while lines are missing or malformed (MAX_ATTEMPTS
    total). Any cue still missing afterwards keeps its English text so cue
    alignment is never corrupted. Positions in `skip` are not translated.
    """
    targets = [i for i in batch if not skip or i not in skip]
    expected = {i + 1 for i in targets}
    stdin_text = build_batch_input(subs, batch, skip=skip)
    got = {}
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if cancel is not None and cancel.is_set():
            raise TranslationCancelled()
        try:
            out = provider.translate(PROMPT, stdin_text, CLAUDE_TIMEOUT)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            if log:
                log("attempt %d failed: %s" % (attempt, exc))
            continue
        for n, text in parse_response(out, expected).items():
            got.setdefault(n, text)
        if expected <= set(got):
            break
        if log:
            log("attempt %d: %d/%d lines parsed, retrying"
                % (attempt, len(got), len(expected)))
    if not got and last_error is not None:
        raise RuntimeError("provider failed for the whole batch: %s" % last_error)
    result = {}
    for i in targets:
        n = i + 1
        if n in got:
            result[i] = got[n]
        else:
            result[i] = subs[i].text  # keep English rather than corrupt alignment
            if log:
                log("cue %d left in English after retries" % n)
    return result


def translate_all(subs, provider, progress=None, log=None, workers=None,
                  cancel=None, initial=None, on_batch=None, batch_size=None):
    """Translate every cue; returns a list of Sinhala texts aligned to subs order.

    provider   - a providers.Provider (CLI, Anthropic, Gemini, or OpenAI-compatible)
    initial    - {position: text} already translated (resume); those batches skip
    on_batch   - called with each finished batch dict (used for checkpointing)
    batch_size - cues per request (larger = fewer requests, helps free-tier caps)
    progress(batches_done, batches_total) is called after each batch.
    """
    stop = cancel if cancel is not None else threading.Event()
    texts = [None] * len(subs)
    if initial:
        for i, t in initial.items():
            i = int(i)
            if 0 <= i < len(texts):
                texts[i] = t

    # Cues with no translatable words ([music], bare notes, numbers) never go to
    # the model - copy them through untouched. Pure token/time savings.
    for i, cue in enumerate(subs):
        if texts[i] is None and not needs_translation(cue.text):
            texts[i] = cue.text

    # Deduplicate: identical source lines ("Yeah.", "Okay.") are translated once
    # and the result is copied to every other occurrence after the run. Movies
    # repeat short lines heavily, so this cuts real work substantially.
    dup_of = {}
    first_seen = {}
    for i, cue in enumerate(subs):
        if texts[i] is not None:
            continue
        key = flatten(cue.text)
        if key in first_seen:
            dup_of[i] = first_seen[key]
        else:
            first_seen[key] = i

    def fan_out_duplicates():
        for pos, rep in dup_of.items():
            if texts[pos] is None:
                texts[pos] = (texts[rep] if texts[rep] is not None
                              else subs[pos].text)

    # Batch over the work itself, not over cue positions. Position-ranges meant
    # that every already-known cue (memory hit, duplicate, sound cue) still had
    # to be sent as context, so a "100-cue batch" could carry only a handful of
    # real translations while paying input tokens for all 100. Packing batches
    # with real work only is the single biggest token and time saving here.
    work = [i for i in range(len(subs)) if texts[i] is None and i not in dup_of]
    n_workers = max(1, workers or MAX_WORKERS)
    if batch_size:
        size = max(1, batch_size)
    else:
        # Auto: spread the work evenly over the workers so every worker gets one
        # batch and the whole run finishes in a single round. Wall-clock is then
        # (work / workers) x per-line cost instead of several sequential rounds.
        size = max(20, min(250, -(-len(work) // n_workers)))
    todo = [work[k:k + size] for k in range(0, len(work), size)]
    if not todo:
        fan_out_duplicates()
        return texts
    done = 0
    first_error = None
    user_cancelled = False
    with ThreadPoolExecutor(max_workers=max(1, workers or MAX_WORKERS)) as ex:
        futures = [ex.submit(translate_batch, subs, b, provider, log, stop, None)
                   for b in todo]
        for fut in as_completed(futures):
            try:
                result = fut.result()
            except TranslationCancelled:
                user_cancelled = True
                continue
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                stop.set()  # make the remaining batches bail out quickly
                continue
            for i, t in result.items():
                texts[i] = t
            done += 1
            if on_batch:
                on_batch(dict(result))
            if progress:
                progress(done, len(todo))
    if first_error is not None:
        raise first_error
    if user_cancelled or stop.is_set():
        raise TranslationCancelled()
    fan_out_duplicates()
    return texts


# ---------------------------------------------------------------------------
# Checkpoint (resume) support
# ---------------------------------------------------------------------------

def checkpoint_path(input_path):
    base = input_path[:-4] if input_path.lower().endswith(".srt") else input_path
    return base + ".si.partial.json"


def load_checkpoint(input_path, cue_count):
    """Return {position: text} from a matching checkpoint, else None."""
    path = checkpoint_path(input_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("cues") != cue_count or not data.get("texts"):
            return None
        return {int(k): v for k, v in data["texts"].items()}
    except (ValueError, OSError):
        return None


def save_checkpoint(input_path, cue_count, texts_map):
    """Atomically write the checkpoint sidecar next to the input file."""
    path = checkpoint_path(input_path)
    tmp = path + ".tmp"
    payload = {"source": os.path.abspath(input_path), "cues": cue_count,
               "texts": {str(k): v for k, v in texts_map.items()}}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def clear_checkpoint(input_path):
    try:
        os.remove(checkpoint_path(input_path))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Translation memory: a local SQLite cache of every line ever translated.
#
# Reuse is deliberately limited to SHORT lines (stock phrases like Yeah, Okay,
# Thank you) and [bracket] cues: those are safe out of context. Longer lines
# are always freshly translated, because the same English sentence can need a
# different Sinhala rendering depending on scene, tone, and speaker - and
# accuracy is the top requirement.
# ---------------------------------------------------------------------------

DB_PATH = os.path.join(_HERE, "translations.db")
MEMORY_MAX_WORDS = 5
BRACKET_RE = re.compile(r"^\[[^\]]*\]$")
SINHALA_RE = re.compile(u"[඀-෿]")


def get_memory():
    """The translation-memory database (schema is created/migrated on open)."""
    return memory_db.MemoryDB(DB_PATH)


def memory_reusable(source, tier="machine"):
    """Whether a remembered line can be reused for this cue.

    Short stock phrases ("Yeah.", "Okay.") and [sound cues] are safe whatever
    produced them. Longer lines depend on scene and tone, so a cheap machine
    translation of one is not trusted - but a line an LLM already worked out, or
    one you corrected by hand, is exactly what should come back free next time.
    """
    if BRACKET_RE.match(source) or len(source.split()) <= MEMORY_MAX_WORDS:
        return True
    return memory_db.tier_rank(tier) >= memory_db.tier_rank("llm")


def memory_prefill(subs, min_tier=None):
    """Return {position: sinhala} for cues whose text is safely known already.

    `min_tier` stops a cheap machine translation from being reused when the user
    has chosen a higher-quality engine for this run - otherwise the memory would
    quietly drag a careful pass back down to the quality of the fastest one.
    """
    flat = [flatten(c.text) for c in subs]
    found = get_memory().lookup_detailed(flat, min_tier=min_tier)
    return {i: found[f][0] for i, f in enumerate(flat)
            if f in found and memory_reusable(f, found[f][1])}


def memory_collect(subs, texts):
    """Pairs worth storing: real Sinhala translations plus untouched [cues]."""
    pairs = {}
    for cue, sin in zip(subs, texts):
        src = flatten(cue.text)
        if not src or not sin:
            continue
        if SINHALA_RE.search(sin) or (BRACKET_RE.match(src) and sin.strip() == src):
            pairs[src] = sin
    return pairs


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------

def default_output_path(input_path):
    """Movie.2021.srt -> Movie.2021.si.srt (PotPlayer auto-loads it next to the video)."""
    base = input_path[:-4] if input_path.lower().endswith(".srt") else input_path
    return base + ".si.srt"


def unused_path(path):
    """Return path itself if free, else the first 'name (n).srt' that is free."""
    if not os.path.exists(path):
        return path
    base = path[:-4] if path.lower().endswith(".srt") else path
    n = 1
    while os.path.exists("%s (%d).srt" % (base, n)):
        n += 1
    return "%s (%d).srt" % (base, n)


def write_output(subs, texts, out_path):
    """Write the translated .srt: same cue indexes and timestamps, UTF-8 without BOM."""
    if len(texts) != len(subs) or any(t is None for t in texts):
        raise ValueError("translation count does not match cue count")
    for cue, text in zip(subs, texts):
        cue.text = text
    subs.save(out_path, encoding="utf-8")


# ---------------------------------------------------------------------------
# Optional OpenSubtitles support (active only when OPENSUBTITLES_API_KEY is set)
# ---------------------------------------------------------------------------

OS_API_BASE = "https://api.opensubtitles.com/api/v1"


def opensubtitles_key():
    """The only place the key is read: from the environment (or .env), never hardcoded."""
    return os.environ.get("OPENSUBTITLES_API_KEY", "").strip()


def _os_headers(key):
    return {"Api-Key": key, "User-Agent": "SinhalaSub v1.0", "Accept": "application/json"}


def _norm_title(text):
    """Lowercase, alphanumeric-only form of a title, for comparing loosely."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


_TITLE_STOPWORDS = {"the", "a", "an", "of", "and", "part", "ii", "iii", "iv", "vs"}


def _title_tokens(text):
    """Meaningful words of a title, for judging whether two titles are related."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _TITLE_STOPWORDS}


def split_year(query):
    """Pull a trailing 4-digit year out of a film name.

    People naturally type "Mortal Kombat II 2026" into one box, but the API
    cannot match a title with the year glued on and silently returns unrelated
    films instead, so the year is separated before searching.
    """
    m = re.search(r"^(.*?)[\s(\[]+((?:19|20)\d{2})[\s)\]]*$", (query or "").strip())
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2)
    return (query or "").strip(), ""


def _is_related(movie, query):
    """Whether a returned film plausibly answers what was searched for.

    OpenSubtitles answers an unmatched search with a page of unrelated films
    rather than an empty list, so anything sharing no meaningful word with the
    query is dropped instead of being shown as if it were a result.
    """
    if not movie:
        return True  # nothing to judge it on; let the user decide
    qt, mt = _title_tokens(query), _title_tokens(movie)
    if qt & mt:
        return True
    nq, nm = _norm_title(query), _norm_title(movie)
    return bool(nq) and (nq in nm or nm in nq)


def os_search(key, query, year=""):
    """Search subtitles by film name; returns a list of result dicts.

    OpenSubtitles matches loosely - searching "The Gift" also returns "The
    Gifted", "The Ultimate Gift" and so on - and its `release` field is a
    release name that often does not contain the film's title at all. So each
    result carries the film name and year, the label always shows them, exact
    title matches are listed first, and the most-downloaded (usually the
    best-synced) subtitle for a film comes before the rest.
    """
    import requests  # lazy: local-file mode must work without requests installed

    # A year typed into the film box has to be separated out, or the API matches
    # nothing and answers with unrelated films.
    query, inline_year = split_year(query)
    year = str(year).strip() or inline_year

    def fetch(with_year):
        params = {"query": query, "languages": "en"}
        if with_year:
            params["year"] = with_year
        resp = requests.get(OS_API_BASE + "/subtitles", params=params,
                            headers=_os_headers(key), timeout=30)
        resp.raise_for_status()
        return resp.json().get("data", []) or []

    data = fetch(year)
    kept = [d for d in data
            if _is_related(((d.get("attributes") or {}).get("feature_details")
                            or {}).get("movie_name")
                           or ((d.get("attributes") or {}).get("feature_details")
                               or {}).get("title"), query)]
    # A year that matches no film makes the API ignore the title entirely, so if
    # filtering leaves nothing, try again on the title alone.
    if year and not kept:
        data = fetch("")
        kept = [d for d in data
                if _is_related(((d.get("attributes") or {}).get("feature_details")
                                or {}).get("movie_name")
                               or ((d.get("attributes") or {}).get("feature_details")
                                   or {}).get("title"), query)]
    hidden = len(data) - len(kept)

    wanted = _norm_title(query)
    results = []
    for item in kept:
        attrs = item.get("attributes") or {}
        files = attrs.get("files") or []
        if not files or files[0].get("file_id") is None:
            continue
        details = attrs.get("feature_details") or {}
        movie = (details.get("movie_name") or details.get("title") or "").strip()
        # Entries arrive prefixed with the year, e.g. "2010 - Inception", and for
        # films with no year on record just " - Inception".
        movie = re.sub(r"^\s*(?:(?:19|20)\d{2})?\s*-\s*", "", movie).strip()
        yr = details.get("year") or ""
        release = (attrs.get("release") or files[0].get("file_name") or "").strip()
        downloads = int(attrs.get("download_count") or 0)

        head = movie or release or str(files[0]["file_id"])
        if movie and yr:
            head = "%s (%s)" % (movie, yr)
        label = head
        # Skip the release when it just repeats the title, e.g. "Inception (2010)".
        if release and _norm_title(release) not in (_norm_title(head),
                                                    _norm_title(movie)):
            label += "  ·  %s" % release[:52]
        if downloads:
            label += "  ·  %s downloads" % f"{downloads:,}"

        results.append({
            "file_id": files[0]["file_id"],
            "movie": movie,
            "year": str(yr),
            "release": release,
            "downloads": downloads,
            "label": label,
            "hidden_unrelated": hidden,
        })

    def rank(r):
        norm = _norm_title(r["movie"])
        exact = 0 if norm and norm == wanted else (1 if wanted in norm else 2)
        return (exact, -r["downloads"])

    results.sort(key=rank)
    if not results:
        # Report it rather than showing a page of films nobody asked for.
        return [{"file_id": None, "movie": "", "year": "", "release": "",
                 "downloads": 0, "hidden_unrelated": hidden,
                 "label": "No subtitles found for \"%s\"%s" %
                          (query, " (%d unrelated result(s) hidden)" % hidden
                           if hidden else "")}]
    return results


def os_download(key, file_id, dest_path):
    """Request a download link for file_id and save the .srt to dest_path."""
    import requests
    resp = requests.post(OS_API_BASE + "/download", json={"file_id": file_id},
                         headers=_os_headers(key), timeout=30)
    resp.raise_for_status()
    link = resp.json().get("link")
    if not link:
        raise RuntimeError("OpenSubtitles returned no download link")
    # Only follow an encrypted link. The URL comes from a remote service, so a
    # compromised or tampered response must not be able to downgrade us to
    # plain HTTP or point at a local file:// path.
    if not str(link).lower().startswith("https://"):
        raise RuntimeError("Refusing a non-HTTPS download link from OpenSubtitles")
    data = requests.get(link, timeout=60)
    data.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(data.content)
    return dest_path


# ---------------------------------------------------------------------------
# Tkinter GUI (modern dark theme, pure ttk - no extra dependencies)
# ---------------------------------------------------------------------------

PREVIEW_COUNT = 10
SINHALA_FONT = ("Nirmala UI", 11)

BG = "#0f1018"          # window background
CARD = "#171926"        # raised panels
FIELD = "#1f2233"       # inputs
FIELD_HI = "#272b40"    # hovered inputs
BORDER = "#2b2f47"
TEXT = "#ececf6"
DIM = "#9296b4"
ACCENT = "#7c6cff"
ACCENT_HOVER = "#9186ff"
OK = "#4ade80"
WARN = "#fbbf24"


def apply_style(root):
    root.configure(bg=BG)
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Dim.TLabel", foreground=DIM)
    style.configure("Header.TLabel", font=("Segoe UI Semibold", 17))
    style.configure("TButton", background=FIELD, foreground=TEXT, borderwidth=0,
                    focusthickness=0, padding=(14, 7))
    style.map("TButton",
              background=[("active", "#333754"), ("disabled", "#1b1d2c")],
              foreground=[("disabled", "#595c78")])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
    style.map("Accent.TButton",
              background=[("active", ACCENT_HOVER), ("disabled", "#3b3766")],
              foreground=[("disabled", "#8f8cb5")])
    style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT,
                    insertcolor=TEXT, bordercolor=BORDER, lightcolor=FIELD,
                    darkcolor=FIELD, padding=6)
    style.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                    foreground=TEXT, arrowcolor=TEXT, bordercolor=BORDER,
                    lightcolor=FIELD, darkcolor=FIELD, padding=4)
    style.map("TCombobox",
              fieldbackground=[("readonly", FIELD)],
              foreground=[("readonly", TEXT)])
    style.configure("TSpinbox", fieldbackground=FIELD, foreground=TEXT,
                    background=FIELD, arrowcolor=TEXT, bordercolor=BORDER,
                    lightcolor=FIELD, darkcolor=FIELD, insertcolor=TEXT, padding=4)
    style.configure("Horizontal.TProgressbar", troughcolor=FIELD, background=ACCENT,
                    bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT, thickness=12)
    style.configure("TCheckbutton", background=BG, foreground=TEXT, focuscolor=BG,
                    indicatorcolor=FIELD)
    style.map("TCheckbutton",
              background=[("active", BG)],
              indicatorcolor=[("selected", ACCENT)])
    style.configure("TLabelframe", background=BG, bordercolor=BORDER,
                    lightcolor=BG, darkcolor=BG)
    style.configure("TLabelframe.Label", background=BG, foreground=DIM)

    # Tabs: clam renders these almost invisibly by default, so set every state
    # explicitly - the selected tab reads as a lit card, the rest sit back.
    style.configure("TNotebook", background=BG, bordercolor=BORDER,
                    lightcolor=BG, darkcolor=BG, tabmargins=(0, 6, 0, 0))
    style.configure("TNotebook.Tab", background=CARD, foreground=DIM,
                    bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                    padding=(20, 10), font=("Segoe UI Semibold", 10))
    style.map("TNotebook.Tab",
              background=[("selected", ACCENT), ("active", FIELD_HI)],
              foreground=[("selected", "#ffffff"), ("active", TEXT)],
              lightcolor=[("selected", ACCENT)],
              darkcolor=[("selected", ACCENT)],
              expand=[("selected", (0, 0, 0, 0))])

    style.configure("Card.TFrame", background=CARD)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT)
    style.configure("CardDim.TLabel", background=CARD, foreground=DIM)
    style.configure("Title.TLabel", font=("Segoe UI Semibold", 20), foreground=TEXT)
    style.configure("OK.TLabel", foreground=OK, background=BG)
    style.configure("Warn.TLabel", foreground=WARN, background=BG)
    style.configure("Small.TButton", padding=(9, 4))
    style.configure("Vertical.TScrollbar", background=FIELD, troughcolor=BG,
                    bordercolor=BG, arrowcolor=DIM, lightcolor=FIELD,
                    darkcolor=FIELD)


def _fmt_time(seconds):
    m, s = divmod(int(max(0, seconds)), 60)
    h, m = divmod(m, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


COLOR_PRESETS = [
    ("None (player default)", ""),
    ("Yellow", "#FFD700"),
    ("Cyan", "#00FFFF"),
    ("Light green", "#90EE90"),
    ("Orange", "#FFA500"),
    ("White", "#FFFFFF"),
    ("Custom...", None),
]


class SinhalaSubApp:
    def __init__(self, root, dnd_available=False):
        self.root = root
        self.dnd = dnd_available
        self._images = []  # keep PhotoImage refs alive
        self.claude_path = find_claude()
        self.os_key = providers.load_secrets().get("opensubtitles", "") or opensubtitles_key()
        self.subs = None
        self.texts = None
        self.current_input = None
        self.msgs = queue.Queue()
        self.os_results = []
        self.cancel_event = threading.Event()
        self.t_start = None
        self.running = False
        self.eta_target = None
        self.batches_done = 0
        self.batches_total = 0
        self.phase = ""
        self.last_model = ""
        self.settings = load_settings()
        self.color_hex = self.settings.get("color_hex", DEFAULT_COLOR)
        self._loaded = False  # gate autosave until widgets are built
        self._alive = True    # stop the poll/tick loops once the window closes

        apply_style(root)
        root.title("SinhalaSub — by NLK")
        root.minsize(800, 620)
        icon = os.path.join(self.ASSETS, "app.ico")
        if os.path.isfile(icon):
            try:
                root.iconbitmap(icon)
            except tk.TclError:
                pass  # some window managers reject .ico; harmless

        self.provider_key = self.settings.get("provider", "cli")

        menubar = tk.Menu(root)
        self.providers_menu = tk.Menu(menubar, tearoff=0)
        self._menu_provider = tk.StringVar(value=self.provider_key)
        for desc in providers.PROVIDERS:
            self.providers_menu.add_radiobutton(
                label=desc["label"], value=desc["key"],
                variable=self._menu_provider,
                command=lambda k=desc["key"]: self._select_provider(k))
        self.providers_menu.add_separator()
        self.providers_menu.add_command(label="Settings…",
                                        command=self.open_provider_settings)
        menubar.add_cascade(label="Providers", menu=self.providers_menu)

        tools = tk.Menu(menubar, tearoff=0)
        tools.add_command(label="OpenSubtitles API key…",
                          command=self.open_opensubtitles_key)
        tools.add_command(label="Sign in to Claude (for polish)…",
                          command=self.claude_sign_in)
        tools.add_separator()
        tools.add_command(label="Character & place names…", command=self.open_names)
        tools.add_command(label="Quality report…", command=self.show_quality_report)
        tools.add_command(label="Translation memory…", command=self.show_memory_stats)
        tools.add_separator()
        tools.add_command(label="Open output folder",
                          command=self.open_output_folder)
        menubar.add_cascade(label="Tools", menu=tools)
        menubar.add_command(label="About", command=self.show_about)
        root.config(menu=menubar)

        outer = ttk.Frame(root, padding=(16, 12, 16, 14))
        outer.pack(fill="both", expand=True)
        self._os_panel = None

        self._build_header(outer)

        # Engine knobs live in Providers -> Settings so this window stays clean.
        saved_model = self.settings.get("model")
        init_model = saved_model if saved_model in MODEL_CHOICES else (
            DEFAULT_MODEL if DEFAULT_MODEL in MODEL_CHOICES else "CLI default")
        self.model_var = tk.StringVar(value=init_model)
        self.workers_var = tk.StringVar(value=str(self.settings.get("workers", MAX_WORKERS)))
        self.batch_var = tk.StringVar(value=str(self.settings.get("batch_size", "Auto")))

        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)
        main = ttk.Frame(nb, padding=16)
        colour_tab = ttk.Frame(nb, padding=16)
        review_tab = ttk.Frame(nb, padding=16)
        batch_tab = ttk.Frame(nb, padding=16)
        timing_tab = ttk.Frame(nb, padding=16)
        nb.add(main, text="Translate")
        nb.add(review_tab, text="Review & Fix")
        nb.add(colour_tab, text="Colour & Style")
        nb.add(batch_tab, text="Batch")
        nb.add(timing_tab, text="Timing")
        self.main = main

        hint = ("Drop a .srt file anywhere on this tab, or browse for one."
                if self.dnd else "Choose an English .srt file.")
        ttk.Label(main, text=hint, style="Dim.TLabel").pack(anchor="w")

        file_row = ttk.Frame(main)
        file_row.pack(fill="x", pady=(6, 0))
        ttk.Label(file_row, text="English .srt:").pack(side="left")
        self.input_var = tk.StringVar()
        ttk.Entry(file_row, textvariable=self.input_var).pack(
            side="left", fill="x", expand=True, padx=8)
        ttk.Button(file_row, text="Browse...", command=self.browse).pack(side="left")

        self.video_var = tk.StringVar(value="")
        ttk.Label(main, textvariable=self.video_var, style="OK.TLabel").pack(
            anchor="w", pady=(4, 0))

        lang_row = ttk.Frame(main)
        lang_row.pack(fill="x", pady=(10, 0))
        ttk.Label(lang_row, text="Translate from", style="Dim.TLabel").pack(side="left")
        saved_lang = self.settings.get("source_lang", "auto")
        self._lang_labels = {name: code
                             for code, name in providers.SOURCE_LANGUAGES}
        self.lang_var = tk.StringVar(value=providers.language_name(saved_lang))
        ttk.Combobox(lang_row, textvariable=self.lang_var, state="readonly", width=22,
                     values=[n for _c, n in providers.SOURCE_LANGUAGES]).pack(
            side="left", padx=(6, 6))
        ttk.Label(lang_row, text="→  සිංහල", style="Dim.TLabel").pack(side="left")
        self.lang_var.trace_add("write", self._on_lang_change)

        if self.os_key:
            self._build_opensubtitles(main)

        opts2 = ttk.Frame(main)
        opts2.pack(fill="x", pady=(12, 0))
        self.memory_var = tk.BooleanVar(value=self.settings.get("memory", True))
        ttk.Checkbutton(opts2, text="Translation memory (reuse saved lines)",
                        variable=self.memory_var).pack(side="left")
        self.fresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts2, text="Re-translate fresh (ignore saved)",
                        variable=self.fresh_var).pack(side="left", padx=18)

        # Kept so the translate/preview flow can still tint the output.
        self.color_name = tk.StringVar(
            value=next((n for n, h in COLOR_PRESETS if h == self.color_hex),
                       "Custom..." if self.color_hex else "None (player default)"))

        act = ttk.Frame(main)
        act.pack(fill="x", pady=(14, 0))
        self.translate_btn = ttk.Button(
            act, text="Translate to Sinhala", style="Accent.TButton",
            command=self.start_translate)
        self.translate_btn.pack(side="left")
        self.cancel_btn = ttk.Button(act, text="Cancel", command=self.cancel_run)
        self.cancel_btn.pack(side="left", padx=8)
        self.cancel_btn.state(["disabled"])

        self.progress = ttk.Progressbar(main, mode="determinate")
        self.progress.pack(fill="x", pady=(14, 0))
        self.status_var = tk.StringVar(value="Select an English .srt file.")
        ttk.Label(main, textvariable=self.status_var, style="Dim.TLabel",
                  wraplength=660).pack(anchor="w", pady=(8, 0))

        self._build_review_tab(review_tab)
        self._build_colour_tab(colour_tab)
        self._build_batch_tab(batch_tab)
        self._build_timing_tab(timing_tab)
        # Dropping a file anywhere on either file field loads it.
        self._make_drop_target(main, self._drop_translate)
        self._make_drop_target(colour_tab, self._drop_colour)

        self._update_translate_gate()

        # Remember choices: save whenever the user changes any of these.
        self._loaded = True
        self.model_var.trace_add("write", self._persist)
        self.workers_var.trace_add("write", self._persist)
        self.batch_var.trace_add("write", self._persist)
        self.memory_var.trace_add("write", self._persist)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._poll_id = root.after(100, self._poll)
        self._tick_id = root.after(1000, self._tick)

    # ----- settings persistence --------------------------------------------

    def _persist(self, *_):
        if not self._loaded:
            return
        # Merge into self.settings so provider/providers keys survive.
        self.settings["model"] = self.model_var.get()
        self.settings["workers"] = self.workers_var.get()
        self.settings["batch_size"] = self.batch_var.get()
        self.settings["memory"] = bool(self.memory_var.get())
        self.settings["color_hex"] = self.color_hex
        self.settings["color_name"] = self.color_name.get()
        if hasattr(self, "auto_vars"):
            self.settings["scheme"] = {
                k: {"on": bool(v.get()), "hex": self.auto_hex.get(k, "")}
                for k, v in self.auto_vars.items()}
        save_settings(self.settings)

    def _on_close(self):
        self._persist()
        self._alive = False
        for after_id in (getattr(self, "_poll_id", None), getattr(self, "_tick_id", None)):
            if after_id:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
        self.root.destroy()

    # ----- provider selection ----------------------------------------------

    def _engine_label(self):
        desc = providers.provider_by_key(self.provider_key)
        if desc["key"] == "cli":
            return "Claude Code CLI"
        pconf = (self.settings.get("providers") or {}).get(desc["key"], {})
        model = pconf.get("model") or desc["default_model"]
        return "%s (%s)" % (desc["label"], model)

    def _refresh_engine_header(self):
        if getattr(self, "_engine_text", None) is not None:
            self.header.itemconfigure(
                self._engine_text,
                text="any language → සිංහල   ·   Engine: %s" % self._engine_label())
        self._update_translate_gate()

    def _update_translate_gate(self):
        prov = providers.build_active_provider(self.settings, cli_path=self.claude_path)
        if prov.available():
            if not self.running:
                self.translate_btn.state(["!disabled"])
            self.status_var.set("Select an English .srt file.")
        else:
            self.translate_btn.state(["disabled"])
            desc = providers.provider_by_key(self.provider_key)
            if desc["key"] == "cli":
                self.status_var.set(
                    "ERROR: the claude CLI was not found on PATH. Install Claude "
                    "Code and sign in, or pick another provider in Providers.")
            else:
                self.status_var.set(
                    "%s needs an API key. Open Providers → Settings… to add it."
                    % desc["label"])

    def _on_lang_change(self, *_):
        code = self._lang_labels.get(self.lang_var.get(), "auto")
        self.settings["source_lang"] = code
        self._persist()

    def _select_provider(self, key):
        self.provider_key = key
        self.settings["provider"] = key
        save_settings(self.settings)
        # Reset parallelism to this engine's sensible default (CLI 3, API 10);
        # the user can still tweak the spinbox afterwards.
        self.workers_var.set(str(providers.default_workers(key)))
        self._refresh_engine_header()

    def _glossary_text(self):
        return "\n".join("%s = %s" % (k, v)
                         for k, v in (self.settings.get("glossary") or {}).items())

    def _save_glossary(self, raw):
        """Parse 'Term = සිංහල' lines into the saved glossary."""
        gloss = {}
        for line in (raw or "").splitlines():
            if "=" not in line:
                continue
            term, _, val = line.partition("=")
            term, val = term.strip(), val.strip()
            if term and val:
                gloss[term] = val
        self.settings["glossary"] = gloss

    def open_opensubtitles_key(self):
        """Standalone dialog for the OpenSubtitles key (also in provider settings)."""
        win = tk.Toplevel(self.root)
        win.title("OpenSubtitles API key")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        win.after(1, lambda: self._centre(win))
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Paste your OpenSubtitles API key to enable movie search.",
                  style="Dim.TLabel", wraplength=380).pack(anchor="w")
        var = tk.StringVar(value=providers.load_secrets().get("opensubtitles", ""))
        entry = ttk.Entry(frm, textvariable=var, show="•", width=44)
        entry.pack(fill="x", pady=(10, 6))
        ttk.Button(frm, text="Get a key ↗",
                   command=lambda: webbrowser.open(self.OS_KEY_URL)).pack(anchor="w")

        def save():
            sec = providers.load_secrets()
            val = var.get().strip()
            if val:
                sec["opensubtitles"] = val
            else:
                sec.pop("opensubtitles", None)
            providers.save_secrets(sec)
            self.os_key = val or opensubtitles_key()
            if self.os_key and self._os_panel is None:
                self._build_opensubtitles(self.main)
            win.destroy()

        btns = ttk.Frame(frm)
        btns.pack(anchor="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left", padx=6)
        ttk.Button(btns, text="Save", style="Accent.TButton",
                   command=save).pack(side="left")

    def claude_sign_in(self):
        """Open a terminal running the Claude CLI so the user can log in there.

        Signing in is an interactive browser flow owned by the CLI, so the honest
        thing is to launch it rather than pretend the app can do it silently.
        """
        if not self.claude_path:
            if messagebox.askyesno(
                    "Claude CLI not found",
                    "The Claude CLI is not installed, so the polish pass cannot "
                    "run.\n\nOpen the install instructions in your browser?"):
                webbrowser.open("https://claude.com/product/claude-code")
            return
        messagebox.showinfo(
            "Sign in to Claude",
            "A terminal window will open running the Claude CLI.\n\n"
            "1. Choose your sign-in method and complete it in the browser\n"
            "2. Type  /quit  when you are signed in\n"
            "3. Come back here - the polish pass will start working")
        try:
            subprocess.Popen(["cmd", "/k", self.claude_path],
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except Exception as exc:  # noqa: BLE001 - report rather than crash
            messagebox.showerror("SinhalaSub",
                                 "Could not open a terminal:\n%s" % exc)

    def open_names(self):
        """Approve the Sinhala spelling of each character and place name once.

        Approved names are stored in the database and pushed into the glossary of
        every future run, so a character is never spelled two different ways -
        the usual giveaway of a machine-translated subtitle.
        """
        path = self.input_var.get().strip()
        detected = set()
        if path and os.path.isfile(path):
            try:
                detected = colorize.find_names([c.text for c in load_srt(path)])
            except Exception:  # noqa: BLE001 - saved names still editable
                detected = set()
        try:
            known = get_memory().names()
        except sqlite3.Error:
            known = {}
        terms = sorted(set(detected) | set(known))
        if not terms:
            messagebox.showinfo(
                "Names",
                "No names found yet.\n\nPick an English .srt on the Translate tab "
                "first - names are detected from it.")
            return

        win = tk.Toplevel(self.root)
        win.title("Character & place names")
        win.geometry("560x520")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        win.after(1, lambda: self._centre(win))
        ttk.Label(win, style="Dim.TLabel", wraplength=520,
                  text="Set how each name should be written in Sinhala. Approved "
                       "names are reused in every future movie, so spelling stays "
                       "consistent. Leave one blank to let the translator decide.").pack(
            anchor="w", padx=14, pady=(12, 8))

        # Scrollable rows - a movie can easily have 40+ names.
        area = ttk.Frame(win)
        area.pack(fill="both", expand=True, padx=14)
        canvas = tk.Canvas(area, bg=BG, highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(area, orient="vertical", command=canvas.yview)
        rows = ttk.Frame(canvas)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        window_id = canvas.create_window((0, 0), window=rows, anchor="nw")
        rows.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(-int(e.delta / 120), "units"))

        entries = {}
        for term in terms:
            row = ttk.Frame(rows)
            row.pack(fill="x", pady=2)
            mark = "•" if term in known else " "
            ttk.Label(row, text="%s %s" % (mark, term), width=24).pack(side="left")
            var = tk.StringVar(value=known.get(term, ""))
            ttk.Entry(row, textvariable=var, font=SINHALA_FONT).pack(
                side="left", fill="x", expand=True)
            entries[term] = var

        status = tk.StringVar(
            value="%d name(s) · %d already approved" % (len(terms), len(known)))
        ttk.Label(win, textvariable=status, style="Dim.TLabel").pack(
            anchor="w", padx=14, pady=(8, 0))

        def suggest_blank():
            """Machine-translate only the names with no spelling yet."""
            blanks = [t for t, v in entries.items() if not v.get().strip()]
            if not blanks:
                status.set("Every name already has a spelling.")
                return
            status.set("Suggesting %d name(s)…" % len(blanks))
            win.update_idletasks()

            def work():
                try:
                    prov = providers.GoogleTranslateProvider(
                        source=self.settings.get("source_lang") or "auto")
                    stdin = "TRANSLATE (%d lines):\n" % len(blanks)
                    stdin += "".join("%d|||%s\n" % (i + 1, t)
                                     for i, t in enumerate(blanks))
                    out = prov.translate("", stdin, 60)
                    got = {}
                    for line in out.splitlines():
                        num, _, body = line.partition("|||")
                        if num.strip().isdigit():
                            got[int(num.strip())] = body.strip()
                except Exception as exc:  # noqa: BLE001 - shown to the user
                    self.root.after(0, lambda: status.set(
                        "Could not suggest: %s" % str(exc)[:80]))
                    return

                def apply():
                    for i, term in enumerate(blanks):
                        if got.get(i + 1):
                            entries[term].set(got[i + 1])
                    status.set("Suggested %d name(s) - review, then Save." % len(got))
                self.root.after(0, apply)

            threading.Thread(target=work, daemon=True).start()

        def save_all():
            saved = 0
            try:
                mem = get_memory()
                for term, var in entries.items():
                    val = var.get().strip()
                    if val:
                        mem.learn_name(term, val)
                        saved += 1
            except sqlite3.Error as exc:
                messagebox.showerror("SinhalaSub", "Could not save:\n%s" % exc,
                                     parent=win)
                return
            win.destroy()
            self.status_var.set(
                "%d name(s) approved - they will be used in every future translation."
                % saved)

        btns = ttk.Frame(win, padding=(14, 12))
        btns.pack(fill="x")
        ttk.Button(btns, text="Suggest missing", command=suggest_blank).pack(side="left")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=6)
        ttk.Button(btns, text="Save names", style="Accent.TButton",
                   command=save_all).pack(side="right")

    def show_quality_report(self):
        """List every cue that breaks a subtitling rule, for the loaded file."""
        subs = self.subs
        if subs is None:
            path = (self.input_var.get().strip()
                    or self.colour_input.get().strip())
            if not path or not os.path.isfile(path):
                messagebox.showinfo(
                    "Quality report",
                    "Translate a file first, or pick one on the Translate tab.")
                return
            try:
                subs = load_srt(path)
            except Exception as exc:
                messagebox.showerror("SinhalaSub", "Could not read that file:\n%s" % exc)
                return
        issues = quality.check(subs)

        win = tk.Toplevel(self.root)
        win.title("Quality report")
        win.geometry("760x480")
        win.configure(bg=BG)
        ttk.Label(win, text=quality.summarise(issues, len(subs)),
                  style="Dim.TLabel", wraplength=720).pack(
            anchor="w", padx=14, pady=(12, 6))
        wrap = ttk.Frame(win)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        text = tk.Text(wrap, wrap="word", bg=CARD, fg=TEXT, relief="flat",
                       highlightthickness=0, padx=12, pady=10,
                       font=("Consolas", 9))
        bar = ttk.Scrollbar(wrap, command=text.yview)
        text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("hdr", foreground=WARN)
        if not issues:
            text.insert("end", "Nothing to fix - this file passes every check.\n")
        for issue in issues:
            text.insert("end", "cue %-5s %-16s " % (issue.index, issue.kind), "hdr")
            text.insert("end", "%s\n" % issue.detail)
        text.configure(state="disabled")
        win.transient(self.root)

    def show_memory_stats(self):
        """What the database has learned so far."""
        try:
            mem = get_memory()
            s = mem.stats()
            runs = mem.history(limit=8)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("SinhalaSub", "Could not read the database:\n%s" % exc)
            return
        lines = [
            "Translated lines remembered:  %d" % s["lines"],
            "Your manual corrections:      %d" % s["corrections"],
            "Learned names:                %d" % s["names"],
            "Files translated:             %d" % s["runs"],
            "Cues translated in total:     %d" % s["cues_total"],
        ]
        if runs:
            lines.append("")
            lines.append("Recent runs:")
            for r in runs:
                lines.append("  %-28s %-8s %5d cues  %5.0fs"
                             % (r["file"][:28], r["engine"], r["cues"], r["seconds"]))
        lines.append("")
        lines.append("Corrections always beat engine output, and a machine")
        lines.append("translation is never reused when a better engine is selected.")
        messagebox.showinfo("Translation memory", "\n".join(lines))

    def open_output_folder(self):
        target = (self.current_input or self.input_var.get().strip()
                  or self.colour_input.get().strip())
        folder = os.path.dirname(os.path.abspath(target)) if target else _HERE
        try:
            os.startfile(folder)  # noqa: S606 - Windows shell open
        except Exception:  # noqa: BLE001
            messagebox.showinfo("SinhalaSub", folder)

    DONATE_EMAIL = "1118niranjan@gmail.com"

    def show_about(self):
        win = tk.Toplevel(self.root)
        win.title("About SinhalaSub")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        win.after(1, lambda: self._centre(win))
        frm = ttk.Frame(win, padding=20)
        frm.pack(fill="both", expand=True)

        logo = self._load_image("logo.png", (72, 72))
        if logo:
            tk.Label(frm, image=logo, bd=0, bg=BG).pack()
        ttk.Label(frm, text="SinhalaSub", style="Title.TLabel").pack(pady=(8, 0))
        ttk.Label(frm, text="Created by NLK", style="Dim.TLabel").pack()
        ttk.Label(frm, style="Dim.TLabel", wraplength=420, justify="center",
                  text="Translate movie subtitles from any language into natural, "
                       "meaning-based spoken Sinhala. Free and fast with Google "
                       "Translate, or add an LLM for the hardest lines.").pack(
            pady=(12, 0))

        ttk.Separator(frm, orient="horizontal").pack(fill="x", pady=14)
        ttk.Label(frm, text="Support this project", style="Dim.TLabel").pack()
        ttk.Label(frm, text=self.DONATE_EMAIL,
                  font=("Segoe UI Semibold", 11)).pack(pady=(4, 0))
        row = ttk.Frame(frm)
        row.pack(pady=(10, 0))
        ttk.Button(row, text="Donate via PayPal",
                   style="Accent.TButton",
                   command=lambda: webbrowser.open(
                       "https://www.paypal.com/paypalme/")).pack(side="left")
        ttk.Button(row, text="Copy email",
                   command=self._copy_donate_email).pack(side="left", padx=8)
        ttk.Button(frm, text="Close", command=win.destroy).pack(pady=(16, 0))

    def _copy_donate_email(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.DONATE_EMAIL)
        messagebox.showinfo("Copied",
                            "%s copied to the clipboard.\n\nPaste it into PayPal's "
                            "\"Send to\" box to donate." % self.DONATE_EMAIL)

    # Where each provider hands out an API key (opened by the "Get a key" button).
    KEY_URLS = {
        "anthropic": "https://console.anthropic.com/settings/keys",
        "gemini": "https://aistudio.google.com/apikey",
        "openai": "https://platform.openai.com/api-keys",
    }
    OS_KEY_URL = "https://www.opensubtitles.com/en/consumers"

    def open_provider_settings(self):
        desc0 = providers.provider_by_key(self.provider_key)
        secrets = providers.load_secrets()
        pconf = (self.settings.get("providers") or {}).get(desc0["key"], {})

        win = tk.Toplevel(self.root)
        win.title("Provider settings")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        win.after(1, lambda: self._centre(win))
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)

        def current_desc():
            return next(p for p in providers.PROVIDERS if p["label"] == prov_var.get())

        ttk.Label(frm, text="Provider", style="Dim.TLabel").grid(row=0, column=0, sticky="w")
        prov_var = tk.StringVar(value=desc0["label"])
        labels = [p["label"] for p in providers.PROVIDERS]
        ttk.Combobox(frm, textvariable=prov_var, values=labels, state="readonly",
                     width=24).grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        key_var = tk.StringVar(value=secrets.get(desc0["key"], ""))
        base_var = tk.StringVar(value=pconf.get("base_url") or desc0.get("default_base_url") or "")
        model_var = tk.StringVar(value=pconf.get("model") or desc0["default_model"])

        ttk.Label(frm, text="API key", style="Dim.TLabel").grid(row=1, column=0, sticky="w")
        key_entry = ttk.Entry(frm, textvariable=key_var, show="•", width=30)
        key_entry.grid(row=1, column=1, sticky="ew", pady=4)
        get_key_btn = ttk.Button(
            frm, text="Get a key ↗",
            command=lambda: webbrowser.open(self.KEY_URLS.get(current_desc()["key"], "")))
        get_key_btn.grid(row=1, column=2, sticky="w", padx=(6, 0))

        ttk.Label(frm, text="Base URL", style="Dim.TLabel").grid(row=2, column=0, sticky="w")
        base_entry = ttk.Entry(frm, textvariable=base_var, width=36)
        base_entry.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(frm, text="Model", style="Dim.TLabel").grid(row=3, column=0, sticky="w")
        ttk.Entry(frm, textvariable=model_var, width=36).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Separator(frm, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        ttk.Label(frm, text="OpenSubtitles key", style="Dim.TLabel").grid(
            row=5, column=0, sticky="w")
        os_key_var = tk.StringVar(value=secrets.get("opensubtitles", ""))
        ttk.Entry(frm, textvariable=os_key_var, show="•", width=30).grid(
            row=5, column=1, sticky="ew", pady=4)
        ttk.Button(frm, text="Get a key ↗",
                   command=lambda: webbrowser.open(self.OS_KEY_URL)).grid(
            row=5, column=2, sticky="w", padx=(6, 0))
        ttk.Label(frm, text="(optional — enables the movie search panel)",
                  style="Dim.TLabel").grid(row=6, column=1, columnspan=2, sticky="w")

        # --- performance knobs, moved off the main window ---
        ttk.Separator(frm, orient="horizontal").grid(
            row=7, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        ttk.Label(frm, text="Claude model", style="Dim.TLabel").grid(
            row=8, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=self.model_var, values=MODEL_CHOICES,
                     state="readonly", width=14).grid(row=8, column=1, sticky="w", pady=3)
        ttk.Label(frm, text="Parallel batches", style="Dim.TLabel").grid(
            row=9, column=0, sticky="w")
        ttk.Spinbox(frm, from_=1, to=20, textvariable=self.workers_var, width=5,
                    state="readonly").grid(row=9, column=1, sticky="w", pady=3)
        ttk.Label(frm, text="Cues per batch", style="Dim.TLabel").grid(
            row=10, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=self.batch_var, state="readonly", width=8,
                     values=["Auto", "30", "50", "100", "150", "250"]).grid(
            row=10, column=1, sticky="w", pady=3)

        ttk.Label(frm, text="Glossary", style="Dim.TLabel").grid(
            row=11, column=0, sticky="nw", pady=(6, 0))
        gloss_var = tk.StringVar(value=self._glossary_text())
        gloss = tk.Text(frm, height=3, width=34, bg=FIELD, fg=TEXT, relief="flat",
                        insertbackground=TEXT, font=("Segoe UI", 9))
        gloss.insert("1.0", gloss_var.get())
        gloss.grid(row=11, column=1, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(frm, text="one per line:  Marseille = මාර්සෙයි",
                  style="Dim.TLabel").grid(row=12, column=1, columnspan=2, sticky="w")

        status = ttk.Label(frm, text="", style="Dim.TLabel", wraplength=340)
        status.grid(row=13, column=0, columnspan=3, sticky="w", pady=(6, 0))

        def sync_fields(*_):
            d = current_desc()
            no_key = not d["needs_key"]  # CLI providers (Claude, Gemini CLI) need none
            key_entry.configure(state=("disabled" if no_key else "normal"))
            get_key_btn.configure(state=("disabled" if no_key else "normal"))
            base_entry.configure(state=("normal" if d["key"] == "openai" else "disabled"))
            saved = (self.settings.get("providers") or {}).get(d["key"], {})
            key_var.set(providers.load_secrets().get(d["key"], ""))
            base_var.set(saved.get("base_url") or d.get("default_base_url") or "")
            model_var.set(saved.get("model") or d["default_model"])
        prov_var.trace_add("write", sync_fields)
        sync_fields()

        def do_test():
            d = current_desc()
            prov = providers.make_provider(
                d["key"], model=model_var.get().strip() or d["default_model"],
                api_key=key_var.get().strip(),
                base_url=base_var.get().strip() or d.get("default_base_url"),
                cli_path=self.claude_path)
            status.configure(text="Testing… (up to 30s)")

            def work():
                try:
                    ok, msg = prov.test()
                except Exception as exc:  # noqa: BLE001 - shown to the user
                    ok, msg = False, str(exc)[:300]

                def show():
                    try:
                        status.configure(text=("✓ " if ok else "✗ ") + msg)
                    except tk.TclError:
                        pass  # dialog was closed before the result came back

                try:
                    win.after(0, show)
                except tk.TclError:
                    pass

            # Run the network probe off the UI thread so the window stays responsive.
            threading.Thread(target=work, daemon=True).start()

        def do_save():
            d = current_desc()
            self.provider_key = d["key"]
            self.settings["provider"] = d["key"]
            provs = dict(self.settings.get("providers") or {})
            provs[d["key"]] = {"model": model_var.get().strip() or d["default_model"],
                               "base_url": base_var.get().strip() or d.get("default_base_url")}
            self.settings["providers"] = provs
            save_settings(self.settings)
            sec = providers.load_secrets()
            if d["needs_key"]:
                sec[d["key"]] = key_var.get().strip()
            os_val = os_key_var.get().strip()
            if os_val:
                sec["opensubtitles"] = os_val
            elif "opensubtitles" in sec:
                del sec["opensubtitles"]
            providers.save_secrets(sec)
            self.os_key = os_val or opensubtitles_key()
            if self.os_key and self._os_panel is None:
                self._build_opensubtitles(self.main)
            self._save_glossary(gloss.get("1.0", "end"))
            self._persist()
            self._menu_provider.set(d["key"])
            self._refresh_engine_header()
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=14, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Test connection", command=do_test).pack(side="left")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left", padx=8)
        ttk.Button(btns, text="Save", style="Accent.TButton",
                   command=do_save).pack(side="left")
        frm.columnconfigure(1, weight=1)

    # ----- widgets ---------------------------------------------------------

    def _on_color(self, _event=None):
        name = self.color_name.get()
        hex_val = dict(COLOR_PRESETS).get(name, "")
        if hex_val is None:  # Custom...
            picked = colorchooser.askcolor(
                color=self.color_hex or "#FFD700", parent=self.root,
                title="Pick a subtitle colour")[1]
            if picked:
                self.color_hex = picked
            else:  # cancelled: fall back to what the swatch already shows
                self.color_name.set(next(
                    (n for n, h in COLOR_PRESETS if h == self.color_hex),
                    "Custom..." if self.color_hex else "None (player default)"))
        else:
            self.color_hex = hex_val
        self.swatch.configure(bg=self.color_hex or FIELD)
        self._persist()

    # ----- header + drag and drop ------------------------------------------

    ASSETS = os.path.join(_HERE, "assets")

    def _load_image(self, name, size, scrim=0):
        """Load and resize an asset; returns None when Pillow/file is missing.

        `scrim` darkens the left portion of the image so light text placed there
        stays readable no matter how bright the artwork is underneath.
        """
        path = os.path.join(self.ASSETS, name)
        if not os.path.isfile(path):
            return None
        try:
            from PIL import Image, ImageDraw, ImageTk
            img = Image.open(path).convert("RGBA").resize(size, Image.LANCZOS)
            if scrim:
                w, h = img.size
                veil = Image.new("L", (w, 1))
                px = veil.load()
                for x in range(w):
                    # Strong on the left, fading out by `scrim` of the width.
                    t = min(1.0, x / max(1.0, w * scrim))
                    px[x, 0] = int(215 * (1.0 - t) ** 1.5)
                mask = veil.resize((w, h))
                dark = Image.new("RGBA", (w, h), (8, 9, 16, 255))
                dark.putalpha(mask)
                img = Image.alpha_composite(img, dark)
            photo = ImageTk.PhotoImage(img)
            self._images.append(photo)   # prevent garbage collection
            return photo
        except Exception:  # noqa: BLE001 - the app must run without Pillow
            return None

    HEADER_H = 68

    def _build_header(self, parent):
        """Banner, logo and titles drawn on one canvas.

        A canvas is used rather than labels because canvas text has no opaque
        background box - labels would paint a visible rectangle over the artwork.
        """
        self.header = tk.Canvas(parent, height=self.HEADER_H, bg=BG,
                                highlightthickness=0, bd=0)
        self.header.pack(fill="x", pady=(0, 12))

        banner = self._load_image("header_pro.png", (1600, self.HEADER_H), scrim=0.55)
        if banner:
            self.header.create_image(0, 0, image=banner, anchor="nw")

        logo = self._load_image("logo.png", (52, 52))
        text_x = 8
        if logo:
            self.header.create_image(6, self.HEADER_H // 2, image=logo, anchor="w")
            text_x = 68

        self.header.create_text(text_x, 20, text="SinhalaSub", anchor="w",
                                fill=TEXT, font=("Segoe UI Semibold", 20))
        self._engine_text = self.header.create_text(
            text_x + 2, 46, anchor="w", fill=DIM, font=("Segoe UI", 9),
            text="any language → සිංහල   ·   Engine: %s" % self._engine_label())
        # Kept for compatibility with code that updates the engine line.
        self.subtitle_lbl = None

    def _centre(self, win):
        """Place a dialog over the middle of the main window, fully on screen."""
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        px, py = self.root.winfo_rootx(), self.root.winfo_rooty()
        pw, ph = self.root.winfo_width(), self.root.winfo_height()
        x = max(0, min(px + (pw - w) // 2, win.winfo_screenwidth() - w))
        y = max(0, min(py + (ph - h) // 3, win.winfo_screenheight() - h))
        win.geometry("+%d+%d" % (x, y))

    def _make_drop_target(self, widget, handler):
        """Register a widget so dropped .srt files load into the right field."""
        if not self.dnd:
            return
        try:
            from tkinterdnd2 import DND_FILES
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", handler)
        except Exception:  # noqa: BLE001 - dropping is a nicety, never fatal
            pass

    @staticmethod
    def _first_srt(event):
        paths = parse_drop(getattr(event, "data", ""))
        for p in paths:
            if p.lower().endswith(".srt") and os.path.isfile(p):
                return p
        return paths[0] if paths else None

    def _drop_translate(self, event):
        path = self._first_srt(event)
        if path:
            self.input_var.set(path)
            self.status_var.set("Loaded: %s" % os.path.basename(path))
            self._note_video_for(path)

    def _drop_colour(self, event):
        path = self._first_srt(event)
        if path:
            self.colour_input.set(path)
            self.colour_status.set("Loaded: %s" % os.path.basename(path))

    # ----- video auto-detect -------------------------------------------------

    VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".webm", ".ts")

    def find_video_for(self, srt_path):
        """Find the movie file this subtitle belongs to, in the same folder.

        Prefers the video whose name best matches the subtitle's name, so a
        season folder picks the right episode rather than the first file.
        """
        folder = os.path.dirname(os.path.abspath(srt_path))
        stem = os.path.basename(srt_path)
        for suffix in (".si.srt", ".srt"):
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        stem_l = stem.lower()
        best, best_score = None, 0
        try:
            entries = os.listdir(folder)
        except OSError:
            return None
        for name in entries:
            if not name.lower().endswith(self.VIDEO_EXTS):
                continue
            vstem = os.path.splitext(name)[0].lower()
            if vstem == stem_l:
                return os.path.join(folder, name)
            score = len(os.path.commonprefix([vstem, stem_l]))
            if score > best_score:
                best, best_score = os.path.join(folder, name), score
        return best if best_score >= 4 else None

    def _note_video_for(self, srt_path):
        video = self.find_video_for(srt_path)
        if video:
            self.video_var.set("Movie found: %s" % os.path.basename(video))
        else:
            self.video_var.set("")

    # ----- Review & Fix tab -------------------------------------------------

    def _build_review_tab(self, parent):
        ttk.Label(parent, style="Dim.TLabel", wraplength=700,
                  text="Open a translated .srt beside its English original, fix any "
                       "line, and the fix is remembered forever - the same line is "
                       "never mistranslated again, on any movie.").pack(anchor="w")

        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Sinhala .srt:").pack(side="left")
        self.review_input = tk.StringVar()
        ttk.Entry(row, textvariable=self.review_input).pack(
            side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text="Browse...", command=self._review_browse).pack(side="left")
        ttk.Button(row, text="Load", command=self.load_review).pack(
            side="left", padx=(6, 0))

        filt = ttk.Frame(parent)
        filt.pack(fill="x", pady=(8, 0))
        self.review_only_issues = tk.BooleanVar(value=False)
        ttk.Checkbutton(filt, text="Show only lines with a quality problem",
                        variable=self.review_only_issues,
                        command=self._fill_review).pack(side="left")

        body = ttk.Frame(parent)
        body.pack(fill="both", expand=True, pady=(10, 0))
        self.review_list = tk.Listbox(
            body, height=10, bg=FIELD, fg=TEXT, selectbackground=ACCENT,
            selectforeground="#ffffff", relief="flat", highlightthickness=0,
            borderwidth=0, exportselection=False, font=SINHALA_FONT)
        bar = ttk.Scrollbar(body, command=self.review_list.yview)
        self.review_list.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.review_list.pack(side="left", fill="both", expand=True)
        self.review_list.bind("<<ListboxSelect>>", self._on_review_select)

        ttk.Label(parent, text="English", style="Dim.TLabel").pack(
            anchor="w", pady=(10, 0))
        self.review_src = tk.Text(parent, height=2, wrap="word", bg=CARD, fg=DIM,
                                  relief="flat", highlightthickness=0, padx=8,
                                  pady=5, font=("Segoe UI", 9))
        self.review_src.pack(fill="x")
        self.review_src.configure(state="disabled")

        ttk.Label(parent, text="Sinhala (edit me)", style="Dim.TLabel").pack(
            anchor="w", pady=(8, 0))
        self.review_edit = tk.Text(parent, height=3, wrap="word", bg=FIELD, fg=TEXT,
                                   relief="flat", highlightthickness=0, padx=8,
                                   pady=5, insertbackground=TEXT, font=SINHALA_FONT)
        self.review_edit.pack(fill="x")

        act = ttk.Frame(parent)
        act.pack(fill="x", pady=(10, 0))
        ttk.Button(act, text="Save this fix", style="Accent.TButton",
                   command=self.save_review_fix).pack(side="left")
        ttk.Button(act, text="Save file", command=self.save_review_file).pack(
            side="left", padx=8)
        self.review_status = tk.StringVar(
            value="Load a translated subtitle to review it.")
        ttk.Label(parent, textvariable=self.review_status, style="Dim.TLabel",
                  wraplength=700).pack(anchor="w", pady=(8, 0))

        self._review_subs = None      # the Sinhala file being reviewed
        self._review_src_subs = None  # its English original, when found
        self._review_rows = []        # listbox row -> cue position

    def _review_browse(self):
        path = filedialog.askopenfilename(
            title="Choose a translated .srt",
            filetypes=[("SubRip subtitles", "*.srt"), ("All files", "*.*")])
        if path:
            self.review_input.set(path)
            self.load_review()

    @staticmethod
    def english_original_for(si_path):
        """The English file a .si.srt came from, if it is still alongside it."""
        low = si_path.lower()
        if low.endswith(".si.srt"):
            candidate = si_path[: -len(".si.srt")] + ".srt"
            if os.path.isfile(candidate):
                return candidate
        return None

    def load_review(self):
        path = self.review_input.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("SinhalaSub", "Choose an existing .srt file first.")
            return
        try:
            self._review_subs = load_srt(path)
        except Exception as exc:
            messagebox.showerror("SinhalaSub", "Could not read that file:\n%s" % exc)
            return
        english = self.english_original_for(path)
        self._review_src_subs = None
        if english:
            try:
                src = load_srt(english)
                if len(src) == len(self._review_subs):
                    self._review_src_subs = src
            except Exception:  # noqa: BLE001 - reviewing works without it
                pass
        self._fill_review()

    def _fill_review(self):
        subs = self._review_subs
        self.review_list.delete(0, "end")
        self._review_rows = []
        if subs is None:
            return
        flagged = set()
        if self.review_only_issues.get():
            flagged = {i.index for i in quality.check(subs)}
        for pos, cue in enumerate(subs):
            if flagged and cue.index not in flagged:
                continue
            self._review_rows.append(pos)
            self.review_list.insert(
                "end", "%4d  %s" % (cue.index, flatten(cue.text)[:70]))
        note = "" if not flagged else " (showing %d flagged)" % len(self._review_rows)
        src_note = ("English original found - shown for each line."
                    if self._review_src_subs else
                    "English original not found; editing Sinhala only.")
        self.review_status.set("%d cues%s. %s" % (len(subs), note, src_note))

    def _on_review_select(self, _event=None):
        sel = self.review_list.curselection()
        if not sel or self._review_subs is None:
            return
        pos = self._review_rows[sel[0]]
        self.review_src.configure(state="normal")
        self.review_src.delete("1.0", "end")
        if self._review_src_subs is not None:
            self.review_src.insert("1.0", flatten(self._review_src_subs[pos].text))
        self.review_src.configure(state="disabled")
        self.review_edit.delete("1.0", "end")
        self.review_edit.insert("1.0", self._review_subs[pos].text)

    def save_review_fix(self):
        """Apply the edit to the file and remember it as a permanent correction."""
        sel = self.review_list.curselection()
        if not sel or self._review_subs is None:
            messagebox.showinfo("SinhalaSub", "Select a line to fix first.")
            return
        pos = self._review_rows[sel[0]]
        new_text = self.review_edit.get("1.0", "end").strip()
        if not new_text:
            messagebox.showinfo("SinhalaSub", "The Sinhala text cannot be empty.")
            return
        self._review_subs[pos].text = new_text

        saved_note = "kept in this file only"
        if self._review_src_subs is not None:
            english = flatten(self._review_src_subs[pos].text)
            if english:
                try:
                    get_memory().save_correction(english, new_text)
                    saved_note = "remembered for every future movie"
                except sqlite3.Error:
                    pass
        self._fill_review()
        if sel[0] < self.review_list.size():
            self.review_list.selection_set(sel[0])
        self.review_status.set("Line %d fixed - %s."
                               % (self._review_subs[pos].index, saved_note))

    def save_review_file(self):
        if self._review_subs is None:
            messagebox.showinfo("SinhalaSub", "Load a file first.")
            return
        path = self.review_input.get().strip()
        base = path[:-4] if path.lower().endswith(".srt") else path
        out = unused_path(base + ".fixed.srt")
        try:
            self._review_subs.save(out, encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("SinhalaSub", "Could not write the file:\n%s" % exc)
            return
        self.review_status.set("Saved: %s" % out)
        messagebox.showinfo("SinhalaSub", "Saved:\n%s" % out)

    # ----- Batch tab --------------------------------------------------------

    def _build_batch_tab(self, parent):
        ttk.Label(parent, text="Translate every subtitle in a folder",
                  style="Dim.TLabel", wraplength=680).pack(anchor="w")
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Folder:").pack(side="left")
        self.batch_dir = tk.StringVar()
        ttk.Entry(row, textvariable=self.batch_dir).pack(
            side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text="Browse...", command=self._batch_browse).pack(side="left")

        opts = ttk.Frame(parent)
        opts.pack(fill="x", pady=(10, 0))
        self.batch_recursive = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Include sub-folders (whole season)",
                        variable=self.batch_recursive).pack(side="left")
        self.batch_skip_done = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Skip files already translated",
                        variable=self.batch_skip_done).pack(side="left", padx=18)

        act = ttk.Frame(parent)
        act.pack(fill="x", pady=(12, 0))
        self.batch_btn = ttk.Button(act, text="Translate all", style="Accent.TButton",
                                    command=self.start_batch)
        self.batch_btn.pack(side="left")
        ttk.Button(act, text="Scan", command=self._batch_scan).pack(side="left", padx=8)

        self.batch_progress = ttk.Progressbar(parent, mode="determinate")
        self.batch_progress.pack(fill="x", pady=(12, 0))
        self.batch_status = tk.StringVar(value="Pick a folder of .srt files.")
        ttk.Label(parent, textvariable=self.batch_status, style="Dim.TLabel",
                  wraplength=680).pack(anchor="w", pady=(8, 0))

        self.batch_list = tk.Listbox(
            parent, height=8, bg=FIELD, fg=TEXT, selectbackground=ACCENT,
            relief="flat", highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 9))
        self.batch_list.pack(fill="both", expand=True, pady=(10, 0))

    def _batch_browse(self):
        path = filedialog.askdirectory(title="Choose a folder of subtitles")
        if path:
            self.batch_dir.set(path)
            self._batch_scan()

    def _batch_files(self):
        root_dir = self.batch_dir.get().strip()
        if not root_dir or not os.path.isdir(root_dir):
            return []
        found = []
        walker = (os.walk(root_dir) if self.batch_recursive.get()
                  else [(root_dir, [], os.listdir(root_dir))])
        for folder, _dirs, names in walker:
            for name in sorted(names):
                if not name.lower().endswith(".srt"):
                    continue
                if name.lower().endswith(".si.srt"):
                    continue  # already a Sinhala output
                full = os.path.join(folder, name)
                if self.batch_skip_done.get() and os.path.exists(
                        default_output_path(full)):
                    continue
                found.append(full)
        return found

    def _batch_scan(self):
        files = self._batch_files()
        self.batch_list.delete(0, "end")
        for f in files:
            self.batch_list.insert("end", os.path.basename(f))
        self.batch_status.set("%d file(s) to translate." % len(files))

    def start_batch(self):
        files = self._batch_files()
        if not files:
            messagebox.showinfo("SinhalaSub", "Nothing to translate in that folder.")
            return
        provider = providers.build_active_provider(
            self.settings, cli_path=self.claude_path)
        if not provider.available():
            self._update_translate_gate()
            return
        if not messagebox.askyesno(
                "Batch translate",
                "Translate %d subtitle file(s) with %s?\n\nThe app will work through "
                "them one by one and save each result next to its input."
                % (len(files), self._engine_label())):
            return
        self.cancel_event = threading.Event()
        self.batch_btn.state(["disabled"])
        self.batch_progress.configure(maximum=len(files), value=0)
        threading.Thread(target=self._batch_worker,
                         args=(files, provider), daemon=True).start()

    def _batch_worker(self, files, provider):
        try:
            workers = max(1, min(20, int(self.workers_var.get())))
        except ValueError:
            workers = providers.default_workers(self.provider_key)
        use_memory = bool(self.memory_var.get())
        base_tier = memory_db.engine_tier(self.provider_key)
        done = 0
        for path in files:
            if self.cancel_event.is_set():
                break
            self.msgs.put(("batch_status", "Translating %s (%d/%d)"
                           % (os.path.basename(path), done + 1, len(files))))
            started = time.time()
            try:
                subs = load_srt(path)
                initial = (memory_prefill(subs, min_tier=base_tier)
                           if use_memory else {})
                texts = translate_all(subs, provider, workers=workers,
                                      cancel=self.cancel_event, initial=initial)
                # Feed the database from batch runs too, so a season builds up
                # memory the same way single files do.
                if use_memory:
                    try:
                        self._remember(subs, texts, provider, base_tier)
                        get_memory().record_run(
                            path, engine=self.provider_key, cues=len(subs),
                            seconds=time.time() - started)
                    except sqlite3.Error:
                        pass
                out_texts = ([colorize.colour_line(t, self.color_hex) for t in texts]
                             if self.color_hex else texts)
                write_output(subs, out_texts, unused_path(default_output_path(path)))
            except TranslationCancelled:
                break
            except Exception as exc:  # noqa: BLE001 - keep going through the list
                self.msgs.put(("batch_status", "Failed on %s: %s"
                               % (os.path.basename(path), str(exc)[:120])))
                continue
            done += 1
            self.msgs.put(("batch_progress", done))
        self.msgs.put(("batch_done", done, len(files)))

    def _remember(self, subs, texts, provider, base_tier):
        """Store a finished translation, tiering each line by what produced it."""
        pairs = memory_collect(subs, texts)
        if not pairs:
            return
        mem = get_memory()
        polished = getattr(provider, "polished_sources", None) or set()
        if polished:
            mem.store({s: t for s, t in pairs.items() if s in polished},
                      engine=self.provider_key + "+llm", tier="llm")
            pairs = {s: t for s, t in pairs.items() if s not in polished}
        mem.store(pairs, engine=self.provider_key, tier=base_tier)

    # ----- Timing tab -------------------------------------------------------

    def _build_timing_tab(self, parent):
        ttk.Label(parent, text="Shift subtitle timing when it runs early or late",
                  style="Dim.TLabel", wraplength=680).pack(anchor="w")
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Subtitle .srt:").pack(side="left")
        self.timing_input = tk.StringVar()
        ttk.Entry(row, textvariable=self.timing_input).pack(
            side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text="Browse...",
                   command=self._timing_browse).pack(side="left")

        ctl = ttk.Frame(parent)
        ctl.pack(fill="x", pady=(12, 0))
        ttk.Label(ctl, text="Shift by (seconds)", style="Dim.TLabel").pack(side="left")
        self.shift_var = tk.StringVar(value="0.0")
        ttk.Entry(ctl, textvariable=self.shift_var, width=10).pack(side="left", padx=8)
        for label, delta in (("-1s", -1), ("-0.5s", -0.5), ("+0.5s", 0.5), ("+1s", 1)):
            ttk.Button(ctl, text=label, style="Small.TButton",
                       command=lambda d=delta: self._bump_shift(d)).pack(
                side="left", padx=2)

        ttk.Label(parent, style="Dim.TLabel", wraplength=680,
                  text="Positive numbers make the subtitles appear later, negative "
                       "makes them appear earlier. The result is saved as a new file, "
                       "your original is never changed.").pack(anchor="w", pady=(10, 0))

        act = ttk.Frame(parent)
        act.pack(fill="x", pady=(12, 0))
        ttk.Button(act, text="Apply shift & save", style="Accent.TButton",
                   command=self.apply_shift).pack(side="left")
        self.timing_status = tk.StringVar(value="Pick a subtitle to retime.")
        ttk.Label(parent, textvariable=self.timing_status, style="Dim.TLabel",
                  wraplength=680).pack(anchor="w", pady=(8, 0))

    def _timing_browse(self):
        path = filedialog.askopenfilename(
            title="Choose a subtitle to retime",
            filetypes=[("SubRip subtitles", "*.srt"), ("All files", "*.*")])
        if path:
            self.timing_input.set(path)

    def _bump_shift(self, delta):
        try:
            current = float(self.shift_var.get())
        except ValueError:
            current = 0.0
        self.shift_var.set("%.1f" % (current + delta))

    def apply_shift(self):
        path = self.timing_input.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("SinhalaSub", "Choose an existing .srt file first.")
            return
        try:
            seconds = float(self.shift_var.get())
        except ValueError:
            messagebox.showerror("SinhalaSub", "Shift must be a number, e.g. 2.5")
            return
        if not seconds:
            self.timing_status.set("Shift is zero - nothing to change.")
            return
        try:
            subs = load_srt(path)
            subs.shift(seconds=seconds)
            base = path[:-4] if path.lower().endswith(".srt") else path
            out = unused_path("%s.shifted.srt" % base)
            subs.save(out, encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("SinhalaSub", "Could not retime that file:\n%s" % exc)
            return
        self.timing_status.set("Saved: %s" % out)
        messagebox.showinfo("SinhalaSub", "Shifted by %+.1fs\n\nSaved:\n%s"
                            % (seconds, out))

    # ----- Colour & Style tab ----------------------------------------------

    AUTO_ROWS = [
        ("name", "Names & places", "#00E5FF"),
        ("sound", "Sound & music cues", "#8A8FA8"),
        ("speaker1", "Speaker 1 (dialogue)", "#FFD700"),
        ("speaker2", "Speaker 2 (dialogue)", "#7CFC98"),
        ("emphasis", "Shouting / italics", "#FF7A7A"),
        ("normal", "All other lines", ""),
    ]

    def _build_colour_tab(self, parent):
        saved = self.settings.get("scheme") or {}
        ttk.Label(parent, text="Colour any subtitle file", style="Dim.TLabel",
                  wraplength=660).pack(anchor="w")

        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Subtitle .srt:").pack(side="left")
        self.colour_input = tk.StringVar()
        ttk.Entry(row, textvariable=self.colour_input).pack(
            side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text="Browse...", command=self._colour_browse).pack(side="left")

        mode = ttk.Frame(parent)
        mode.pack(fill="x", pady=(12, 0))
        self.colour_mode = tk.StringVar(value=self.settings.get("colour_mode", "single"))
        ttk.Radiobutton(mode, text="One colour for everything", value="single",
                        variable=self.colour_mode,
                        command=self._sync_colour_mode).pack(side="left")
        ttk.Radiobutton(mode, text="Auto colour (by what the line is)", value="auto",
                        variable=self.colour_mode,
                        command=self._sync_colour_mode).pack(side="left", padx=16)

        # Mode-specific controls live in this slot so they always sit between the
        # mode radios and the action buttons, whichever mode is showing.
        self.colour_body = ttk.Frame(parent)
        self.colour_body.pack(fill="x")

        # --- single colour ---
        self.single_box = ttk.Frame(self.colour_body)
        self.single_box.pack(fill="x", pady=(10, 0))
        ttk.Label(self.single_box, text="Colour", style="Dim.TLabel").pack(side="left")
        box = ttk.Combobox(self.single_box, textvariable=self.color_name,
                           values=[n for n, _ in COLOR_PRESETS],
                           state="readonly", width=20)
        box.pack(side="left", padx=(6, 6))
        box.bind("<<ComboboxSelected>>", self._on_color)
        self.swatch = tk.Label(self.single_box, width=3,
                               bg=self.color_hex or FIELD, relief="flat")
        self.swatch.pack(side="left")

        # --- auto colour ---
        self.auto_box = ttk.LabelFrame(self.colour_body, text=" What to colour ", padding=10)
        self.auto_vars, self.auto_hex, self.auto_swatches = {}, {}, {}
        for key, label, default_hex in self.AUTO_ROWS:
            conf = saved.get(key) or {}
            r = ttk.Frame(self.auto_box)
            r.pack(fill="x", pady=2)
            var = tk.BooleanVar(value=conf.get("on", bool(default_hex)))
            self.auto_vars[key] = var
            ttk.Checkbutton(r, text=label, variable=var, width=24).pack(side="left")
            hexv = conf.get("hex", default_hex)
            self.auto_hex[key] = hexv
            sw = tk.Label(r, width=3, bg=hexv or FIELD, relief="flat")
            sw.pack(side="left", padx=6)
            self.auto_swatches[key] = sw
            ttk.Button(r, text="Pick…",
                       command=lambda k=key: self._pick_auto_colour(k)).pack(side="left")

        ttk.Label(parent, style="Dim.TLabel", wraplength=660,
                  text="Auto colour reads each line and decides: names get their own "
                       "colour, [sound cues] are dimmed, the two speakers in a dashed "
                       "dialogue get different colours, and shouting stands out.").pack(
            anchor="w", pady=(10, 0))

        act = ttk.Frame(parent)
        act.pack(fill="x", pady=(12, 0))
        ttk.Button(act, text="Apply colours & save", style="Accent.TButton",
                   command=self.apply_colours).pack(side="left")
        ttk.Button(act, text="Preview", command=self.preview_colours).pack(
            side="left", padx=8)
        self.colour_status = tk.StringVar(value="Pick any .srt - English or Sinhala.")
        ttk.Label(parent, textvariable=self.colour_status, style="Dim.TLabel",
                  wraplength=660).pack(anchor="w", pady=(8, 0))
        self._sync_colour_mode()

    def _sync_colour_mode(self):
        if self.colour_mode.get() == "auto":
            self.single_box.pack_forget()
            self.auto_box.pack(fill="x", pady=(10, 0))
        else:
            self.auto_box.pack_forget()
            self.single_box.pack(fill="x", pady=(10, 0))
        self.settings["colour_mode"] = self.colour_mode.get()
        self._persist()

    def _pick_auto_colour(self, key):
        picked = colorchooser.askcolor(
            color=self.auto_hex.get(key) or "#FFD700", parent=self.root,
            title="Colour for this category")[1]
        if picked:
            self.auto_hex[key] = picked
            self.auto_swatches[key].configure(bg=picked)
            self.auto_vars[key].set(True)
            self._persist()

    def _colour_browse(self):
        path = filedialog.askopenfilename(
            title="Choose a subtitle file",
            filetypes=[("SubRip subtitles", "*.srt"), ("All files", "*.*")])
        if path:
            self.colour_input.set(path)

    def _current_scheme(self):
        return {k: self.auto_hex.get(k, "")
                for k in self.auto_vars
                if self.auto_vars[k].get() and self.auto_hex.get(k)}

    def _colourise(self, subs):
        """Return a list of coloured cue texts for the loaded file."""
        if self.colour_mode.get() == "single":
            return [colorize.colour_line(c.text, self.color_hex) for c in subs]
        scheme = self._current_scheme()
        names = (colorize.find_names([c.text for c in subs])
                 if scheme.get("name") else set())
        return [colorize.auto_colour_line(c.text, scheme, names) for c in subs]

    def _load_colour_input(self):
        path = self.colour_input.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("SinhalaSub", "Choose an existing .srt file first.")
            return None, None
        try:
            return load_srt(path), path
        except Exception as exc:
            messagebox.showerror("SinhalaSub", "Could not read that file:\n%s" % exc)
            return None, None

    def preview_colours(self):
        subs, _ = self._load_colour_input()
        if subs is None:
            return
        coloured = self._colourise(subs)
        win = tk.Toplevel(self.root)
        win.title("Colour preview - first 15 cues")
        win.geometry("720x460")
        win.configure(bg=BG)
        text = tk.Text(win, wrap="word", font=SINHALA_FONT, bg=CARD, fg=TEXT,
                       relief="flat", highlightthickness=0, padx=12, pady=10)
        text.pack(fill="both", expand=True)
        for i in range(min(15, len(subs))):
            body = coloured[i]
            shown = re.sub(r"</?font[^>]*>", "", body)
            hexes = re.findall(r'color="(#[0-9A-Fa-f]{6})"', body)
            tag = None
            if hexes:
                tag = "c%s" % hexes[0].lstrip("#")
                text.tag_configure(tag, foreground=hexes[0])
            text.insert("end", "%d  %s\n" % (subs[i].index, colorize.classify(subs[i].text)),
                        ())
            text.insert("end", "%s\n\n" % shown, (tag,) if tag else ())
        text.configure(state="disabled")
        win.transient(self.root)

    def apply_colours(self):
        subs, path = self._load_colour_input()
        if subs is None:
            return
        coloured = self._colourise(subs)
        base = path[:-4] if path.lower().endswith(".srt") else path
        out = unused_path(base + ".coloured.srt")
        try:
            for cue, body in zip(subs, coloured):
                cue.text = body
            subs.save(out, encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("SinhalaSub", "Could not write the file:\n%s" % exc)
            return
        self._persist()
        self.colour_status.set("Saved: %s" % out)
        messagebox.showinfo("SinhalaSub", "Saved:\n%s" % out)

    def _build_opensubtitles(self, parent):
        if self._os_panel is not None:
            return
        box = ttk.LabelFrame(parent, text=" OpenSubtitles search (optional) ", padding=10)
        box.pack(fill="x", pady=(12, 0))
        self._os_panel = box
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Film", style="Dim.TLabel").pack(side="left")
        self.os_query = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.os_query)
        entry.pack(side="left", fill="x", expand=True, padx=(6, 8))
        entry.bind("<Return>", lambda _e: self.os_do_search())
        # A year makes all the difference: OpenSubtitles matches titles loosely,
        # so "The Gift" alone also returns "The Gifted" and "The Ultimate Gift".
        ttk.Label(row, text="Year", style="Dim.TLabel").pack(side="left")
        self.os_year = tk.StringVar()
        year_entry = ttk.Entry(row, textvariable=self.os_year, width=6)
        year_entry.pack(side="left", padx=(6, 8))
        year_entry.bind("<Return>", lambda _e: self.os_do_search())
        self.os_search_btn = ttk.Button(row, text="Search", command=self.os_do_search)
        self.os_search_btn.pack(side="left")
        self.os_list = tk.Listbox(
            box, height=6, exportselection=False, bg=FIELD, fg=TEXT,
            selectbackground=ACCENT, selectforeground="#ffffff",
            relief="flat", highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 9))
        self.os_list.pack(fill="x", pady=(8, 0))
        ttk.Label(box, style="Dim.TLabel", wraplength=640,
                  text="Each row shows the film and year it belongs to. Results for "
                       "the film you typed are listed first, most-downloaded "
                       "first - those are usually the best synced.").pack(
            anchor="w", pady=(6, 0))
        self.os_dl_btn = ttk.Button(
            box, text="Download selected as English .srt", command=self.os_do_download)
        self.os_dl_btn.pack(anchor="e", pady=(8, 0))

    def _busy(self, busy):
        state = ["disabled"] if busy else ["!disabled"]
        self.translate_btn.state(state)
        self.cancel_btn.state(["!disabled"] if busy else ["disabled"])
        if self.os_key:
            self.os_search_btn.state(state)
            self.os_dl_btn.state(state)

    # ----- translate flow --------------------------------------------------

    def browse(self):
        path = filedialog.askopenfilename(
            title="Choose an English .srt",
            filetypes=[("SubRip subtitles", "*.srt"), ("All files", "*.*")])
        if path:
            self.input_var.set(path)
            self._note_video_for(path)

    def start_translate(self):
        path = self.input_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("SinhalaSub", "Choose an existing .srt file first.")
            return
        try:
            subs = load_srt(path)
        except Exception as exc:
            messagebox.showerror("SinhalaSub", "Could not parse the .srt file:\n%s" % exc)
            return

        fresh = self.fresh_var.get()
        if fresh:
            clear_checkpoint(path)

        # A saved translation covering the whole file loads instantly with no
        # claude calls - so changing colour and re-saving costs nothing.
        cache = None if fresh else load_checkpoint(path, len(subs))
        if cache and all(i in cache for i in range(len(subs))):
            self.subs = subs
            self.current_input = path
            self.last_model = self.model_var.get()
            self.texts = [cache[i] for i in range(len(subs))]
            self.status_var.set(
                "Loaded the saved translation instantly - no usage spent. "
                "Adjust the colour if you like, then Save. "
                "(Tick \"Re-translate fresh\" to redo it from scratch.)")
            self._show_preview()
            return

        initial = {}
        mem_hits = 0
        if not fresh and self.memory_var.get():
            try:
                # Only reuse lines at least as good as this run's engine.
                initial = memory_prefill(
                    subs, min_tier=memory_db.engine_tier(self.provider_key))
                mem_hits = len(initial)
            except sqlite3.Error:
                initial = {}
        if cache:  # partial - offer to resume
            if messagebox.askyesno(
                    "Resume?",
                    "A previous run already translated %d of %d cues of this file.\n\n"
                    "Resume from there?" % (len(cache), len(subs))):
                initial.update(cache)
            else:
                clear_checkpoint(path)

        # For the CLI provider the main-window Model dropdown chooses the model;
        # API providers take their model from Providers -> Settings.
        if self.provider_key == "cli":
            self.settings["model"] = self.model_var.get()
        self.last_model = self._engine_label()
        self.provider = providers.build_active_provider(
            self.settings, cli_path=self.claude_path)
        if not self.provider.available():
            self._update_translate_gate()
            return
        try:
            workers = max(1, min(20, int(self.workers_var.get())))
        except ValueError:
            workers = providers.default_workers(self.provider_key)
        try:
            batch_size = max(5, min(250, int(self.batch_var.get())))
        except ValueError:
            batch_size = None  # "Auto": sized from the work and worker count

        self.subs = subs
        self.texts = None
        self.current_input = path
        self.cancel_event = threading.Event()
        self.t_start = time.time()
        self.eta_target = None
        self.batches_done = 0
        self.batches_total = len(make_batches(len(subs), batch_size))
        self.progress.configure(maximum=self.batches_total, value=0)
        self._busy(True)
        known = len(initial)
        self.phase = "Translating %d cues with %d parallel workers" % (len(subs), workers)
        if known:
            self.phase += " · %d already known (%d from memory)" % (known, mem_hits)
        self.running = True
        threading.Thread(
            target=self._worker,
            args=(subs, path, initial, workers, batch_size), daemon=True).start()

    def _worker(self, subs, path, initial, workers, batch_size):
        saved = {int(k): v for k, v in (initial or {}).items()}

        def on_batch(result):
            saved.update(result)
            try:
                save_checkpoint(path, len(subs), saved)
            except OSError:
                pass

        try:
            texts = translate_all(
                subs, self.provider,
                progress=lambda d, t: self.msgs.put(("progress", d, t)),
                log=lambda m: self.msgs.put(("status", m)),
                workers=workers, cancel=self.cancel_event,
                initial=initial, on_batch=on_batch, batch_size=batch_size)
            self.msgs.put(("done", texts))
        except TranslationCancelled:
            self.msgs.put(("cancelled", len(saved), len(subs)))
        except Exception as exc:
            self.msgs.put(("error", "Translation failed: %s" % exc))

    def cancel_run(self):
        self.cancel_event.set()
        self.cancel_btn.state(["disabled"])
        self.status_var.set("Cancelling - waiting for in-flight batches to finish...")

    def _poll(self):
        try:
            while True:
                msg = self.msgs.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    done, total = msg[1], msg[2]
                    self.batches_done, self.batches_total = done, total
                    self.progress.configure(maximum=total, value=done)
                    if self.t_start and done:
                        remaining = (time.time() - self.t_start) / done * (total - done)
                        self.eta_target = time.time() + remaining
                elif kind == "status":
                    self.status_var.set(str(msg[1]))
                elif kind == "done":
                    self.running = False
                    self.texts = msg[1]
                    took = _fmt_time(time.time() - self.t_start) if self.t_start else "?"
                    self.status_var.set(
                        "Translation finished in %s - review the preview." % took)
                    self._show_preview()
                elif kind == "cancelled":
                    self.running = False
                    self._busy(False)
                    self.progress.configure(value=0)
                    self.status_var.set(
                        "Cancelled. %d of %d cues are saved - press Translate again "
                        "to resume from there." % (msg[1], msg[2]))
                elif kind == "error":
                    self.running = False
                    self._busy(False)
                    self.status_var.set(str(msg[1]))
                    messagebox.showerror("SinhalaSub", str(msg[1]))
                elif kind == "batch_status":
                    self.batch_status.set(str(msg[1]))
                elif kind == "batch_progress":
                    self.batch_progress.configure(value=msg[1])
                elif kind == "batch_done":
                    self.batch_btn.state(["!disabled"])
                    self.batch_status.set(
                        "Finished %d of %d file(s)." % (msg[1], msg[2]))
                    self._batch_scan()
                elif kind == "os_results":
                    self._os_fill(msg[1])
                elif kind == "os_saved":
                    self._busy(False)
                    self.input_var.set(msg[1])
                    self.status_var.set("Downloaded English subtitle: %s" % msg[1])
                elif kind == "os_error":
                    self._busy(False)
                    self.status_var.set(str(msg[1]))
                    messagebox.showerror("OpenSubtitles", str(msg[1]))
        except queue.Empty:
            pass
        if self._alive:
            self._poll_id = self.root.after(100, self._poll)

    def _tick(self):
        """Once a second while a run is active: live countdown, never frozen."""
        if not self._alive:
            return
        if self.running:
            if self.eta_target is not None:
                remaining = self.eta_target - time.time()
                eta = ("about %s remaining" % _fmt_time(remaining)
                       if remaining > 2 else "almost done...")
                self.status_var.set("Batch %d of %d done · %s"
                                    % (self.batches_done, self.batches_total, eta))
            elif self.t_start:
                self.status_var.set(
                    "%s · elapsed %s (estimate appears after the first batch)"
                    % (self.phase, _fmt_time(time.time() - self.t_start)))
        if self._alive:
            self._tick_id = self.root.after(1000, self._tick)

    # ----- preview + confirm ------------------------------------------------

    def _show_preview(self):
        count = min(PREVIEW_COUNT, len(self.subs))
        win = tk.Toplevel(self.root)
        win.title("Preview - first %d cues" % count)
        win.geometry("720x480")
        win.configure(bg=BG)

        btns = ttk.Frame(win, padding=10)
        btns.pack(side="bottom", fill="x")
        ttk.Button(btns, text="Save .si.srt", style="Accent.TButton",
                   command=lambda: self._save(win)).pack(side="right")
        ttk.Button(btns, text="Save as (other formats)…",
                   command=self.export_as).pack(side="right", padx=8)
        ttk.Button(btns, text="Cancel (do not save)",
                   command=lambda: self._cancel_preview(win)).pack(side="right", padx=8)

        text = tk.Text(win, wrap="word", font=SINHALA_FONT, bg=CARD, fg=TEXT,
                       relief="flat", highlightthickness=0, padx=12, pady=10,
                       insertbackground=TEXT)
        scroll = ttk.Scrollbar(win, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("hdr", foreground=DIM, font=("Segoe UI", 9))
        text.tag_configure("en", foreground=DIM)
        text.tag_configure("si", foreground=self.color_hex or "#ffffff")
        for i in range(count):
            cue = self.subs[i]
            text.insert("end", "%d  [%s → %s]\n" % (cue.index, cue.start, cue.end), "hdr")
            text.insert("end", "%s\n" % flatten(cue.text), "en")
            text.insert("end", "%s\n\n" % self.texts[i], "si")
        text.configure(state="disabled")

        win.transient(self.root)
        win.grab_set()
        win.after(1, lambda: self._centre(win))
        win.protocol("WM_DELETE_WINDOW", lambda: self._cancel_preview(win))

    def _save(self, win):
        src = self.current_input or self.input_var.get().strip()
        out = default_output_path(src)  # always next to the input file
        if os.path.exists(out):
            ans = messagebox.askyesnocancel(
                "File exists",
                "%s already exists.\n\nYes = overwrite it\nNo = save under a new name\n"
                "Cancel = go back to the preview" % out,
                parent=win)
            if ans is None:
                return
            if not ans:
                out = unused_path(out)
        pairs = memory_collect(self.subs, self.texts) if self.memory_var.get() else {}
        out_texts = self.texts
        if self.color_hex:
            out_texts = ['<font color="%s">%s</font>' % (self.color_hex, t)
                         for t in self.texts]
        try:
            write_output(self.subs, out_texts, out)
        except Exception as exc:
            messagebox.showerror("SinhalaSub", "Could not write the file:\n%s" % exc,
                                 parent=win)
            return
        # Keep the completed translation cached next to the file so re-opening
        # it (e.g. just to change colour) loads instantly and spends no usage.
        try:
            save_checkpoint(src, len(self.subs),
                            {i: t for i, t in enumerate(self.texts)})
        except OSError:
            pass
        note = ""
        if pairs:
            try:
                # A hybrid run mixes machine and LLM output, so each line is
                # stored at the quality that actually produced it.
                self._remember(self.subs, self.texts, self.provider,
                               memory_db.engine_tier(self.provider_key))
                mem = get_memory()
                mem.record_run(src, engine=self.provider_key, cues=len(self.subs),
                               seconds=(time.time() - self.t_start)
                               if self.t_start else 0)
                note = " · memory now holds %d lines" % mem.stats()["lines"]
            except sqlite3.Error:
                pass

        # Quality report: catches unreadable timing, over-long lines and any cue
        # that quietly stayed in English, before the file ever reaches a player.
        report = ""
        try:
            issues = quality.check(self.subs)
            report = quality.summarise(issues, len(self.subs))
        except Exception:  # noqa: BLE001 - a report must never block saving
            issues = []

        win.destroy()
        self._busy(False)
        self.progress.configure(value=0)
        self.status_var.set("Saved: %s%s" % (out, note))
        self.last_issues = issues
        msg = "Saved:\n%s" % out
        if report:
            msg += "\n\n%s" % report
            if issues:
                msg += "\n\n(Tools → Quality report shows each line.)"
        messagebox.showinfo("SinhalaSub", msg)

    def export_as(self, subs=None, base_path=None, colour=None):
        """Ask for a format and encoding, then write the file for that player."""
        subs = subs if subs is not None else self.subs
        if subs is None:
            messagebox.showinfo("SinhalaSub", "Translate or load a subtitle first.")
            return
        base_path = base_path or self.current_input or self.input_var.get().strip()
        colour = self.color_hex if colour is None else colour

        win = tk.Toplevel(self.root)
        win.title("Save subtitle as…")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        win.after(1, lambda: self._centre(win))
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Format", style="Dim.TLabel").grid(row=0, column=0,
                                                               sticky="w")
        fmt_labels = [f["label"] for f in subtitle_export.FORMATS]
        fmt_var = tk.StringVar(value=fmt_labels[0])
        ttk.Combobox(frm, textvariable=fmt_var, values=fmt_labels, state="readonly",
                     width=46).grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(frm, text="Encoding", style="Dim.TLabel").grid(row=1, column=0,
                                                                 sticky="w")
        enc_labels = [e["label"] for e in subtitle_export.ENCODINGS]
        enc_var = tk.StringVar(value=enc_labels[0])
        ttk.Combobox(frm, textvariable=enc_var, values=enc_labels, state="readonly",
                     width=46).grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(frm, text="Frame rate", style="Dim.TLabel").grid(row=2, column=0,
                                                                   sticky="w")
        fps_var = tk.StringVar(value=str(subtitle_export.DEFAULT_FPS))
        ttk.Combobox(frm, textvariable=fps_var, state="readonly", width=12,
                     values=["23.976", "24", "25", "29.97", "30"]).grid(
            row=2, column=1, sticky="w", pady=4)
        ttk.Label(frm, text="(only used by MicroDVD .sub)",
                  style="Dim.TLabel").grid(row=3, column=1, sticky="w")

        ttk.Label(frm, style="Dim.TLabel", wraplength=430,
                  text="For most TVs and players choose SubRip (.srt). If a very old "
                       "player shows nothing, try MicroDVD (.sub), and if it still "
                       "shows nothing try UTF-8 with BOM. Note that a TV can only "
                       "display Sinhala if it has a Sinhala font built in.").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        def do_save():
            fmt = subtitle_export.FORMATS[fmt_labels.index(fmt_var.get())]
            enc = subtitle_export.ENCODINGS[enc_labels.index(enc_var.get())]["key"]
            try:
                fps = float(fps_var.get())
            except ValueError:
                fps = subtitle_export.DEFAULT_FPS
            base = base_path or os.path.join(_HERE, "subtitle")
            if base.lower().endswith(".srt"):
                base = base[:-4]
            suggested = os.path.basename(base) + ".si" + fmt["ext"]
            dest = filedialog.asksaveasfilename(
                title="Save subtitle as", defaultextension=fmt["ext"],
                initialdir=os.path.dirname(os.path.abspath(base)),
                initialfile=suggested,
                filetypes=[(fmt["label"], "*" + fmt["ext"]), ("All files", "*.*")])
            if not dest:
                return
            try:
                subtitle_export.write(subs, dest, fmt["key"], encoding=enc,
                                      fps=fps, colour=colour)
            except Exception as exc:  # noqa: BLE001 - report, never crash
                messagebox.showerror("SinhalaSub",
                                     "Could not write the file:\n%s" % exc, parent=win)
                return
            win.destroy()
            self.status_var.set("Saved: %s" % dest)
            messagebox.showinfo("SinhalaSub", "Saved:\n%s" % dest)

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left", padx=6)
        ttk.Button(btns, text="Save", style="Accent.TButton",
                   command=do_save).pack(side="left")
        frm.columnconfigure(1, weight=1)

    def _cancel_preview(self, win):
        win.destroy()
        self._busy(False)
        self.progress.configure(value=0)
        self.status_var.set(
            "Not saved. The finished translation is kept - press Translate again "
            "to get the preview back instantly.")

    # ----- OpenSubtitles flow ------------------------------------------------

    def os_do_search(self):
        query = self.os_query.get().strip()
        if not query:
            return
        self._busy(True)
        self.status_var.set("Searching OpenSubtitles...")

        def work():
            try:
                self.msgs.put(("os_results",
                               os_search(self.os_key, query,
                                         self.os_year.get().strip())))
            except Exception as exc:
                self.msgs.put(("os_error", "Search failed: %s" % exc))

        threading.Thread(target=work, daemon=True).start()

    def _os_fill(self, results):
        self.os_results = results
        self.os_list.delete(0, "end")
        for r in results:
            self.os_list.insert("end", r["label"])
        self._busy(False)
        films = len({r["movie"] for r in results if r["movie"]})
        note = ""
        if films > 1:
            note = (" across %d different films - check the name on each row, "
                    "or add a year to narrow it." % films)
        self.status_var.set("Found %d subtitle(s)%s" % (len(results), note))

    def os_do_download(self):
        sel = self.os_list.curselection()
        if not sel or not self.os_results:
            messagebox.showinfo("OpenSubtitles", "Search and select a result first.")
            return
        chosen = self.os_results[sel[0]]
        file_id = chosen["file_id"]
        if file_id is None:  # the "no subtitles found" message row
            messagebox.showinfo(
                "OpenSubtitles",
                "That row is a message, not a subtitle.\n\nTry the film's exact "
                "title without the year, or a shorter version of the name.")
            return
        stem = chosen["movie"] or chosen["release"] or str(file_id)
        if chosen["movie"] and chosen["year"]:
            stem = "%s.%s" % (chosen["movie"], chosen["year"])
        safe = re.sub(r'[\\/:*?"<>|]+', "_", stem)[:80]
        dest = filedialog.asksaveasfilename(
            title="Save English .srt as", defaultextension=".srt",
            initialfile=safe + ".srt",
            filetypes=[("SubRip subtitles", "*.srt")])
        if not dest:
            return
        self._busy(True)
        self.status_var.set("Downloading from OpenSubtitles...")

        def work():
            try:
                self.msgs.put(("os_saved", os_download(self.os_key, file_id, dest)))
            except Exception as exc:
                self.msgs.put(("os_error", "Download failed: %s" % exc))

        threading.Thread(target=work, daemon=True).start()


def make_root():
    """Root window with file drag-and-drop when tkinterdnd2 is installed."""
    try:
        from tkinterdnd2 import TkinterDnD
        return TkinterDnD.Tk(), True
    except Exception:  # noqa: BLE001 - plain Tk still works, just no dropping
        return tk.Tk(), False


def parse_drop(data):
    """Turn a drag-and-drop payload into a list of paths.

    Tk hands paths over as a single string with {braces} around any path that
    contains spaces, which is common for movie folders.
    """
    paths, buf, in_brace = [], "", False
    for ch in data or "":
        if ch == "{":
            in_brace = True
        elif ch == "}":
            in_brace = False
            if buf.strip():
                paths.append(buf.strip())
            buf = ""
        elif ch == " " and not in_brace:
            if buf.strip():
                paths.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        paths.append(buf.strip())
    return paths


def main():
    root, dnd = make_root()
    SinhalaSubApp(root, dnd_available=dnd)
    root.mainloop()


if __name__ == "__main__":
    main()
