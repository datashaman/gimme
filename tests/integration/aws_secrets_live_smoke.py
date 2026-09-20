"""Opt-in AWS Secrets Manager smoke test through the normal Deployment apply path.

The operator supplies an isolated, already-registered state directory. This program reads
AWS metadata and one exact secret version, then activates the Deployment on its Target. It
never creates, edits, tags, rotates, or deletes AWS objects.
"""

from __future__ import annotations

import json
import os
import re

from gimme.control import AWSSecretsManagerStore
import gimme.server as gimme


def main() -> None:
    if os.environ.get("GIMME_AWS_SECRETS_LIVE") != "1":
        raise SystemExit("set GIMME_AWS_SECRETS_LIVE=1 to authorize the opt-in smoke test")
    deployment_name = os.environ.get("GIMME_AWS_SECRETS_DEPLOYMENT", "")
    if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", deployment_name) is None:
        raise SystemExit("GIMME_AWS_SECRETS_DEPLOYMENT must be a registered bounded name")

    state = gimme.store.load()
    deployment = state.deployments.get(deployment_name)
    if deployment is None:
        raise SystemExit("the requested Deployment is not registered")
    private_identities = [
        value
        for reference in deployment.secrets.values()
        if isinstance(state.secret_stores.get(reference.store), AWSSecretsManagerStore)
        for value in (reference.secret, reference.field)
    ]
    if not private_identities:
        raise SystemExit("the Deployment has no AWS Secrets Manager reference to verify")

    plan = gimme.plan_deployment_resources(deployment_name)
    if not plan["ready"] or any(
        item["status"] == "missing" for item in plan["secret_versions"]
    ):
        raise RuntimeError("deployment secret plan is not ready")
    applied = gimme.apply_deployment_resources(deployment_name, str(plan["plan_id"]))
    current = gimme.plan_deployment_resources(deployment_name)
    if any(item["status"] != "current" for item in current["secret_versions"]):
        raise RuntimeError("applied secret manifest did not become current")

    public_text = json.dumps(
        {
            "plan": plan,
            "applied": applied,
            "current": current,
            "operations": gimme.list_operations(
                operation="deployment_resources", subject=deployment_name
            ),
        },
        sort_keys=True,
        default=str,
    )
    journal_text = (gimme.store.root / "operations.jsonl").read_text()
    if any(value in public_text or value in journal_text for value in private_identities):
        raise RuntimeError("a private AWS secret reference crossed a public surface")
    if set(applied) - {"changed", "deployment", "correlation_id"}:
        raise RuntimeError("deployment resource apply returned an unexpected field")
    print("AWS Secrets Manager protected activation smoke test passed")


if __name__ == "__main__":
    main()
