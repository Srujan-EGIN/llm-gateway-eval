"""Start everything in Docker, run the test suite against both gateways, write the report.

    uv run python scripts/run_eval.py              # full run
    uv run python scripts/run_eval.py --setup-only # start + provision, run no tests

Everything runs on one Docker network so both gateways take the same path to the provider.
Host ports (127.0.0.1 only):
  9100  mock provider (fake OpenAI-compatible models)
  4000  LiteLLM Proxy, "hardened" profile (recommended production settings) + Postgres
  4001  LiteLLM Proxy, "default" profile (out-of-the-box settings) + Postgres
  5000  MLflow server with its AI Gateway (both profiles are endpoints on one server)
"""

import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = ROOT / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
PY = sys.executable
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

# Images pinned by digest (condition: "pin the exact image").
IMAGES = json.loads((ROOT / "images.json").read_text())

NET = "gweval"
PREFIX = "gweval-"
MASTER_KEY = "sk-" + secrets.token_hex(16)
PG_PASSWORD = secrets.token_hex(8)
MOCK_IN_NET = "http://gweval-mock:9100"  # how gateways reach the provider
MOCK_FROM_HOST = "http://127.0.0.1:9100"  # how tests reach it directly (latency baseline)


def log(msg: str) -> None:
    print(f"[run_eval] {msg}", flush=True)


def docker(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, capture_output=capture, text=True,
                          encoding="utf-8", errors="replace")


def cleanup() -> None:
    names = docker("ps", "-aq", "--filter", f"name={PREFIX}", check=False).stdout.split()
    if names:
        docker("rm", "-f", *names, check=False)
    vols = docker("volume", "ls", "-q", "--filter", f"name={PREFIX}", check=False).stdout.split()
    if vols:
        docker("volume", "rm", "-f", *vols, check=False)
    docker("network", "rm", NET, check=False)


def run_container(name: str, image: str, args: list[str], *, ports: list[str] = (),
                  env: dict | None = None, mounts: list[str] = (), entrypoint: str | None = None):
    cmd = ["run", "-d", "--name", PREFIX + name, "--network", NET,
           "--network-alias", PREFIX + name]
    for p in ports:
        cmd += ["-p", f"127.0.0.1:{p}"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    for m in mounts:
        cmd += ["-v", m]
    if entrypoint:
        cmd += ["--entrypoint", entrypoint]
    docker(*cmd, image, *args)


def wait_http(url: str, timeout: float = 240, headers: dict | None = None) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, headers=headers, timeout=3).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {url}")


# ---------------------------------------------------------------- LiteLLM
def litellm_config(hardened: bool) -> dict:
    def dep(group: str, model: str) -> dict:
        return {"model_name": group,
                "litellm_params": {"model": f"openai/{model}", "api_base": f"{MOCK_IN_NET}/v1",
                                   "api_key": "fake-key"}}

    cfg = {
        "model_list": [
            dep("fast-model", "fast-model"),
            dep("smart-model", "smart-model"),
            dep("gpt-4o-mini", "gpt-4o-mini"),
            dep("embed-model", "embed-model"),
            # "eu-chat": primary is broken, the EU backup must take over.
            dep("eu-chat", "broken-model"),
            dep("eu-backup", "backup-model"),
            # Exists in the gateway but is NOT an allowed fallback for eu-chat.
            dep("outside-model", "outside-model"),
        ],
        "router_settings": {"fallbacks": [{"eu-chat": ["eu-backup"]}], "num_retries": 0},
        "litellm_settings": {"num_retries": 0, "request_timeout": 30, "telemetry": False},
        "general_settings": {"master_key": MASTER_KEY, "proxy_batch_write_at": 2},
    }
    if hardened:
        cfg["litellm_settings"]["turn_off_message_logging"] = True
        cfg["general_settings"]["store_prompts_in_spend_logs"] = False
    return cfg


def start_litellm(profile: str, port: int) -> None:
    d = RUN_DIR / f"litellm-{profile}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(yaml.safe_dump(litellm_config(profile == "hardened"),
                                                  sort_keys=False))
    env = {"DATABASE_URL": f"postgresql://postgres:{PG_PASSWORD}@gweval-pg:5432/litellm_{profile}"}
    if profile == "hardened":
        env["DISABLE_ADMIN_UI"] = "True"
    run_container(f"litellm-{profile}", IMAGES["litellm"],
                  ["--config", "/app/config.yaml", "--port", "4000", "--num_workers", "1"],
                  ports=[f"{port}:4000"], env=env,
                  mounts=[f"{(d / 'config.yaml').resolve()}:/app/config.yaml:ro"])


