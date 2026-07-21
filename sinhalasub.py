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

import providers

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
                  cancel=None, initial=None, on_batch=None):
    """Translate every cue; returns a list of Sinhala texts aligned to subs order.

    provider  - a providers.Provider (CLI, Anthropic, Gemini, or OpenAI-compatible)
    initial   - {position: text} already translated (resume); those batches skip
    on_batch  - called with each finished batch dict (used for checkpointing)
    progress(batches_done, batches_total) is called after each batch.
    """
    stop = cancel if cancel is not None else threading.Event()
    texts = [None] * len(subs)
    if initial:
        for i, t in initial.items():
            i = int(i)
            if 0 <= i < len(texts):
                texts[i] = t
    todo = []
    for b in make_batches(len(subs)):
        if any(texts[i] is None for i in b):
            todo.append((b, {i for i in b if texts[i] is not None}))
    if not todo:
        return texts
    done = 0
    first_error = None
    user_cancelled = False
    with ThreadPoolExecutor(max_workers=max(1, workers or MAX_WORKERS)) as ex:
        futures = [ex.submit(translate_batch, subs, b, provider, log, stop, skip)
                   for b, skip in todo]
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


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS memory ("
        " source TEXT PRIMARY KEY,"
        " sinhala TEXT NOT NULL,"
        " model TEXT,"
        " created TEXT)")
    return con


def memory_reusable(source):
    """Only short stock phrases and [sound cues] are safe to reuse blindly."""
    return bool(BRACKET_RE.match(source)) or len(source.split()) <= MEMORY_MAX_WORDS


