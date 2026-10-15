"""Start everything in Docker, run the test suite against every gateway, write the report.

    uv run python scripts/run_eval.py              # full run
    uv run python scripts/run_eval.py --setup-only # start + provision, run no tests

Everything runs on one Docker network so both gateways take the same path to the provider.
Host ports (127.0.0.1 only):
  9100  mock provider (fake OpenAI-compatible models)
  4000  LiteLLM Proxy, "hardened" profile (recommended production settings) + Postgres
  4001  LiteLLM Proxy, "default" profile (out-of-the-box settings) + Postgres
  4002  Bifrost, "hardened" profile
  4003  Bifrost, "default" profile
  5000  MLflow server with its AI Gateway (both profiles are endpoints on one server)
  5001  MLflow with its optional login add-on (security tests only)
"""

import base64
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
BIFROST_ADMIN = ("admin", "Bf-" + secrets.token_hex(12))
BIFROST_APP_KEY = "sk-bf-" + secrets.token_hex(16)      # virtual key: all models
BIFROST_LIMITED_KEY = "sk-bf-" + secrets.token_hex(16)  # virtual key: fast-model only
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


# ---------------------------------------------------------------- Bifrost
def bifrost_config(hardened: bool) -> dict:
    """Same models and fallback rule as LiteLLM. Login and keys are on in both profiles."""
    return {
        "auth_config": {"is_enabled": True, "admin_username": BIFROST_ADMIN[0],
                        "admin_password": BIFROST_ADMIN[1]},
        "client": {
            "enable_logging": True,
            # The No-Retention switch. Default (off) stores prompts and responses in logs.db.
            "disable_content_logging": hardened,
            "enforce_auth_on_inference": True,
            "allow_direct_keys": False,
        },
        "providers": {"openai": {
            "keys": [{"name": "mock", "value": "fake-key", "models": ["*"], "weight": 1}],
            "network_config": {"base_url": MOCK_IN_NET, "allow_private_network": True,
                               "max_retries": 0},
        }},
        "governance": {
            "virtual_keys": [
                {"id": "vk-app", "name": "app", "value": BIFROST_APP_KEY, "is_active": True,
                 "provider_configs": [{"provider": "openai", "allowed_models": ["*"],
                                       "weight": 1, "key_ids": ["*"]}]},
                {"id": "vk-limited", "name": "eu-mode", "value": BIFROST_LIMITED_KEY,
                 "is_active": True,
                 "provider_configs": [{"provider": "openai", "allowed_models": ["fast-model"],
                                       "weight": 1, "key_ids": ["*"]}]},
            ],
            # "eu-chat": primary is broken, the backup must take over; outside-model never.
            "routing_rules": [{
                "id": "eu-chat", "name": "eu-chat", "enabled": True,
                "cel_expression": "model == 'eu-chat'", "scope": "global", "priority": 0,
                "targets": [{"provider": "openai", "model": "broken-model", "weight": 1}],
                "fallbacks": ["openai/backup-model"],
            }],
        },
        "config_store": {"enabled": True, "type": "sqlite",
                         "config": {"path": "/app/data/config.db"}},
        "logs_store": {"enabled": True, "type": "sqlite", "config": {"path": "/app/data/logs.db"}},
    }


def start_bifrost(profile: str, port: int) -> None:
    d = RUN_DIR / f"bifrost-{profile}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(bifrost_config(profile == "hardened"), indent=2))
    run_container(f"bifrost-{profile}", IMAGES["bifrost"], [], ports=[f"{port}:8080"],
                  mounts=[f"{(d / 'config.json').resolve()}:/app/data/config.json:ro"])


# ---------------------------------------------------------------- MLflow
MLFLOW_ADMIN = ("admin", "Adm-" + secrets.token_hex(12))
MLFLOW_APP_USER = ("app-user", "App-" + secrets.token_hex(12))


