import hashlib
import json
from pathlib import Path

import pytest

from gimme.control import S3BackupDestination, SSEAES256
from gimme.recovery import (
    ComponentDump,
    ObjectMetadata,
    RecoveryError,
    create_recovery_point,
    delete_recovery_point_versions,
    find_recovery_point,
    list_recovery_points,
    manifest_key,
    preflight_backup_destination,
    recovery_point_deletion_targets,
    recovery_point_id,
    restore_event_key,
    safety_recovery_point_protected,
)


class FakeS3:
    def __init__(self, *, versioning: str = "Enabled") -> None:
        self.versioning = versioning
        self.objects: dict[str, bytes] = {}
        self.versions: dict[str, str] = {}
        self.puts = 0
        self.deletes: list[tuple[str, str | None]] = []

    def bucket_versioning(self, destination, credentials) -> str:
        return self.versioning

    def put_object(self, destination, credentials, key, body, sha256) -> ObjectMetadata:
        self.puts += 1
        self.objects[key] = body
        version_id = f"v{self.puts}"
        self.versions[key] = version_id
        return ObjectMetadata(
            bytes=len(body), sha256=sha256, server_side_encryption="AES256",
            version_id=version_id,
        )

    def head_object(self, destination, credentials, key, version_id=None) -> ObjectMetadata | None:
        body = self.objects.get(key)
        if body is None or (version_id is not None and self.versions.get(key) != version_id):
            return None
        return ObjectMetadata(
            bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
            server_side_encryption="AES256", version_id=self.versions.get(key),
        )

    def get_object(self, destination, credentials, key, version_id=None) -> bytes:
        if version_id is not None and self.versions.get(key) != version_id:
            raise KeyError(key)
        return self.objects[key]

    def delete_object(self, destination, credentials, key, version_id=None) -> None:
        self.deletes.append((key, version_id))
        if version_id is not None and self.versions.get(key) != version_id:
            return
        self.objects.pop(key, None)
        self.versions.pop(key, None)

    def list_keys(self, destination, credentials, prefix) -> list[str]:
        return [key for key in self.objects if key.startswith(prefix)]


class FailingPutS3(FakeS3):
    def __init__(self, *, fail_after: int) -> None:
        super().__init__()
        self.fail_after = fail_after

    def put_object(self, destination, credentials, key, body, sha256) -> ObjectMetadata:
        if self.puts >= self.fail_after:
            raise RuntimeError("simulated transport failure")
        return super().put_object(destination, credentials, key, body, sha256)


def destination() -> S3BackupDestination:
    return S3BackupDestination(bucket="gimme-backups", region="us-east-1", encryption=SSEAES256())


def dump(tmp_path: Path, *, content: bytes = b"pg-dump-bytes") -> ComponentDump:
    path = tmp_path / "postgres.dump"
    path.write_bytes(content)
    return ComponentDump(
        kind="postgres", local_path=path, sha256=hashlib.sha256(content).hexdigest(),
        bytes=len(content),
    )


def test_preflight_leaves_no_residual_objects() -> None:
    adapter = FakeS3()

    result = preflight_backup_destination(destination(), None, adapter)

    assert result["versioning"] == "enabled"
    assert adapter.objects == {}
    [(_, version_id)] = adapter.deletes
    assert version_id == "v1", "must delete the exact version the probe wrote, not the latest"


def test_preflight_cleans_up_probe_object_even_on_failure() -> None:
    class MismatchS3(FakeS3):
        def get_object(self, destination, credentials, key) -> bytes:
            return b"tampered-in-flight"

    adapter = MismatchS3()

    with pytest.raises(RecoveryError, match="round_trip_mismatch"):
        preflight_backup_destination(destination(), None, adapter)

    assert adapter.objects == {}
    [(_, version_id)] = adapter.deletes
    assert version_id == "v1"


