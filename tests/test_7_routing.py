"""Model routing: how each gateway picks the model that serves a request.

Runs against LiteLLM and Bifrost only. Same setup in both (see gateways/):
  split-model    two backends weighted 80 / 20
  regional-chat  an EU and a US backend; the request asks for EU
                 (LiteLLM: tag "region:eu"; Bifrost: routing rule on header "x-region: eu")
  eu-primary     broken primary -> broken first backup -> working second backup
  flaky-model    one broken and one healthy backend behind one name
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from conftest import TARGETS, mock_calls, reset_mock

OWN_GW = True
ROUTING_GATEWAYS = ["litellm", "bifrost"]
EU_HEADER = {"litellm": {"x-litellm-tags": "region:eu"}, "bifrost": {"x-region": "eu"}}


@pytest.fixture(params=ROUTING_GATEWAYS)
def gw(request):
    return {"name": request.param, **TARGETS["gateways"][request.param]}


def _post(gw, model, headers=None, content="route me"):
    try:
        r = httpx.post(gw["base_url"] + "/chat/completions", timeout=60,
                       headers={"Authorization": f"Bearer {gw['api_key']}", **(headers or {})},
                       json={"model": model, "messages": [{"role": "user", "content": content}]})
        return r.status_code, (r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else None)
    except (httpx.HTTPError, ValueError, KeyError):
        return 0, None


def _served(models_seen):
    return Counter(c["model"] for c in mock_calls() if c["model"] in models_seen)


def test_weighted_split(gw, note):
    """Traffic is split by the configured weights (80 / 20, 500 requests)"""
    reset_mock()
    with ThreadPoolExecutor(25) as ex:
        res = list(ex.map(lambda _: _post(gw, "split-model"), range(500)))
    counts = _served({"split-a", "split-b"})
    share_a = counts["split-a"] / max(1, sum(counts.values()))
    note("split-a / split-b", f"{counts['split-a']} / {counts['split-b']} ({share_a:.0%} / {1 - share_a:.0%})")
    assert sum(1 for s, _ in res if s == 200) == 500
    assert 0.72 <= share_a <= 0.88


def test_region_routing(gw, note):
    """Requests that ask for the EU region always go to the EU backend (100 requests)"""
    reset_mock()
    res = [_post(gw, "regional-chat", EU_HEADER[gw["name"]]) for _ in range(100)]
    eu = _served({"eu-model", "us-model"})
    reset_mock()
    for _ in range(40):
        _post(gw, "regional-chat")
    untagged = _served({"eu-model", "us-model"})
    note("EU requests served by eu-model / us-model", f"{eu['eu-model']} / {eu['us-model']}")
    note("requests without a region went to eu / us (informational)",
         f"{untagged['eu-model']} / {untagged['us-model']}")
    assert sum(1 for s, _ in res if s == 200) == 100
    assert eu["us-model"] == 0 and eu["eu-model"] == 100


def test_fallback_chain_order(gw, note):
    """A fallback chain is tried in order: broken -> broken backup -> working backup"""
    reset_mock()
    status, text = _post(gw, "eu-primary")
    order = [c["model"] for c in mock_calls()]
    note("provider calls, in order", " -> ".join(order))
    assert status == 200 and text == "Reply from eu-model."
    assert order == ["eu-broken", "eu-broken-2", "eu-model"]


def test_failing_backend_taken_out(gw, note):
    """A backend that keeps failing is taken out of rotation (60 requests, 1 of 2 backends broken; pass: at most 10 fail)"""
    reset_mock()
    res = [_post(gw, "flaky-model") for _ in range(60)]
    failed = sum(1 for s, _ in res if s != 200)
    to_broken = _served({"flaky-broken"})["flaky-broken"]
    note("failed requests", f"{failed} / 60")
    note("requests sent to the broken backend", to_broken)
    if gw["name"] == "bifrost":
        note("why", "the free version has no automatic cooldown; the circuit breaker is Enterprise")
    # Without any health check, about half (~30) would fail. With 4 workers each seeing a
    # failure or two before the shared cooldown kicks in, a handful is expected.
    assert failed <= 10, "the broken backend kept receiving traffic"
