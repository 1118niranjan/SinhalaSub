# Pluggable LLM Providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let anyone connect their own LLM backbone (Claude Code CLI, Anthropic API, Google Gemini, or any OpenAI-compatible / local endpoint) from inside SinhalaSub, and translate a full movie in ~1–3 minutes instead of ~15.

**Architecture:** Introduce a provider layer (`providers.py`) behind the single engine call. `translate_batch()` calls `provider.translate(prompt, stdin_text, timeout)` instead of spawning the Claude CLI. The CLI stays the default. A Providers menu + Settings dialog configure the active provider and key; `.env` is also supported. All translation logic (prompt, batching, memory, checkpoints) is unchanged.

**Tech Stack:** Python 3.11+, Tkinter (stdlib), `pysrt`, `requests` (all API providers use raw HTTPS via `requests` — no heavy SDKs), `pytest` for tests.

## Global Constraints

- **Default provider is `cli` (Claude Code CLI)** — existing behaviour must not change for current users; no API key required.
- **No new runtime dependencies** beyond `pysrt` + `requests` (already in `requirements.txt`). No `anthropic` / `openai` / `google-generativeai` SDKs.
- **API keys are never hardcoded or committed.** GUI-entered keys go in `secrets.json` (gitignored); `.env`/env vars are the fallback. Key precedence: `secrets.json` value wins, else environment.
- **Providers return the model's raw text** in the existing `NUMBER|||translation` line format so `parse_response()` is unchanged.
- **Default models:** cli=`CLI default`, anthropic=`claude-haiku-4-5`, gemini=`gemini-2.5-flash`, openai=`gpt-4o-mini`. All overridable.
- **Attribution string, verbatim:** `Created by NLK`.
- **Windows/PowerShell** dev environment; subprocess calls keep `CREATE_NO_WINDOW`.

---

### Task 1: Project scaffolding + provider registry, secrets, key resolution

**Files:**
- Create: `providers.py`
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Produces: `PROVIDERS` (list of dicts), `provider_by_key(key) -> dict`, `SECRETS_PATH`, `load_secrets(path=None) -> dict`, `save_secrets(data, path=None) -> None`, `resolve_api_key(key, secrets=None) -> str`, base class `Provider` with `test() -> tuple[bool, str]`.

- [ ] **Step 1: Initialise git so per-task commits work**

Run:
```bash
cd "c:/Users/Termi/Desktop/Sinahala Sub/SinhalaSub"
git init
git add .gitignore README.md requirements.txt sinhalasub.py SinhalaSub.pyw .env.example docs
git commit -m "chore: initial commit before provider work"
```
Expected: a repo is created and the first commit succeeds. (`.env`, `translations.db`, `settings.json`, `__pycache__` are already gitignored and stay out.)

- [ ] **Step 2: Install pytest**

Run: `pip install pytest`
Expected: pytest installs (or "already satisfied").

- [ ] **Step 3: Write the failing test**

Create `tests/__init__.py` (empty) and `tests/conftest.py`:
```python
class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text or ""

    def json(self):
        return self._json


def make_response(status_code=200, json_data=None, text=""):
    """Build a stand-in for requests.Response for provider tests."""
    return FakeResponse(status_code, json_data, text)
```

Create `tests/test_registry.py`:
```python
import providers


def test_four_providers_with_expected_keys():
    keys = [p["key"] for p in providers.PROVIDERS]
    assert keys == ["cli", "anthropic", "gemini", "openai"]


def test_default_models():
    assert providers.provider_by_key("anthropic")["default_model"] == "claude-haiku-4-5"
    assert providers.provider_by_key("gemini")["default_model"] == "gemini-2.5-flash"
    assert providers.provider_by_key("openai")["default_model"] == "gpt-4o-mini"
    assert providers.provider_by_key("cli")["needs_key"] is False


def test_provider_by_key_falls_back_to_cli():
    assert providers.provider_by_key("nonexistent")["key"] == "cli"


def test_secrets_round_trip(tmp_path):
    path = tmp_path / "secrets.json"
    providers.save_secrets({"anthropic": "sk-abc"}, path=str(path))
    assert providers.load_secrets(path=str(path)) == {"anthropic": "sk-abc"}


def test_resolve_api_key_prefers_secrets_over_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert providers.resolve_api_key("anthropic", secrets={"anthropic": "from-gui"}) == "from-gui"


def test_resolve_api_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert providers.resolve_api_key("anthropic", secrets={}) == "from-env"
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python -m pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'providers'`.

