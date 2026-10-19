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
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

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
BIFROST_RATELIMITED_KEY = "sk-bf-" + secrets.token_hex(16)  # virtual key: 20 requests/minute
BIFROST_SOVEREIGN_KEY = "sk-bf-" + secrets.token_hex(16)  # virtual key: EU providers only
BIFROST_AGENT_KEY = "sk-bf-" + secrets.token_hex(16)  # virtual key: all MCP tools
BIFROST_MCP_SEARCH_KEY = "sk-bf-" + secrets.token_hex(16)  # virtual key: MCP search_files only
MCP_TOKEN = "mcp-secret-" + secrets.token_hex(12)  # the connector's secret (fake MCP server)
MCP_URL = "http://gweval-mcp:9200/mcp"
# Bifrost's Bedrock connector only accepts a TLS host for private endpoints, so the fake Bedrock
# also listens on HTTPS under a DNS-style name, with a certificate generated per run.
BEDROCK_HOST = "bedrock.mock.internal:9443"
BEDROCK_CA_PEM = ""


def make_tls_cert(out_dir: Path) -> str:
    """Self-signed certificate for the fake Bedrock host. Returns the certificate PEM."""
    from datetime import timedelta
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bedrock.mock.internal")])
    now = datetime.utcnow()
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("bedrock.mock.internal")]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    out_dir.mkdir(parents=True, exist_ok=True)
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    (out_dir / "cert.pem").write_text(pem)
    (out_dir / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return pem
# Fairness for the load tests: every gateway container gets the same CPU budget, and the
# Python gateways (LiteLLM, MLflow) run one worker process per CPU. Bifrost (Go) uses its CPUs natively.
GATEWAY_CPUS = 4
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
                  env: dict | None = None, mounts: list[str] = (), entrypoint: str | None = None,
                  cpus: int | None = None, aliases: list[str] = ()):
    cmd = ["run", "-d", "--name", PREFIX + name, "--network", NET,
           "--network-alias", PREFIX + name]
    for a in aliases:
        cmd += ["--network-alias", a]
    if cpus:
        cmd += ["--cpus", str(cpus)]
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


# ---------------------------------------------------------------- gateway configs
GATEWAYS_DIR = ROOT / "gateways"


def render(template: Path, out: Path, **values: str) -> Path:
    """Copy a config from gateways/ into the run folder, filling in ${NAME} placeholders."""
    text = template.read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("${" + k + "}", v)
    left = re.findall(r"\$\{[A-Z_]+\}", text)
    assert not left, f"unfilled placeholder(s) {left} in {template}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


# ---------------------------------------------------------------- LiteLLM
def start_litellm(profile: str, port: int) -> None:
    cfg = render(GATEWAYS_DIR / "litellm" / f"config.{profile}.yaml",
                 RUN_DIR / f"litellm-{profile}" / "config.yaml",
                 MOCK_URL=MOCK_IN_NET, MASTER_KEY=MASTER_KEY, REDIS_HOST=PREFIX + "redis",
                 MCP_URL=MCP_URL, MCP_TOKEN=MCP_TOKEN)
    env = {"DATABASE_URL": f"postgresql://postgres:{PG_PASSWORD}@gweval-pg:5432/litellm_{profile}"}
    if profile == "hardened":
        env["DISABLE_ADMIN_UI"] = "True"
        # Shared state for the 4 workers (rate limits, cooldowns). router_settings alone is not enough.
        env["REDIS_HOST"], env["REDIS_PORT"] = PREFIX + "redis", "6379"
    run_container(f"litellm-{profile}", IMAGES["litellm"],
                  ["--config", "/app/config.yaml", "--port", "4000",
                   "--num_workers", str(GATEWAY_CPUS if profile == "hardened" else 1)],
                  ports=[f"{port}:4000"], env=env, cpus=GATEWAY_CPUS,
                  mounts=[f"{cfg.resolve()}:/app/config.yaml:ro"])


