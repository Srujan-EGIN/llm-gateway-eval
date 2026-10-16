"""Features under load: do they still hold when many requests arrive at once?

Each test fires a burst of simultaneous requests (default 50 at a time) and checks a feature:
answers reach the right caller, streaming, fallback, key limits, rate limits, No-Retention and
usage accounting. Every request carries a unique tag ("TAG:...") that the fake provider echoes
back, so an answer delivered to the wrong caller is detected.
"""

import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from openai import OpenAI

from conftest import find_in_sinks, mock_calls, reset_mock, wait_for_async_writes

CONCURRENCY = 50


def _burst(fn, n, concurrency=CONCURRENCY):
    with ThreadPoolExecutor(concurrency) as ex:
        return list(ex.map(fn, range(n)))


def _post(gw, model, content, auth=None, timeout=60):
    try:
        r = httpx.post(gw["base_url"] + "/chat/completions", timeout=timeout,
                       headers={"Authorization": auth or f"Bearer {gw['api_key']}"},
                       json={"model": model, "messages": [{"role": "user", "content": content}]})
        text = r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else None
        return r.status_code, text
    except (httpx.HTTPError, ValueError, KeyError):
        return 0, None  # connection error / timeout / unreadable body


def _tag():
    return "TAG:" + uuid.uuid4().hex[:12]


def test_answers_not_mixed_up_under_load(gw, note):
    """200 requests, 50 at once: all succeed and every caller gets its own answer"""
    tags = [_tag() for _ in range(200)]
    res = _burst(lambda i: _post(gw, "fast-model", f"hello {tags[i]}"), 200)
    ok = sum(1 for s, _ in res if s == 200)
    wrong = sum(1 for i, (s, t) in enumerate(res) if s == 200 and t != f"Reply from fast-model. {tags[i]}")
    note("succeeded", f"{ok} / 200")
    note("answers delivered to the wrong caller", wrong)
    assert ok == 200 and wrong == 0


def test_streaming_under_load(gw, note):
    """50 streams at once: every stream completes with its own full answer"""
    client = OpenAI(base_url=gw["base_url"], api_key=gw["api_key"], max_retries=0, timeout=60)
    tags = [_tag() for _ in range(50)]

    def stream(i):
        try:
            chunks = client.chat.completions.create(
                model="fast-model", stream=True,
                messages=[{"role": "user", "content": f"stream {tags[i]}"}])
            return "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
        except Exception:  # noqa: BLE001 - any failure counts as a broken stream
            return None

    texts = _burst(stream, 50)
    good = sum(1 for i, t in enumerate(texts) if t == f"Reply from fast-model. {tags[i]}")
    note("complete, correct streams", f"{good} / 50")
    assert good == 50


def test_fallback_under_load(gw, note):
    """100 requests to a failing model, 50 at once: all fall back, never outside the allowed list"""
    reset_mock()
    tags = [_tag() for _ in range(100)]
    res = _burst(lambda i: _post(gw, "eu-chat", f"fallback {tags[i]}"), 100)
    served_by_backup = sum(1 for i, (s, t) in enumerate(res)
                           if s == 200 and t == f"Reply from backup-model. {tags[i]}")
    models = {c["model"] for c in mock_calls()}
    note("answered by backup-model", f"{served_by_backup} / 100")
    note("models the provider received", ", ".join(sorted(models)))
    assert served_by_backup == 100
    assert "outside-model" not in models


def _limited_auth(gw, **limits):
    """A key limited to fast-model (and optional limits), per gateway."""
    if gw["name"] == "litellm":
        r = httpx.post(gw["root_url"] + "/key/generate", timeout=30,
                       headers={"Authorization": gw["auth_header"]},
                       json={"models": limits.get("models", ["fast-model"]),
                             **({"rpm_limit": limits["rpm"]} if "rpm" in limits else {})})
        r.raise_for_status()
        return f"Bearer {r.json()['key']}"
    if gw["name"] == "bifrost":
        key = gw["ratelimited_key"] if "rpm" in limits else gw["limited_key"]
        return f"Bearer {key}"
    return None