- [ ] **Step 5: Write minimal implementation**

Create `providers.py`:
```python
"""Pluggable translation backends for SinhalaSub.

Each provider exposes translate(prompt, stdin_text, timeout) and returns the
model's raw text in the existing NUMBER|||translation line format, so the rest
of the app (parse_response, batching, memory, checkpoints) is unchanged.
"""

import json
import os
import subprocess
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS_PATH = os.path.join(_HERE, "secrets.json")

# On Windows the claude CLI is a .cmd shim; hide the console window it spawns.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PROVIDERS = [
    {"key": "cli", "label": "Claude Code CLI", "needs_key": False,
     "default_model": "CLI default", "env_key": None, "env_base": None,
     "default_base_url": None, "default_workers": 3},
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_registry.py -v`
Expected: PASS (6 passed).

- [ ] **Step 7: Commit**

```bash
git add providers.py tests/__init__.py tests/conftest.py tests/test_registry.py
git commit -m "feat: provider registry, secrets store, key resolution"
```

---

### Task 2: CLI provider (wrap the existing subprocess engine)

**Files:**
- Modify: `providers.py`
- Test: `tests/test_cli_provider.py`

**Interfaces:**
- Consumes: `Provider`, `_NO_WINDOW`.
- Produces: `CliProvider(model="CLI default", cli_path=None)` with `available()` and `translate(prompt, stdin_text, timeout) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_provider.py`:
```python
import subprocess
import types

import providers


def _fake_completed(returncode=0, stdout="1|||හලෝ\n", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_available_true_when_cli_path_set():
    assert providers.CliProvider(cli_path="C:/x/claude.cmd").available() is True


def test_available_false_when_no_cli(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda name: None)
    assert providers.CliProvider(cli_path=None).available() is False


def test_translate_builds_args_and_returns_stdout(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["input"] = kwargs.get("input")
        return _fake_completed(stdout="1|||OK\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    prov = providers.CliProvider(model="sonnet", cli_path="claude.cmd")
    out = prov.translate("PROMPT", "1|||Hi\n", timeout=60)
    assert out == "1|||OK\n"
    assert seen["args"][:3] == ["claude.cmd", "-p", "PROMPT"]
    assert "--model" in seen["args"] and "sonnet" in seen["args"]
    assert seen["input"] == "1|||Hi\n"


def test_translate_cli_default_omits_model_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **kw: seen.setdefault("args", args) or _fake_completed())
    providers.CliProvider(model="CLI default", cli_path="claude.cmd").translate("P", "x", 60)
    assert "--model" not in seen["args"]


def test_translate_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **kw: _fake_completed(returncode=1, stderr="boom"))
    prov = providers.CliProvider(cli_path="claude.cmd")
    try:
        prov.translate("P", "x", 60)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "boom" in str(exc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_provider.py -v`
Expected: FAIL — `AttributeError: module 'providers' has no attribute 'CliProvider'` (and no `shutil`).

- [ ] **Step 3: Write minimal implementation**

In `providers.py`, add `import shutil` at the top (next to the other imports) and add the class after `Provider`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_provider.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add providers.py tests/test_cli_provider.py
git commit -m "feat: CLI provider wrapping the existing subprocess engine"
```

---

### Task 3: Anthropic API provider

**Files:**
- Modify: `providers.py`
- Test: `tests/test_anthropic_provider.py`

**Interfaces:**
- Consumes: `Provider`.
- Produces: `AnthropicProvider(model, api_key, max_tokens=8000)` with `available()`, `translate(...)`, inherited `test()`. Endpoint `https://api.anthropic.com/v1/messages`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_anthropic_provider.py`:
```python
import requests

import providers
from tests.conftest import make_response


def test_available_requires_key():
    assert providers.AnthropicProvider(model="claude-haiku-4-5", api_key="k").available() is True
    assert providers.AnthropicProvider(model="claude-haiku-4-5", api_key="").available() is False


def test_translate_posts_and_extracts_text(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        seen["headers"] = headers
        return make_response(200, {"content": [{"type": "text", "text": "1|||හ"}]})

    monkeypatch.setattr(requests, "post", fake_post)
    prov = providers.AnthropicProvider(model="claude-haiku-4-5", api_key="sk-1")
    out = prov.translate("PROMPT", "1|||Hi\n", timeout=60)

    assert out == "1|||හ"
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-1"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["json"]["model"] == "claude-haiku-4-5"
    assert seen["json"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert seen["json"]["system"][0]["text"] == "PROMPT"
    assert seen["json"]["messages"] == [{"role": "user", "content": "1|||Hi\n"}]


def test_translate_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: make_response(401, text="bad key"))
    prov = providers.AnthropicProvider(model="claude-haiku-4-5", api_key="sk-1")
    try:
        prov.translate("P", "x", 60)
        assert False
    except RuntimeError as exc:
        assert "401" in str(exc) and "bad key" in str(exc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_anthropic_provider.py -v`
