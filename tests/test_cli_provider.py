import subprocess
import types

from sinhalasub import providers


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

    def fake_run(args, **kw):
        seen["args"] = args
        return _fake_completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
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
