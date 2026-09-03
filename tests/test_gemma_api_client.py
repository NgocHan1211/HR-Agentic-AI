import json
from unittest.mock import patch

from payroll.formula.formula_extractor import GemmaAPICompletionClient, OpenRouterCompletionClient


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps({"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}).encode()


class _OpenRouterResponse(_Response):
    def read(self):
        return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()


def test_gemma_client_uses_supported_default_model_and_api_key_header():
    client = GemmaAPICompletionClient(api_key="test-key")
    with patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
        assert client.complete(system="system", user="user") == "{}"

    request = urlopen.call_args.args[0]
    assert request.full_url.endswith("/models/gemma-4-31b-it:generateContent")
    assert request.get_header("X-goog-api-key") == "test-key"
    assert "test-key" not in request.full_url
    request_body = json.loads(request.data.decode())
    assert request_body["systemInstruction"]["parts"][0]["text"] == "system"


def test_openrouter_client_uses_free_router_and_bearer_key():
    client = OpenRouterCompletionClient(api_key="test-key")
    with patch("urllib.request.urlopen", return_value=_OpenRouterResponse()) as urlopen:
        assert client.complete(system="system", user="user") == "{}"

    request = urlopen.call_args.args[0]
    assert request.full_url == "https://openrouter.ai/api/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer test-key"
    request_body = json.loads(request.data.decode())
    assert request_body["model"] == "openrouter/free"
    assert request_body["response_format"] == {"type": "json_object"}
    assert request_body["provider"] == {"require_parameters": True}