Expected: FAIL — `AttributeError: module 'providers' has no attribute 'AnthropicProvider'`.

- [ ] **Step 3: Write minimal implementation**

In `providers.py`, add after `CliProvider`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_anthropic_provider.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add providers.py tests/test_anthropic_provider.py
git commit -m "feat: Anthropic API provider with prompt caching"
```

---

### Task 4: Google Gemini provider

**Files:**
- Modify: `providers.py`
- Test: `tests/test_gemini_provider.py`

**Interfaces:**
- Consumes: `Provider`.
- Produces: `GeminiProvider(model, api_key, max_tokens=8000)` with `available()`, `translate(...)`. Endpoint `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=…`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_gemini_provider.py`:
```python
import requests

import providers
from tests.conftest import make_response


def test_available_requires_key():
    assert providers.GeminiProvider(model="gemini-2.5-flash", api_key="k").available() is True
    assert providers.GeminiProvider(model="gemini-2.5-flash", api_key="").available() is False


def test_translate_posts_and_extracts_text(monkeypatch):
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        return make_response(200, {
            "candidates": [{"content": {"parts": [{"text": "1|||හ"}]}}]
        })

    monkeypatch.setattr(requests, "post", fake_post)
    prov = providers.GeminiProvider(model="gemini-2.5-flash", api_key="g-1")
    out = prov.translate("PROMPT", "1|||Hi\n", timeout=60)

    assert out == "1|||හ"
    assert "models/gemini-2.5-flash:generateContent" in seen["url"]
    assert "key=g-1" in seen["url"]
    assert seen["json"]["system_instruction"]["parts"][0]["text"] == "PROMPT"
    assert seen["json"]["contents"][0]["parts"][0]["text"] == "1|||Hi\n"


def test_translate_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: make_response(400, text="bad request"))
    try:
        providers.GeminiProvider(model="gemini-2.5-flash", api_key="g").translate("P", "x", 60)
        assert False
    except RuntimeError as exc:
        assert "400" in str(exc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gemini_provider.py -v`
Expected: FAIL — no `GeminiProvider`.

- [ ] **Step 3: Write minimal implementation**

In `providers.py`, add after `AnthropicProvider`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gemini_provider.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add providers.py tests/test_gemini_provider.py
git commit -m "feat: Google Gemini provider"
```

---

### Task 5: OpenAI-compatible provider (OpenAI, OpenRouter, local LLMs)

**Files:**
- Modify: `providers.py`
- Test: `tests/test_openai_provider.py`

**Interfaces:**
- Consumes: `Provider`.
- Produces: `OpenAIProvider(model, api_key="", base_url="https://api.openai.com/v1", max_tokens=8000)` with `available()`, `translate(...)`. POSTs `{base_url}/chat/completions`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_openai_provider.py`:
```python
import requests

import providers
from tests.conftest import make_response


def test_available_true_with_base_url_even_without_key():
    # local endpoints (Ollama/LM Studio) often need no key
    prov = providers.OpenAIProvider(model="llama3.1", api_key="",
                                    base_url="http://localhost:11434/v1")
    assert prov.available() is True


def test_translate_posts_chat_completions(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        seen["headers"] = headers
        return make_response(200, {"choices": [{"message": {"content": "1|||හ"}}]})

    monkeypatch.setattr(requests, "post", fake_post)
    prov = providers.OpenAIProvider(model="gpt-4o-mini", api_key="sk-o",
                                    base_url="https://api.openai.com/v1/")
    out = prov.translate("PROMPT", "1|||Hi\n", timeout=60)

    assert out == "1|||හ"
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"  # trailing slash trimmed
    assert seen["headers"]["Authorization"] == "Bearer sk-o"
    assert seen["json"]["model"] == "gpt-4o-mini"
    assert seen["json"]["messages"][0] == {"role": "system", "content": "PROMPT"}
    assert seen["json"]["messages"][1] == {"role": "user", "content": "1|||Hi\n"}


def test_translate_omits_auth_header_without_key(monkeypatch):
    seen = {}
    monkeypatch.setattr(requests, "post",
                        lambda url, json=None, headers=None, timeout=None:
                        seen.setdefault("headers", headers)
                        or make_response(200, {"choices": [{"message": {"content": "x"}}]}))
    providers.OpenAIProvider(model="llama3.1", api_key="",
                             base_url="http://localhost:11434/v1").translate("P", "x", 60)
    assert "Authorization" not in seen["headers"]


def test_translate_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: make_response(500, text="server error"))
    try:
        providers.OpenAIProvider(model="gpt-4o-mini", api_key="k").translate("P", "x", 60)
        assert False
    except RuntimeError as exc:
        assert "500" in str(exc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_openai_provider.py -v`