# ---------------------------------------------------------------- MLflow
def start_mlflow() -> None:
    env = {
        "MLFLOW_CRYPTO_KEK_PASSPHRASE": secrets.token_hex(24),
        "MLFLOW_GATEWAY_API_BASE_ALLOW_PRIVATE_IPS": "true",  # provider is on a private network
        "MLFLOW_DISABLE_TELEMETRY": "true",
        # MLflow only accepts https provider URLs by default; the mock is plain http.
        "MLFLOW_GATEWAY_API_BASE_ALLOWED_SCHEMES": "http,https",
    }
    run_container("mlflow", IMAGES["mlflow"],
                  ["mlflow", "server", "--host", "0.0.0.0", "--port", "5000",
                   "--backend-store-uri", "sqlite:////data/mlflow.db",
                   "--default-artifact-root", "/data/artifacts",
                   "--allowed-hosts", "localhost:5000,127.0.0.1:5000",
                   "--workers", "1"],
                  ports=["5000:5000"], env=env, mounts=[f"{PREFIX}mlflow-data:/data"])


def provision_mlflow() -> None:
    """Create MLflow gateway endpoints equivalent to the LiteLLM config."""
    from mlflow.entities.gateway_endpoint import (FallbackConfig, FallbackStrategy,
                                                  GatewayEndpointModelConfig,
                                                  GatewayModelLinkageType)
    from mlflow.tracking._tracking_service.utils import _get_store

    store = _get_store("http://127.0.0.1:5000")
    secret = store.create_gateway_secret(
        secret_name="mock-key", secret_value={"api_key": "fake-key"}, provider="openai",
        auth_config={"api_base": f"{MOCK_IN_NET}/v1"})

    defs = {}
    for m in ["fast-model", "smart-model", "gpt-4o-mini", "embed-model", "broken-model",
              "backup-model", "outside-model"]:
        defs[m] = store.create_gateway_model_definition(
            name=f"def-{m}", secret_id=secret.secret_id, provider="openai",
            model_name=m).model_definition_id

    def endpoint(name, model, tracking, fallback=None):
        configs = [GatewayEndpointModelConfig(model_definition_id=defs[model],
                                              linkage_type=GatewayModelLinkageType.PRIMARY)]
        kw = {}
        if fallback:
            configs.append(GatewayEndpointModelConfig(
                model_definition_id=defs[fallback], linkage_type=GatewayModelLinkageType.FALLBACK,
                fallback_order=0))
            kw["fallback_config"] = FallbackConfig(strategy=FallbackStrategy.SEQUENTIAL,
                                                   max_attempts=2)
        store.create_gateway_endpoint(name=name, model_configs=configs,
                                      usage_tracking=tracking, **kw)

    # "hardened" profile = usage tracking (request traces) switched off
    for m in ["fast-model", "smart-model", "gpt-4o-mini", "embed-model", "outside-model"]:
        endpoint(m, m, tracking=False)
    endpoint("eu-chat", "broken-model", tracking=False, fallback="backup-model")
    # "default" profile = MLflow's default (usage tracking on)
    endpoint("fast-model-default", "fast-model", tracking=True)
    endpoint("gpt-4o-mini-default", "gpt-4o-mini", tracking=True)


