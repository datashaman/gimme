from __future__ import annotations

import base64
import hashlib
import json
import re
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable, Literal

from gimme.control import AmbientArtifactAuth, S3ArtifactStore, SopsArtifactAuth
from gimme.control_plans import exact_plan
from gimme.artifact_public import public_store_policy
from gimme.secrets import (
    plan_secret_references,
    protected_secret_file,
    resolve_planned_secret_references,
)


VERSION_ID = re.compile(r"^[A-Za-z0-9._+=/-]{1,1024}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def artifact_auth_references(auth: AmbientArtifactAuth | SopsArtifactAuth):
    if isinstance(auth, AmbientArtifactAuth):
        return None
    values = {
        "access_key_id": auth.access_key_id,
        "secret_access_key": auth.secret_access_key,
    }
    if auth.session_token is not None:
        values["session_token"] = auth.session_token
    return values


def target_store_policy(name: str, store: S3ArtifactStore) -> dict[str, object]:
    return {
        "name": name,
        "bucket": store.bucket,
        "region": store.region,
        "endpoint": store.endpoint,
        "addressing": store.addressing,
        "encryption": store.encryption.model_dump(mode="json"),
    }


def _evidence(output: str, role: Literal["publisher", "reader"]):
    lines = [line.split("] ", 1)[-1].strip() for line in output.splitlines()]
    prefix = "GIMME_ARTIFACT_STORE_RESULT|"
    matches = [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]
    if len(matches) != 1:
        raise RuntimeError("artifact_store_verification_result_missing")
    try:
        value = json.loads(base64.b64decode(matches[0], validate=True))
    except (ValueError, json.JSONDecodeError):
        raise RuntimeError("artifact_store_verification_result_invalid") from None
    common = {"status", "role", "versioning", "checksum"}
    expected = common | (
        {"encryption", "probe_deleted", "reader_version"}
        if role == "publisher" else set()
    )
    if not isinstance(value, dict) or set(value) != expected:
        raise RuntimeError("artifact_store_verification_result_invalid")
    if value["status"] != "ready" or value["role"] != role:
        raise RuntimeError("artifact_store_verification_failed")
    if not isinstance(value["checksum"], str) or SHA256.fullmatch(value["checksum"]) is None:
        raise RuntimeError("artifact_store_verification_result_invalid")
    if role == "publisher" and (
        value["versioning"] != "enabled"
        or value["encryption"] not in {"aes256", "kms"}
        or value["probe_deleted"] is not True
        or not isinstance(value["reader_version"], str)
        or VERSION_ID.fullmatch(value["reader_version"]) is None
    ):
        raise RuntimeError("artifact_store_verification_result_invalid")
    if role == "reader" and value["versioning"] != "exact-version-read":
        raise RuntimeError("artifact_store_verification_result_invalid")
    return value


@dataclass(frozen=True)
class ArtifactStoreOrchestrator:
    store: Any
    runner: Any
    assert_plan: Callable[[dict[str, object], str], None]
    legacy_server: Callable[[Any], Any]

    def _verification_context(
        self,
        name: str,
        target: str,
        role: Literal["publisher", "reader"],
        reader_version: str | None = None,
    ) -> tuple[Any, S3ArtifactStore, Any, object, dict[str, object]]:
        state = self.store.load()
        definition = state.artifact_stores.get(name)
        selected_target = state.targets.get(target)
        if definition is None:
            raise KeyError("artifact store is not registered")
        if selected_target is None:
            raise KeyError("target is not registered")
        if selected_target.role != "deployment":
            raise ValueError("artifact verification requires a Deployment-capable Target")
        if role == "publisher" and reader_version is not None:
            raise ValueError("publisher verification does not accept a reader version")
        if role == "reader" and (
            reader_version is None or VERSION_ID.fullmatch(reader_version) is None
        ):
            raise ValueError("reader verification requires one bounded exact object version")
        auth = definition.publisher_auth if role == "publisher" else definition.reader_auth
        references = artifact_auth_references(auth)
        planned = None if references is None else plan_secret_references(
            state, self.store.secrets_path, references
        )
        plan = exact_plan({
            "kind": "artifact_store_verification",
            "artifact_store": name,
            "target": target,
            "store_policy": public_store_policy(definition),
            "target_policy": selected_target.model_dump(mode="json"),
            "role": role,
            "reader_version": reader_version,
            "credential_versions_sha256": hashlib.sha256(json.dumps(
                planned or [], sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest(),
            "effects": (
                [
                    "write and read one encrypted Target-generated probe object",
                    "delete the exact probe object version before returning",
                    "write the fixed Gimme reader-capability object",
                ] if role == "publisher" else [
                    "read one exact fixed Gimme reader-capability object version",
                    "make no object-store changes",
                ]
            ),
        })
        return state, definition, selected_target, planned, plan

    def plan_verification(
        self,
        name: str,
        target: str,
        role: Literal["publisher", "reader"],
        reader_version: str | None = None,
    ) -> dict[str, object]:
        return self._verification_context(name, target, role, reader_version)[-1]

    def verify(
        self,
        name: str,
        target: str,
        role: Literal["publisher", "reader"],
        plan_id: str,
        reader_version: str | None = None,
    ) -> dict[str, object]:
        state, definition, selected_target, planned, expected = self._verification_context(
            name, target, role, reader_version
        )
        self.assert_plan(expected, plan_id)
        auth = definition.publisher_auth if role == "publisher" else definition.reader_auth
        references = artifact_auth_references(auth)
        credentials = {} if references is None else resolve_planned_secret_references(
            state, self.store.secrets_path, references, planned
        )
        context = protected_secret_file(credentials) if credentials else nullcontext(None)
        with context as credential_file:
            result = self.runner.run(
                "gimme:artifact-store:verify",
                self.legacy_server(selected_target),
                stack=selected_target.stack,
                artifact_store=target_store_policy(name, definition),
                artifact_probe_role=role,
                artifact_reader_version=reader_version,
                secret_file=credential_file,
                timeout=240,
            )
        evidence = _evidence(result.output, role)
        return {
            "artifact_store": name,
            "target": target,
            **evidence,
        }