Expected: FAIL — no `OpenAIProvider`.

- [ ] **Step 3: Write minimal implementation**

In `providers.py`, add after `GeminiProvider`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_openai_provider.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add providers.py tests/test_openai_provider.py
git commit -m "feat: OpenAI-compatible provider (OpenAI, OpenRouter, local LLMs)"
```

---

### Task 6: Factory + active-provider builder + default workers

**Files:**
- Modify: `providers.py`
- Test: `tests/test_factory.py`

**Interfaces:**
- Consumes: all provider classes, `provider_by_key`, `resolve_api_key`.
- Produces: `make_provider(key, *, model=None, api_key="", base_url=None, cli_path=None, max_tokens=8000) -> Provider`; `build_active_provider(settings, secrets=None, cli_path=None) -> Provider`; `default_workers(key) -> int`.
  - `settings` schema read here: `settings["provider"]` (key), `settings["model"]` (CLI model), `settings["providers"][key]` = `{"model": str, "base_url": str}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_factory.py`:
```python
import providers


def test_make_provider_types():
    assert isinstance(providers.make_provider("cli", cli_path="c"), providers.CliProvider)
    assert isinstance(providers.make_provider("anthropic", api_key="k"), providers.AnthropicProvider)
    assert isinstance(providers.make_provider("gemini", api_key="k"), providers.GeminiProvider)
    assert isinstance(providers.make_provider("openai", api_key="k"), providers.OpenAIProvider)


def test_make_provider_uses_default_model_when_none():
    prov = providers.make_provider("anthropic", api_key="k")
    assert prov.model == "claude-haiku-4-5"


def test_default_workers():
    assert providers.default_workers("cli") == 3
    assert providers.default_workers("openai") == 10


def test_build_active_provider_cli(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda name: "claude.cmd")
    prov = providers.build_active_provider({"provider": "cli", "model": "sonnet"})
    assert isinstance(prov, providers.CliProvider)
    assert prov.model == "sonnet"


def test_build_active_provider_openai_reads_settings_and_key():
    settings = {
        "provider": "openai",
        "providers": {"openai": {"model": "llama3.1",
                                 "base_url": "http://localhost:11434/v1"}},
    }
    prov = providers.build_active_provider(settings, secrets={"openai": "sk-x"})
    assert isinstance(prov, providers.OpenAIProvider)
    assert prov.model == "llama3.1"
    assert prov.base_url == "http://localhost:11434/v1"
    assert prov.api_key == "sk-x"


def test_build_active_provider_defaults_to_cli_for_unknown(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda name: "claude.cmd")
    prov = providers.build_active_provider({})
    assert isinstance(prov, providers.CliProvider)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_factory.py -v`
Expected: FAIL — no `make_provider`.

- [ ] **Step 3: Write minimal implementation**

In `providers.py`, add at the end of the file:
```python
def make_provider(key, *, model=None, api_key="", base_url=None, cli_path=None,
                  max_tokens=8000):
    desc = provider_by_key(key)
    key = desc["key"]
    model = model or desc["default_model"]
    if key == "cli":
        return CliProvider(model=model, cli_path=cli_path)
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
    if key == "cli":
        return make_provider("cli", model=(settings or {}).get("model") or "CLI default",
                             cli_path=cli_path)
    pconf = ((settings or {}).get("providers") or {}).get(key, {})
    model = pconf.get("model") or desc["default_model"]
    base_url = pconf.get("base_url") or desc.get("default_base_url")
    api_key = resolve_api_key(key, secrets)
    return make_provider(key, model=model, api_key=api_key, base_url=base_url)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_factory.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add providers.py tests/test_factory.py
