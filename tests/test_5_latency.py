"""Speed under load: how much time does each gateway add, and does it stay error-free?

The mock provider answers in a fixed ~100 ms. At 1, 10, 50 and 100 requests at once, the same
request is timed sent directly to the provider and through the gateway; the difference is the
gateway's overhead. The timing client runs in a container on the same Docker network
(scripts/latency_probe.py), so host port-forwarding doesn't distort the numbers.

Fairness: every gateway container is capped at 4 CPUs; LiteLLM and MLflow run 4 workers.
Results are from one machine: compare the gateways with each other, not with other setups.
"""

import json
import statistics
import subprocess
from pathlib import Path


from conftest import TARGETS

ROOT = Path(__file__).resolve().parent.parent
IN_NET = {
    "litellm": "http://gweval-litellm-hardened:4000/v1/chat/completions",
    "bifrost": "http://gweval-bifrost-hardened:8080/v1/chat/completions",
    "mlflow": "http://gweval-mlflow:5000/gateway/mlflow/v1/chat/completions",
}
LEVELS = [{"concurrency": 1, "requests": 60}, {"concurrency": 10, "requests": 200},
          {"concurrency": 50, "requests": 400}, {"concurrency": 100, "requests": 400}]
_CACHE: dict[str, dict] = {}  # one probe run per gateway, shared by the tests below


def _probe(gw) -> dict:
    if gw["name"] in _CACHE:
        return _CACHE[gw["name"]]
    cfg = {
        "direct": {"url": "http://gweval-mock:9100/v1/chat/completions"},
        "gateway": {"url": IN_NET[gw["name"]],
                    "headers": {"Authorization": f"Bearer {gw['api_key']}"}},
        "body": {"model": "fast-model", "messages": [{"role": "user", "content": "timing"}]},
        "levels": LEVELS,
    }
    cfg_file = Path(TARGETS["run_dir"]) / f"latency-{gw['name']}.json"
    cfg_file.write_text(json.dumps(cfg))
    out = subprocess.run(
        ["docker", "run", "--rm", "--network", "gweval",
         "-v", f"{(ROOT / 'scripts').resolve()}:/probe:ro",
         "-v", f"{cfg_file.parent.resolve()}:/cfg:ro",
         "--entrypoint", "python", TARGETS["images"]["mlflow"],
         "/probe/latency_probe.py", f"/cfg/{cfg_file.name}"],
        capture_output=True, text=True)
    assert out.returncode == 0, f"latency probe failed: {out.stderr[-600:]}"
    r = json.loads(out.stdout.strip().splitlines()[-1])
    (Path(TARGETS["run_dir"]) / f"latency-{gw['name']}-raw.json").write_text(json.dumps(r))
    _CACHE[gw["name"]] = r
    return r


def _pct(values, p):
    return statistics.quantiles(values, n=100)[p - 1] if len(values) > 1 else float("nan")


def test_overhead(gw, note):
    """Time added per call at 1 / 10 / 50 / 100 requests at once (informational)"""
    r = _probe(gw)
    note("informational", True)
    for level, d in r.items():
        added = round(_pct(d["gateway"], 50) - _pct(d["direct"], 50), 1)
        added95 = round(_pct(d["gateway"], 95) - _pct(d["direct"], 95), 1)
        note(f"{level} at once: added p50 / p95 (ms)", f"{added} / {added95}")


def test_throughput(gw, note):
    """Requests per second through the gateway at 100 at once (informational)"""
    d = _probe(gw)["100"]
    note("informational", True)
    rps = round(len(d["gateway"]) / d["gateway_wall_s"], 1) if d["gateway_wall_s"] else 0
    note("gateway req/s at 100 at once", rps)
    note("provider alone could do (req/s)", round(100 / 0.1))


def test_no_errors_under_load(gw, note):
    """No failed requests at 50 and 100 requests at once"""
    r = _probe(gw)
    for level in ("50", "100"):
        note(f"errors at {level} at once", f"{r[level]['gateway_errors']} of "
             f"{r[level]['gateway_errors'] + len(r[level]['gateway'])}")
    assert r["50"]["gateway_errors"] == 0 and r["100"]["gateway_errors"] == 0
