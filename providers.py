"""Pluggable translation backends for SinhalaSub.

Each provider exposes translate(prompt, stdin_text, timeout) and returns the
model's raw text in the existing NUMBER|||translation line format, so the rest
of the app (parse_response, batching, memory, checkpoints) is unchanged.
"""

import json
import os
import shutil
import subprocess
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS_PATH = os.path.join(_HERE, "secrets.json")

# On Windows the claude CLI is a .cmd shim; hide the console window it spawns.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PROVIDERS = [
    # 10 workers: measured cost is ~1.9s per translated line of inference, so
    # wall-clock is dominated by how many lines are in flight at once, not by
    # process startup (~6s, ~11% of a batch). Parallelism costs no extra tokens.
    {"key": "cli", "label": "Claude Code CLI", "needs_key": False,
     "default_model": "CLI default", "env_key": None, "env_base": None,
     "default_base_url": None, "default_workers": 10},
    # Free machine translation: no key, no usage, and it parallelises cleanly,
    # so it is by far the fastest option (whole movie in a couple of minutes).
    {"key": "google", "label": "Google Translate (free, fast)", "needs_key": False,
     "default_model": "google-translate", "env_key": None, "env_base": None,
     "default_base_url": None, "default_workers": 20},
    {"key": "gemini-cli", "label": "Gemini CLI (Google login)", "needs_key": False,
     "default_model": "gemini-2.5-flash", "env_key": None, "env_base": None,
     "default_base_url": None, "default_workers": 4},
    {"key": "anthropic", "label": "Anthropic API", "needs_key": True,
     "default_model": "claude-haiku-4-5", "env_key": "ANTHROPIC_API_KEY",
     "env_base": None, "default_base_url": None, "default_workers": 10},
    {"key": "gemini", "label": "Google Gemini", "needs_key": True,
     "default_model": "gemini-2.5-flash", "env_key": "GEMINI_API_KEY",
     "env_base": None, "default_base_url": None, "default_workers": 10},
    {"key": "openai", "label": "OpenAI / Local LLM", "needs_key": True,
     "default_model": "gpt-4o-mini", "env_key": "OPENAI_API_KEY",
     "env_base": "OPENAI_BASE_URL", "default_base_url": "https://api.openai.com/v1",
     "default_workers": 10},
]

_BY_KEY = {p["key"]: p for p in PROVIDERS}


def provider_by_key(key):
    """Return a provider descriptor, defaulting to the CLI provider."""
    return _BY_KEY.get(key) or _BY_KEY["cli"]


def load_secrets(path=None):
    path = path or SECRETS_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_secrets(data, path=None):
    path = path or SECRETS_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def resolve_api_key(key, secrets=None):
    """GUI-entered key (secrets.json) wins; otherwise the provider's env var."""
    desc = provider_by_key(key)
    secrets = secrets if secrets is not None else load_secrets()
    val = (secrets.get(desc["key"]) or "").strip()
    if val:
        return val
    env = desc.get("env_key")
    return (os.environ.get(env, "").strip() if env else "")


