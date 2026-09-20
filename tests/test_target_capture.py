import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from gimme import recovery
from gimme.target_capture import (
    Component, ObjectMetadata, capture_postgres, publish, recovery_point_id,
)


class Store:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put(self, key, body, sha256):
        version = f"version-{len(self.objects) + 1}"
        metadata = ObjectMetadata(len(body), sha256, "AES256", version)
        self.objects[(key, version)] = (body, metadata)
        self.objects[(key, None)] = (body, metadata)
        return metadata

    def head(self, key, version_id=None):
        item = self.objects.get((key, version_id))
        return None if item is None else item[1]

    def get(self, key, version_id=None):
        return self.objects[(key, version_id)][0]

    def delete(self, key, version_id):
        self.deleted.append((key, version_id))
        self.objects.pop((key, version_id), None)


def test_identity_matches_existing_recovery_contract() -> None:
    assert recovery_point_id("example-app", "primary", "scheduled-abc") == (
        recovery.recovery_point_id("example-app", "primary", "scheduled-abc")
    )


def test_postgres_capture_uses_only_fixed_argv_and_hashes_output(tmp_path) -> None:
    observed = {}

    def execute(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        Path(argv[-1]).write_bytes(b"dump")
        return subprocess.CompletedProcess(argv, 0)

    component = capture_postgres("example_app", "17.2", tmp_path, execute=execute)

    assert observed["argv"][0] == "/usr/bin/pg_dump"
    assert "shell" not in observed["kwargs"]
    assert component.sha256 == hashlib.sha256(b"dump").hexdigest()
    component.path.unlink()

    def fails(_argv, **_kwargs):
        raise subprocess.TimeoutExpired("secret command", 1800)

    from gimme.target_capture import CaptureFailure
    try:
        capture_postgres("example_app", "17.2", tmp_path, execute=fails)
    except CaptureFailure as error:
        assert str(error) == "recovery_capture_failed"
        assert "secret command" not in str(error)
    else:
        raise AssertionError("capture failure was not bounded")


def test_publish_verifies_exact_versions_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "postgres.dump"
    path.write_bytes(b"dump")
    component = Component(
        "postgres", path, 4, hashlib.sha256(b"dump").hexdigest(), "17.2",
        "pg-custom-v1",
    )
    store = Store()
    point_id = recovery_point_id("example-app", "primary", "scheduled-abc")
    now = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)

    first = publish(store, "example-app", "primary", point_id, [component], observed_at=now)
    second = publish(store, "example-app", "primary", point_id, [component], observed_at=now)

    assert second == first
    assert recovery._validate_manifest(first) == first
    assert first["schema_version"] == 3
    assert first["components"][0]["version_id"].startswith("version-")
    encoded = json.dumps(first, sort_keys=True, separators=(",", ":")).encode()
    assert len(encoded) < 8192


def test_lost_manifest_response_preserves_published_components(tmp_path) -> None:
    path = tmp_path / "postgres.dump"
    path.write_bytes(b"dump")
    component = Component(
        "postgres", path, 4, hashlib.sha256(b"dump").hexdigest(), "17.2",
        "pg-custom-v1",
    )

    class LostResponseStore(Store):
        lost = False

        def put(self, key, body, sha256):
            metadata = super().put(key, body, sha256)
            if key.endswith("manifest.json") and not self.lost:
                self.lost = True
                raise TimeoutError("private provider response")
            return metadata

    store = LostResponseStore()
    point_id = recovery_point_id("example-app", "primary", "scheduled-abc")
    try:
        publish(store, "example-app", "primary", point_id, [component])
    except TimeoutError:
        pass
    else:
        raise AssertionError("lost response was not reproduced")

    assert store.deleted == []
    assert publish(store, "example-app", "primary", point_id, [component])[
        "recovery_point_id"
    ] == point_id
