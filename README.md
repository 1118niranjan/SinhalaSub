# SinhalaSub

Translate an English movie subtitle (`.srt`) into natural, meaning-based spoken
Sinhala using the LLM backbone of your choice — the local `claude` CLI (default,
no API key), the Anthropic API, Google Gemini, or **any** OpenAI-compatible or
local model (OpenRouter, Ollama, LM Studio, LiteLLM).

*Created by NLK.*

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

## Connecting an LLM (where to put your API key)

Two ways — either works, and neither is ever committed to GitHub:

**In the app (easiest).** Open the **Providers** menu at the top → pick a
provider → **Settings…**. Paste your API key (and, for OpenAI-compatible/local,
the Base URL and model), then click **Test connection** to confirm it works, and
**Save**. Your choice is remembered.

**Or in `.env`.** Copy `.env.example` to `.env` and fill the block for your
provider. Set `SINHALASUB_PROVIDER` to `cli`, `anthropic`, `gemini`, or `openai`.

| Provider | Key goes in | Default model | Notes |
|---|---|---|---|
| **Google Translate (free, fast)** | — (no key) | — | **Fastest option: a full movie in ~4 minutes, free and unlimited, no sign-in.** Machine translation, so the Sinhala is more literal than an LLM's — great for watching, less nuanced on jokes/slang. |
| Claude Code CLI | — (no key) | your `/model` setting | Best Sinhala quality, but ~35 min per movie and it consumes your Claude usage limit (measured ~1 cue/sec ceiling) |
| Gemini CLI (Google login) | — (no key) | `gemini-2.5-flash` | Free via your Google account. Install `npm i -g @google/gemini-cli`, run `gemini` once → **Login with Google**. Slower than the Gemini API (spawns per batch) but free and doesn't touch your Claude limit. |
| Anthropic API | `ANTHROPIC_API_KEY` | `claude-haiku-4-5` | System prompt is prompt-cached for speed |
| Google Gemini (API) | `GEMINI_API_KEY` | `gemini-2.5-flash` | Fastest Gemini option; free API key from aistudio.google.com/apikey |
| OpenAI / Local | `OPENAI_API_KEY` (+ `OPENAI_BASE_URL`) | `gpt-4o-mini` | Works with OpenAI, **OpenRouter** (`https://openrouter.ai/api/v1`, free key + free models like `google/gemini-2.0-flash-exp:free`), **Ollama** (`http://localhost:11434/v1`), and **LM Studio** (`http://localhost:1234/v1`). Set your model name. |

> **Free via OpenRouter:** create a free key at [openrouter.ai/keys](https://openrouter.ai/keys) (no card), pick a `:free` model, and set the base URL above. The free tier allows ~50 requests/day — raise **Cues per batch** to ~60 so a full movie fits in one day.

**Speed (measured on a 2-hour movie, ~2000 cues):**

| Engine | Time | Cost |
|---|---|---|
| Google Translate | **~4 min** | free, unlimited |
| HTTP APIs (Gemini / OpenRouter / Anthropic) | ~3–5 min | free tier or cents |
| Claude Code CLI | ~35 min | your Claude usage limit |

The app also skips cues that need no translation (`[music]`, `♪`, numbers),
translates repeated lines (`Yeah.`, `Okay.`) only once, and packs each request
with real work only — so every engine sends far fewer tokens than before.
For the best mix of speed and quality use a fast model (Claude Haiku, Gemini
Flash, `gpt-4o-mini`); switch to a larger model (Sonnet/Opus, Gemini Pro) only
when a film needs extra polish.

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
