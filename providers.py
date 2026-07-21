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
