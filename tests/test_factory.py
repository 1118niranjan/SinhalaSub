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
    assert providers.default_workers("cli") == 10
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