@pytest.mark.parametrize("status", ["Suspended", "Disabled"])
def test_preflight_rejects_unavailable_or_disabled_versioning(status: str) -> None:
    adapter = FakeS3(versioning=status)

    with pytest.raises(RecoveryError, match="versioning_disabled"):
        preflight_backup_destination(destination(), None, adapter)

    assert adapter.objects == {}, "must not attempt the round trip on an unversioned bucket"


def test_preflight_rejects_an_unencrypted_probe_upload() -> None:
    class UnencryptedS3(FakeS3):
        def put_object(self, destination, credentials, key, body, sha256) -> ObjectMetadata:
            self.puts += 1
            self.objects[key] = body
            return ObjectMetadata(bytes=len(body), sha256=sha256, server_side_encryption="")

    adapter = UnencryptedS3()

    with pytest.raises(RecoveryError, match="not_encrypted"):
        preflight_backup_destination(destination(), None, adapter)

    assert adapter.objects == {}


def test_create_recovery_point_publishes_manifest_after_verification(tmp_path: Path) -> None:
    adapter = FakeS3()
    point_id = recovery_point_id("checkout", "primary", "req-1")

    manifest = create_recovery_point(
        "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
    )

    assert manifest["recovery_point_id"] == point_id
    assert manifest["components"][0]["kind"] == "postgres"
    assert manifest["components"][0]["version_id"] == "v1"
    assert adapter.puts == 2  # component, then manifest


def test_deletion_targets_are_resolved_only_from_the_published_manifest(tmp_path: Path) -> None:
    adapter = FakeS3()
    point_id = recovery_point_id("checkout", "primary", "req-1")
    create_recovery_point(
        "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
    )

    targets = recovery_point_deletion_targets(
        "primary", destination(), None, adapter, "checkout", point_id
    )

    assert targets["recovery_point_id"] == point_id
    assert targets["components"] == 1 and targets["bytes"] == len(b"pg-dump-bytes")
    assert targets["manifest_version_id"] == "v2"


def test_delete_removes_exact_component_versions_then_exact_manifest_version(
    tmp_path: Path,
) -> None:
    adapter = FakeS3()
    point_id = recovery_point_id("checkout", "primary", "req-1")
    manifest = create_recovery_point(
        "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
    )

    result = delete_recovery_point_versions(
        "primary", destination(), None, adapter, "checkout", point_id
    )

    assert result["state"] == "deleted"
    assert adapter.deletes[-2:] == [
        (manifest["components"][0]["key"], "v1"),
        (manifest_key("checkout", point_id), "v2"),
    ]
    assert adapter.objects == {}


def test_partial_delete_stays_visible_and_retry_is_idempotent(tmp_path: Path) -> None:
    class FailManifestOnce(FakeS3):
        failed = False

        def delete_object(self, destination, credentials, key, version_id=None) -> None:
            if key.endswith("/manifest.json") and not self.failed:
                self.failed = True
                raise RuntimeError("access denied: private provider detail")
            super().delete_object(destination, credentials, key, version_id)

    adapter = FailManifestOnce()
    point_id = recovery_point_id("checkout", "primary", "req-1")
    create_recovery_point(
        "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
    )

    with pytest.raises(RecoveryError, match="^recovery_point_deletion_failed$"):
        delete_recovery_point_versions(
            "primary", destination(), None, adapter, "checkout", point_id
        )
    inventory = list_recovery_points(
        "primary", destination(), None, adapter, "checkout"
    )
    [partial] = inventory["recovery_points"]
    assert partial["state"] == "deletion_failed"
    assert partial["deleted_components"] == 1

    result = delete_recovery_point_versions(
        "primary", destination(), None, adapter, "checkout", point_id
    )
    assert result["state"] == "deleted"
    assert adapter.objects == {}


