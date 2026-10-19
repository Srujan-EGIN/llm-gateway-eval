"""Shared fixtures. Every test runs once per gateway, and each result is written to RESULTS_FILE."""

import json
import os
import subprocess
import time
import uuid

import httpx
import pytest
from openai import OpenAI

TARGETS = json.loads(open(os.environ["TARGETS_FILE"]).read())
RESULTS_FILE = os.environ.get("RESULTS_FILE")
GATEWAYS = list(TARGETS["gateways"])


def pytest_generate_tests(metafunc):
    # Modules that set OWN_GW = True define their own "gw" fixture and target list.
    if "gw" in metafunc.fixturenames and not getattr(metafunc.module, "OWN_GW", False):
        metafunc.parametrize("gw", GATEWAYS, indirect=True)


@pytest.fixture
def gw(request):
    return {"name": request.param, **TARGETS["gateways"][request.param]}


@pytest.fixture
def client(gw):
    return OpenAI(base_url=gw["base_url"], api_key=gw["api_key"], max_retries=0, timeout=60)


@pytest.fixture
def note(record_property):
    """note("key", value): attach a finding to this result (shown in the report)."""
    return record_property


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if not RESULTS_FILE or rep.when != "call" and not (rep.when == "setup" and rep.skipped):
        return
    gw = item.callspec.params.get("gw") if hasattr(item, "callspec") else None
    msg = ""
    if rep.failed:
        msg = str(rep.longrepr.reprcrash.message) if hasattr(rep.longrepr, "reprcrash") else str(rep.longrepr)
    elif rep.skipped:
        msg = rep.longrepr[2] if isinstance(rep.longrepr, tuple) else str(rep.longrepr)
    with open(RESULTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "check": item.originalname, "gateway": gw, "outcome": rep.outcome,
            "message": msg[:400], "details": dict(item.user_properties),
            "doc": (item.function.__doc__ or "").strip().splitlines()[0] if item.function.__doc__ else "",
        }) + "\n")


# ---------------------------------------------------------------- helpers
def _mock_urls() -> list[str]:
    return [TARGETS["mock"]] + ([TARGETS["mock_tls"]] if TARGETS.get("mock_tls") else [])


def mock_calls() -> list[dict]:
    """Every call the fake provider received (HTTP and HTTPS listeners), oldest first."""
    calls = []
    for url in _mock_urls():
        calls += httpx.get(url + "/_calls", verify=False).json()
    return sorted(calls, key=lambda c: c["time"])


def reset_mock() -> None:
    for url in _mock_urls():
        httpx.delete(url + "/_calls", verify=False)


def mcp_calls() -> list[dict]:
    """Every tool call the fake MCP server actually executed."""
    return httpx.get(TARGETS["mcp"]["url_from_host"] + "/_calls").json()


def reset_mcp() -> None:
    httpx.delete(TARGETS["mcp"]["url_from_host"] + "/_calls")


def new_canary() -> str:
    return "CANARY" + uuid.uuid4().hex[:16].upper()


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (p.stdout or "") + (p.stderr or "")


_SCAN = (
    "import os,sys\n"
    "needle=sys.argv[2].encode()\n"
    "for d,_,fs in os.walk(sys.argv[1]):\n"
    "  for f in fs:\n"
    "    p=os.path.join(d,f)\n"
    "    try:\n"
    "      if needle in open(p,'rb').read(): print('HIT',p)\n"
    "    except OSError: pass\n"
)


def find_in_sinks(sinks: list[dict], needle: str) -> list[str]:
    """Return a description of every sink (logs, database, data folder) containing `needle`."""
    hits = []
    for s in sinks:
        if s["type"] == "logs":
            if needle in _run(["docker", "logs", s["container"]]):
                hits.append(f"container logs ({s['container']})")
        elif s["type"] == "pg":
            dump = _run(["docker", "exec", s["container"], "pg_dump", "-U", "postgres",
                         "-d", s["db"], "--data-only"])
            table, found = None, set()
            for line in dump.splitlines():
                if line.startswith("COPY "):
                    table = line.split()[1]
                elif needle in line:
                    found.add(table)
            hits += [f"Postgres table {t}" for t in sorted(found)]
        elif s["type"] == "dir" and s.get("scanner") == "grep":  # images without Python
            out = _run(["docker", "exec", s["container"], "grep", "-rlF", needle, s["path"]])
            hits += [f"file {line.strip()}" for line in out.splitlines()
                     if line.startswith(s["path"])]
        elif s["type"] == "dir":
            out = _run(["docker", "exec", s["container"], "python", "-c", _SCAN, s["path"], needle])
            hits += [f"file {line[4:]}" for line in out.splitlines() if line.startswith("HIT ")]
    return hits


def wait_for_async_writes(seconds: float = 12) -> None:
    """Both gateways write logs/traces in the background; give them time to land."""
    time.sleep(seconds)