git commit -m "feat: provider factory, active-provider builder, default workers"
```

---

### Task 7: Route translation through the provider (replace claude_path/model)

**Files:**
- Modify: `sinhalasub.py` (remove `run_claude`; change `translate_batch` and `translate_all` signatures; the `_extra_args`, `_NO_WINDOW`, and `find_claude` usages)
- Test: `tests/test_translate_integration.py`

**Interfaces:**
- Consumes: `providers.Provider`.
- Produces: `translate_batch(subs, batch, provider, log=None, cancel=None, skip=None)`; `translate_all(subs, provider, progress=None, log=None, workers=None, cancel=None, initial=None, on_batch=None)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_translate_integration.py`:
```python
import pysrt

import sinhalasub


class FakeProvider:
    """Echoes each TRANSLATE line back as 'N|||SI-<n>' so alignment is testable."""

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def translate(self, prompt, stdin_text, timeout):
        self.calls += 1
        out = []
        for line in stdin_text.splitlines():
            if "|||" not in line:
                continue
            num, _, _ = line.partition("|||")
            num = num.strip()
            if num.isdigit():
                out.append("%s|||SI-%s" % (num, num))
        # build_batch_input includes CONTEXT lines too; the parser keeps only
        # the numbers this batch expects, so echoing everything is fine.
        return "\n".join(out) + "\n"


def _subs(n):
    subs = pysrt.SubRipFile()
    for i in range(n):
        subs.append(pysrt.SubRipItem(
            index=i + 1,
            start=pysrt.SubRipTime(0, 0, i),
            end=pysrt.SubRipTime(0, 0, i + 1),
            text="line %d" % (i + 1)))
    return subs


def test_translate_all_uses_provider_and_aligns(monkeypatch):
    monkeypatch.setattr(sinhalasub, "BATCH_SIZE", 3)
    subs = _subs(7)
    prov = FakeProvider()
    texts = sinhalasub.translate_all(subs, prov, workers=2)
    assert len(texts) == 7
    assert texts[0] == "SI-1"
    assert texts[6] == "SI-7"
    assert prov.calls >= 3  # 7 cues / batch size 3 => 3 batches


def test_translate_batch_keeps_english_when_provider_returns_nothing():
    subs = _subs(2)

    class Empty:
        def translate(self, prompt, stdin_text, timeout):
            return ""

    result = sinhalasub.translate_batch(subs, [0, 1], Empty())
    assert result[0] == "line 1"  # kept English rather than corrupt alignment
    assert result[1] == "line 2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_translate_integration.py -v`
Expected: FAIL — `translate_all()` / `translate_batch()` still take `claude_path`/`model`.

- [ ] **Step 3: Edit `sinhalasub.py`**

3a. Delete the `run_claude` function (`sinhalasub.py:217-240`) and the module-level `_extra_args` / `_NO_WINDOW` block (`sinhalasub.py:207-214`) — this logic now lives in `providers.CliProvider`.

3b. Add `import providers` near the top with the other imports.

3c. Replace `translate_batch` (`sinhalasub.py:243-284`) with:
```python
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
```

3d. Replace the header/signature and body of `translate_all` (`sinhalasub.py:287-338`) so it takes `provider` instead of `claude_path`/`model`:
```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_translate_integration.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the whole suite (no regressions)**

Run: `python -m pytest -v`
Expected: PASS (all tasks 1–7 green).

- [ ] **Step 6: Commit**

```bash
git add sinhalasub.py tests/test_translate_integration.py
git commit -m "refactor: translate through provider layer instead of run_claude"
```

---

### Task 8: GUI — Providers menu, About (Created by NLK), active-engine header, wiring

**Files:**
- Modify: `sinhalasub.py` (`SinhalaSubApp.__init__`, `start_translate`, `_worker`, and the `find_claude`/availability gating)

**Interfaces:**
- Consumes: `providers.PROVIDERS`, `providers.build_active_provider`, `providers.provider_by_key`, `providers.default_workers`.
- Produces: `self.provider_key` (str), `self.provider` built at run start, menu + About dialog. (No new public function; verified manually.)

- [ ] **Step 1: Add the menu bar, About dialog, and engine header in `__init__`**

In `SinhalaSubApp.__init__`, right after `root.minsize(720, 340)`:
```python
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
```

Add these methods to `SinhalaSubApp` (anywhere among the other methods):
```python
    def _engine_label(self):
        desc = providers.provider_by_key(self.provider_key)
        if desc["key"] == "cli":
            return "Claude Code CLI"
        pconf = (self.settings.get("providers") or {}).get(desc["key"], {})
        model = pconf.get("model") or desc["default_model"]
        return "%s (%s)" % (desc["label"], model)

    def _refresh_engine_header(self):
        self.subtitle_lbl.configure(
            text="English → සිංහල subtitles · Engine: %s"
            % self._engine_label())
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
```

- [ ] **Step 2: Give the subtitle label a handle so the header can update**

Replace the existing subtitle label line (`sinhalasub.py:659-660`):
```python
        ttk.Label(main, text="English → සිංහල subtitles · runs on your local claude CLI",
                  style="Dim.TLabel").pack(anchor="w", pady=(0, 12))