class Provider:
    """Base class. Subclasses implement available() and translate()."""

    def available(self):
        return True

    def translate(self, prompt, stdin_text, timeout):
        raise NotImplementedError

    def test(self):
        """Send one trivial line and report (ok, message) for the GUI."""
        started = time.time()
        try:
            out = self.translate(
                "You are a translator. Return each numbered line unchanged in the "
                "form NUMBER|||text.",
                "1|||Hello\n",
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as text
            return (False, str(exc)[:300])
        if out and out.strip():
            return (True, "OK (%.1fs)" % (time.time() - started))
        return (False, "Empty response from provider")


class CliProvider(Provider):
    """Runs the local Claude Code CLI headlessly, once per batch (existing engine)."""

    def __init__(self, model="CLI default", cli_path=None):
        self.model = None if model in (None, "", "CLI default") else model
        self.cli_path = cli_path or shutil.which("claude")
        # Skip loading the user's MCP servers on every spawn (startup cost).
        # Dropped automatically if the installed CLI is too old to know the flag.
        self._extra_args = ["--strict-mcp-config"]

    def available(self):
        return bool(self.cli_path)

    def translate(self, prompt, stdin_text, timeout):
        if not self.cli_path:
            raise RuntimeError("claude CLI not found on PATH")
        args = [self.cli_path, "-p", prompt] + list(self._extra_args)
        if self.model:
            args += ["--model", self.model]
        result = subprocess.run(
            args, input=stdin_text, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        if result.returncode != 0:
            err = ((result.stderr or "") + " " + (result.stdout or "")).strip()
            if self._extra_args and ("unknown option" in err.lower()
                                     or "unrecognized" in err.lower()):
                self._extra_args = []
                return self.translate(prompt, stdin_text, timeout)
            raise RuntimeError(
                "claude CLI failed (exit %d): %s" % (result.returncode, err[:500]))
        return result.stdout


class GeminiCliProvider(Provider):
    """Runs Google's Gemini CLI headlessly, using its Google-account login.

    No API key: the user runs `gemini` once and picks "Login with Google" (free
    tier). We spawn `gemini -p <instruction>` with the batch content on stdin,
    exactly like the Claude CLI. Being a per-batch process spawn, it is slower
    than the Gemini API but costs nothing and doesn't touch any Claude usage.
    """

    def __init__(self, model="gemini-2.5-flash", cli_path=None):
        self.model = (model or "").strip()  # blank = the CLI's own default model
        self.cli_path = cli_path or shutil.which("gemini")

    def available(self):
        return bool(self.cli_path)

    def translate(self, prompt, stdin_text, timeout):
        if not self.cli_path:
            raise RuntimeError(
                "gemini CLI not found on PATH. Install it with "
                "'npm install -g @google/gemini-cli', then run 'gemini' once and "
                "choose Login with Google.")
        args = [self.cli_path, "-p", prompt]
        if self.model:
            args += ["-m", self.model]
        result = subprocess.run(
            args, input=stdin_text, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        if result.returncode != 0:
            err = ((result.stderr or "") + " " + (result.stdout or "")).strip()
            raise RuntimeError(
                "gemini CLI failed (exit %d): %s" % (result.returncode, err[:500]))
        return result.stdout


def _google_translator_cls():
    """Imported lazily so the app runs without deep-translator installed."""
    from deep_translator import GoogleTranslator
    return GoogleTranslator


class GoogleTranslateProvider(Provider):
    """Free machine translation via Google Translate - no API key, no usage.

    This is not an LLM: it translates each line directly, so the tuned prompt
    does not apply and the Sinhala is more literal than Claude's. In exchange it
    is free, unlimited, and fast. Only the TRANSLATE section is sent - context
    lines exist for LLM scene understanding and would just waste requests here.
    """

    def __init__(self, model=None, source="en", target="si", glossary=None):
        self.model = model or "google-translate"
        self.source = source
        self.target = target
        self.glossary = glossary or {}

    def available(self):
        try:
            _google_translator_cls()
            return True
        except Exception:  # noqa: BLE001 - library missing or broken import
            return False

    @staticmethod
    def _targets(stdin_text):
        """Pull [(number, source_text)] from the TRANSLATE section only."""
        targets = []
        in_target = False
        for line in stdin_text.splitlines():
            if line.startswith("TRANSLATE"):
                in_target = True
                continue
            if not in_target or "|||" not in line:
                continue
            num, _, src = line.partition("|||")
            num = num.strip()
            if num.isdigit():
                targets.append((num, src.strip()))
        return targets

    def translate(self, prompt, stdin_text, timeout):
        import subtitle_text as st

        targets = self._targets(stdin_text)
        if not targets:
            return ""

        # 1. Peel off markup (dashes, italics, music notes) so it is never
        #    translated as words, and calm down SHOUTED LINES.
        cores, rebuilds = [], []
        for _, raw in targets:
            core, rebuild = st.unwrap(raw)
            core = st.normalise_caps(core)
            core = st.apply_glossary(core, self.glossary)
            cores.append(core)
            rebuilds.append(rebuild)

        # 2. Join sentences that the subtitle file split across adjacent cues -
        #    translating half a sentence on its own is what produces nonsense.
        groups = []
        for grp in st.group_sentences(cores):
            run = [grp[0]]
            for idx in grp[1:]:
                adjacent = int(targets[idx][0]) == int(targets[run[-1]][0]) + 1
                if adjacent:
                    run.append(idx)
                else:
                    groups.append(run)
                    run = [idx]
            groups.append(run)

        joined = [" ".join(cores[i] for i in g).strip() for g in groups]
        translator = _google_translator_cls()(source=self.source, target=self.target)
        try:
            results = translator.translate_batch(joined)
        except Exception as exc:  # noqa: BLE001 - surfaced to the retry loop
            raise RuntimeError("Google Translate failed: %s" % str(exc)[:300])
        results = list(results or [])

        # 3. Share each translation back across the cues it came from, then put
        #    the original markup back on.
        out = {}
        for g, got in zip(groups, results + [None] * (len(groups) - len(results))):
            counts = [max(1, len(cores[i].split())) for i in g]
            parts = st.split_translation(got or "", counts)
            for i, part in zip(g, parts):
                text = (part or "").strip() or cores[i]  # never leave a cue blank
                out[i] = rebuilds[i](text)

        lines = ["%s|||%s" % (targets[i][0], out.get(i, cores[i]))
                 for i in range(len(targets))]
        return "\n".join(lines) + "\n"


class AnthropicProvider(Provider):
    """Direct Claude API. The system prompt is prompt-cached across batches."""

    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, model, api_key, max_tokens=8000):
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens

    def available(self):
        return bool(self.api_key)

    def translate(self, prompt, stdin_text, timeout):
        import requests  # lazy: CLI-only use never needs it
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": prompt,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": stdin_text}],
        }
        resp = requests.post(self.URL, json=body, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError("Anthropic API error %d: %s"
                               % (resp.status_code, (resp.text or "")[:300]))
        data = resp.json()
        return "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")