def test_key_limits_under_load(gw, note):
    """100 mixed requests with a limited key, 50 at once: allowed ones pass, others never reach the provider"""
    auth = _limited_auth(gw)
    if auth is None:
        pytest.fail("no per-key model limits (no keys without the login add-on)")
    reset_mock()
    models = ["fast-model" if i % 2 == 0 else "outside-model" for i in range(100)]
    res = _burst(lambda i: _post(gw, models[i], f"limit {_tag()}", auth=auth), 100)
    allowed_ok = sum(1 for i, (s, _) in enumerate(res) if models[i] == "fast-model" and s == 200)
    blocked = sum(1 for i, (s, _) in enumerate(res) if models[i] == "outside-model" and 400 <= s < 500)
    leaked = "outside-model" in {c["model"] for c in mock_calls()}
    note("allowed model succeeded", f"{allowed_ok} / 50")
    note("other model refused", f"{blocked} / 50")
    note("other model reached the provider", leaked)
    assert allowed_ok == 50 and blocked == 50 and not leaked


def test_rate_limit_under_burst(gw, note):
    """A key limited to 20 requests/minute lets at most 20 of a 60-request burst through"""
    auth = _limited_auth(gw, models=["fast-model"], rpm=20)
    if auth is None:
        pytest.fail("no per-key request rate limits in MLflow's gateway "
                    "(only cost budgets, which need stored traces)")
    res = _burst(lambda i: _post(gw, "fast-model", f"burst {_tag()}", auth=auth), 60, concurrency=60)
    ok = sum(1 for s, _ in res if s == 200)
    limited = sum(1 for s, _ in res if s == 429)
    other = 60 - ok - limited
    note("let through", f"{ok} / 60 (limit 20)")
    note("refused with 429", limited)
    note("other errors", other)
    assert 1 <= ok <= 20 and other == 0


def test_no_retention_under_load(gw, note):
    """100 private prompts, 50 at once (hardened settings): none stored anywhere"""
    prefix = "CANARYLOAD" + uuid.uuid4().hex[:10].upper()
    res = _burst(lambda i: _post(gw, "fast-model", f"secret {prefix}N{i}"), 100)
    wait_for_async_writes(15)
    hits = find_in_sinks(gw["sinks"], prefix)
    note("succeeded", f"{sum(1 for s, _ in res if s == 200)} / 100")
    note("prompts found in", "; ".join(hits) or "nowhere")
    assert not hits


def _litellm_usage(model):
    sql = (f"select count(*), coalesce(sum(spend),0), count(distinct spend) from \"LiteLLM_SpendLogs\" "
           f"where model = 'openai/{model}'")
    out = subprocess.run(["docker", "exec", "gweval-pg", "psql", "-U", "postgres", "-d",
                          "litellm_hardened", "-tAc", sql], capture_output=True, text=True).stdout
    n, total, distinct = out.strip().split("|")
    return int(n), float(total), int(distinct)


def _bifrost_usage(gw, model):
    logs = httpx.get(gw["root_url"] + "/api/logs", auth=tuple(gw["admin"]), timeout=30,
                     params={"models": model, "limit": 1000}).json()["logs"]
    costs = [l.get("cost") or 0 for l in logs]
    return len(costs), sum(costs), len(set(costs))


def test_usage_counted_under_load(gw, note):
    """100 identical calls, 50 at once (No-Retention on): exactly 100 usage records with the right total cost"""
    model = "gpt-4o-mini-2024-07-18"  # used by no other test, so its records are isolated
    if gw["name"] == "mlflow":
        _burst(lambda i: _post(gw, model, "count me"), 100)
        pytest.fail("no usage or cost records while No-Retention (usage tracking off) is on")
    res = _burst(lambda i: _post(gw, model, "count me"), 100)
    assert sum(1 for s, _ in res if s == 200) == 100, "not all requests succeeded"
    usage = _litellm_usage if gw["name"] == "litellm" else (lambda m: _bifrost_usage(gw, m))
    n = total = distinct = 0
    for _ in range(30):  # usage is written in background batches
        time.sleep(1)
        n, total, distinct = usage(model)
        if n >= 100:
            break
    note("usage records", f"{n} / 100")
    note("total cost (USD)", f"{total:.8f}")
    note("same cost on every record", distinct == 1)
    assert n == 100 and distinct == 1 and total > 0
