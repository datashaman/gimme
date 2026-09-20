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
from gimme.control_plans import exact_plan
from gimme.execution import execution_fingerprint
from gimme.secrets import (
    plan_secret_references,
    protected_secret_file,
    resolve_planned_secret_references,
)


BUILD_ID = re.compile(r"^build_v1_[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
FINGERPRINT = re.compile(r"^(?:repo|exec)_[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][A-Za-z0-9.-]+)?$")
CAPABILITY_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PUBLISHED_AT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00$"
)


def _result(output: str) -> dict[str, object]:
    lines = [line.split("] ", 1)[-1].strip() for line in output.splitlines()]
    prefix = "GIMME_ARTIFACT_RESULT|"
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
class ArtifactBuildOrchestrator:
    store: Any
    runner: Any
    assert_plan: Callable[[dict[str, object], str], None]
    legacy_server: Callable[[Any], Any]
    legacy_app: Callable[[Any, Any], Any]

    def _context(self, name: str):
        state = self.store.load()
        deployment = state.deployments.get(name)
        if deployment is None:
            raise KeyError("deployment is not registered")
        if deployment.release_mode != "artifact":
            raise ValueError("artifact builds require artifact release mode")
        application = state.applications[deployment.application]
        build = application.build
        if build is None:
            raise ValueError("application has no artifact build policy")
        if application.frontend is not None:
            raise ValueError("artifact_frontend_not_supported")
        if build.secrets:
            raise ValueError("artifact_build_secrets_not_supported")
        target = state.targets[build.target]
        definition = state.artifact_stores[build.artifact_store]
        return state, deployment, application, build, target, definition

    def _run(self, target, request: dict[str, object], credential_file=None):
        return self.runner.run(
            "gimme:artifact:run",
            self.legacy_server(target),
            stack=target.stack,
            artifact_request=request,
            secret_file=credential_file,
            timeout=3600,
        )

    def _revision(self, deployment, application, target) -> str:
        if deployment.source.kind == "commit":
            return deployment.source.ref
        result = self.runner.run(
            "gimme:resolve-revision",
            self.legacy_server(target),
            stack=target.stack,
            app_name=deployment.application,
            app=self.legacy_app(application, deployment),
            source_kind=deployment.source.kind,
            runtimes={
                name: pin.model_dump(mode="json")
                for name, pin in deployment.runtimes.items()
            },
            mise_version=target.runtimes.mise_version,
            php_extensions=application.php_extensions,
            timeout=60,
        )
        for raw in result.output.splitlines():
            line = raw.split("] ", 1)[-1].strip()
            if line.startswith("GIMME_REVISION|"):
                revision = line.split("|", 1)[1]
                if COMMIT.fullmatch(revision):
                    return revision
        raise RuntimeError("artifact_source_revision_invalid")

    def _source_inspection(self, deployment, application, target, revision: str):
        request = {
            "operation": "inspect",
            "repository": application.repository,
            "commit": revision,
            "runtimes": {
                name: pin.model_dump(mode="json")
                for name, pin in deployment.runtimes.items()
            },
            "php_extensions": application.php_extensions,
        }
        value = _result(self._run(target, request).output)
        if set(value) != {
            "status", "commit", "repository_fingerprint", "composer_lock_sha256",
            "composer_lock_bytes", "capability",
        } or value.get("status") != "ready" or value.get("commit") != revision:
            raise RuntimeError("artifact_source_inspection_invalid")
        if (
            not isinstance(value.get("repository_fingerprint"), str)
            or FINGERPRINT.fullmatch(value["repository_fingerprint"]) is None
            or not isinstance(value.get("composer_lock_sha256"), str)
            or SHA256.fullmatch(value["composer_lock_sha256"]) is None
            or not isinstance(value.get("composer_lock_bytes"), int)
            or not 0 < value["composer_lock_bytes"] <= 4 * 1024 * 1024
            or not isinstance(value.get("capability"), dict)
        ):
            raise RuntimeError("artifact_source_inspection_invalid")
        capability = value["capability"]
        if set(capability) != {
            "php", "composer", "php_extensions", "system", "machine"
        } or any(
            not isinstance(capability.get(name), str)
            or VERSION.fullmatch(capability[name]) is None
            for name in ("php", "composer")
        ):
            raise RuntimeError("artifact_source_inspection_invalid")
        extensions = capability["php_extensions"]
        if (
            not isinstance(extensions, list)
            or len(extensions) > 256
            or extensions != sorted(set(extensions))
            or any(
                not isinstance(extension, str)
                or CAPABILITY_NAME.fullmatch(extension) is None
                for extension in extensions
            )
            or any(
                not isinstance(capability[name], str)
                or CAPABILITY_NAME.fullmatch(capability[name]) is None
                for name in ("system", "machine")
            )
        ):
            raise RuntimeError("artifact_source_inspection_invalid")
        return value

    def _publisher_credentials(self, state, definition):
        references = artifact_auth_references(definition.publisher_auth)
        if references is None:
            return [], {}
        planned = plan_secret_references(state, self.store.secrets_path, references)
        resolved = resolve_planned_secret_references(
            state, self.store.secrets_path, references, planned
        )
        return planned, resolved

    def _publication(
        self, target, definition, application: str, build_id: str, credentials: dict[str, str]
    ) -> dict[str, object]:
        request = {
            "operation": "publication",
            "application": application,
            "build_id": build_id,
            "store": _store_policy(definition),
        }
        context = protected_secret_file(credentials) if credentials else nullcontext(None)
        with context as credential_file:
            value = _result(self._run(target, request, credential_file).output)
        status = value.get("status")
        if status not in {
            "absent", "published", "malformed", "missing", "checksum_invalid"
        } or value.get("build_id") != build_id:
            raise RuntimeError("artifact_publication_status_invalid")
        if status == "published":
            if set(value) != {
                "status", "build_id", "artifact_digest", "tree_digest", "bytes"
            } or any(
                not isinstance(value.get(name), str)
                or SHA256.fullmatch(value[name]) is None
                for name in ("artifact_digest", "tree_digest")
            ) or (
                not isinstance(value.get("bytes"), int)
                or not 0 < value["bytes"] <= 512 * 1024 * 1024
            ):
                raise RuntimeError("artifact_publication_status_invalid")
        elif set(value) != {"status", "build_id"}:
            raise RuntimeError("artifact_publication_status_invalid")
        return value

    @staticmethod
    def _build_id(inputs: dict[str, object]) -> str:
        encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
        return "build_v1_" + hashlib.sha256(b"gimme-build-v1\0" + encoded).hexdigest()

    def _plan_context(self, name: str):
        state, deployment, application, build, target, definition = self._context(name)
        revision = self._revision(deployment, application, target)
        inspection = self._source_inspection(deployment, application, target, revision)
        identity = {
            "application": deployment.application,
            "repository_fingerprint": inspection["repository_fingerprint"],
            "commit": revision,
            "composer_lock_sha256": inspection["composer_lock_sha256"],
            "composer_lock_bytes": inspection["composer_lock_bytes"],
            "build_policy": build.model_dump(mode="json"),
            "runtimes": {
                runtime: pin.model_dump(mode="json")
                for runtime, pin in sorted(deployment.runtimes.items())
            },
            "php_extensions": application.php_extensions,
            "capability": inspection["capability"],
            "packaging_version": "laravel_v1",
            "execution_fingerprint": execution_fingerprint(),
        }
        build_id = self._build_id(identity)
        planned, credentials = self._publisher_credentials(state, definition)
        publication = self._publication(
            target, definition, deployment.application, build_id, credentials
        )
        plan = exact_plan({
            "kind": "artifact_build",
            "deployment": name,
            "application": deployment.application,
            "build_id": build_id,
            "identity": identity,
            "publisher_credential_versions": planned,
            "publication": publication,
            "effects": [
                "fetch the exact reviewed commit into an isolated Build Target workspace",
                "install frozen production Composer dependencies without scripts",
                "create and verify one deterministic laravel_v1 archive",
                "upload and read back encrypted archive bytes on the Build Target",
                "publish the private authoritative manifest last when absent",
                "remove all Target workspace and credential material",
            ],
        })
        return (
            state, deployment, application, build, target, definition, credentials, plan
        )

    def plan_build_artifact(self, name: str) -> dict[str, object]:
        return self._plan_context(name)[-1]

    def build_artifact(self, name: str, plan_id: str) -> dict[str, object]:
        (
            _state, deployment, application, _build, target, definition, credentials, expected
        ) = self._plan_context(name)
        self.assert_plan(expected, plan_id)
        identity = expected["identity"]
        if not isinstance(identity, dict):
            raise RuntimeError("artifact_plan_invalid")
        request = {
            "operation": "build",
            "application": deployment.application,
            "repository": application.repository,
            "commit": identity["commit"],
            "runtimes": identity["runtimes"],
            "php_extensions": identity["php_extensions"],
            "composer_lock_sha256": identity["composer_lock_sha256"],
            "capability": identity["capability"],
            "build_id": expected["build_id"],
            "store": _store_policy(definition),
        }
        context = protected_secret_file(credentials) if credentials else nullcontext(None)
        with context as credential_file:
            value = _result(self._run(target, request, credential_file).output)
        common = {"status", "application", "build_id"}
        if value.get("status") == "non_reproducible_build":
            if set(value) != common:
                raise RuntimeError("artifact_build_result_invalid")
            return value
        if set(value) != common | {"artifact_digest", "tree_digest", "bytes"}:
            raise RuntimeError("artifact_build_result_invalid")
        if (
            value.get("status") not in {"published", "idempotent"}
            or value.get("application") != deployment.application
            or value.get("build_id") != expected["build_id"]
            or not isinstance(value.get("artifact_digest"), str)
            or SHA256.fullmatch(value["artifact_digest"]) is None
            or not isinstance(value.get("tree_digest"), str)
            or SHA256.fullmatch(value["tree_digest"]) is None
            or not isinstance(value.get("bytes"), int)
            or not 0 < value["bytes"] <= 512 * 1024 * 1024
        ):
            raise RuntimeError("artifact_build_result_invalid")
        return value

    def list_artifacts(self, application_name: str) -> dict[str, object]:
        state = self.store.load()
        application = state.applications.get(application_name)
        if application is None:
            raise KeyError("application is not registered")
        build = application.build
        if build is None:
            return {"application": application_name, "artifacts": []}
        target = state.targets[build.target]
        definition = state.artifact_stores[build.artifact_store]
        _, credentials = self._publisher_credentials(state, definition)
        request = {
            "operation": "inventory",
            "application": application_name,
            "store": _store_policy(definition),
        }
        context = protected_secret_file(credentials) if credentials else nullcontext(None)
        with context as credential_file:
            value = _result(self._run(target, request, credential_file).output)
        artifacts = value.get("artifacts")
        if (
            set(value) != {"application", "artifacts"}
            or value.get("application") != application_name
            or not isinstance(artifacts, list)
        ):
            raise RuntimeError("artifact_inventory_invalid")
        if len(artifacts) > 100:
            raise RuntimeError("artifact_inventory_invalid")
        previous = None
        for item in artifacts:
            if not isinstance(item, dict) or set(item) != {
                "build_id", "status", "published_at", "artifact_digest"
            }:
                raise RuntimeError("artifact_inventory_invalid")
            if item["build_id"] != "unknown" and (
                not isinstance(item["build_id"], str)
                or BUILD_ID.fullmatch(item["build_id"]) is None
            ):
                raise RuntimeError("artifact_inventory_invalid")
            if item["status"] not in {
                "ready", "malformed", "foreign", "missing", "unsupported",
                "checksum_invalid",
            }:
                raise RuntimeError("artifact_inventory_invalid")
            if item["published_at"] != "" and (
                not isinstance(item["published_at"], str)
                or PUBLISHED_AT.fullmatch(item["published_at"]) is None
            ):
                raise RuntimeError("artifact_inventory_invalid")
            if item["artifact_digest"] is not None and (
                not isinstance(item["artifact_digest"], str)
                or SHA256.fullmatch(item["artifact_digest"]) is None
            ):
                raise RuntimeError("artifact_inventory_invalid")
            if item["status"] == "ready" and (
                item["published_at"] == "" or item["artifact_digest"] is None
            ):
                raise RuntimeError("artifact_inventory_invalid")
            order = (item["published_at"], item["build_id"])
            if previous is not None and order > previous:
                raise RuntimeError("artifact_inventory_invalid")
            previous = order
        return value
