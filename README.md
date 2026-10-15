# LLM Gateway Evaluation: LiteLLM vs Bifrost vs MLflow AI Gateway

A hands-on test lab that runs **LiteLLM Proxy**, **Bifrost** and **MLflow AI Gateway** side by side and checks them against the same requirements with automated tests. The goal is to choose a gateway on evidence, not opinion.

**➡️ Full results: [`results/REPORT.md`](results/REPORT.md)**

## The result in one table

| Requirement | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on |
|---|:-:|:-:|:-:|:-:|
| Chat, streaming, tool calling, embeddings | ✅ | ✅ | ✅ | – |
| Fallback to a backup model, never to a model outside the allowed list | ✅ | ✅ | ✅ | – |
| No-Retention: prompt stored nowhere (with hardened settings) | ✅ | ✅ | ✅ | – |
| Stores prompts with **default** settings | **No** | Yes (request log) | Yes (database + trace files) | – |
| **Cost tracked while No-Retention is on** | ✅ | ✅ | ❌ | – |
| Refuses calls with no key or a wrong key | ✅ | ✅ | ❌ | ✅ |
| A key can be limited to certain models | ✅ | ✅ | ❌ | ✅ |
| Configuration can't be changed without logging in | ✅ | ✅ | ❌ | ✅ |
| The app's own credential can't change the configuration | ✅ | ✅ | ❌ | ❌ |
| Admin UI can be switched off or needs login | ✅ | ✅ | ❌ | ✅ |
| Image pinned by digest, no known malware file | ✅ | ✅ | ✅ | ✅ |
| **Checks passed** | **17 / 17** | **17 / 17** | **10 / 17** | **6 / 7** (security only) |
| Time added per call, 1 / 10 at once (p50) | ~9 / ~53 ms | **~1.6 / ~2.4 ms** | ~12 / ~90 ms | – |

– = not tested (same behaviour as plain MLflow)

### What the findings mean

1. **LiteLLM and Bifrost both meet every requirement tested.** They differ on defaults and speed:
   - **Safe defaults:** LiteLLM doesn't store prompt text even with default settings. Bifrost stores it in its request log until `disable_content_logging` is switched on, so that setting has to be in every deployment.
   - **Speed:** Bifrost (Go) adds ~1–3 ms. LiteLLM (Python) adds ~9 ms one at a time and ~50 ms with 10 at once. This test ran LiteLLM with **one worker process**, while Bifrost uses all CPU cores by default. LiteLLM can run more workers or instances, so the gap shrinks in production. Even so, Bifrost has much more headroom.
   - **What wasn't tested here:** provider coverage (LiteLLM lists 100+ providers, Bifrost about 30, including Bedrock, Azure, vLLM and Ollama), how mature each project is, and which features each keeps behind a paid tier.
2. **MLflow can't do No-Retention and cost tracking together.** It works out cost and budgets from its stored request traces, and those traces contain the prompts. Turning traces off removes both. LiteLLM and Bifrost both keep a usage and cost record without the prompt text.
3. **MLflow's gateway is open by default.** Without its login add-on, anyone who can reach the server can call models, read the configuration, and add provider credentials. The add-on fixes most of this, but it needs the `-full` image, and **any logged-in user can still add provider credentials and destination URLs**.
4. **Small notes:**
   - LiteLLM's `DISABLE_ADMIN_UI` blocks the login, but an empty dashboard page is still served at `/ui`.
   - Bifrost serves its UI page without login, but every admin API needs login.
   - Bifrost refuses a model the key isn't allowed to use with `400` when the provider isn't named, and `403 model_blocked` when it is. Either way the request never reaches the provider.

## How it works

```
                  ┌─────────────────────────── Docker network ────────────────────────────┐
  tests (pytest) ─┼─► LiteLLM Proxy (hardened / default) ─┐                                │
                  │   Bifrost       (hardened / default) ─┼─► fake model provider          │
                  │   MLflow AI Gateway                  ─┤   • fast-model, backup-model … │
                  │   MLflow + login add-on              ─┘   • broken-model (always fails)│
                  │   Postgres (LiteLLM's database)            • records every call        │
                  └────────────────────────────────────────────────────────────────────────┘
```

- **Fake provider** ([`mock_provider/server.py`](mock_provider/server.py)): an OpenAI-compatible stand-in for Bedrock, Azure or an on-prem model server. It's free and predictable, and it records which model really served each request.
- **Same setup for every gateway** ([`scripts/run_eval.py`](scripts/run_eval.py)): the same models, the same fallback rule (`eu-chat`: broken primary → `backup-model`; `outside-model` is never allowed), and the same hardened settings.
- **No-Retention test:** a unique marker phrase goes into the prompt. Then every container log, the database and every data file is searched byte by byte for it.
- **Speed test:** the timing client runs inside the Docker network ([`scripts/latency_probe.py`](scripts/latency_probe.py)) and alternates direct and gateway calls, so host networking doesn't skew the numbers.
- **Report** ([`scripts/make_report.py`](scripts/make_report.py)): built automatically from the test results.

### Settings tested

| Profile | LiteLLM | Bifrost | MLflow |
|---|---|---|---|
| **Hardened** | `turn_off_message_logging`, `store_prompts_in_spend_logs: false`, `DISABLE_ADMIN_UI=True`, master key | `disable_content_logging`, admin login, `enforce_auth_on_inference`, virtual keys | Endpoint `usage_tracking: false`, own encryption passphrase |
| **Default** | Out-of-the-box (master key only) | Out-of-the-box logging (content stored); login and keys on | Out-of-the-box (`usage_tracking: true`) |
| **Login add-on** | – | – | `--app-name basic-auth`, non-admin user with a role allowed to use only some endpoints |

Pinned images ([`images.json`](images.json)):
- LiteLLM: `ghcr.io/berriai/litellm-database:v1.104.0`
- Bifrost: `maximhq/bifrost:v2.2.6`
- MLflow: `ghcr.io/mlflow/mlflow:v3.17.0`, and `v3.17.0-full` for the login add-on

All are pinned by `sha256` digest.

## Run it yourself

Needs Docker and [uv](https://docs.astral.sh/uv/). The images total about 4.5 GB on first pull.

```bash
uv sync
uv run python scripts/run_eval.py
```

The run takes about 5 minutes and ends with a **VERDICT PER GATEWAY** summary (pass count and failed checks for each gateway). It also writes `results/REPORT.md`, plus logs and raw results under `runs/<timestamp>/`.

> Run it through `scripts/run_eval.py`, not `pytest` directly: the script starts the gateways and tells the tests where they are. If you have an older copy, run `git pull` first; Bifrost was added in commit `57926f9`. It also runs on GitHub Actions (**Actions → Evaluate gateways → Run workflow**), and the report appears in the run summary.

The run exits with a non-zero code because MLflow's failures are recorded as failed tests. That's intended: they are findings.

## Limits of this test

- **The provider is fake.** Real Amazon Bedrock, Azure AI Foundry and on-prem servers weren't called, so provider-specific features (prompt caching, guardrails, EU inference profiles, Azure deployments) still need a test with real credentials.
- **Speed was measured on one machine.** LiteLLM ran with one worker, while Bifrost and MLflow used their defaults. Use the numbers to compare the gateways, not to predict production.
- **The No-Retention check covers what each gateway writes itself** (logs, database, data folder). It doesn't cover the provider, or tools you add later such as Langfuse or OpenTelemetry exporters.
- Versions were current as of October 2026. All three projects release often, so re-run the tests after upgrading.