# ---------------------------------------------------------------- main
def targets() -> dict:
    pg = {"type": "pg", "container": PREFIX + "pg"}
    return {
        "run_dir": str(RUN_DIR),
        "mock": MOCK_FROM_HOST,
        "images": IMAGES,
        "gateways": {
            "litellm": {
                "base_url": "http://127.0.0.1:4000/v1",
                "root_url": "http://127.0.0.1:4000",
                "embeddings_url": "http://127.0.0.1:4000/v1",
                "api_key": MASTER_KEY,
                "sinks": [{"type": "logs", "container": PREFIX + "litellm-hardened"},
                          {**pg, "db": "litellm_hardened"}],
                "default_profile": {
                    "base_url": "http://127.0.0.1:4001/v1", "api_key": MASTER_KEY,
                    "chat_model": "fast-model", "cost_model": "gpt-4o-mini",
                    "sinks": [{"type": "logs", "container": PREFIX + "litellm-default"},
                              {**pg, "db": "litellm_default"}],
                },
            },
            "mlflow": {
                "base_url": "http://127.0.0.1:5000/gateway/mlflow/v1",
                "root_url": "http://127.0.0.1:5000",
                "embeddings_url": "http://127.0.0.1:5000/gateway/openai/v1",
                "api_key": "not-needed",
                "sinks": [{"type": "logs", "container": PREFIX + "mlflow"},
                          {"type": "dir", "container": PREFIX + "mlflow", "path": "/data"}],
                "default_profile": {
                    "base_url": "http://127.0.0.1:5000/gateway/mlflow/v1",
                    "api_key": "not-needed",
                    "chat_model": "fast-model-default", "cost_model": "gpt-4o-mini-default",
                    "sinks": [{"type": "logs", "container": PREFIX + "mlflow"},
                              {"type": "dir", "container": PREFIX + "mlflow", "path": "/data"}],
                },
            },
        },
    }


def save_logs() -> None:
    for c in ["mock", "pg", "litellm-hardened", "litellm-default", "mlflow"]:
        out = docker("logs", PREFIX + c, check=False)
        (RUN_DIR / f"{c}.log").write_text((out.stdout or "") + (out.stderr or ""),
                                          encoding="utf-8")


def main() -> int:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log(f"run directory: {RUN_DIR}")
    cleanup()
    try:
        docker("network", "create", NET)

        # Mock provider: runs on the MLflow image only because it already has fastapi/uvicorn.
        run_container("mock", IMAGES["mlflow"],
                      ["-m", "uvicorn", "mock_provider.server:app", "--host", "0.0.0.0",
                       "--port", "9100", "--log-level", "warning"],
                      ports=["9100:9100"], env={"MOCK_LATENCY_MS": "100", "PYTHONPATH": "/app"},
                      mounts=[f"{(ROOT / 'mock_provider').resolve()}:/app/mock_provider:ro"],
                      entrypoint="python")

        run_container("pg", IMAGES["postgres"], [], env={"POSTGRES_PASSWORD": PG_PASSWORD})
        for _ in range(60):
            if docker("exec", PREFIX + "pg", "pg_isready", "-U", "postgres",
                      check=False).returncode == 0:
                break
            time.sleep(1)
        time.sleep(2)
        for db in ("litellm_hardened", "litellm_default"):
            docker("exec", PREFIX + "pg", "psql", "-U", "postgres", "-c", f"CREATE DATABASE {db}")

        start_litellm("hardened", 4000)
        start_litellm("default", 4001)
        start_mlflow()

        wait_http(f"{MOCK_FROM_HOST}/health")
        auth = {"Authorization": f"Bearer {MASTER_KEY}"}
        for port in (4000, 4001):
            wait_http(f"http://127.0.0.1:{port}/health/liveliness", timeout=300, headers=auth)
        wait_http("http://127.0.0.1:5000/health", timeout=300)
        log("all services up; provisioning MLflow endpoints")
        provision_mlflow()

        targets_file = RUN_DIR / "targets.json"
        targets_file.write_text(json.dumps(targets(), indent=2))

        if "--setup-only" in sys.argv:
            keep = int(os.environ.get("KEEP_SECONDS", "600"))
            log(f"setup only; services stay up for {keep}s; targets: {targets_file}")
            time.sleep(keep)
            return 0

        results_file = RUN_DIR / "results.jsonl"
        log("running tests")
        code = subprocess.call(
            [PY, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-rA",
             f"--junitxml={RUN_DIR / 'junit.xml'}"],
            cwd=ROOT, env={**os.environ, "TARGETS_FILE": str(targets_file),
                           "RESULTS_FILE": str(results_file), "MLFLOW_DISABLE_AGENT_HINT": "1"})
        save_logs()
        subprocess.call([PY, str(ROOT / "scripts" / "make_report.py"), str(RUN_DIR)], cwd=ROOT)
        log(f"report: {ROOT / 'results' / 'REPORT.md'}")
        return code
    except Exception:
        save_logs()
        raise
    finally:
        if "--keep" not in sys.argv:
            cleanup()


if __name__ == "__main__":
    sys.exit(main())
