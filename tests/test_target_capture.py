import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from gimme import recovery
from gimme.target_capture import (
    BotoObjectStore, Component, ObjectMetadata, capture_postgres, capture_valkey, publish,
    delete_recovery_point_versions, enforce_retention, recovery_point_id, retention_candidates,
    restore_protected_points, verified_inventory,
)
from gimme.target_capture import CaptureFailure


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
        latest = self.objects.get((key, None))
        self.objects.pop((key, version_id), None)
        if latest is not None and latest[1].version_id == version_id:
            self.objects.pop((key, None), None)

    def list(self, prefix):
        return sorted({key for key, _version in self.objects if key.startswith(prefix)})


def test_boto_store_passes_session_token_and_maps_expiry() -> None:
    observed = {}

    class Client:
        def put_object(self, **_kwargs):
            error = RuntimeError("secret provider message")
            error.response = {"Error": {"Code": "ExpiredToken"}}
            raise error

    class Boto:
        @staticmethod
        def client(service, **kwargs):
            observed.update(kwargs)
            return Client()

    destination = {
        "name": "primary", "provider": "s3_compatible", "bucket": "backups",
        "region": "us-east-1", "endpoint": None, "addressing": "virtual_hosted",
        "encryption": {"method": "aes256"}, "auth_mode": "stored",
    }
    store = BotoObjectStore(destination, {
        "access_key_id": "access", "secret_access_key": "secret",
        "session_token": "session",
    }, boto_module=Boto)

    assert observed["aws_session_token"] == "session"
    try:
        store.put("key", b"body", hashlib.sha256(b"body").hexdigest())
    except CaptureFailure as error:
        assert str(error) == "credentials_expired"
        assert "secret provider message" not in str(error)
    else:
        raise AssertionError("expired session credential was accepted")


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


def test_target_retention_selects_verified_ordinary_points_and_deletes_exact_versions(
    tmp_path,
) -> None:
    store = Store()
    point_ids = []
    for index in range(3):
        path = tmp_path / f"postgres-{index}.dump"
        body = f"dump-{index}".encode()
        path.write_bytes(body)
        point_id = recovery_point_id("example-app", "primary", f"scheduled-{index}")
        point_ids.append(point_id)
        publish(
            store, "example-app", "primary", point_id,
            [Component(
                "postgres", path, len(body), hashlib.sha256(body).hexdigest(), "17.2",
                "pg-custom-v1",
            )],
            observed_at=datetime(2026, 9, 20, 10, index, tzinfo=UTC),
        )

    inventory = verified_inventory(store, "example-app", "primary")
    assert retention_candidates(inventory, 2, point_ids[2]) == [point_ids[0]]
    manifest = next(
        item for item in inventory if item["recovery_point_id"] == point_ids[0]
    )
    component = manifest["components"][0]

    assert delete_recovery_point_versions(
        store, "example-app", "primary", point_ids[0]
    ) == 1
    assert (component["key"], component["version_id"]) in store.deleted
    assert verified_inventory(store, "example-app", "primary") == [
        item for item in inventory if item["recovery_point_id"] != point_ids[0]
    ]
    assert enforce_retention(
        store, "example-app", "primary", 1, point_ids[2]
    ) == {"outcome": "succeeded", "error_code": None, "deleted": 1, "remaining": 1}


def test_target_retention_stops_on_exact_version_failure_and_never_selects_safety() -> None:
    replacement = "rp_" + "3" * 20
    manifests = [
        {"recovery_point_id": "rp_" + "1" * 20, "created_at": "2026-01-01", "safety": True},
        {"recovery_point_id": "rp_" + "2" * 20, "created_at": "2026-01-02", "safety": False},
        {"recovery_point_id": replacement, "created_at": "2026-01-03", "safety": False},
    ]
    assert retention_candidates(
        manifests, 1, replacement, {"rp_" + "1" * 20}
    ) == ["rp_" + "2" * 20]


def test_target_restore_events_protect_incomplete_source_and_release_completed_safety() -> None:
    store = Store()
    source = "rp_" + "1" * 20
    safety = "rp_" + "2" * 20
    manifests = [{"recovery_point_id": safety, "safety": True}]

    def event(sequence, state):
        document = {
            "schema_version": 2, "deployment": "example-app", "request_id": "restore-1",
            "sequence": sequence, "state": state,
            "created_at": f"2026-09-20T10:0{sequence}:00+00:00",
            "source_recovery_point_id": source, "destination": "primary",
            "safety_recovery_point_id": safety,
        }
        body = json.dumps(document, sort_keys=True).encode()
        store.put(
            f"gimme/restores/example-app/restore-1/{sequence:06d}.json",
            body, hashlib.sha256(body).hexdigest(),
        )

    event(0, "started")
    assert restore_protected_points(store, "example-app", manifests) == {source, safety}
    for sequence, state in enumerate((
        "maintenance_entered", "safety_verified", "artifact_verified", "shadow_verified",
        "data_replaced", "verification_succeeded", "cleanup_completed", "completed",
    ), 1):
        event(sequence, state)
    assert restore_protected_points(store, "example-app", manifests) == set()


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
        "addressing": "path", "encryption": {"method": "aes256"},
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


def test_only_derived_systemd_valkey_credential_path_uses_mount_security() -> None:
    from gimme.target_capture import systemd_valkey_credential

    assert systemd_valkey_credential(Path(
        "/run/credentials/gimme-recovery-example-app.service/valkey"
    ))
    assert not systemd_valkey_credential(Path(
        "/tmp/gimme-recovery-example-app.service/valkey"
    ))
    assert not systemd_valkey_credential(Path(
        "/run/credentials/gimme-recovery-example-app.service/aws"
    ))