def start_mlflow(name: str = "mlflow", port: int = 5000, login: bool = False) -> None:
    env = {
        "MLFLOW_CRYPTO_KEK_PASSPHRASE": secrets.token_hex(24),
        "MLFLOW_GATEWAY_API_BASE_ALLOW_PRIVATE_IPS": "true",  # provider is on a private network
        "MLFLOW_DISABLE_TELEMETRY": "true",
        # MLflow only accepts https provider URLs by default; the mock is plain http.
        "MLFLOW_GATEWAY_API_BASE_ALLOWED_SCHEMES": "http,https",
    }
    args = ["mlflow", "server", "--host", "0.0.0.0", "--port", "5000",
            "--backend-store-uri", "sqlite:////data/mlflow.db",
            "--default-artifact-root", "/data/artifacts",
            "--allowed-hosts", f"localhost:{port},127.0.0.1:{port},{PREFIX}{name}:5000",
            "--workers", "1"]
    if login:
        # MLflow's optional login add-on. Needs the "-full" image (mlflow[auth]).
        args += ["--app-name", "basic-auth"]
        env["MLFLOW_AUTH_ADMIN_PASSWORD"] = MLFLOW_ADMIN[1]
        env["MLFLOW_FLASK_SERVER_SECRET_KEY"] = secrets.token_hex(16)
    run_container(name, IMAGES["mlflow_login" if login else "mlflow"], args,
                  ports=[f"{port}:5000"], env=env, mounts=[f"{PREFIX}{name}-data:/data"])


def provision_mlflow(url: str = "http://127.0.0.1:5000", login: bool = False) -> None:
    """Create MLflow gateway endpoints equivalent to the LiteLLM config."""
    if login:
        os.environ["MLFLOW_TRACKING_USERNAME"], os.environ["MLFLOW_TRACKING_PASSWORD"] = MLFLOW_ADMIN
    from mlflow.entities.gateway_endpoint import (FallbackConfig, FallbackStrategy,
                                                  GatewayEndpointModelConfig,
                                                  GatewayModelLinkageType)
    from mlflow.tracking._tracking_service.utils import _get_store

    store = _get_store(url)
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

    if login:
        _mlflow_limited_user(url, store)
        del os.environ["MLFLOW_TRACKING_USERNAME"], os.environ["MLFLOW_TRACKING_PASSWORD"]


def _mlflow_limited_user(url: str, store) -> None:
    """A non-admin user that may USE every endpoint except outside-model (like a per-mode key)."""
    def post(path, body):
        r = httpx.post(url + path, json=body, auth=MLFLOW_ADMIN, timeout=30)
        r.raise_for_status()
        return r.json()

    post("/api/2.0/mlflow/users/create",
         {"username": MLFLOW_APP_USER[0], "password": MLFLOW_APP_USER[1]})
    role_id = post("/api/3.0/mlflow/roles/create",
                   {"name": "eu-mode", "workspace": "default"})["role"]["id"]
    for name in ["fast-model", "smart-model", "gpt-4o-mini", "embed-model", "eu-chat"]:
        ep = store.get_gateway_endpoint(name=name)
        post("/api/3.0/mlflow/roles/permissions/add",
             {"role_id": role_id, "resource_type": "gateway_endpoint",
              "resource_pattern": ep.endpoint_id, "permission": "USE"})
    post("/api/3.0/mlflow/roles/assign", {"username": MLFLOW_APP_USER[0], "role_id": role_id})


