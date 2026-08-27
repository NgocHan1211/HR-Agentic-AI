import json
from unittest.mock import patch

from payroll.formula.formula_extractor import GemmaAPICompletionClient


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps({"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}).encode()


def test_gemma_client_uses_supported_default_model_and_api_key_header():
    client = GemmaAPICompletionClient(api_key="test-key")
    with patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
        assert client.complete(system="system", user="user") == "{}"

    request = urlopen.call_args.args[0]
    assert request.full_url.endswith("/models/gemma-4-31b-it:generateContent")
    assert request.get_header("X-goog-api-key") == "test-key"
    assert "test-key" not in request.full_url
