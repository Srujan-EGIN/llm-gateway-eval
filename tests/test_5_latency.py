"""Overhead: how much time does each gateway add on top of the provider?

The mock provider answers in a fixed ~100 ms. The same request is timed sent directly to the
provider and through each gateway; the difference is the gateway's overhead.

The timing client runs in a container on the same Docker network (scripts/latency_probe.py),
so host port-forwarding doesn't distort the numbers. Results are from one machine: compare the
gateways with each other, not with numbers from other setups.
"""

import json
import statistics
import subprocess
from pathlib import Path

from conftest import TARGETS

ROOT = Path(__file__).resolve().parent.parent
# Same in-network names as scripts/run_eval.py
IN_NET = {
    "litellm": "http://gweval-litellm-hardened:4000/v1/chat/completions",
    "bifrost": "http://gweval-bifrost-hardened:8080/v1/chat/completions",
    "mlflow": "http://gweval-mlflow:5000/gateway/mlflow/v1/chat/completions",
}


def _pct(values, p):
    return statistics.quantiles(values, n=100)[p - 1]


def test_overhead(gw, note):
    """Gateway overhead per call (informational)"""
    cfg = {
        "direct": {"url": "http://gweval-mock:9100/v1/chat/completions"},
        "gateway": {"url": IN_NET[gw["name"]],
                    "headers": {"Authorization": f"Bearer {gw['api_key']}"}},
        "body": {"model": "fast-model", "messages": [{"role": "user", "content": "timing"}]},
        "n_seq": 60, "n_conc": 200, "concurrency": 10,
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

    def added(kind, p):
        return round(_pct(r[f"gateway_{kind}"], p) - _pct(r[f"direct_{kind}"], p), 1)

    note("informational", True)
    note("one at a time: added p50 (ms)", added("seq", 50))
    note("one at a time: added p95 (ms)", added("seq", 95))
    note("10 at once: added p50 (ms)", added("conc", 50))
    note("10 at once: added p95 (ms)", added("conc", 95))
    if r["self_reported"]:
        note("gateway's own overhead header, p50 (ms)", round(statistics.median(r["self_reported"]), 1))
    assert len(r["gateway_seq"]) == 60 and len(r["gateway_conc"]) == 200
