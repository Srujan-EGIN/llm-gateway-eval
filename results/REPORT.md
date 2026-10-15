# Results: LiteLLM Proxy vs Bifrost vs MLflow AI Gateway

Generated 2026-10-15 14:58 by `scripts/run_eval.py`. Every row is an automated test in [`tests/`](../tests).

| | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| **Checks passed** | **17 / 17** | **17 / 17** | **10 / 17** | **6 / 7** |
| Image | `ghcr.io/berriai/litellm-database:v1.104.0` | `maximhq/bifrost:v2.2.6` | `ghcr.io/mlflow/mlflow:v3.17.0` | `ghcr.io/mlflow/mlflow:v3.17.0-full` |

✅ passed  ❌ failed  ℹ️ measured, not graded  – not tested

¹ MLflow with `--app-name basic-auth` (needs the `-full` image). Tested for **security checks only**; every other behaviour is the same as plain MLflow.

## Basic features

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Chat completion works | ✅ tokens reported: 7 | ✅ tokens reported: 7 | ✅ tokens reported: 7 | – |
| Streaming works | ✅ chunks: 4 | ✅ chunks: 4 | ✅ chunks: 4 | – |
| Tool calling works | ✅ tool called: get_weather | ✅ tool called: get_weather | ✅ tool called: get_weather | – |
| Embeddings work | ✅ vectors: 2 | ✅ vectors: 2 | ✅ vectors: 2 | – |
| Unknown model names are refused | ✅ status: 400 | ✅ status: 400 | ✅ status: 404 | – |

## Fallback

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Falls back to the backup when the main model fails | ✅ provider calls, in order: broken-model -> backup-model | ✅ provider calls, in order: broken-model -> backup-model | ✅ provider calls, in order: broken-model -> backup-model | – |
| Fallback never uses a model outside the allowed list | ✅ models called over 10 requests: backup-model, broken-model | ✅ models called over 10 requests: backup-model, broken-model | ✅ models called over 10 requests: backup-model, broken-model | – |

## No-Retention and cost

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Hardened settings: prompt text is stored nowhere | ✅ prompt found in: nowhere | ✅ prompt found in: nowhere | ✅ prompt found in: nowhere | – |
| Default settings: is prompt text stored? (informational) | ℹ️ stores prompts by default: no; prompt found in: nowhere | ℹ️ stores prompts by default: yes; prompt found in: file /app/data/logs.db-wal | ℹ️ stores prompts by default: yes; prompt found in: file /data/mlflow.db; 2 trace files | – |
| Default settings: cost is tracked per call | ✅ cost of one call (USD): 3.15e-06 | ✅ cost of one call (USD): 3.15e-06 | ✅ cost of one call (USD): 3.15e-06 | – |
| Cost is still tracked while No-Retention settings are on | ✅ cost of one call (USD): 3.15e-06; usage row in database: {'model': 'openai/gpt-4o-mini', 'spend': 3.15e-06, 'total_tokens': 6}; prompt stored in that row: False | ✅ cost of one call (USD): 3.15e-06 | ❌ cost of one call (USD): None; AssertionError: no cost available: cost tracking depends on stored request traces | – |

## Security

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Calls without a key are refused | ✅ status without key: 401 | ✅ status without key: 401 | ❌ status without key: 200; assert 200 in (401, 403) | ✅ status without key: 401 |
| Calls with a wrong key are refused | ✅ status with wrong key: 401 | ✅ status with wrong key: 401 | ❌ status with wrong key: 200; assert 200 in (401, 403) | ✅ status with wrong key: 401 |
| A key can be limited to certain models (e.g. one key per compliance mode) | ✅ how: virtual key limited to fast-model; allowed model: 200; other model: 403; other model reached the provider: False | ✅ how: virtual key limited to fast-model (from config); allowed model: 200; other model: 400; other model reached the provider: False | ❌ Failed: no keys or users without the login add-on, so no per-model limits | ✅ how: user with a role that may USE every endpoint except outside-model; allowed model: 200; other model: 403; other model reached the provider: False |
| Gateway configuration can't be read or changed without logging in | ✅ read model config without key: 401 | ✅ list providers without login: 401; add a provider without login: 401 | ❌ list endpoints without login: 200; create a provider secret without login: 200; assert (200 in (401, 403)) | ✅ list endpoints without login: 401; create a provider secret without login: 401 |
| The app's own (non-admin) credential can't change gateway configuration | ✅ add a model with the app key: 403; create a new key with the app key: 401 | ✅ add a provider with the app's virtual key: 401; create a virtual key with the app's virtual key: 401 | ❌ Failed: there is no separate app credential: anyone can change the configuration | ❌ create a provider secret as the non-admin app user: 200; AssertionError: any logged-in user can add provider credentials and destinations |
| The admin UI can be switched off or requires login | ✅ login route says 'Admin UI is Disabled': True; empty dashboard page still served at /ui (no login possible): True | ✅ admin API without login: 401; empty UI page still served at / (login required for data): True | ❌ UI status without login: 200; AssertionError: the MLflow UI (including gateway settings) is open | ✅ UI status without login: 401 |
| Runs from an image pinned by digest, with no known malware startup file | ✅ image: ghcr.io/berriai/litellm-database:v1.104.0; litellm_init.pth (March 2026 malware) present: False | ✅ image: maximhq/bifrost:v2.2.6; Python startup files: n/a (Go binary) | ✅ image: ghcr.io/mlflow/mlflow:v3.17.0; litellm_init.pth (March 2026 malware) present: False | ✅ image: ghcr.io/mlflow/mlflow:v3.17.0-full; litellm_init.pth (March 2026 malware) present: False |

## Speed

| Check | LiteLLM Proxy | Bifrost | MLflow AI Gateway | MLflow + login add-on¹ |
|---|---|---|---|---|
| Gateway overhead per call (informational) | ℹ️ one at a time: added p50 (ms): 9.3; one at a time: added p95 (ms): 10.5; 10 at once: added p50 (ms): 53.3; 10 at once: added p95 (ms): 75.1; gateway's own overhead header, p50 (ms): 4.3 | ℹ️ one at a time: added p50 (ms): 1.6; one at a time: added p95 (ms): 1.7; 10 at once: added p50 (ms): 2.4; 10 at once: added p95 (ms): 3.0 | ℹ️ one at a time: added p50 (ms): 11.9; one at a time: added p95 (ms): 14.3; 10 at once: added p50 (ms): 91.6; 10 at once: added p95 (ms): 182.9; gateway's own overhead header, p50 (ms): 9.0 | – |

## How to read this

- **Hardened** means each gateway's recommended production settings: LiteLLM with `turn_off_message_logging`, Bifrost with `disable_content_logging`, MLflow endpoints with usage tracking (request traces) off. **Default** means the out-of-the-box settings.
- The provider is a **fake model** (`mock_provider/`) that answers in ~100 ms, so tests are free and repeatable. Real Bedrock was not called in this run.
- Speed numbers come from one machine with everything in Docker. Compare the two gateways with each other, not with other benchmarks.
