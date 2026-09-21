from __future__ import annotations

import hashlib
import json
from typing import Any

from gimme.control import ApplicationConfig, ControlState, S3ArtifactStore


def _sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def public_application_policy(definition: ApplicationConfig) -> dict[str, Any]:
    """Return application policy without build-secret names or references."""
    value = definition.model_dump(mode="json")
    build = value.get("build")
    if build is not None:
        references = build.pop("secrets")
        build["build_secrets_used"] = bool(references)
        build["build_secret_count"] = len(references)
        build["build_secret_references_sha256"] = _sha256(references)
    return value


def public_store_policy(definition: S3ArtifactStore) -> dict[str, Any]:
    """Return Artifact Store policy without authentication references."""
    value = definition.model_dump(mode="json")
    value["publisher_auth"] = {"mode": definition.publisher_auth.mode}
    value["reader_auth"] = {"mode": definition.reader_auth.mode}
    return value


def public_state(state: ControlState) -> dict[str, Any]:
    """Return desired state with artifact credential and build-secret references redacted."""
    value = state.model_dump(mode="json")
    value["applications"] = {
        name: public_application_policy(application)
        for name, application in state.applications.items()
    }
    value["artifact_stores"] = {
        name: public_store_policy(definition)
        for name, definition in state.artifact_stores.items()
    }
    return value