def memory_lookup(sources):
    """Return {source: sinhala} for every source line found in the database."""
    sources = list(set(sources))
    found = {}
    con = _db()
    try:
        for start in range(0, len(sources), 500):
            chunk = sources[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for src, sin in con.execute(
                    "SELECT source, sinhala FROM memory WHERE source IN (%s)" % marks,
                    chunk):
                found[src] = sin
    finally:
        con.close()
    return found


def memory_store(pairs, model=""):
    """Insert/refresh translated pairs; returns total rows now in the database."""
    con = _db()
    try:
        con.executemany(
            "INSERT OR REPLACE INTO memory (source, sinhala, model, created) "
            "VALUES (?, ?, ?, datetime('now'))",
            [(src, sin, model) for src, sin in pairs.items()])
        con.commit()
        return con.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    finally:
        con.close()


def memory_prefill(subs):
    """Return {position: sinhala} for cues whose text is safely known already."""
    flat = [flatten(c.text) for c in subs]
    found = memory_lookup(flat)
    return {i: found[f] for i, f in enumerate(flat)
            if f in found and memory_reusable(f)}


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


def os_search(key, query):
    """Search English subtitles by movie name; returns [(file_id, label), ...]."""
    import requests  # lazy: local-file mode must work without requests installed
    resp = requests.get(OS_API_BASE + "/subtitles",
                        params={"query": query, "languages": "en"},
                        headers=_os_headers(key), timeout=30)
    resp.raise_for_status()
    results = []
    for item in resp.json().get("data", []):
        attrs = item.get("attributes", {})
        files = attrs.get("files") or []
        if not files or files[0].get("file_id") is None:
            continue
        label = attrs.get("release") or files[0].get("file_name") or str(files[0]["file_id"])
        results.append((files[0]["file_id"], label))
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

BG = "#131420"
CARD = "#1c1e2e"
FIELD = "#262940"
BORDER = "#2e3150"
TEXT = "#e9e9f4"
DIM = "#9a9db8"
ACCENT = "#7c6cff"
ACCENT_HOVER = "#9186ff"


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
    def __init__(self, root):
        self.root = root
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
        root.title("SinhalaSub")
        root.minsize(720, 340)

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
        menubar.add_command(label="About", command=self.show_about)
        root.config(menu=menubar)

        main = ttk.Frame(root, padding=16)
        main.pack(fill="both", expand=True)
        self.main = main
        self._os_panel = None

        ttk.Label(main, text="SinhalaSub", style="Header.TLabel").pack(anchor="w")
        self.subtitle_lbl = ttk.Label(
            main,
            text="English → සිංහල subtitles · Engine: %s" % self._engine_label(),
            style="Dim.TLabel")
        self.subtitle_lbl.pack(anchor="w", pady=(0, 12))

        file_row = ttk.Frame(main)
        file_row.pack(fill="x")
        ttk.Label(file_row, text="English .srt:").pack(side="left")
        self.input_var = tk.StringVar()
        ttk.Entry(file_row, textvariable=self.input_var).pack(
            side="left", fill="x", expand=True, padx=8)
        ttk.Button(file_row, text="Browse...", command=self.browse).pack(side="left")

        if self.os_key:
            self._build_opensubtitles(main)

        opts = ttk.Frame(main)
        opts.pack(fill="x", pady=(12, 0))
        ttk.Label(opts, text="Model", style="Dim.TLabel").pack(side="left")
        saved_model = self.settings.get("model")
        init_model = saved_model if saved_model in MODEL_CHOICES else (
            DEFAULT_MODEL if DEFAULT_MODEL in MODEL_CHOICES else "CLI default")
        self.model_var = tk.StringVar(value=init_model)
        ttk.Combobox(opts, textvariable=self.model_var, values=MODEL_CHOICES,
                     state="readonly", width=11).pack(side="left", padx=(6, 18))
        ttk.Label(opts, text="Parallel batches", style="Dim.TLabel").pack(side="left")
        self.workers_var = tk.StringVar(value=str(self.settings.get("workers", MAX_WORKERS)))
        ttk.Spinbox(opts, from_=1, to=20, textvariable=self.workers_var,
                    width=4, state="readonly").pack(side="left", padx=6)

        ttk.Label(main, style="Dim.TLabel", wraplength=660,
                  text="Tip: \"CLI default\" follows your Claude Code /model setting "
                       "(opus = best but heaviest on your usage limit). Choose sonnet "
                       "for far lighter usage at high quality, or haiku for the least "
                       "usage.").pack(anchor="w", pady=(6, 0))

        opts2 = ttk.Frame(main)
        opts2.pack(fill="x", pady=(8, 0))
        ttk.Label(opts2, text="Subtitle colour", style="Dim.TLabel").pack(side="left")
        preset = next((n for n, h in COLOR_PRESETS if h == self.color_hex),
                      "Custom..." if self.color_hex else "None (player default)")
        self.color_name = tk.StringVar(value=preset)
        color_box = ttk.Combobox(opts2, textvariable=self.color_name,
                                 values=[n for n, _ in COLOR_PRESETS],
                                 state="readonly", width=18)
        color_box.pack(side="left", padx=(6, 6))
        color_box.bind("<<ComboboxSelected>>", self._on_color)
        self.swatch = tk.Label(opts2, width=3, bg=self.color_hex or FIELD,
                               relief="flat")
        self.swatch.pack(side="left")
        self.memory_var = tk.BooleanVar(value=self.settings.get("memory", True))
        ttk.Checkbutton(opts2, text="Translation memory (reuse saved lines)",
                        variable=self.memory_var).pack(side="left", padx=18)
        self.fresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts2, text="Re-translate fresh (ignore saved)",
                        variable=self.fresh_var).pack(side="left")

        act = ttk.Frame(main)
        act.pack(fill="x", pady=(12, 0))
        self.translate_btn = ttk.Button(
            act, text="Translate to Sinhala", style="Accent.TButton",
            command=self.start_translate)
        self.translate_btn.pack(side="left")
        self.cancel_btn = ttk.Button(act, text="Cancel", command=self.cancel_run)
        self.cancel_btn.pack(side="left", padx=8)
        self.cancel_btn.state(["disabled"])

        self.progress = ttk.Progressbar(main, mode="determinate")
        self.progress.pack(fill="x", pady=(12, 0))
        self.status_var = tk.StringVar(value="Select an English .srt file.")
        ttk.Label(main, textvariable=self.status_var, style="Dim.TLabel",
                  wraplength=660).pack(anchor="w", pady=(8, 0))

        self._update_translate_gate()

        # Remember choices: save whenever the user changes any of these.
        self._loaded = True
        self.model_var.trace_add("write", self._persist)
        self.workers_var.trace_add("write", self._persist)
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
        self.settings["memory"] = bool(self.memory_var.get())
        self.settings["color_hex"] = self.color_hex
        self.settings["color_name"] = self.color_name.get()
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
        self.subtitle_lbl.configure(
            text="English → සිංහල subtitles · Engine: %s" % self._engine_label())
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

    def _select_provider(self, key):
        self.provider_key = key
        self.settings["provider"] = key
        save_settings(self.settings)
        # Reset parallelism to this engine's sensible default (CLI 3, API 10);
        # the user can still tweak the spinbox afterwards.
        self.workers_var.set(str(providers.default_workers(key)))
        self._refresh_engine_header()

    def show_about(self):
        messagebox.showinfo(
            "About SinhalaSub",
            "SinhalaSub\n\n"
            "Translate English movie subtitles into natural, meaning-based "
            "spoken Sinhala using the LLM backbone of your choice — Claude "
            "Code CLI, Anthropic API, Google Gemini, or any OpenAI-compatible / "
            "local model.\n\n"
            "Created by NLK")

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

        status = ttk.Label(frm, text="", style="Dim.TLabel", wraplength=340)
        status.grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))

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
            status.configure(text="Testing…")
            win.update_idletasks()
            prov = providers.make_provider(
                d["key"], model=model_var.get().strip() or d["default_model"],
                api_key=key_var.get().strip(),
                base_url=base_var.get().strip() or d.get("default_base_url"),
                cli_path=self.claude_path)
            ok, msg = prov.test()
            status.configure(text=("✓ " if ok else "✗ ") + msg)

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
            self._menu_provider.set(d["key"])
            self._refresh_engine_header()
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=3, sticky="e", pady=(12, 0))
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

    def _build_opensubtitles(self, parent):
        if self._os_panel is not None:
            return
        box = ttk.LabelFrame(parent, text=" OpenSubtitles search (optional) ", padding=10)
        box.pack(fill="x", pady=(12, 0))
        self._os_panel = box
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.os_query = tk.StringVar()
        ttk.Entry(row, textvariable=self.os_query).pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        self.os_search_btn = ttk.Button(row, text="Search", command=self.os_do_search)
        self.os_search_btn.pack(side="left")
        self.os_list = tk.Listbox(
            box, height=5, exportselection=False, bg=FIELD, fg=TEXT,
            selectbackground=ACCENT, selectforeground="#ffffff",
            relief="flat", highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 10))
        self.os_list.pack(fill="x", pady=(8, 0))
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
                initial = memory_prefill(subs)
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

        self.subs = subs
        self.texts = None
        self.current_input = path
        self.cancel_event = threading.Event()
        self.t_start = time.time()
        self.eta_target = None
        self.batches_done = 0
        self.batches_total = len(make_batches(len(subs)))
        self.progress.configure(maximum=self.batches_total, value=0)
        self._busy(True)
        known = len(initial)
        self.phase = "Translating %d cues with %d parallel workers" % (len(subs), workers)
        if known:
            self.phase += " · %d already known (%d from memory)" % (known, mem_hits)
        self.running = True
        threading.Thread(
            target=self._worker,
            args=(subs, path, initial, workers), daemon=True).start()

    def _worker(self, subs, path, initial, workers):
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
                initial=initial, on_batch=on_batch)
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
                total = memory_store(pairs, self.last_model)
                note = " · translation memory now holds %d lines" % total
            except sqlite3.Error:
                pass
        win.destroy()
        self._busy(False)
        self.progress.configure(value=0)
        self.status_var.set("Saved: %s%s" % (out, note))
        messagebox.showinfo("SinhalaSub", "Saved:\n%s" % out)

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
                self.msgs.put(("os_results", os_search(self.os_key, query)))
            except Exception as exc:
                self.msgs.put(("os_error", "Search failed: %s" % exc))

        threading.Thread(target=work, daemon=True).start()

    def _os_fill(self, results):
        self.os_results = results
        self.os_list.delete(0, "end")
        for _, label in results:
            self.os_list.insert("end", label)
        self._busy(False)
        self.status_var.set("Found %d result(s)." % len(results))

    def os_do_download(self):
        sel = self.os_list.curselection()
        if not sel or not self.os_results:
            messagebox.showinfo("OpenSubtitles", "Search and select a result first.")
            return
        file_id, label = self.os_results[sel[0]]
        safe = re.sub(r'[\\/:*?"<>|]+', "_", label)[:80]
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


def main():
    root = tk.Tk()
    SinhalaSubApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