# ---------------------------------------------------------------- Bifrost
def start_bifrost(profile: str, port: int) -> None:
    cfg = render(GATEWAYS_DIR / "bifrost" / f"config.{profile}.json",
                 RUN_DIR / f"bifrost-{profile}" / "config.json",
                 MOCK_URL=MOCK_IN_NET, BIFROST_ADMIN_PASSWORD=BIFROST_ADMIN[1],
                 BIFROST_APP_KEY=BIFROST_APP_KEY, BIFROST_LIMITED_KEY=BIFROST_LIMITED_KEY,
                 BIFROST_RATELIMITED_KEY=BIFROST_RATELIMITED_KEY,
                 BIFROST_SOVEREIGN_KEY=BIFROST_SOVEREIGN_KEY, BIFROST_AGENT_KEY=BIFROST_AGENT_KEY,
                 BIFROST_MCP_SEARCH_KEY=BIFROST_MCP_SEARCH_KEY,
                 BEDROCK_HOST=BEDROCK_HOST,
                 BEDROCK_CA_PEM=json.dumps(BEDROCK_CA_PEM)[1:-1],  # JSON-escaped PEM
                 MCP_URL=MCP_URL, MCP_TOKEN=MCP_TOKEN)
    run_container(f"bifrost-{profile}", IMAGES["bifrost"], [], ports=[f"{port}:8080"],
                  cpus=GATEWAY_CPUS,
                  mounts=[f"{cfg.resolve()}:/app/data/config.json:ro"])


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
            "--workers", str(GATEWAY_CPUS)]
    if login:
        # MLflow's optional login add-on. Needs the "-full" image (mlflow[auth]).
        args += ["--app-name", "basic-auth"]
        env["MLFLOW_AUTH_ADMIN_PASSWORD"] = MLFLOW_ADMIN[1]
        env["MLFLOW_FLASK_SERVER_SECRET_KEY"] = secrets.token_hex(16)
    run_container(name, IMAGES["mlflow_login" if login else "mlflow"], args,
                  ports=[f"{port}:5000"], env=env, cpus=GATEWAY_CPUS, mounts=[f"{PREFIX}{name}-data:/data"])


def provision_mlflow(url: str = "http://127.0.0.1:5000", login: bool = False) -> None:
    """MLflow is configured through its API: see gateways/mlflow/endpoints.yaml."""
    sys.path.insert(0, str(GATEWAYS_DIR / "mlflow"))
    from provision import provision
    provision(url, MOCK_IN_NET, admin=MLFLOW_ADMIN if login else None,
              app_user=MLFLOW_APP_USER if login else None)


# ---------------------------------------------------------------- main
def targets() -> dict:
    pg = {"type": "pg", "container": PREFIX + "pg"}
    return {
        "run_dir": str(RUN_DIR),
        "mock": MOCK_FROM_HOST,
        "mock_tls": "https://127.0.0.1:9443",  # same fake provider over HTTPS (Bifrost's Bedrock)
        "mcp": {"url_from_host": "http://127.0.0.1:9200", "token": MCP_TOKEN},
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
                "ratelimited_key": BIFROST_RATELIMITED_KEY,
                "sovereign_key": BIFROST_SOVEREIGN_KEY,
                "agent_key": BIFROST_AGENT_KEY,
                "mcp_search_key": BIFROST_MCP_SEARCH_KEY,
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
    for c in ["mock", "mock-tls", "mcp", "pg", "redis", "litellm-hardened", "litellm-default", "bifrost-hardened",
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
                       "--port", "9100", "--log-level", "warning",
                       # Cloud providers keep idle connections ~60 s; uvicorn's default is 5 s.
                       "--timeout-keep-alive", "65"],
                      ports=["9100:9100"], env={"MOCK_LATENCY_MS": "100", "PYTHONPATH": "/app"},
                      mounts=[f"{(ROOT / 'mock_provider').resolve()}:/app/mock_provider:ro"],
                      entrypoint="python")
        # Fake MCP server (tool connector tests), same image for the same reason.
        run_container("mcp", IMAGES["mlflow"],
                      ["-m", "uvicorn", "mock_provider.mcp_server:app", "--host", "0.0.0.0",
                       "--port", "9200", "--log-level", "warning", "--timeout-keep-alive", "65"],
                      ports=["9200:9200"], env={"MCP_TOKEN": MCP_TOKEN, "PYTHONPATH": "/app"},
                      mounts=[f"{(ROOT / 'mock_provider').resolve()}:/app/mock_provider:ro"],
                      entrypoint="python")

        global BEDROCK_CA_PEM
        BEDROCK_CA_PEM = make_tls_cert(RUN_DIR / "tls")
        run_container("mock-tls", IMAGES["mlflow"],
                      ["-m", "uvicorn", "mock_provider.server:app", "--host", "0.0.0.0",
                       "--port", "9443", "--log-level", "warning", "--timeout-keep-alive", "65",
                       "--ssl-certfile", "/tls/cert.pem", "--ssl-keyfile", "/tls/key.pem"],
                      ports=["9443:9443"], env={"MOCK_LATENCY_MS": "100", "PYTHONPATH": "/app"},
                      mounts=[f"{(ROOT / 'mock_provider').resolve()}:/app/mock_provider:ro",
                              f"{(RUN_DIR / 'tls').resolve()}:/tls:ro"],
                      entrypoint="python", aliases=["bedrock.mock.internal"])

        run_container("pg", IMAGES["postgres"], [], env={"POSTGRES_PASSWORD": PG_PASSWORD})
        run_container("redis", IMAGES["redis"], [])  # LiteLLM: shared limits across workers
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
        wait_http("http://127.0.0.1:9200/health")
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