def test_safety_point_is_protected_until_its_restore_record_completes(tmp_path: Path) -> None:
    adapter = FakeS3()
    point_id = recovery_point_id("checkout", "primary", "safety-1")
    manifest = create_recovery_point(
        "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path),
        safety_restore_request_id="restore-1",
    )

    assert safety_recovery_point_protected(
        destination(), None, adapter, "checkout", manifest
    ) is True
    for sequence, state in enumerate(("started", "completed")):
        event = {
            "schema_version": 1,
            "deployment": "checkout",
            "request_id": "restore-1",
            "sequence": sequence,
            "state": state,
            "safety_recovery_point_id": point_id,
        }
        body = json.dumps(event, sort_keys=True).encode()
        adapter.put_object(
            destination(), None, restore_event_key("checkout", "restore-1", sequence),
            body, hashlib.sha256(body).hexdigest(),
        )

    assert safety_recovery_point_protected(
        destination(), None, adapter, "checkout", manifest
    ) is False


def test_duplicate_apply_is_a_deterministic_no_op(tmp_path: Path) -> None:
    adapter = FakeS3()
    point_id = recovery_point_id("checkout", "primary", "req-1")
    first = create_recovery_point(
        "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
    )
    puts_after_first = adapter.puts

    second = create_recovery_point(
        "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
    )

    assert second == first
    assert adapter.puts == puts_after_first


def test_different_request_ids_produce_different_recovery_points(tmp_path: Path) -> None:
    first_id = recovery_point_id("checkout", "primary", "req-1")
    second_id = recovery_point_id("checkout", "primary", "req-2")

    assert first_id != second_id


def test_partial_upload_is_cleaned_up_on_failure(tmp_path: Path) -> None:
    adapter = FailingPutS3(fail_after=1)
    point_id = recovery_point_id("checkout", "primary", "req-1")

    with pytest.raises(RuntimeError, match="simulated transport failure"):
        create_recovery_point(
            "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
        )

    assert adapter.objects == {}


def test_ambiguous_manifest_publish_does_not_delete_the_now_published_component(
    tmp_path: Path,
) -> None:
    class LostResponseS3(FakeS3):
        def put_object(self, destination, credentials, key, body, sha256) -> ObjectMetadata:
            written = super().put_object(destination, credentials, key, body, sha256)
            if key.endswith("/manifest.json"):
                raise RuntimeError("simulated response timeout after a successful write")
            return written

    adapter = LostResponseS3()
    point_id = recovery_point_id("checkout", "primary", "req-1")

    with pytest.raises(RuntimeError, match="simulated response timeout"):
        create_recovery_point(
            "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
        )

    # The manifest actually landed; a retry of the same deterministic point_id must find
    # it rather than a component the exception handler wrongly deleted out from under it.
    retried = create_recovery_point(
        "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
    )
    assert retried["recovery_point_id"] == point_id


def test_component_checksum_mismatch_is_rejected_before_upload(tmp_path: Path) -> None:
    adapter = FakeS3()
    original = dump(tmp_path)
    bad = ComponentDump(
        kind="postgres", local_path=original.local_path, sha256="0" * 64, bytes=original.bytes
    )
    point_id = recovery_point_id("checkout", "primary", "req-1")

    with pytest.raises(RecoveryError, match="checksum_mismatch"):
        create_recovery_point("primary", destination(), None, adapter, "checkout", point_id, bad)

    assert adapter.puts == 0


def test_component_upload_confirmation_mismatch_is_cleaned_up(tmp_path: Path) -> None:
    class TruncatingS3(FakeS3):
        def head_object(self, destination, credentials, key) -> ObjectMetadata | None:
            real = super().head_object(destination, credentials, key)
            if real is None:
                return None
            return ObjectMetadata(
                bytes=real.bytes - 1, sha256=real.sha256,
                server_side_encryption=real.server_side_encryption,
            )

    adapter = TruncatingS3()
    point_id = recovery_point_id("checkout", "primary", "req-1")

    with pytest.raises(RecoveryError, match="verification_failed"):
        create_recovery_point(
            "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
        )

    assert adapter.objects == {}
    [(_, version_id)] = adapter.deletes
    assert version_id == "v1", "must delete the exact version the failed upload wrote"


