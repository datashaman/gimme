import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from gimme import recovery
from gimme.target_capture import (
    BotoObjectStore, Component, ObjectMetadata, capture_postgres, capture_valkey, publish,
    recovery_point_id,
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

    def get(self, key, version_id=None, max_bytes=512 * 1024 * 1024):
        assert len(self.objects[(key, version_id)][0]) <= max_bytes
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


def test_boto_store_uses_bounded_destination_credentials_and_exact_versions() -> None:
    class Body:
        def read(self, _limit):
            return b"dump"

    class Client:
        def put_object(self, **kwargs):
            assert kwargs["Bucket"] == "gimme-backups"
            assert kwargs["ServerSideEncryption"] == "AES256"
            return {"ServerSideEncryption": "AES256", "VersionId": "version-1"}

        def head_object(self, **kwargs):
            assert kwargs["VersionId"] == "version-1"
            return {
                "ContentLength": 4, "Metadata": {"gimme-sha256": "a" * 64},
                "ServerSideEncryption": "AES256", "VersionId": "version-1",
            }

        def get_object(self, **kwargs):
            assert kwargs["VersionId"] == "version-1"
            return {"Body": Body()}

        def delete_object(self, **kwargs):
            assert kwargs["VersionId"] == "version-1"

    class Boto:
        observed = None

        @classmethod
        def client(cls, service, **kwargs):
            assert service == "s3"
            cls.observed = kwargs
            return Client()

    destination = {
        "name": "primary", "provider": "s3_compatible", "bucket": "gimme-backups",
        "region": "us-east-1", "endpoint": "minio.example.test:9000",
        "addressing": "path", "encryption": {"method": "AES256"},
        "auth_mode": "stored",
    }
    store = BotoObjectStore(
        destination,
        {"access_key_id": "access-canary", "secret_access_key": "secret-canary"},
        boto_module=Boto,
    )

    written = store.put("fixed-key", b"dump", "a" * 64)
    assert written.version_id == "version-1"
    assert store.head("fixed-key", "version-1").bytes == 4
    assert store.get("fixed-key", "version-1") == b"dump"
    store.delete("fixed-key", "version-1")
    assert Boto.observed["endpoint_url"] == "https://minio.example.test:9000"
    assert Boto.observed["aws_access_key_id"] == "access-canary"


def test_valkey_capture_reuses_fixed_binary_and_validates_marker(tmp_path) -> None:
    observed = {}

    def execute(argv, **kwargs):
        observed["argv"] = argv
        Path(argv[1]).write_bytes(b"archive")
        sha256 = hashlib.sha256(b"archive").hexdigest()
        return subprocess.CompletedProcess(
            argv, 0, f"GIMME_VALKEY_BACKUP|{sha256}|7|2|2026-09-20T10:00:00+00:00\n", ""
        )

    component = capture_valkey(
        "gimme:example-app:", "127.0.0.1", 6379, False, "8.0", tmp_path,
        execute=execute,
    )

    assert observed["argv"][0] == "/usr/local/libexec/gimme-capture-valkey"
    assert observed["argv"][2:] == [
        "gimme:example-app:", "127.0.0.1", "6379", "no", "-",
    ]
    assert component.kind == "valkey"
    assert component.records == 2
    component.path.unlink()
