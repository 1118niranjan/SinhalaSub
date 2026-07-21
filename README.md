# SinhalaSub

Translate an English movie subtitle (`.srt`) into natural, meaning-based spoken
Sinhala — using your local `claude` CLI (Claude Code subscription, headless) as
the translation engine. No API keys, no per-token cost.

## Requirements

- Windows, Python 3.11+
- Claude Code installed and signed in (`claude -p "hi"` must work in a terminal)
- `pip install -r requirements.txt` (pysrt + requests)

## Run

```
python sinhalasub.py
```

or double-click **`SinhalaSub.pyw`** to open it without a console window.

1. **Browse** to a local English `.srt` (or use the OpenSubtitles search — see below).
2. Pick a **model**, **parallel batches**, and an optional **subtitle colour**
   (written into the .srt as `<font color>` tags, rendered by PotPlayer), then
   click **Translate to Sinhala**. The status line shows a live countdown of
   the estimated time remaining, updating every second.
3. A **preview** of the first 10 translated cues opens (in your chosen colour).
   Click **Save .si.srt** to write the file, or **Cancel** to discard. If the
   output already exists you choose: overwrite, save under a new name, or go back.

The output is written next to the input as `<basename>.si.srt` — e.g.
`Movie.2021.srt` → `Movie.2021.si.srt` — in UTF-8 (no BOM), with cue indexes and
timestamps identical to the input. Put it next to `Movie.2021.mkv` and PotPlayer
auto-loads it.

## Choosing a model (and saving your usage limit)

The **Model** dropdown controls both quality and how fast you burn your
Claude subscription's usage window:

- **CLI default** — follows your Claude Code `/model` setting. If that is
  **opus**, you get the best quality but it is by far the heaviest on your
  usage limit; a full movie can exhaust a session window.
- **sonnet** (recommended) — high quality, *much* lighter on your limit.
- **haiku** — fastest and lightest, slightly less polished.

Whatever you select IS used — selecting sonnet really runs sonnet. If a
translation stops with "session limit" from claude, switch to sonnet/haiku
and press Translate again; it resumes from where it stopped, so you lose
nothing.

Your choices are remembered: the model, subtitle colour, parallel-batch
count, and translation-memory toggle are saved to `settings.json` the moment
you change them, so you pick once in the app and it sticks on the next
launch — no code or `.env` editing needed. (A `SINHALASUB_MODEL` in `.env`
only sets the first-run default; your in-app choice takes over after that.)

Note: parallel batches change *speed*, not total usage — the same number of
cues costs the same whether you run 1 or 6 at a time; more workers just
finish sooner (and reach the limit sooner). Lower the "Parallel batches"
number if you want a gentler, slower run.

## Quality and speed

- Batches run **in parallel** (default 3 workers). This does not affect
  quality: each batch's context is the previous *source English* cues, so
  batches are independent.
- Each call runs with `--strict-mcp-config` so your Claude Code MCP
  servers/connectors are not loaded on every batch, and hidden so **no
  console windows pop up** during translation.
- The prompt enforces: phonetic transliteration of names keeping every
  syllable (Marseille → මාර්සෙයි, not මාසේ), living Sinhala idioms over
  literal phrasing, and profanity kept at full strength, never sanitized.

## Changing colour is free and instant

Once a file is fully translated and saved, the result is cached next to it
(`<basename>.si.partial.json`). Load the **same** English .srt again, pick a
different colour, and press Translate: it loads the saved translation
**instantly with no usage spent** and jumps straight to the preview — just
Save. To force a brand-new translation instead (e.g. after changing the
model), tick **"Re-translate fresh (ignore saved)"** before pressing
Translate.

## Translation memory (translations.db)

Every line you save is stored in a local SQLite database. On the next
translation, **short stock phrases** (up to 5 words — "Thank you.", "Okay.",
"What?") and `[sound cues]` that are already in the database are reused
instantly at zero cost; claude only translates what's left. Longer sentences
are always freshly translated, because the same English line can need a
different Sinhala rendering depending on the scene — accuracy stays first.
Reused lines are still shown to the model as context so scenes stay coherent.
Untick "Translation memory" before translating to disable both reuse and
saving. The database grows with every movie and is yours — it never leaves
your machine (and is gitignored).

## Interrupted? Resume for free

After every finished batch, progress is checkpointed to
`<basename>.si.partial.json` next to the input. If the run is cancelled, hits
your usage limit, or the app closes, press **Translate** again on the same
file and it resumes from where it stopped. The checkpoint is deleted after a
successful save.

## Configuration (.env)

Copy `.env.example` to `.env` and edit. Real environment variables always
override `.env`. Keys: `OPENSUBTITLES_API_KEY`, `SINHALASUB_MODEL`,
`SINHALASUB_WORKERS`, `SINHALASUB_BATCH_SIZE`, `SINHALASUB_TIMEOUT`.
`.env` is gitignored, so the folder is safe to push to GitHub as-is.

## How it translates

Cues are sent to `claude -p` in batches of ~30, with the previous 3 cues
included as read-only context so pronouns, idioms, running jokes, and profanity
resolve to their intended meaning — fluent spoken Sinhala, not word-for-word.
Sound/music cues like `[music]` are left untouched. Malformed batches are
retried twice; any cue that still fails keeps its English text so alignment is
never corrupted.

## Optional: OpenSubtitles search

Set `OPENSUBTITLES_API_KEY` (in `.env` or the environment) and a search panel
appears: type a movie name, search, select a result, and download the English
`.srt`. Without the key the panel is hidden and the app works fully in
local-file mode.
