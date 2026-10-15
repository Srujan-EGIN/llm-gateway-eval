# LLM Gateway Evaluation: LiteLLM vs MLflow AI Gateway

A hands-on test lab that runs **LiteLLM Proxy** and **MLflow AI Gateway** side by side and checks both against the same requirements with automated tests. The goal is to choose a gateway on evidence, not opinion.

**➡️ Full results: [`results/REPORT.md`](results/REPORT.md)**

## The result in one table

| Requirement | LiteLLM Proxy | MLflow AI Gateway | MLflow + login add-on |
|---|:-:|:-:|:-:|
| Chat, streaming, tool calling, embeddings | ✅ | ✅ | – |
| Fallback to a backup model, never to a model outside the allowed list | ✅ | ✅ | – |
| No-Retention: prompt stored nowhere (with hardened settings) | ✅ | ✅ | – |
| Stores prompts with **default** settings | No | **Yes** (database + trace files) | – |
| **Cost tracked while No-Retention is on** | ✅ | ❌ | – |
| Refuses calls with no key or a wrong key | ✅ | ❌ | ✅ |
| A key can be limited to certain models | ✅ | ❌ | ✅ |
| Configuration can't be changed without logging in | ✅ | ❌ | ✅ |
| The app's own credential can't change the configuration | ✅ | ❌ | ❌ |
| Admin UI can be switched off or needs login | ✅ | ❌ | ✅ |
| Image pinned by digest, no known malware file | ✅ | ✅ | ✅ |
| **Checks passed** | **17 / 17** | **10 / 17** | **6 / 7** (security only) |
| Time added per call, 1 / 10 at once (p50, across runs) | ~9–10 / ~50–55 ms | ~12–13 / ~80–90 ms | – |

– = not tested (same behaviour as plain MLflow)

### What the findings mean

1. **MLflow can't do No-Retention and cost tracking together.** It works out cost and budgets from its stored request traces, and those traces contain the prompts. Turning traces off removes the stored prompts and the cost data together. LiteLLM keeps a usage and cost row for each call without the prompt text, so it can do both.
2. **MLflow's gateway is open by default.** Without its login add-on, anyone who can reach the server can call models, read the configuration, and add provider credentials. The add-on fixes most of this, but it isn't in the standard image (it needs the `-full` image), and **any logged-in user can still add provider credentials and destination URLs**.
3. **Both gateways' basic features and fallback work correctly.** Neither ever fell back to a model outside the allowed list.
4. **LiteLLM's `DISABLE_ADMIN_UI` blocks the login**, but an empty dashboard page is still served at `/ui`. That's minor, but worth knowing.

## How it works

```
                   ┌─────────────────────────── Docker network ───────────────────────────┐
  tests (pytest) ──┼──► LiteLLM Proxy (hardened)  ─┐                                       │
                   │    LiteLLM Proxy (default)   ─┼──► fake model provider (mock_provider) │
                   │    MLflow AI Gateway         ─┤    • fast-model, backup-model …        │
                   │    MLflow + login add-on     ─┘    • broken-model (always fails)       │
                   │    Postgres (LiteLLM's database)    • records every call               │
                   └───────────────────────────────────────────────────────────────────────┘
```

- **Fake provider** ([`mock_provider/server.py`](mock_provider/server.py)): an OpenAI-compatible stand-in for Bedrock and other providers. It's free and predictable, and it records which model really served each request.
- **Same setup for both gateways** ([`scripts/run_eval.py`](scripts/run_eval.py)): the same models, the same fallback rule (`eu-chat`: broken primary → `backup-model`; `outside-model` is never allowed), and the same hardened settings.
- **No-Retention test:** a unique marker phrase goes into the prompt. Then every container log, the Postgres database and every data file is searched byte by byte for it.
- **Speed test:** the timing client runs inside the Docker network ([`scripts/latency_probe.py`](scripts/latency_probe.py)) and alternates direct and gateway calls, so host networking doesn't skew the numbers.
- **Report** ([`scripts/make_report.py`](scripts/make_report.py)): built automatically from the test results.

### Settings tested

| Profile | LiteLLM | MLflow |
|---|---|---|
| **Hardened** | `turn_off_message_logging: true`, `store_prompts_in_spend_logs: false`, `DISABLE_ADMIN_UI=True`, master key | Endpoint `usage_tracking: false`, own encryption passphrase |
| **Default** | Out-of-the-box (master key only) | Out-of-the-box (`usage_tracking: true`) |
| **Login add-on** | – | `--app-name basic-auth`, non-admin user with a role allowed to use only some endpoints |

Pinned images ([`images.json`](images.json)):
- LiteLLM: `ghcr.io/berriai/litellm-database:v1.104.0`
- MLflow: `ghcr.io/mlflow/mlflow:v3.17.0`, and `v3.17.0-full` for the login add-on

All are pinned by `sha256` digest.

## Run it yourself

Needs Docker and [uv](https://docs.astral.sh/uv/). The images total about 4 GB on first pull.

```bash
uv sync
uv run python scripts/run_eval.py
```

This writes `results/REPORT.md`, plus logs and raw results under `runs/<timestamp>/`. It also runs on GitHub Actions (**Actions → Evaluate gateways → Run workflow**), and the report appears in the run summary.

The run exits with a non-zero code because MLflow's failures are recorded as failed tests. That's intended: they are findings.

## Limits of this test

- **The provider is fake.** Real Amazon Bedrock wasn't called, so Bedrock-specific features (prompt caching, guardrails, EU inference profiles) still need a test with real AWS credentials.
- **Speed was measured on one machine, with one worker per gateway.** Use it to compare the two gateways, not to predict production numbers.
- **The No-Retention check covers what each gateway writes itself** (logs, database, data folder). It doesn't cover the provider, or tools you add later such as Langfuse or OpenTelemetry exporters.
- Versions were current as of October 2026. Both projects release often, so re-run the tests after upgrading.
