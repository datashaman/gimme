"""Fixed Laravel environment contract for a managed RDS PostgreSQL binding."""

from __future__ import annotations

from gimme.control import SecretReference

TRUST_BUNDLE_PATH = ".gimme/aws-rds-global-bundle.pem"


def contract_variables(
    host: str, port: int, database: str, trust_bundle_path: str
) -> dict[str, str]:
    return {
        "DB_CONNECTION": "pgsql",
        "DB_HOST": host,
        "DB_PORT": str(port),
        "DB_DATABASE": database,
        "DB_SSLMODE": "verify-full",
        "DB_SSLROOTCERT": trust_bundle_path,
    }


def credential_references(
    store: str, resource_name: str, deployment_name: str
) -> dict[str, SecretReference]:
    secret = f"{resource_name}/{deployment_name}"
    return {
        "DB_USERNAME": SecretReference(store=store, secret=secret, field="username"),
        "DB_PASSWORD": SecretReference(store=store, secret=secret, field="password"),
    }
