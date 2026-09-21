from __future__ import annotations

import base64
import hashlib
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
from gimme.execution import execution_fingerprint


SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION_ID = re.compile(r"^[A-Za-z0-9._+=/-]{1,1024}$")


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def release_contract(deployment, application) -> dict[str, str]:
    primary = application.default_health if deployment.health == "inherit" else deployment.health
    health = [
        *([primary] if primary is not None else []),
        *application.health_probes,
        *deployment.health_probes,
    ]
    processes = {
        "workers": (
            None if deployment.workers is None
            else deployment.workers.model_dump(mode="json")
        ),
        "scheduler": (
            None if deployment.scheduler is None
            else deployment.scheduler.model_dump(mode="json")
        ),
    }
    return {
        "schema": "laravel_release_v1",
        "health_sha256": _digest([probe.model_dump(mode="json") for probe in health]),
        "processes_sha256": _digest(processes),
    }


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
    run_deployment: Callable[..., Any] | None = None

    def _run(self, name: str, target, request, credential_file, timeout: int):
        if self.run_deployment is not None:
            return self.run_deployment(
                "gimme:artifact:run",
                name,
                artifact_request=request,
                artifact_secret_file=credential_file,
                timeout=timeout,
            )
        return self.runner.run(
            "gimme:artifact:run",
            self.legacy_server(target),
            stack=target.stack,
            artifact_request=request,
            artifact_secret_file=credential_file,
            timeout=timeout,
        )

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
        return self.resolve_expected(name, expected)

    def resolve_expected(
        self, name: str, expected: dict[str, object]
    ) -> dict[str, object]:
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
            result = self._run(name, target, request, credential_file, 3600)
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

    def expected_from_release(
        self, name: str, metadata: dict[str, object]
    ) -> dict[str, object]:
        state = self.store.load()
        deployment = state.deployments[name]
        application = state.applications[deployment.application]
        build = application.build
        if deployment.release_mode != "artifact" or build is None:
            raise ValueError("artifact promotion requires artifact release mode")
        seed = metadata.get("promotion_seed")
        commit = metadata.get("commit")
        if (
            metadata.get("application") != deployment.application
            or not isinstance(seed, dict)
            or set(seed) != {
                "repository_fingerprint", "composer_lock_sha256", "composer_lock_bytes",
                "frontend_lock", "capability",
            }
            or not isinstance(commit, str)
        ):
            raise RuntimeError("artifact_release_metadata_invalid")
        identity = {
            "application": deployment.application,
            **seed,
            "commit": commit,
            "build_policy": {
                "target": build.target,
                "artifact_store": build.artifact_store,
                "packaging": build.packaging,
                "build_secrets_used": bool(build.secrets),
                "build_secret_count": len(build.secrets),
                "build_secret_names": sorted(build.secrets),
            },
            "runtimes": {
                runtime: pin.model_dump(mode="json")
                for runtime, pin in sorted(deployment.runtimes.items())
            },
            "php_extensions": application.php_extensions,
            "frontend": (
                application.frontend.model_dump(mode="json")
                if application.frontend is not None else None
            ),
            "packaging_version": "laravel_v1",
            "execution_fingerprint": execution_fingerprint(),
        }
        build_id = self.build_orchestrator.build_id(identity)
        definition = state.artifact_stores[build.artifact_store]
        return {
            "state": state,
            "deployment": deployment,
            "application": application,
            "build": build,
            "target": state.targets[build.target],
            "definition": definition,
            "identity": identity,
            "build_id": build_id,
            "build_secret_versions": [],
        }

    def live_release(self, name: str) -> dict[str, object]:
        state = self.store.load()
        deployment = state.deployments[name]
        if deployment.release_mode != "artifact":
            raise ValueError("artifact promotion requires artifact release mode")
        target = state.targets[deployment.target]
        result = self._run(name, target, {"operation": "release"}, None, 60)
        metadata = _result(result.output)
        if metadata.get("application") != deployment.application:
            raise RuntimeError("artifact_release_metadata_invalid")
        return metadata

    def materialize_request(self, context: dict[str, object]) -> dict[str, object]:
        artifact = context["artifact"]
        definition = context["definition"]
        return {
            "operation": "materialize",
            "artifact": artifact,
            "store": _store_policy(definition),
            "build_identity": context["identity"],
            "release_contract": release_contract(
                context["deployment"], context["application"]
            ),
        }

    def apply_arguments(self, context: dict[str, object]):
        credentials = context["reader_credentials"]
        document = {"store": credentials, "build": {}}
        secret_context = (
            protected_secret_file(document) if credentials else nullcontext(None)
        )
        return self.materialize_request(context), secret_context
