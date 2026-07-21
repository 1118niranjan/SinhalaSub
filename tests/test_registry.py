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
