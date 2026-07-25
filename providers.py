"""Pluggable translation backends for SinhalaSub.

Each provider exposes translate(prompt, stdin_text, timeout) and returns the
model's raw text in the existing NUMBER|||translation line format, so the rest
of the app (parse_response, batching, memory, checkpoints) is unchanged.
"""

import json
import os
import shutil
import subprocess
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS_PATH = os.path.join(_HERE, "secrets.json")

# On Windows the claude CLI is a .cmd shim; hide the console window it spawns.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PROVIDERS = [
    # Free machine translation: no key, no usage, and it parallelises cleanly,
    # so it is by far the fastest option (whole movie in a couple of minutes).
    {"key": "google", "label": "Google Translate (free, fast)", "needs_key": False,
     "default_model": "google-translate", "env_key": None, "env_base": None,
     "default_base_url": None, "default_workers": 20},
    # Google does the bulk; the Claude CLI re-does only the hardest lines.
    {"key": "hybrid", "label": "Google + Claude polish", "needs_key": False,
     "default_model": "hybrid", "env_key": None, "env_base": None,
     "default_base_url": None, "default_workers": 10},
    # One entry covers OpenAI, OpenRouter and any local model (Ollama, LM Studio).
    {"key": "openai", "label": "OpenRouter / OpenAI / Local", "needs_key": True,
     "default_model": "openrouter/free", "env_key": "OPENAI_API_KEY",
     "env_base": "OPENAI_BASE_URL", "default_base_url": "https://openrouter.ai/api/v1",
     "default_workers": 10},
]

_BY_KEY = {p["key"]: p for p in PROVIDERS}


DEFAULT_PROVIDER = "google"


def provider_by_key(key):
    """Return a provider descriptor, defaulting to the fast free engine."""
    return _BY_KEY.get(key) or _BY_KEY[DEFAULT_PROVIDER]


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
        # API keys live here, so keep the file readable by this user only
        # instead of inheriting whatever the folder allows.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best effort; some filesystems do not support it
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


class HybridProvider(Provider):
    """Fast engine for everything, a better engine for the demanding lines.

    Short lines ("Yeah.", "Okay.") are where machine translation is already
    fine; long, clause-heavy lines are where it falls apart. So the fast engine
    does the bulk in seconds and only the long lines are re-done by the LLM,
    which keeps the run quick and the usage small while lifting the hard parts.
    """

    # Wording that means "your Claude allowance is gone" rather than a transient
    # glitch. Once we see it we stop polishing for the rest of the run instead of
    # paying the CLI's slow startup on every remaining batch just to fail again.
    _EXHAUSTED = ("usage limit", "rate limit", "quota", "429",
                  "limit reached", "too many requests", "overloaded")

    def __init__(self, fast, good, min_words=14, max_polish=8):
        self.fast = fast
        self.good = good
        self.min_words = min_words      # only genuinely long lines get polished
        self.max_polish = max_polish    # hard cap per batch, keeps runs quick
        self.model = "hybrid"
        self.polish_disabled = False    # set once the allowance is exhausted
        self.polished = 0
        # Source lines the better engine actually handled. The app stores these
        # at LLM quality so a later free run can reuse them instead of paying
        # for the same hard line twice. Batches run in threads, hence the lock.
        self.polished_sources = set()
        self._lock = threading.Lock()

    def available(self):
        # The fast engine alone is enough to produce a complete file.
        return bool(self.fast and self.fast.available())

    def _is_hard(self, text):
        """A line worth spending an LLM call on: long and clause-heavy."""
        words = text.split()
        if len(words) < self.min_words:
            return False
        # Multiple clauses (commas / conjunctions) are where MT actually breaks.
        return ("," in text or ";" in text
                or any(w.lower() in ("that", "which", "because", "although",
                                     "unless", "while", "before", "after",
                                     "whether", "since")
                       for w in words))

    def translate(self, prompt, stdin_text, timeout):
        targets = GoogleTranslateProvider._targets(stdin_text)
        if not targets:
            return ""
        merged = {}
        for line in self.fast.translate(prompt, stdin_text, timeout).splitlines():
            num, _, body = line.partition("|||")
            if num.strip().isdigit():
                merged[num.strip()] = body.strip()

        if self.polish_disabled:
            return self._render(targets, merged)

        hard = [(n, src) for n, src in targets if self._is_hard(src)]
        # Longest first, so the cap spends the LLM on the worst offenders.
        hard.sort(key=lambda p: -len(p[1].split()))
        hard = hard[:self.max_polish]
        if not hard:
            return self._render(targets, merged)

        sub_stdin = ("TRANSLATE (%d lines - output exactly these numbers):\n"
                     % len(hard))
        sub_stdin += "".join("%s|||%s\n" % (n, s) for n, s in hard)
        try:
            out = self.good.translate(prompt, sub_stdin, timeout)
            by_num = dict(hard)
            got = set()
            for line in out.splitlines():
                num, _, body = line.partition("|||")
                num, body = num.strip(), body.strip()
                if num.isdigit() and body:
                    merged[num] = body
                    got.add(num)
            with self._lock:
                self.polished += len(got)
                # Record the English sources the LLM genuinely translated.
                self.polished_sources.update(by_num[n] for n in got if n in by_num)
        except Exception as exc:  # noqa: BLE001 - the fast result still stands
            msg = str(exc).lower()
            if any(sig in msg for sig in self._EXHAUSTED):
                # Allowance gone: finish the whole movie on Google alone.
                self.polish_disabled = True
        return self._render(targets, merged)

    @staticmethod
    def _render(targets, merged):
        return "\n".join("%s|||%s" % (n, merged.get(n, src))
                         for n, src in targets) + "\n"


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
    if key == "google":
        return GoogleTranslateProvider(model=model)
    if key == "hybrid":
        return HybridProvider(GoogleTranslateProvider(),
                              CliProvider(cli_path=cli_path))
    return OpenAIProvider(model=model, api_key=api_key,
                          base_url=base_url or desc["default_base_url"],
                          max_tokens=max_tokens)


def default_workers(key):
    return provider_by_key(key)["default_workers"]


def build_active_provider(settings, secrets=None, cli_path=None):
    """Build the provider the user selected, resolving key/model/base_url."""
    settings = settings or {}
    desc = provider_by_key(settings.get("provider", DEFAULT_PROVIDER))
    key = desc["key"]
    gloss = dict(settings.get("glossary") or {})
    # Names learned from earlier movies keep a character's spelling consistent;
    # anything the user typed explicitly still wins.
    try:
        import memory_db
        import os as _os
        gloss = memory_db.MemoryDB(
            _os.path.join(_HERE, "translations.db")).glossary_with_names(gloss)
    except Exception:  # noqa: BLE001 - the glossary is an enhancement, not a need
        pass
    if key == "google":
        return GoogleTranslateProvider(glossary=gloss)
    if key == "hybrid":
        return HybridProvider(
            GoogleTranslateProvider(glossary=gloss),
            CliProvider(model=settings.get("model") or "CLI default",
                        cli_path=cli_path),
            min_words=int(settings.get("polish_min_words") or 14))
    pconf = (settings.get("providers") or {}).get(key, {})
    model = pconf.get("model") or desc["default_model"]
    base_url = pconf.get("base_url") or desc.get("default_base_url")
    api_key = resolve_api_key(key, secrets)
    return make_provider(key, model=model, api_key=api_key, base_url=base_url)