```
with:
```python
        self.subtitle_lbl = ttk.Label(
            main,
            text="English → සිංහල subtitles · Engine: %s"
            % self._engine_label(),
            style="Dim.TLabel")
        self.subtitle_lbl.pack(anchor="w", pady=(0, 12))
```

- [ ] **Step 3: Replace the CLI-only startup gate**

Replace the block that disables the button when the CLI is missing (`sinhalasub.py:730-734`):
```python
        if not self.claude_path:
            self.translate_btn.state(["disabled"])
            self.status_var.set(
                "ERROR: the claude CLI was not found on PATH. Install Claude Code, "
                "sign in, then restart this app.")
```
with:
```python
        self._update_translate_gate()
```

- [ ] **Step 3b: Raise the parallel-batch spinbox cap to 20**

Change the workers spinbox (`sinhalasub.py:684-685`) from `to=6` to `to=20`:
```python
        ttk.Spinbox(opts, from_=1, to=20, textvariable=self.workers_var,
                    width=4, state="readonly").pack(side="left", padx=6)
```

- [ ] **Step 4: Build the active provider at run start**

In `start_translate`, replace the model-resolution block (`sinhalasub.py:874-880`):
```python
        model = self.model_var.get()
        self.last_model = model
        model = None if model == "CLI default" else model
        try:
            workers = max(1, min(6, int(self.workers_var.get())))
        except ValueError:
            workers = MAX_WORKERS
```
with:
```python
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
```

- [ ] **Step 5: Pass the provider to the worker**

In `start_translate`, change the worker thread args (`sinhalasub.py:897-899`) from:
```python
        threading.Thread(
            target=self._worker,
            args=(subs, path, initial, model, workers), daemon=True).start()
```
to:
```python
        threading.Thread(
            target=self._worker,
            args=(subs, path, initial, workers), daemon=True).start()
```

Change `_worker` (`sinhalasub.py:901`) signature and its `translate_all` call:
```python
    def _worker(self, subs, path, initial, workers):
```
and inside it replace the `translate_all(...)` call (`sinhalasub.py:912-917`) with:
```python
            texts = translate_all(
                subs, self.provider,
                progress=lambda d, t: self.msgs.put(("progress", d, t)),
                log=lambda m: self.msgs.put(("status", m)),
                workers=workers, cancel=self.cancel_event,
                initial=initial, on_batch=on_batch)
