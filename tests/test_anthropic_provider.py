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
