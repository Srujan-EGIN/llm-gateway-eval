"""Latency and load probe, run INSIDE the Docker network so host port-forwarding adds no noise.

Standard library only, so it runs in any Python image:
    python latency_probe.py <config.json>

config: {"direct": {"url": ...}, "gateway": {"url": ..., "headers": {...}}, "body": {...},
         "levels": [{"concurrency": 1, "requests": 60}, {"concurrency": 10, "requests": 200}, ...]}

For each level it sends the same number of requests directly to the provider and through the
gateway (alternating batches, so drift on the machine affects both equally) and records every
latency, the errors, and the gateway's throughput. Prints one JSON object.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def call(target, body):
    """Returns (latency_ms, ok)."""
    req = urllib.request.Request(target["url"], data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **target.get("headers", {})})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
            ok = r.status == 200
    except (urllib.error.URLError, OSError):
        ok = False
    return (time.perf_counter() - t) * 1000, ok


def run_level(direct, gw, body, concurrency, requests):
    res = {"direct": [], "gateway": [], "gateway_errors": 0, "direct_errors": 0, "gateway_wall_s": 0.0}
    batches = 4 if requests >= 40 else 1
    per = requests // batches
    with ThreadPoolExecutor(concurrency) as ex:
        for _ in range(batches):
            for name, target in (("direct", direct), ("gateway", gw)):
                t0 = time.perf_counter()
                out = list(ex.map(lambda _: call(target, body), range(per)))
                if name == "gateway":
                    res["gateway_wall_s"] += time.perf_counter() - t0
                res[name] += [ms for ms, ok in out if ok]
                res[f"{name}_errors"] += sum(1 for _, ok in out if not ok)
    return res


def main():
    cfg = json.load(open(sys.argv[1]))
    direct, gw, body = cfg["direct"], cfg["gateway"], cfg["body"]
    for _ in range(10):  # warm-up
        call(direct, body), call(gw, body)
    out = {str(lv["concurrency"]): run_level(direct, gw, body, lv["concurrency"], lv["requests"])
           for lv in cfg["levels"]}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
