"""No-Retention and cost tracking, and whether you can have both at once.

A unique "canary" string is put in the prompt, then every place the gateway writes to
(container logs, its database, its data folder) is searched for it byte by byte.

Profiles (see scripts/run_eval.py):
  hardened  LiteLLM: turn_off_message_logging + store_prompts_in_spend_logs=false
            MLflow:  endpoint usage_tracking=false (no request traces)
  default   out-of-the-box settings
"""

import json
import subprocess
import time

import httpx
import pytest
from openai import OpenAI

from conftest import find_in_sinks, new_canary, wait_for_async_writes


def _summary(hits):
    if not hits:
        return "nowhere"
    traces = [h for h in hits if "/traces/" in h]
    rest = [h for h in hits if "/traces/" not in h]
    return "; ".join(rest + ([f"{len(traces)} trace files"] if traces else []))


def _send_canary(base_url, api_key, model, canary):
    c = OpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=60)
    msg = [{"role": "user", "content": f"My account number is {canary}."}]
    c.chat.completions.create(model=model, messages=msg)
    list(c.chat.completions.create(model=model, messages=msg, stream=True))


def test_no_retention_hardened(gw, note):
    """Hardened settings: prompt text is stored nowhere"""
    canary = new_canary()
    _send_canary(gw["base_url"], gw["api_key"], "fast-model", canary)
    wait_for_async_writes()
    hits = find_in_sinks(gw["sinks"], canary)
    note("prompt found in", _summary(hits))
    assert not hits, f"prompt text was stored in: {hits}"


def test_retention_default(gw, note):
    """Default settings: is prompt text stored? (informational)"""
    dp = gw["default_profile"]
    canary = new_canary()
    _send_canary(dp["base_url"], dp["api_key"], dp["chat_model"], canary)
    wait_for_async_writes()
    hits = find_in_sinks(dp["sinks"], canary)
    note("informational", True)
    note("stores prompts by default", "yes" if hits else "no")
    note("prompt found in", _summary(hits))


def _litellm_last_spend(db: str) -> dict:
    sql = ('select row_to_json(t) from (select model, spend, total_tokens, messages::text, '
           'response::text from "LiteLLM_SpendLogs" order by "startTime" desc limit 1) t')
    out = subprocess.run(["docker", "exec", "gweval-pg", "psql", "-U", "postgres", "-d", db,
                          "-tAc", sql], capture_output=True, text=True).stdout.strip()
    return json.loads(out) if out else {}


def _mlflow_latest_trace_cost(experiment_name: str):
    from mlflow import MlflowClient
    c = MlflowClient("http://127.0.0.1:5000")
    exp = c.get_experiment_by_name(experiment_name)
    if exp is None:
        return None
    traces = c.search_traces(locations=[exp.experiment_id], max_results=1,
                             order_by=["timestamp_ms DESC"])
    if not traces:
        return None
    for span in traces[0].data.spans:
        cost = (span.attributes or {}).get("mlflow.llm.cost")
        if cost:
            return cost.get("total_cost") if isinstance(cost, dict) else cost
    return None


def _cost_for(gw, base_url, api_key, model):
    r = httpx.post(base_url + "/chat/completions", timeout=60,
                   headers={"Authorization": f"Bearer {api_key}"},
                   json={"model": model, "messages": [{"role": "user", "content": "cost?"}]})
    r.raise_for_status()
    if gw["name"] == "litellm":
        return float(r.headers.get("x-litellm-response-cost") or 0) or None
    if gw["name"] == "bifrost":
        # Cost lives in Bifrost's request log (written asynchronously); the log id is the
        # response's x-request-id. Poll for that exact entry.
        root, req_id = base_url.rsplit("/v1", 1)[0], r.headers.get("x-request-id")
        for _ in range(20):
            time.sleep(1)
            logs = httpx.get(root + "/api/logs", auth=tuple(gw["admin"]), timeout=30,
                             params={"models": model, "limit": 20, "sort_by": "timestamp",
                                     "order": "desc"}).json()["logs"]
            row = next((l for l in logs if l["id"] == req_id), None)
            if row and row.get("cost"):
                return row["cost"]
        print(f"bifrost cost lookup: request {req_id} log row = {row}")  # shown on failure
        return None
    wait_for_async_writes(5)
    return _mlflow_latest_trace_cost(f"gateway/{model}")


def test_cost_tracked_default(gw, note):
    """Default settings: cost is tracked per call"""
    dp = gw["default_profile"]
    cost = _cost_for(gw, dp["base_url"], dp["api_key"], dp["cost_model"])
    note("cost of one call (USD)", cost)
    assert cost and cost > 0


def test_cost_tracked_with_no_retention(gw, note):
    """Cost is still tracked while No-Retention settings are on"""
    cost = _cost_for(gw, gw["base_url"], gw["api_key"], "gpt-4o-mini")
    note("cost of one call (USD)", cost)
    if gw["name"] == "litellm":
        wait_for_async_writes(6)
        row = _litellm_last_spend(gw["sinks"][1]["db"])
        note("usage row in database", {k: row.get(k) for k in ("model", "spend", "total_tokens")})
        note("prompt stored in that row", row.get("messages") not in (None, "{}", "[]", "null"))
    assert cost and cost > 0, "no cost available: cost tracking depends on stored request traces"