class GeminiProvider(Provider):
    """Google Gemini native generateContent API."""

    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, model, api_key, max_tokens=8000):
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens

    def available(self):
        return bool(self.api_key)

    def translate(self, prompt, stdin_text, timeout):
        import requests
        url = "%s/%s:generateContent?key=%s" % (self.BASE, self.model, self.api_key)
        body = {
            "system_instruction": {"parts": [{"text": prompt}]},
            "contents": [{"role": "user", "parts": [{"text": stdin_text}]}],
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }
        resp = requests.post(url, json=body, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError("Gemini API error %d: %s"
                               % (resp.status_code, (resp.text or "")[:300]))
        data = resp.json()
        cands = data.get("candidates", [])
        if not cands:
            raise RuntimeError("Gemini returned no candidates: %s"
                               % (resp.text or "")[:200])
        parts = cands[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)


class OpenAIProvider(Provider):
    """Any OpenAI-compatible chat endpoint: OpenAI, OpenRouter, Ollama, LM Studio."""

    def __init__(self, model, api_key="", base_url="https://api.openai.com/v1",
                 max_tokens=8000):
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.max_tokens = max_tokens

    def available(self):
        return bool(self.base_url)

    def translate(self, prompt, stdin_text, timeout):
        import requests
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": prompt},
                         {"role": "user", "content": stdin_text}],
            "max_tokens": self.max_tokens,
            "temperature": 0.3,
        }
        resp = requests.post(self.base_url + "/chat/completions",
                             json=body, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError("OpenAI-compatible API error %d: %s"
                               % (resp.status_code, (resp.text or "")[:300]))
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("No choices in response: %s" % (resp.text or "")[:200])
        return choices[0].get("message", {}).get("content", "") or ""


def make_provider(key, *, model=None, api_key="", base_url=None, cli_path=None,
                  max_tokens=8000):
    desc = provider_by_key(key)
    key = desc["key"]
    model = model or desc["default_model"]
    if key == "cli":
        return CliProvider(model=model, cli_path=cli_path)
    if key == "google":
        return GoogleTranslateProvider(model=model)
    if key == "gemini-cli":
        # Self-resolves the 'gemini' binary; ignore any Claude cli_path passed in.
        return GeminiCliProvider(model=model)
    if key == "anthropic":
        return AnthropicProvider(model=model, api_key=api_key, max_tokens=max_tokens)
    if key == "gemini":
        return GeminiProvider(model=model, api_key=api_key, max_tokens=max_tokens)
    return OpenAIProvider(model=model, api_key=api_key,
                          base_url=base_url or desc["default_base_url"],
                          max_tokens=max_tokens)


def default_workers(key):
    return provider_by_key(key)["default_workers"]


def build_active_provider(settings, secrets=None, cli_path=None):
    """Build the provider the user selected, resolving key/model/base_url."""
    desc = provider_by_key((settings or {}).get("provider", "cli"))
    key = desc["key"]
    if key == "google":
        return GoogleTranslateProvider(
            glossary=(settings or {}).get("glossary") or {})
    if key == "cli":
        return make_provider("cli", model=(settings or {}).get("model") or "CLI default",
                             cli_path=cli_path)
    pconf = ((settings or {}).get("providers") or {}).get(key, {})
    model = pconf.get("model") or desc["default_model"]
    base_url = pconf.get("base_url") or desc.get("default_base_url")
    api_key = resolve_api_key(key, secrets)
    return make_provider(key, model=model, api_key=api_key, base_url=base_url)
