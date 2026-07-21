# SinhalaSub — Pluggable LLM Providers (Design)

**Date:** 2026-07-21
**Author:** NLK (design assisted by Claude Code)
**Status:** Approved for planning

## 1. Goal

Make SinhalaSub's translation engine **pluggable** so anyone who downloads it from
GitHub can connect the LLM backbone of their choice — the local Claude Code CLI
(current behaviour), or an API/local LLM — from inside the app, and translate a
full movie **fast** without losing the current translation quality.

Two user requirements drive this:

1. **Dynamic backbone.** Support Claude Code CLI, Anthropic API, Google Gemini,
   and any OpenAI-compatible endpoint (OpenAI, OpenRouter, and local LLMs such as
   Ollama / LM Studio / LiteLLM) — selectable and configurable in the GUI, with
   clear documentation of where to put API keys.
2. **Speed.** A 2-hour movie currently takes ~15 minutes. Target: ~1–3 minutes on
   any API provider, with quality preserved.

Attribution: the app credits **"Created by NLK"** in an About menu and the README.

## 2. Why it is slow today (diagnosis)

Every batch calls `run_claude()` in `sinhalasub.py`, which runs
`subprocess.run([claude, "-p", ...])`. This **launches the whole Claude Code CLI
as a new process for each batch**. That process cold-starts Node.js, loads config,
authenticates, and routes to a model *before* any translation happens.

For a ~1,500-cue movie: ~50 batches, 3 workers ≈ 17 sequential rounds; each spawn
costs ~3–8 s of startup plus ~5–15 s inference → ~10–25 s/batch → ~6–15 min
(worse with retries). `--strict-mcp-config` only trims part of the startup tax.

**The dominant cost is re-launching the CLI per batch.** API providers remove that
entirely (one warm HTTPS connection, ~0 startup per call), allow far more
parallelism (10–20 vs 3), and fast models return in 1–3 s. The pluggability
feature and the speed fix are therefore the same work.

## 3. Architecture

The app is already engine-agnostic: batching, retries, translation memory,
checkpoints, colour/preview, and the GUI only need "send text → get text back".
Only `run_claude()` knows about Claude. **We introduce a provider layer behind
that single function and change nothing else about the translation logic.**

### 3.1 Provider interface (new module `providers.py`)

Each provider is a small class:

```
class Provider:
    key: str          # stable id, e.g. "cli", "anthropic", "gemini", "openai"
    label: str        # menu text, e.g. "OpenAI / Local LLM"
    needs_key: bool   # whether an API key is required

    def available(self) -> bool
        # CLI: claude on PATH. API: key present (or local endpoint reachable).

    def translate(self, prompt: str, stdin_text: str, model: str,
                  timeout: int) -> str
        # Returns the model's raw text output, in the SAME format the current
        # code expects (lines of "NUMBER|||translation"), so parse_response()
        # is unchanged. `prompt` is the existing PROMPT constant; `stdin_text`
        # is the existing build_batch_input() output.

    def test(self) -> tuple[bool, str]
        # Sends one trivial batch ("1|||Hello") and reports (ok, message+latency)
        # for the GUI "Test connection" button.
```

`translate_batch()` changes only in that it calls `provider.translate(PROMPT,
stdin_text, model, timeout)` instead of `run_claude(...)`. All retry, parse,
keep-English-on-failure, and memory behaviour is untouched.

### 3.2 The four providers

**CLI (`cli`) — default, unchanged.** Wraps the existing `run_claude()` /
`_extra_args` / `CREATE_NO_WINDOW` logic verbatim. No key. `model` values:
`CLI default` / `opus` / `sonnet` / `haiku` (as today).

**Anthropic API (`anthropic`).** Raw HTTPS via `requests`:
- `POST https://api.anthropic.com/v1/messages`
- Headers: `x-api-key`, `anthropic-version: 2023-06-01`, `content-type: application/json`
- Body: `system` = `[{type:"text", text: PROMPT, cache_control:{type:"ephemeral"}}]`
  (prompt-cached so repeated batches are ~90% cheaper/faster), `messages` =
  `[{role:"user", content: stdin_text}]`, `max_tokens` = 8000.
- Response text = concatenation of `content[*].text` where `type == "text"`.
- No `thinking` parameter (fast path). Default model **`claude-haiku-4-5`**.

**Google Gemini (`gemini`).** Raw HTTPS via `requests`:
- `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=…`
- Body: `system_instruction.parts[0].text` = PROMPT; `contents` =
  `[{role:"user", parts:[{text: stdin_text}]}]`;
  `generationConfig.maxOutputTokens` = 8000.
- Response text = concatenation of `candidates[0].content.parts[*].text`.
- Default model **`gemini-2.5-flash`**.

**OpenAI-compatible (`openai`) — the "anything" provider.** Raw HTTPS via `requests`:
- `POST {base_url}/chat/completions` (default base `https://api.openai.com/v1`)
- Headers: `Authorization: Bearer {key}` (key optional for local endpoints)
- Body: `messages` = `[{role:"system", content: PROMPT}, {role:"user",
  content: stdin_text}]`, `max_tokens` = 8000, `temperature` = 0.3.
- Response text = `choices[0].message.content`.
- Default model **`gpt-4o-mini`**. Covers OpenAI, OpenRouter
  (`https://openrouter.ai/api/v1`), **Ollama** (`http://localhost:11434/v1`),
  **LM Studio** (`http://localhost:1234/v1`), and LiteLLM — user sets base URL +
  model (e.g. `llama3.1`, `qwen2.5`) for local models.

### 3.3 Dependency decision