# ---------------------------------------------------------------- main
def targets() -> dict:
    pg = {"type": "pg", "container": PREFIX + "pg"}
    return {
        "run_dir": str(RUN_DIR),
        "mock": MOCK_FROM_HOST,
        "images": IMAGES,
        # Only used by the security tests: MLflow with its optional login add-on switched on.
        "security_only": {
            "mlflow_login": {
                "base_url": "http://127.0.0.1:5001/gateway/mlflow/v1",
                "root_url": "http://127.0.0.1:5001",
                "auth_header": "Basic " + base64.b64encode(
                    f"{MLFLOW_APP_USER[0]}:{MLFLOW_APP_USER[1]}".encode()).decode(),
                "sinks": [{"type": "logs", "container": PREFIX + "mlflow-login"}],
            },
        },
        "gateways": {
            "litellm": {
                "base_url": "http://127.0.0.1:4000/v1",
                "root_url": "http://127.0.0.1:4000",
                "embeddings_url": "http://127.0.0.1:4000/v1",
                "api_key": MASTER_KEY,
                "auth_header": f"Bearer {MASTER_KEY}",
                "sinks": [{"type": "logs", "container": PREFIX + "litellm-hardened"},
                          {**pg, "db": "litellm_hardened"}],
                "default_profile": {
                    "base_url": "http://127.0.0.1:4001/v1", "api_key": MASTER_KEY,
                    "chat_model": "fast-model", "cost_model": "gpt-4o-mini",
                    "sinks": [{"type": "logs", "container": PREFIX + "litellm-default"},
                              {**pg, "db": "litellm_default"}],
                },
            },
            "bifrost": {
                "base_url": "http://127.0.0.1:4002/v1",
                "root_url": "http://127.0.0.1:4002",
                "embeddings_url": "http://127.0.0.1:4002/v1",
                "api_key": BIFROST_APP_KEY,
                "auth_header": f"Bearer {BIFROST_APP_KEY}",
                "limited_key": BIFROST_LIMITED_KEY,
                "admin": list(BIFROST_ADMIN),
                "sinks": [{"type": "logs", "container": PREFIX + "bifrost-hardened"},
                          {"type": "dir", "container": PREFIX + "bifrost-hardened",
                           "path": "/app/data", "scanner": "grep"}],
                "default_profile": {
                    "base_url": "http://127.0.0.1:4003/v1", "api_key": BIFROST_APP_KEY,
                    "root_url": "http://127.0.0.1:4003", "admin": list(BIFROST_ADMIN),
                    "chat_model": "fast-model", "cost_model": "gpt-4o-mini",
                    "sinks": [{"type": "logs", "container": PREFIX + "bifrost-default"},
                              {"type": "dir", "container": PREFIX + "bifrost-default",
                               "path": "/app/data", "scanner": "grep"}],
                },
            },
            "mlflow": {
                "base_url": "http://127.0.0.1:5000/gateway/mlflow/v1",
                "root_url": "http://127.0.0.1:5000",
                "embeddings_url": "http://127.0.0.1:5000/gateway/openai/v1",
                "api_key": "not-needed",
                "auth_header": None,
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
    for c in ["mock", "pg", "litellm-hardened", "litellm-default", "bifrost-hardened",
              "bifrost-default", "mlflow", "mlflow-login"]:
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
        # Postgres restarts once during first-time init, so retry until it accepts commands.
        for db in ("litellm_hardened", "litellm_default"):
            for attempt in range(30):
                if docker("exec", PREFIX + "pg", "psql", "-U", "postgres", "-c",
                          f"CREATE DATABASE {db}", check=False).returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f"could not create database {db}")

        start_litellm("hardened", 4000)
        start_litellm("default", 4001)
        start_bifrost("hardened", 4002)
        start_bifrost("default", 4003)
        start_mlflow()
        start_mlflow("mlflow-login", 5001, login=True)

        wait_http(f"{MOCK_FROM_HOST}/health")
        auth = {"Authorization": f"Bearer {MASTER_KEY}"}
        for port in (4000, 4001):
            wait_http(f"http://127.0.0.1:{port}/health/liveliness", timeout=300, headers=auth)
        for port in (4002, 4003):
            wait_http(f"http://127.0.0.1:{port}/health", timeout=300)
        wait_http("http://127.0.0.1:5000/health", timeout=300)
        wait_http("http://127.0.0.1:5001/health", timeout=300)
        log("all services up; provisioning MLflow endpoints")
        provision_mlflow()
        provision_mlflow("http://127.0.0.1:5001", login=True)

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
