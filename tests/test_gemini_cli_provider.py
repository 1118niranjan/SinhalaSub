import subprocess
import types

import providers


def _fake_completed(returncode=0, stdout="1|||හලෝ\n", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_available_true_when_cli_path_set():
    assert providers.GeminiCliProvider(cli_path="C:/x/gemini.cmd").available() is True


def test_available_false_when_no_cli(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda name: None)
    assert providers.GeminiCliProvider(cli_path=None).available() is False


def test_resolves_gemini_binary(monkeypatch):
    seen = {}

    def fake_which(name):
        seen["which"] = name
        return "gemini.cmd"

    monkeypatch.setattr(providers.shutil, "which", fake_which)
    prov = providers.GeminiCliProvider(cli_path=None)
    assert seen["which"] == "gemini"
    assert prov.cli_path == "gemini.cmd"


def test_translate_builds_args_pipes_stdin_and_returns_stdout(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["input"] = kwargs.get("input")
        return _fake_completed(stdout="1|||OK\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    prov = providers.GeminiCliProvider(model="gemini-2.5-flash", cli_path="gemini.cmd")
    out = prov.translate("PROMPT", "1|||Hi\n", timeout=60)
    assert out == "1|||OK\n"
    assert seen["args"][:3] == ["gemini.cmd", "-p", "PROMPT"]
    assert "-m" in seen["args"] and "gemini-2.5-flash" in seen["args"]
    assert seen["input"] == "1|||Hi\n"  # content goes via stdin


def test_translate_omits_model_flag_when_blank(monkeypatch):
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        return _fake_completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    providers.GeminiCliProvider(model="", cli_path="gemini.cmd").translate("P", "x", 60)
    assert "-m" not in seen["args"]


def test_translate_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **kw: _fake_completed(returncode=1, stderr="not logged in"))
    prov = providers.GeminiCliProvider(cli_path="gemini.cmd")
    try:
        prov.translate("P", "x", 60)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "not logged in" in str(exc)
