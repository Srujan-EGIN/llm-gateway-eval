"""Latency probe, run INSIDE the Docker network so host port-forwarding doesn't add noise.

Standard library only, so it runs in any Python image:
    python latency_probe.py <config.json>

config: {"direct": {"url": ...}, "gateway": {"url": ..., "headers": {...}},
         "body": {...}, "n_seq": 60, "n_conc": 200, "concurrency": 10}
Prints one JSON object with the raw timings (ms) for each target and pattern.
Direct and gateway calls are interleaved so slow drift on the machine affects both equally.
"""

import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def call(target, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(target["url"], data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **target.get("headers", {})})
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()
        overhead = r.headers.get("x-litellm-overhead-duration-ms") or \
            r.headers.get("x-mlflow-gateway-overhead-duration-ms")
    return (time.perf_counter() - t) * 1000, float(overhead) if overhead else None


def main():
    cfg = json.load(open(sys.argv[1]))
    direct, gw, body = cfg["direct"], cfg["gateway"], cfg["body"]
    for _ in range(10):  # warm-up
        call(direct, body), call(gw, body)

    out = {"direct_seq": [], "gateway_seq": [], "direct_conc": [], "gateway_conc": [],
           "self_reported": []}
    for _ in range(cfg["n_seq"]):
        out["direct_seq"].append(call(direct, body)[0])
        ms, own = call(gw, body)
        out["gateway_seq"].append(ms)
        if own is not None:
            out["self_reported"].append(own)

    with ThreadPoolExecutor(cfg["concurrency"]) as ex:
        for _ in range(4):  # alternate batches of direct and gateway load
            n = cfg["n_conc"] // 4
            out["direct_conc"] += [r[0] for r in ex.map(lambda _: call(direct, body), range(n))]
            out["gateway_conc"] += [r[0] for r in ex.map(lambda _: call(gw, body), range(n))]
    print(json.dumps(out))


if __name__ == "__main__":
    main()
