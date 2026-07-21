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
