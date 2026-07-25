import providers


def test_make_provider_types():
    assert isinstance(providers.make_provider("google"), providers.GoogleTranslateProvider)
    assert isinstance(providers.make_provider("hybrid"), providers.HybridProvider)
    assert isinstance(providers.make_provider("openai", api_key="k"), providers.OpenAIProvider)


def test_make_provider_uses_default_model_when_none():
    prov = providers.make_provider("openai", api_key="k")
    assert prov.model == "openrouter/free"


def test_default_workers():
    assert providers.default_workers("google") == 20
    assert providers.default_workers("openai") == 10


def test_build_active_provider_hybrid(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda name: "claude.cmd")
    prov = providers.build_active_provider({"provider": "hybrid"})
    assert isinstance(prov, providers.HybridProvider)


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


def test_build_active_provider_defaults_to_google(monkeypatch):
    prov = providers.build_active_provider({})
    assert isinstance(prov, providers.GoogleTranslateProvider)
