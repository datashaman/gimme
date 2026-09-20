from __future__ import annotations

import base64
import json
import re
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable

from gimme.artifact_store_orchestration import (
    artifact_auth_references,
    target_store_policy,
)
from gimme.secrets import (
    plan_secret_references,
    protected_secret_file,
    resolve_planned_secret_references,
)


SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION_ID = re.compile(r"^[A-Za-z0-9._+=/-]{1,1024}$")


def _result(output: str) -> dict[str, object]:
    prefix = "GIMME_ARTIFACT_RESULT|"
    lines = [line.split("] ", 1)[-1].strip() for line in output.splitlines()]
    matches = [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]
    if len(matches) != 1 or len(matches[0]) > 22_000:
        raise RuntimeError("artifact_result_missing")
    try:
        value = json.loads(base64.b64decode(matches[0], validate=True))
    except (ValueError, json.JSONDecodeError):
        raise RuntimeError("artifact_result_invalid") from None
    if not isinstance(value, dict):
        raise RuntimeError("artifact_result_invalid")
    return value


def _store_policy(definition) -> dict[str, object]:
    value = target_store_policy("unused", definition)
    del value["name"]
    return value


@dataclass(frozen=True)
class ArtifactDeploymentOrchestrator:
    store: Any
    runner: Any
    build_orchestrator: Any
    legacy_server: Callable[[Any], Any]

    def _reader_credentials(self, state, definition):
        references = artifact_auth_references(definition.reader_auth)
        if references is None:
            return [], {}
        planned = plan_secret_references(state, self.store.secrets_path, references)
        resolved = resolve_planned_secret_references(
            state, self.store.secrets_path, references, planned
        )
        return planned, resolved

    def context(self, name: str) -> dict[str, object]:
        expected = self.build_orchestrator.expected_build(name)
        state = expected["state"]
        deployment = expected["deployment"]
        if deployment.release_mode != "artifact":
            raise ValueError("artifact deployment requires artifact release mode")
        target = state.targets[deployment.target]
        definition = expected["definition"]
        planned, credentials = self._reader_credentials(state, definition)
        request = {
            "operation": "resolve",
            "application": deployment.application,
            "build_id": expected["build_id"],
            "store": _store_policy(definition),
        }
        document = {"store": credentials, "build": {}}
        secret_context = (
            protected_secret_file(document) if credentials else nullcontext(None)
        )
        with secret_context as credential_file:
            result = self.runner.run(
                "gimme:artifact:run",
                self.legacy_server(target),
                stack=target.stack,
                artifact_request=request,
                artifact_secret_file=credential_file,
                timeout=3600,
            )
        artifact = _result(result.output)
        common = {"status", "application", "build_id"}
        if artifact.get("status") == "missing":
            if (
                set(artifact) != common
                or artifact.get("application") != deployment.application
                or artifact.get("build_id") != expected["build_id"]
            ):
                raise RuntimeError("artifact_resolution_invalid")
        else:
            expected_fields = common | {
                "commit", "schema_version", "format", "artifact_digest", "tree_digest",
                "bytes", "package_version", "manifest_version", "build_secrets_used",
                "build_secret_count",
            }
            if (
                set(artifact) != expected_fields
                or artifact.get("status") != "ready"
                or artifact.get("application") != deployment.application
                or artifact.get("build_id") != expected["build_id"]
                or artifact.get("commit") != expected["identity"]["commit"]
                or artifact.get("schema_version") != 2
                or artifact.get("format") != "laravel_v1"
                or any(
                    not isinstance(artifact.get(field), str)
                    or SHA256.fullmatch(artifact[field]) is None
                    for field in ("artifact_digest", "tree_digest")
                )
                or any(
                    not isinstance(artifact.get(field), str)
                    or VERSION_ID.fullmatch(artifact[field]) is None
                    for field in ("package_version", "manifest_version")
                )
                or not isinstance(artifact.get("bytes"), int)
                or not 0 < artifact["bytes"] <= 512 * 1024 * 1024
                or not isinstance(artifact.get("build_secrets_used"), bool)
                or not isinstance(artifact.get("build_secret_count"), int)
                or not 0 <= artifact["build_secret_count"] <= 32
                or artifact["build_secrets_used"]
                != (artifact["build_secret_count"] > 0)
            ):
                raise RuntimeError("artifact_resolution_invalid")
        return {
            **expected,
            "destination_target": target,
            "reader_credential_versions": planned,
            "reader_credentials": credentials,
            "artifact": artifact,
        }

    def materialize_request(self, context: dict[str, object]) -> dict[str, object]:
        artifact = context["artifact"]
        definition = context["definition"]
        return {
            "operation": "materialize",
            "artifact": artifact,
            "store": _store_policy(definition),
        }

    def apply_arguments(self, context: dict[str, object]):
        credentials = context["reader_credentials"]
        document = {"store": credentials, "build": {}}
        secret_context = (
            protected_secret_file(document) if credentials else nullcontext(None)
        )
        return self.materialize_request(context), secret_context