```

- [ ] **Step 6: Manual verification**

Run: `python sinhalasub.py`
Verify:
- A **Providers** menu and an **About** menu appear at the top.
- **About** shows the description ending with **"Created by NLK"**.
- The header reads `… · Engine: Claude Code CLI`.
- Switching **Providers → Anthropic API** (no key yet) changes the header and shows a status hint telling you to open Settings for a key; the Translate button disables.
- Switching back to **Claude Code CLI** re-enables Translate (if `claude` is on PATH).

- [ ] **Step 7: Run the suite (no regressions) and commit**

```bash
python -m pytest -q
git add sinhalasub.py
git commit -m "feat: Providers menu, About (Created by NLK), engine header + gating"
```
Expected: tests still pass.

---

### Task 9: GUI — provider Settings dialog with Test connection + persistence

**Files:**
- Modify: `sinhalasub.py` (`open_provider_settings` and a save handler)

**Interfaces:**
- Consumes: `providers.PROVIDERS`, `providers.provider_by_key`, `providers.make_provider`, `providers.load_secrets`, `providers.save_secrets`, `providers.resolve_api_key`.
- Produces: `open_provider_settings()` modal; writes `settings["provider"]`, `settings["providers"][key] = {"model", "base_url"}`, and `secrets.json`.

- [ ] **Step 1: Add the Settings dialog**

Add this method to `SinhalaSubApp`:
```python
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

        ttk.Label(frm, text="Provider", style="Dim.TLabel").grid(row=0, column=0, sticky="w")
        prov_var = tk.StringVar(value=desc0["label"])
        labels = [p["label"] for p in providers.PROVIDERS]
        ttk.Combobox(frm, textvariable=prov_var, values=labels, state="readonly",
                     width=24).grid(row=0, column=1, sticky="ew", pady=4)

        key_var = tk.StringVar(value=secrets.get(desc0["key"], ""))
        base_var = tk.StringVar(value=pconf.get("base_url") or desc0.get("default_base_url") or "")
        model_var = tk.StringVar(value=pconf.get("model") or desc0["default_model"])

        ttk.Label(frm, text="API key", style="Dim.TLabel").grid(row=1, column=0, sticky="w")
        key_entry = ttk.Entry(frm, textvariable=key_var, show="•", width=36)
        key_entry.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(frm, text="Base URL", style="Dim.TLabel").grid(row=2, column=0, sticky="w")
        base_entry = ttk.Entry(frm, textvariable=base_var, width=36)
        base_entry.grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(frm, text="Model", style="Dim.TLabel").grid(row=3, column=0, sticky="w")
        ttk.Entry(frm, textvariable=model_var, width=36).grid(row=3, column=1, sticky="ew", pady=4)

        status = ttk.Label(frm, text="", style="Dim.TLabel", wraplength=320)
        status.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        def current_desc():
            return next(p for p in providers.PROVIDERS if p["label"] == prov_var.get())

        def sync_fields(*_):
            d = current_desc()
            is_cli = d["key"] == "cli"
            state = "disabled" if is_cli else "normal"
            key_entry.configure(state=state)
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
                providers.save_secrets(sec)
            self._menu_provider.set(d["key"])
            self._refresh_engine_header()
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Test connection", command=do_test).pack(side="left")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left", padx=8)
        ttk.Button(btns, text="Save", style="Accent.TButton",
                   command=do_save).pack(side="left")
        frm.columnconfigure(1, weight=1)
```

- [ ] **Step 2: Manual verification**

Run: `python sinhalasub.py`
Verify:
- **Providers → Settings…** opens the dialog.
- Selecting **OpenAI / Local LLM** enables both the API key and Base URL fields; selecting **Anthropic API** enables the key but disables Base URL; selecting **Claude Code CLI** disables both.
- Enter a real key (or a local base URL like `http://localhost:11434/v1` with a running Ollama) and click **Test connection** — a ✓ with a latency or a ✗ with the error appears.
- Click **Save** → the header updates to the chosen engine + model, and the choice persists after restarting the app (`settings.json` gets `provider`/`providers`, `secrets.json` gets the key).
- Confirm `secrets.json` is NOT tracked by git (added in Task 10) and the key is never printed anywhere.

- [ ] **Step 3: Commit**

```bash
git add sinhalasub.py
git commit -m "feat: provider Settings dialog with Test connection and persistence"
```

---

### Task 10: Docs & gitignore (.env.example, README, secrets.json)

**Files:**
- Modify: `.gitignore`
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Ignore the secrets file**

Add a line to `.gitignore`:
```
secrets.json
```

- [ ] **Step 2: Rewrite `.env.example` with a clearly labelled block per provider**