All three API providers use the **`requests` library already in
`requirements.txt`** via raw HTTPS — no new/heavy SDKs (`anthropic`, `openai`,
`google-generativeai`). Rationale: the app must stay lightweight for downloaders,
and the OpenAI-compatible/local path is a first-class goal that `requests` serves
uniformly. This is a deliberate, consistent choice across all providers, not an
oversight.

## 4. Configuration & keys (GUI menu + `.env`)

### 4.1 Menu bar (new)

A `tk.Menu` menu bar is added to the root window:

- **Providers ▾** — a radio list of the four providers (checkmark on the active
  one) plus **Settings…**. Selecting a provider switches the active engine;
  **Settings…** opens the dialog below.
- **About** — shows a dialog: app name, one-line description, **"Created by NLK."**

The header line under the title shows the active engine, e.g.
`English → සිංහල · Engine: Claude Code CLI` or `… · Engine: Gemini (gemini-2.5-flash)`.

### 4.2 Settings dialog

A modal `Toplevel` with:

- **Provider** dropdown (the four providers).
- **API key** entry (masked). Hidden/disabled for the CLI provider.
- **Base URL** entry (OpenAI-compatible only; prefilled `https://api.openai.com/v1`).
- **Model** entry — free text, prefilled with the provider's default; for the CLI
  provider this is the `CLI default/opus/sonnet/haiku` dropdown instead.
- **Test connection** button → calls `provider.test()` and shows result + latency.
- **Cancel** / **Save**.

### 4.3 Storage & precedence

- **Non-secret settings** (selected provider, per-provider model, base URL,
  workers, colour, memory) → `settings.json` (already gitignored).
- **API keys entered in the GUI** → new `secrets.json` (gitignored). Never
  hardcoded, never committed.
- **`.env` remains a supported alternative** for keys and settings.
- **Precedence for a provider's key/base URL/model:** a value entered in the GUI
  (`secrets.json`/`settings.json`) wins; otherwise fall back to the
  environment/`.env`. This makes "I typed it in the app" just work, while
  file-first users can rely on `.env`.

Environment variable names (documented in `.env.example`):
`SINHALASUB_PROVIDER`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, plus the existing `SINHALASUB_*` keys. `secrets.json` is added
to `.gitignore`.

## 5. Speed knobs (all providers)

- **Per-provider default workers:** CLI stays at 3; API providers default to 10.
  The workers spinbox cap is raised from 6 to 20 (API only benefits meaningfully).
- **Batch size** stays configurable (`SINHALASUB_BATCH_SIZE`, default 30).
- **`max_tokens`** = 8000 (override `SINHALASUB_MAX_TOKENS`); generous enough that
  a 30-line batch never truncates. If a provider still returns short, the existing
  retry loop (`MAX_ATTEMPTS`) and keep-English fallback protect alignment.

Quality vs. speed remains a **model choice**: fast models (Haiku, Gemini Flash,
gpt-4o-mini) for ~1–3 min runs; Sonnet/Opus/Gemini-Pro/larger for maximum quality
(still faster than the current CLI because there is no per-batch process spawn).

## 6. Error handling

- Providers raise `RuntimeError` with a clear message (HTTP status + short body
  snippet) on failure; the existing `translate_batch()` retry loop handles it.
- On HTTP 429/5xx a provider may honour `Retry-After` with a short sleep before
  raising, so the batch retry has a chance to succeed.
- HTTP calls use `timeout=CLAUDE_TIMEOUT` (renamed conceptually to a generic
  per-call timeout; env stays `SINHALASUB_TIMEOUT`). Cancellation is checked
  between attempts as today; timeouts bound in-flight calls.
- Missing key / unreachable local endpoint → provider `available()` is false and
  Translate is disabled with a status hint pointing to Providers → Settings.

## 7. What stays unchanged

The translation prompt (`PROMPT`), `build_batch_input()`, `parse_response()`,
batching, parallel execution model, translation memory (`translations.db`),
checkpoint/resume, OpenSubtitles panel, colour/preview/save flow, and the dark
theme. Only the engine call and the settings/menu/about UI are added.

## 8. File-by-file changes

- **`providers.py` (new):** provider classes, a registry, provider-settings +
  secrets load/save, and the `test()` probes.
- **`sinhalasub.py`:** call `provider.translate(...)` in `translate_batch()`; add
  the menu bar, Settings dialog, and About dialog; show active-engine header;
  wire provider selection + persistence; raise workers cap and set per-provider
  default workers.
- **`.env.example`:** rewrite with a clearly labelled block per provider (which
  variable to set, base-URL examples for Ollama/LM Studio) and `SINHALASUB_PROVIDER`.
- **`.gitignore`:** add `secrets.json`.
- **`README.md`:** add "Connecting an LLM (where to put your API key)", a
  "Created by NLK" credit, and update the speed section.
- **`requirements.txt`:** unchanged (`pysrt` + `requests` already suffice).

## 9. Testing

- Existing `parse_response()` behaviour is preserved and covered.
- New unit tests (TDD) for each API provider: request-body construction and
  response-text extraction against a **mocked `requests`** (no network), plus the
  error path (non-200 → `RuntimeError`) and `test()` happy path.
- CLI provider test: `translate()` delegates to the existing subprocess path
  (mock `subprocess.run`).
- Manual verification: Test-connection for each provider; a short real `.srt`
  translated end-to-end on at least one API provider and on the CLI provider.

## 10. Out of scope (YAGNI)

Streaming responses; heavy provider SDKs; auto-discovery of local models;
mixing providers within one run; changes to translation-memory rules; theme
changes beyond the menu/dialog/about; concurrency auto-tuning.
