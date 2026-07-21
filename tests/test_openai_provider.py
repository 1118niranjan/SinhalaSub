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

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["headers"] = headers
        return make_response(200, {"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr(requests, "post", fake_post)
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
