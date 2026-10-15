"""Fallback: when the main model fails, the backup answers, and nothing outside the allowed list is used.

Both gateways are configured the same way: endpoint "eu-chat" has a broken primary
("broken-model") and one allowed backup ("backup-model"). "outside-model" exists in the
gateway but is NOT an allowed fallback, so it must never be called.
"""

from conftest import mock_calls, reset_mock

MSG = [{"role": "user", "content": "Fallback please."}]


def test_fallback_used(client, note):
    """Falls back to the backup when the main model fails"""
    reset_mock()
    r = client.chat.completions.create(model="eu-chat", messages=MSG)
    served = [c["model"] for c in mock_calls()]
    note("provider calls, in order", " -> ".join(served))
    assert r.choices[0].message.content == "Reply from backup-model."
    assert served[0] == "broken-model" and served[-1] == "backup-model"


def test_fallback_stays_in_allowed_list(client, note):
    """Fallback never uses a model outside the allowed list"""
    reset_mock()
    for _ in range(10):
        client.chat.completions.create(model="eu-chat", messages=MSG)
    served = {c["model"] for c in mock_calls()}
    note("models called over 10 requests", ", ".join(sorted(served)))
    assert "outside-model" not in served
    assert served <= {"broken-model", "backup-model"}