def test_component_upload_content_mismatch_is_cleaned_up_even_when_metadata_lies(
    tmp_path: Path,
) -> None:
    class LyingContentS3(FakeS3):
        def get_object(self, destination, credentials, key) -> bytes:
            return b"different-bytes-same-length"

    adapter = LyingContentS3()
    point_id = recovery_point_id("checkout", "primary", "req-1")
    content = b"different-bytes-same-length"[::-1]  # same length, different sha256

    with pytest.raises(RecoveryError, match="verification_failed"):
        create_recovery_point(
            "primary", destination(), None, adapter, "checkout", point_id,
            dump(tmp_path, content=content),
        )

    assert adapter.objects == {}


def test_find_recovery_point_returns_none_when_absent() -> None:
    adapter = FakeS3()

    assert (
        find_recovery_point(
            "primary", destination(), None, adapter, "checkout", "rp_" + "0" * 20
        )
        is None
    )


def test_list_recovery_points_reports_newest_first(tmp_path: Path) -> None:
    adapter = FakeS3()
    for index in range(2):
        point_id = recovery_point_id("checkout", "primary", f"req-{index}")
        create_recovery_point(
            "primary", destination(), None, adapter, "checkout", point_id,
            dump(tmp_path, content=f"dump-{index}".encode()),
        )

    inventory = list_recovery_points("primary", destination(), None, adapter, "checkout")

    assert len(inventory["recovery_points"]) == 2
    assert inventory["rejected"] == []


def test_list_recovery_points_rejects_tampered_manifest_without_failing_the_call(
    tmp_path: Path,
) -> None:
    adapter = FakeS3()
    point_id = recovery_point_id("checkout", "primary", "req-1")
    manifest = create_recovery_point(
        "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
    )
    component_key = manifest["components"][0]["key"]
    adapter.objects[component_key] = b"corrupted-after-publish"

    inventory = list_recovery_points("primary", destination(), None, adapter, "checkout")

    assert inventory["recovery_points"] == []
    assert inventory["rejected"] == [point_id]


@pytest.mark.parametrize("corrupt", [
        lambda m: {**m, "schema_version": 1},
    lambda m: {**m, "recovery_point_id": "not-an-id"},
    lambda m: {**m, "components": []},
    lambda m: {**m, "components": [{**m["components"][0], "bytes": -1}]},
    lambda m: {**m, "components": [{**m["components"][0], "sha256": "not-hex"}]},
    lambda m: {k: v for k, v in m.items() if k != "destination"},
    lambda m: {**m, "destination": "someone-elses-destination"},
])
def test_list_recovery_points_rejects_structurally_invalid_manifests(tmp_path, corrupt) -> None:
    adapter = FakeS3()
    point_id = recovery_point_id("checkout", "primary", "req-1")
    manifest = create_recovery_point(
        "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
    )
    key = manifest_key("checkout", point_id)
    adapter.objects[key] = json.dumps(corrupt(manifest)).encode()

    inventory = list_recovery_points("primary", destination(), None, adapter, "checkout")

    assert inventory["recovery_points"] == []
    assert point_id in inventory["rejected"]


def test_a_manifests_content_cannot_be_replayed_under_a_different_recovery_point_id(
    tmp_path: Path,
) -> None:
    adapter = FakeS3()
    original_id = recovery_point_id("checkout", "primary", "req-1")
    create_recovery_point(
        "primary", destination(), None, adapter, "checkout", original_id, dump(tmp_path)
    )
    moved_id = recovery_point_id("checkout", "primary", "req-2")
    moved_key = manifest_key("checkout", moved_id)
    adapter.objects[moved_key] = adapter.objects[manifest_key("checkout", original_id)]

    inventory = list_recovery_points("primary", destination(), None, adapter, "checkout")

    assert [item["recovery_point_id"] for item in inventory["recovery_points"]] == [original_id]
    assert inventory["rejected"] == [moved_id]