Replace the whole contents of `.env.example` with:
```
# SinhalaSub configuration. Copy this file to ".env" and fill in what you need.
# NEVER commit the real .env - it is gitignored. You can also set everything in
# the app: Providers menu -> Settings...  (GUI values take precedence over .env.)

# ---------------------------------------------------------------------------
# Which backbone translates. One of: cli | anthropic | gemini | openai
# Leave as "cli" to use your local Claude Code CLI (no API key needed).
SINHALASUB_PROVIDER=cli

# ---- Anthropic API (provider = anthropic) ---------------------------------
# Put your Anthropic key here. Model is set in the app (default claude-haiku-4-5).
ANTHROPIC_API_KEY=

# ---- Google Gemini (provider = gemini) ------------------------------------
# Put your Gemini key here. Model default: gemini-2.5-flash
GEMINI_API_KEY=

# ---- OpenAI / OpenRouter / local LLM (provider = openai) ------------------
# Put your key here (leave empty for most local servers).
OPENAI_API_KEY=
# Base URL of the endpoint:
#   OpenAI     -> https://api.openai.com/v1
#   OpenRouter -> https://openrouter.ai/api/v1
#   Ollama     -> http://localhost:11434/v1
#   LM Studio  -> http://localhost:1234/v1
OPENAI_BASE_URL=https://api.openai.com/v1

# ---------------------------------------------------------------------------
# Optional: OpenSubtitles search panel
OPENSUBTITLES_API_KEY=

# Default subtitle colour written as <font color> tags, e.g. #FFD700. Empty = none.
SINHALASUB_COLOR=

# Parallel batches (speed). CLI: keep ~3. API providers: 10-20 is fine.
SINHALASUB_WORKERS=3

# Cues per request.
SINHALASUB_BATCH_SIZE=30

# Seconds allowed per request before it is retried.
SINHALASUB_TIMEOUT=600
```

- [ ] **Step 3: Update the README header + credit**

In `README.md`, replace the opening paragraph (`README.md:1-5`) with:
```markdown
# SinhalaSub

Translate an English movie subtitle (`.srt`) into natural, meaning-based spoken
Sinhala using the LLM backbone of your choice — the local `claude` CLI (default,
no API key), the Anthropic API, Google Gemini, or **any** OpenAI-compatible or
local model (OpenRouter, Ollama, LM Studio, LiteLLM).

*Created by NLK.*
```

- [ ] **Step 4: Add a "Connecting an LLM (where to put your API key)" section**

In `README.md`, immediately after the `## Run` section, insert:
```markdown
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
| Claude Code CLI | — (no key) | your `/model` setting | Default; runs on your Claude subscription |
| Anthropic API | `ANTHROPIC_API_KEY` | `claude-haiku-4-5` | System prompt is prompt-cached for speed |
| Google Gemini | `GEMINI_API_KEY` | `gemini-2.5-flash` | |
| OpenAI / Local | `OPENAI_API_KEY` (+ `OPENAI_BASE_URL`) | `gpt-4o-mini` | Ollama: `http://localhost:11434/v1`; LM Studio: `http://localhost:1234/v1`; set your local model name |

**Speed:** API providers are far faster than the CLI because they avoid
launching a process per batch. A 2-hour movie drops from ~15 min to ~1–3 min.
For the best mix of speed and quality use a fast model (Claude Haiku, Gemini
Flash, `gpt-4o-mini`); switch to a larger model (Sonnet/Opus, Gemini Pro) only
when a film needs extra polish.
```

- [ ] **Step 5: Manual verification**

- Confirm `git status` does NOT list `secrets.json` after you save a key in the app.
- Skim the README section renders as a table and reads correctly.

- [ ] **Step 6: Commit**

```bash
git add .gitignore .env.example README.md
git commit -m "docs: provider setup guide, .env template, ignore secrets.json"
```

---

### Task 11: Full-suite green + final manual smoke test

**Files:** none (verification only)

- [ ] **Step 1: Run the whole test suite**

Run: `python -m pytest -v`
Expected: PASS — all tests from Tasks 1–7.

- [ ] **Step 2: End-to-end smoke test on a real short `.srt`**

- Launch `python sinhalasub.py`.
- With **Claude Code CLI** selected, translate a small (~10-cue) English `.srt` → preview appears, Save writes `<name>.si.srt`. (Confirms the refactor didn't regress the default path.)
- Switch to one API provider you have a key for, add the key via **Providers → Settings…**, **Test connection** = ✓, then translate the same file → it finishes in seconds and the preview looks correct.

- [ ] **Step 3: Commit any final fixes**

```bash
git add -A
git commit -m "test: full-suite green and manual smoke test for provider switching"
```

---

## Notes for the implementer

- Line numbers reference the pre-change `sinhalasub.py`; if earlier tasks shifted them, match on the surrounding code shown in each step rather than the exact line.
- The `parse_response()` regex is unchanged — providers only need to return text containing `NUMBER|||translation` lines; extra commentary or context lines are ignored by the parser.
- Keep everything else (translation memory, checkpoints, OpenSubtitles, colour/preview) exactly as-is.
