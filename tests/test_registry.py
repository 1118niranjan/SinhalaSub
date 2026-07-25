import providers


def test_providers_with_expected_keys():
    keys = [p["key"] for p in providers.PROVIDERS]
    assert keys == ["google", "hybrid", "openai"]


def test_default_models():
    assert providers.provider_by_key("openai")["default_model"] == "openrouter/free"
    assert providers.provider_by_key("google")["needs_key"] is False
    assert providers.provider_by_key("hybrid")["needs_key"] is False


def test_provider_by_key_falls_back_to_default():
    assert providers.provider_by_key("nonexistent")["key"] == "google"


def test_secrets_round_trip(tmp_path):
    path = tmp_path / "secrets.json"
    providers.save_secrets({"openai": "sk-abc"}, path=str(path))
    assert providers.load_secrets(path=str(path)) == {"openai": "sk-abc"}


def test_resolve_api_key_prefers_secrets_over_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert providers.resolve_api_key("openai", secrets={"openai": "from-gui"}) == "from-gui"


def test_resolve_api_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert providers.resolve_api_key("openai", secrets={}) == "from-env"
