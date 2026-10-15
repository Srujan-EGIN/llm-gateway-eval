"""Basic features: does the gateway pass these through correctly?"""

import httpx
from openai import OpenAI

MSG = [{"role": "user", "content": "Say hello."}]


def test_chat(client, note):
    """Chat completion works"""
    r = client.chat.completions.create(model="fast-model", messages=MSG)
    assert r.choices[0].message.content == "Reply from fast-model."
    assert r.usage and r.usage.total_tokens > 0
    note("tokens reported", r.usage.total_tokens)


def test_streaming(client, note):
    """Streaming works"""
    chunks = list(client.chat.completions.create(model="fast-model", messages=MSG, stream=True))
    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert len(chunks) > 1, "response was not streamed"
    assert text == "Reply from fast-model."
    note("chunks", len(chunks))


def test_tool_calling(client, note):
    """Tool calling works"""
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "Weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                       "required": ["city"]}}}]
    r = client.chat.completions.create(model="fast-model", messages=MSG, tools=tools)
    calls = r.choices[0].message.tool_calls
    assert calls and calls[0].function.name == "get_weather"
    assert '"Copenhagen"' in calls[0].function.arguments
    note("tool called", calls[0].function.name)


def test_embeddings(gw, note):
    """Embeddings work"""
    c = OpenAI(base_url=gw["embeddings_url"], api_key=gw["api_key"], max_retries=0)
    r = c.embeddings.create(model="embed-model", input=["one", "two"])
    assert len(r.data) == 2 and len(r.data[0].embedding) == 3
    note("vectors", len(r.data))


def test_unknown_model_rejected(gw, note):
    """Unknown model names are refused"""
    r = httpx.post(gw["base_url"] + "/chat/completions", timeout=30,
                   headers={"Authorization": f"Bearer {gw['api_key']}"},
                   json={"model": "does-not-exist", "messages": MSG})
    note("status", r.status_code)
    assert 400 <= r.status_code < 500
