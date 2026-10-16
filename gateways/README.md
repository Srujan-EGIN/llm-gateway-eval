# Gateways under test

Each folder holds the real configuration used in the tests. `scripts/run_eval.py` copies it into the run folder and fills in the `${...}` placeholders (random keys and passwords each run, plus the fake provider's address).

| Gateway | Folder | How it's configured | Profiles | Port |
|---|---|---|---|---|
| **LiteLLM Proxy** `v1.104.0` | [`litellm/`](litellm) | YAML config file, plus Postgres for keys and usage | [hardened](litellm/config.hardened.yaml) · [default](litellm/config.default.yaml) | 4000 · 4001 |
| **Bifrost** `v2.2.6` | [`bifrost/`](bifrost) | JSON config file, plus built-in SQLite for logs and config | [hardened](bifrost/config.hardened.json) · [default](bifrost/config.default.json) | 4002 · 4003 |
| **MLflow AI Gateway** `v3.17.0` | [`mlflow/`](mlflow) | API calls at start-up (MLflow has no gateway config file): [endpoints](mlflow/endpoints.yaml), [script](mlflow/provision.py) | hardened and default endpoints on one server | 5000 |
| **MLflow + login add-on** `v3.17.0-full` | [`mlflow/`](mlflow) | Same endpoints, plus `--app-name basic-auth` and a limited user | security tests only | 5001 |

All gateways get the **same setup**:

| Name the app uses | What it does |
|---|---|
| `fast-model`, `smart-model`, `gpt-4o-mini`, `embed-model` | Normal models (`gpt-4o-mini` lets each gateway use its own price list) |
| `eu-chat` | Primary `broken-model` always fails, so the gateway must fall back to `backup-model` |
| `outside-model` | Exists, but must never be used as a fallback, and the limited key may not call it |
| app key + limited key | One key for every model, one limited to `fast-model` (the "one key per compliance mode" idea) |

**The hardened profile differs from the default only in privacy settings:**
- LiteLLM: `turn_off_message_logging`, `store_prompts_in_spend_logs: false`, `DISABLE_ADMIN_UI`
- Bifrost: `disable_content_logging: true`
- MLflow: `usage_tracking: false` on the endpoint

**Same resources for the load tests:** every gateway container is capped at 4 CPUs; LiteLLM (hardened) and MLflow run 4 worker processes; LiteLLM also gets Redis (via `REDIS_HOST`/`REDIS_PORT`) so rate limits are shared across workers. Bifrost has a 20 requests/minute virtual key (`rate-limited`) for the rate-limit test.

Images are pinned by digest in [`../images.json`](../images.json).
