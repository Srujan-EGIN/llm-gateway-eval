"""Provider connectors: can each gateway reach AWS Bedrock, Azure OpenAI, Azure AI Foundry and an
on-prem model server, fall back across clouds, and keep an EU Sovereign key inside the EU?

Runs against LiteLLM and Bifrost only. The fake provider answers in each provider's own API format
(Bedrock Converse, Azure OpenAI, Azure AI Foundry, OpenAI-compatible for on-prem vLLM) and records
which API was called, which model, which AWS region the request was signed for, and how it was
authenticated. These are fakes: they prove each gateway sends the right request to the right place,
not that real AWS or Azure accept it (that needs real credentials).

Names the app uses (same in both gateways):
  aws-claude       AWS Bedrock, eu-central-1
  azure-gpt        Azure OpenAI, EU deployment
  azure-us-gpt     Azure OpenAI, US deployment (not allowed in EU Sovereign mode)
  foundry-mistral  Azure AI Foundry, non-OpenAI model (Mistral)
  onprem-llama     on-prem vLLM
  eu-assistant     Azure EU (down) -> must fall back to AWS EU
"""

import httpx
import pytest

from conftest import TARGETS, mock_calls, reset_mock

OWN_GW = True


@pytest.fixture(params=["litellm", "bifrost"])
def gw(request):
    return {"name": request.param, **TARGETS["gateways"][request.param]}


def _post(gw, model, key=None):
    r = httpx.post(gw["base_url"] + "/chat/completions", timeout=60,
                   headers={"Authorization": f"Bearer {key or gw['api_key']}"},
                   json={"model": model, "messages": [{"role": "user", "content": "hello"}]})
    text = r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else r.text[:200]
    return r.status_code, text


def test_reaches_aws_bedrock(gw, note):
    """Reaches AWS Bedrock (Converse API), signed for the EU region"""
    reset_mock()
    status, text = _post(gw, "aws-claude")
    calls = [c for c in mock_calls() if c["provider"] == "aws-bedrock"]
    note("answer", text)
    note("Bedrock saw", [(c["model"], c.get("region"), c.get("auth")) for c in calls])
    assert status == 200 and text == "Reply from eu.anthropic.claude-test-v1:0."
    assert calls and calls[0]["region"] == "eu-central-1" and calls[0]["auth"] == "sigv4"


def test_reaches_azure_openai(gw, note):
    """Reaches Azure OpenAI (EU deployment) with its API key"""
    reset_mock()
    status, text = _post(gw, "azure-gpt")
    calls = [c for c in mock_calls() if c["provider"] == "azure-openai"]
    note("answer", text)
    note("Azure saw", [(c["model"], c["path"], c.get("auth")) for c in calls])
    assert status == 200 and text == "Reply from gpt-eu-deploy."
    assert calls and calls[0]["model"] == "gpt-eu-deploy" and calls[0]["auth"] == "api-key"


def test_reaches_azure_foundry_model(gw, note):
    """Reaches a non-OpenAI model (Mistral) on Azure AI Foundry"""
    reset_mock()
    status, text = _post(gw, "foundry-mistral")
    calls = [c for c in mock_calls() if c["provider"] in ("azure-foundry", "azure-openai")]
    note("answer", text)
    note("API used", [(c["provider"], c["path"]) for c in calls])
    if calls and calls[0]["provider"] == "azure-openai":
        note("caveat", "sent via Azure's OpenAI-compatible v1 API; check a real Foundry resource accepts it")
    assert status == 200 and text == "Reply from Mistral-Large-2411."


def test_reaches_onprem_server(gw, note):
    """Reaches an on-prem model server (vLLM, OpenAI-compatible)"""
    reset_mock()
    status, text = _post(gw, "onprem-llama")
    calls = [c for c in mock_calls() if c["model"] == "llama-3.1-8b"]
    note("answer", text)
    note("on-prem server saw", [(c["provider"], c["path"]) for c in calls])
    assert status == 200 and text == "Reply from llama-3.1-8b." and calls


def test_cross_cloud_fallback(gw, note):
    """When Azure EU is down, requests fall back to AWS EU (never to a US provider)"""
    reset_mock()
    status, text = _post(gw, "eu-assistant")
    calls = mock_calls()
    note("provider calls, in order", " -> ".join(f"{c['provider']}:{c['model']}" for c in calls))
    assert status == 200 and text == "Reply from eu.anthropic.claude-test-v1:0."
    assert [c["provider"] for c in calls] == ["azure-openai", "aws-bedrock"]
    assert not any(c["model"] == "gpt-us-deploy" for c in calls)


def _sovereign_key(gw):
    if gw["name"] == "bifrost":
        return gw["sovereign_key"]
    r = httpx.post(gw["root_url"] + "/key/generate", timeout=30,
                   headers={"Authorization": gw["auth_header"]},
                   json={"key_alias": "eu-sovereign",
                         "models": ["aws-claude", "azure-gpt", "foundry-mistral", "onprem-llama",
                                    "eu-assistant"]})
    r.raise_for_status()
    return r.json()["key"]


def test_sovereign_key_blocks_non_eu(gw, note):
    """An EU Sovereign key can use EU providers but never reaches the US Azure deployment"""
    key = _sovereign_key(gw)
    reset_mock()
    eu_status, _ = _post(gw, "aws-claude", key)
    us_status, us_text = _post(gw, "azure-us-gpt", key)
    reached_us = any(c["model"] == "gpt-us-deploy" for c in mock_calls())
    note("EU model (aws-claude)", eu_status)
    note("US model (azure-us-gpt)", us_status)
    note("US deployment reached", reached_us)
    assert eu_status == 200 and 400 <= us_status < 500 and not reached_us
