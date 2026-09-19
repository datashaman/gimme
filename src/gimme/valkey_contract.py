"""The fixed, versioned `laravel-cluster-v1` contract between a managed Valkey binding and a
Laravel application.

Gimme never patches application source and accepts no configuration paths: it injects the
protected `GIMME_VALKEY_*` values below and selects the Redis adapter for exactly the declared
uses. The application declares the contract and reads these values in its own cluster-aware
(PhpRedis or Predis) configuration. Ordinary Deployment values cannot override any of them."""

from __future__ import annotations

from gimme.control import (
    VALKEY_ENV_PREFIX,
    SecretReference,
    ValkeyUse,
)
from gimme.resources_valkey import namespace_prefixes

CONTRACT = "laravel-cluster-v1"
USERNAME_KEY = f"{VALKEY_ENV_PREFIX}USERNAME"
PASSWORD_KEY = f"{VALKEY_ENV_PREFIX}PASSWORD"
# Client behavior is fixed, never per Deployment: a bounded retry with exponential backoff and
# jitter, a short timeout, primary reads only, and TLS with certificate and hostname checks.
CLIENT_SETTINGS = {
    "SCHEME": "tls",
    "CLUSTER": "true",
    "READ_REPLICAS": "false",
    "VERIFY_PEER": "true",
    "TIMEOUT_SECONDS": "2",
    "RETRIES": "3",
    "BACKOFF_MS": "100",
    "BACKOFF_CAP_MS": "2000",
    "JITTER": "true",
}
# A use a Deployment did not declare is pinned to a local driver, so the Target-local Redis
# defaults written when the environment file was first created can never be used.
LOCAL_DRIVERS = {"CACHE_STORE": "file", "SESSION_DRIVER": "file", "QUEUE_CONNECTION": "sync"}
ADAPTER_KEYS = {"cache": "CACHE_STORE", "session": "SESSION_DRIVER", "queue": "QUEUE_CONNECTION"}


def contract_variables(
    deployment_name: str, uses: list[ValkeyUse], host: str, port: int
) -> dict[str, str]:
    """Every non-secret value the contract injects, including the adapter selection."""
    prefixes = namespace_prefixes(deployment_name, list(uses))
    values = {
        f"{VALKEY_ENV_PREFIX}CONTRACT": CONTRACT,
        f"{VALKEY_ENV_PREFIX}HOST": host,
        f"{VALKEY_ENV_PREFIX}PORT": str(port),
        f"{VALKEY_ENV_PREFIX}USES": ",".join(uses),
        **{f"{VALKEY_ENV_PREFIX}{name}": value for name, value in CLIENT_SETTINGS.items()},
        **{
            f"{VALKEY_ENV_PREFIX}{name.upper()}_PREFIX": prefix
            for name, prefix in prefixes.items()
        },
    }
    values.update(LOCAL_DRIVERS)
    values.update({ADAPTER_KEYS[use]: "redis" for use in uses})
    if "horizon" in prefixes:
        values["HORIZON_PREFIX"] = prefixes["horizon"]
    return values


def credential_references(
    store: str, resource_name: str, deployment_name: str
) -> dict[str, SecretReference]:
    """The Resource Credential, referenced like any secret so the exact version is planned and
    resolved at apply time. The values are never seen by the control plane's plans or logs."""
    secret = f"{resource_name}/{deployment_name}"
    return {
        USERNAME_KEY: SecretReference(store=store, secret=secret, field="username"),
        PASSWORD_KEY: SecretReference(store=store, secret=secret, field="password"),
    }


def probe_config(
    deployment_name: str, uses: list[ValkeyUse], host: str, port: int, horizon: bool
) -> dict[str, object]:
    """The non-secret input of the fixed activation probe program. `horizon` is whether the
    Deployment runs Horizon, not merely whether it declares `queue`."""
    return {
        "contract": CONTRACT, "host": host, "port": port, "uses": list(uses), "horizon": horizon,
        "prefixes": namespace_prefixes(deployment_name, list(uses)),
        "deployment": deployment_name,
    }


def probe_names(uses: list[ValkeyUse], horizon: bool) -> list[str]:
    """The fixed checks of the activation probe, in the order it runs them and stops at the
    first failure."""
    return [
        "environment", *(["horizon-compatibility"] if horizon else []), "tls", "auth",
        "default-user", "cluster", "read-after-write", "namespace",
        *(f"use-{use}" for use in uses), *(["use-horizon"] if horizon else []), "cleanup",
    ]
