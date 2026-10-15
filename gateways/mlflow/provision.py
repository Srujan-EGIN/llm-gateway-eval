"""Create MLflow AI Gateway endpoints from gateways/mlflow/endpoints.yaml.

MLflow's gateway is configured through its API (there is no config file), so this script is
MLflow's equivalent of the LiteLLM and Bifrost config files. Called by scripts/run_eval.py.
"""

import os
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).resolve().parent


def provision(url: str, mock_url: str, admin: tuple[str, str] | None = None,
              app_user: tuple[str, str] | None = None) -> None:
    """Create the endpoints. With `admin`, log in first and also create the limited user."""
    spec = yaml.safe_load((HERE / "endpoints.yaml").read_text(encoding="utf-8")
                          .replace("${MOCK_URL}", mock_url))
    if admin:
        os.environ["MLFLOW_TRACKING_USERNAME"], os.environ["MLFLOW_TRACKING_PASSWORD"] = admin
    try:
        from mlflow.entities.gateway_endpoint import (FallbackConfig, FallbackStrategy,
                                                      GatewayEndpointModelConfig,
                                                      GatewayModelLinkageType)
        from mlflow.tracking._tracking_service.utils import _get_store

        store = _get_store(url)
        secret = store.create_gateway_secret(
            secret_name="mock-key", secret_value={"api_key": "fake-key"},
            provider=spec["provider"]["name"],
            auth_config={"api_base": spec["provider"]["api_base"]})
        defs = {m: store.create_gateway_model_definition(
                    name=f"def-{m}", secret_id=secret.secret_id,
                    provider=spec["provider"]["name"], model_name=m).model_definition_id
                for m in spec["models"]}

        for ep in spec["endpoints"]:
            configs = [GatewayEndpointModelConfig(model_definition_id=defs[ep["model"]],
                                                  linkage_type=GatewayModelLinkageType.PRIMARY)]
            kw = {}
            if ep.get("fallback"):
                configs.append(GatewayEndpointModelConfig(
                    model_definition_id=defs[ep["fallback"]],
                    linkage_type=GatewayModelLinkageType.FALLBACK, fallback_order=0))
                kw["fallback_config"] = FallbackConfig(strategy=FallbackStrategy.SEQUENTIAL,
                                                       max_attempts=2)
            store.create_gateway_endpoint(name=ep["name"], model_configs=configs,
                                          usage_tracking=ep["usage_tracking"], **kw)

        if admin and app_user:
            _limited_user(url, store, admin, app_user, spec["limited_user"])
    finally:
        if admin:
            del os.environ["MLFLOW_TRACKING_USERNAME"], os.environ["MLFLOW_TRACKING_PASSWORD"]


def _limited_user(url, store, admin, app_user, spec) -> None:
    """A non-admin user whose role may USE only the listed endpoints (like a per-mode key)."""
    def post(path, body):
        r = httpx.post(url + path, json=body, auth=admin, timeout=30)
        r.raise_for_status()
        return r.json()

    post("/api/2.0/mlflow/users/create", {"username": app_user[0], "password": app_user[1]})
    role_id = post("/api/3.0/mlflow/roles/create",
                   {"name": spec["role"], "workspace": "default"})["role"]["id"]
    for name in spec["may_use"]:
        post("/api/3.0/mlflow/roles/permissions/add",
             {"role_id": role_id, "resource_type": "gateway_endpoint",
              "resource_pattern": store.get_gateway_endpoint(name=name).endpoint_id,
              "permission": "USE"})
    post("/api/3.0/mlflow/roles/assign", {"username": app_user[0], "role_id": role_id})