def test_a_manifest_cannot_borrow_another_recovery_points_component(tmp_path: Path) -> None:
    adapter = FakeS3()
    victim_id = recovery_point_id("checkout", "primary", "req-victim")
    victim = create_recovery_point(
        "primary", destination(), None, adapter, "checkout", victim_id, dump(tmp_path)
    )
    attacker_id = recovery_point_id("checkout", "primary", "req-attacker")
    attacker = create_recovery_point(
        "primary", destination(), None, adapter, "checkout", attacker_id,
        dump(tmp_path, content=b"attacker-controlled-bytes"),
    )
    borrowed = {**attacker, "components": [victim["components"][0]]}
    adapter.objects[manifest_key("checkout", attacker_id)] = json.dumps(borrowed).encode()

    inventory = list_recovery_points("primary", destination(), None, adapter, "checkout")

    ids = [item["recovery_point_id"] for item in inventory["recovery_points"]]
    assert ids == [victim_id]
    assert inventory["rejected"] == [attacker_id]


def test_a_manifest_cannot_name_a_noncanonical_key_inside_its_own_prefix(
    tmp_path: Path,
) -> None:
    adapter = FakeS3()
    point_id = recovery_point_id("checkout", "primary", "req-1")
    manifest = create_recovery_point(
        "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
    )
    original = manifest["components"][0]
    alternate_key = f"gimme/recovery-points/checkout/{point_id}/alternate.dump"
    adapter.objects[alternate_key] = adapter.objects[original["key"]]
    adapter.versions[alternate_key] = original["version_id"]
    changed = {**manifest, "components": [{**original, "key": alternate_key}]}
    adapter.objects[manifest_key("checkout", point_id)] = json.dumps(changed).encode()

    inventory = list_recovery_points("primary", destination(), None, adapter, "checkout")

    assert inventory["recovery_points"] == []
    assert inventory["rejected"] == [point_id]


def test_load_manifest_rejects_oversized_object_without_reading_its_body() -> None:
    class ExplodingReadS3(FakeS3):
        def head_object(
            self, destination, credentials, key, version_id=None
        ) -> ObjectMetadata | None:
            return ObjectMetadata(
                bytes=1024 * 1024, sha256="0" * 64, server_side_encryption="AES256",
                version_id="v1",
            )

        def get_object(self, destination, credentials, key, version_id=None) -> bytes:
            raise AssertionError("must not read an oversized manifest body")

    adapter = ExplodingReadS3()
    adapter.objects[manifest_key("checkout", "rp_" + "0" * 20)] = b"placeholder"

    with pytest.raises(RecoveryError, match="too_large"):
        find_recovery_point(
            "primary", destination(), None, adapter, "checkout", "rp_" + "0" * 20
        )


def test_list_recovery_points_never_returns_another_deployments_manifest(tmp_path: Path) -> None:
    adapter = FakeS3()
    point_id = recovery_point_id("checkout", "primary", "req-1")
    create_recovery_point(
        "primary", destination(), None, adapter, "checkout", point_id, dump(tmp_path)
    )

    inventory = list_recovery_points("primary", destination(), None, adapter, "billing")

    assert inventory["recovery_points"] == []
    assert inventory["rejected"] == []


def test_manifest_and_errors_never_contain_credential_material(tmp_path: Path) -> None:
    adapter = FakeS3()
    point_id = recovery_point_id("checkout", "primary", "req-1")
    credentials = ("AKIA-SECRET-ID", "super-secret-key-value")

    manifest = create_recovery_point(
        "primary", destination(), credentials, adapter, "checkout", point_id, dump(tmp_path)
    )

    import json

    encoded = json.dumps(manifest)
    assert "AKIA-SECRET-ID" not in encoded
    assert "super-secret-key-value" not in encoded
