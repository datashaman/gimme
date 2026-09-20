import base64
import hashlib
import importlib.util
import io
import json
import os
import tarfile
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from gimme.artifact_deployment_orchestration import (
    ArtifactDeploymentOrchestrator,
    release_contract,
)
from gimme.artifact_build_orchestration import ArtifactBuildOrchestrator
from gimme.control import ControlState
from gimme.deployer import CommandResult
from gimme.deployment_release_orchestration import DeploymentReleaseOrchestrator
from gimme.execution import execution_fingerprint


ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "config" / "state.example.json"


def load_artifact_program():
    spec = importlib.util.spec_from_file_location(
        "gimme_target_artifact_deployment", ROOT / "deploy" / "artifact.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


artifact_program = load_artifact_program()


class FakeS3:
    def __init__(self):
        self.objects: dict[str, dict[str, bytes | str]] = {}

    def add(self, key: str, version: str, value: bytes) -> None:
        self.objects.setdefault(key, {})[version] = value
        self.objects[key]["current"] = version

    def get_object(self, Bucket, Key, VersionId=None):
        if Key not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            )
        version = VersionId or self.objects[Key]["current"]
        value = self.objects[Key].get(version)
        if not isinstance(value, bytes):
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchVersion"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            )
        return {
            "Body": io.BytesIO(value),
            "VersionId": version,
            "ServerSideEncryption": "AES256",
        }


STORE = {
    "bucket": "gimme-artifacts",
    "region": "us-east-1",
    "endpoint": None,
    "addressing": "virtual_hosted",
    "encryption": {"method": "aes256"},
}
BUILD_IDENTITY = {
    "application": "example",
    "repository_fingerprint": "repo_" + "1" * 64,
    "commit": "b" * 40,
    "composer_lock_sha256": "2" * 64,
    "composer_lock_bytes": 100,
    "frontend_lock": None,
    "capability": {
        "php": "8.4.1",
        "composer": "2.8.4",
        "php_extensions": [],
        "system": "linux",
        "machine": "x86_64",
        "frontend": None,
    },
    "build_policy": {},
    "runtimes": {},
    "php_extensions": [],
    "frontend": None,
    "packaging_version": "laravel_v1",
    "execution_fingerprint": "exec_" + "3" * 64,
}
RELEASE_CONTRACT = {
    "schema": "laravel_release_v1",
    "health_sha256": "4" * 64,
    "processes_sha256": "5" * 64,
}


def materialize_request(artifact: dict[str, object]) -> dict[str, object]:
    return {
        "artifact": artifact,
        "store": STORE,
        "build_identity": BUILD_IDENTITY,
        "release_contract": RELEASE_CONTRACT,
    }


def seed_artifact(tmp_path: Path, fake: FakeS3):
    source = tmp_path / "source"
    source.mkdir()
    application_file = source / "artisan"
    application_file.write_text("#!/usr/bin/env php\n<?php\n")
    os.chmod(application_file, 0o755)
    entries = [("artisan", application_file)]
    tree_digest = artifact_program.tree_digest(entries)
    archive = tmp_path / "artifact.tar.gz"
    artifact_program.create_archive(entries, archive)
    archive_bytes = archive.read_bytes()
    encoded = json.dumps(BUILD_IDENTITY, sort_keys=True, separators=(",", ":")).encode()
    build_id = "build_v1_" + hashlib.sha256(b"gimme-build-v1\0" + encoded).hexdigest()
    package_key, manifest_key, _ = artifact_program.object_keys("example", build_id)
    package_version, manifest_version = "package-v1", "manifest-v1"
    manifest = {
        "schema_version": 2,
        "application": "example",
        "build_id": build_id,
        "commit": "b" * 40,
        "format": "laravel_v1",
        "artifact_digest": hashlib.sha256(archive_bytes).hexdigest(),
        "tree_digest": tree_digest,
        "bytes": len(archive_bytes),
        "package_version": package_version,
        "published_at": "2026-09-20T12:00:00+00:00",
        "build_secrets_used": False,
        "build_secret_count": 0,
    }
    fake.add(package_key, package_version, archive_bytes)
    fake.add(
        manifest_key,
        manifest_version,
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
    )
    return build_id, manifest, manifest_version


def test_resolve_reports_missing_without_storage_identity(tmp_path: Path, monkeypatch) -> None:
    fake = FakeS3()
    monkeypatch.setattr(artifact_program, "s3_client", lambda *_arguments: fake)
    build_id = "build_v1_" + "a" * 64

    assert artifact_program.resolve_artifact(
        {"application": "example", "build_id": build_id, "store": STORE}, "-"
    ) == {"status": "missing", "application": "example", "build_id": build_id}


def test_exact_artifact_is_resolved_and_materialized_with_readonly_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeS3()
    build_id, manifest, manifest_version = seed_artifact(tmp_path, fake)
    monkeypatch.setattr(artifact_program, "s3_client", lambda *_arguments: fake)
    artifact = artifact_program.resolve_artifact(
        {"application": "example", "build_id": build_id, "store": STORE}, "-"
    )
    assert artifact["status"] == "ready"
    assert artifact["manifest_version"] == manifest_version

    apps_root = tmp_path / "apps"
    release = apps_root / "deployments" / "example" / "releases" / "1"
    release.mkdir(parents=True)
    workspace_root = artifact_program.checked_root(str(apps_root))
    result = artifact_program.materialize_artifact(
        materialize_request(artifact),
        "-",
        workspace_root,
        apps_root,
        str(release),
    )

    assert result == {
        "status": "materialized", "application": "example", "build_id": build_id,
    }
    assert (release / "artisan").read_text() == "#!/usr/bin/env php\n<?php\n"
    metadata = release / ".gimme-artifact.json"
    assert stat_mode(metadata) == 0o444
    expected_metadata = {
        "application": "example",
        "commit": manifest["commit"],
        "build_id": build_id,
        "artifact_digest": manifest["artifact_digest"],
        "tree_digest": manifest["tree_digest"],
        "bytes": manifest["bytes"],
        "manifest_version": manifest_version,
        "package_version": manifest["package_version"],
        "schema_version": 2,
        "build_secrets_used": False,
        "build_secret_count": 0,
        "packaging_schema": "laravel_v1",
        "release_mode": "artifact",
        "promotion_seed": {
            field: BUILD_IDENTITY[field]
            for field in sorted({
                "repository_fingerprint", "composer_lock_sha256",
                "composer_lock_bytes", "frontend_lock", "capability",
            })
        },
        "release_contract": RELEASE_CONTRACT,
    }
    assert json.loads(metadata.read_text()) == expected_metadata
    current = release.parent.parent / "current"
    current.symlink_to(release)
    assert artifact_program.inspect_live_release(apps_root, str(current)) == expected_metadata
    metadata.unlink()
    with pytest.raises(
        artifact_program.ArtifactFailure, match="artifact_release_metadata_invalid"
    ):
        artifact_program.inspect_live_release(apps_root, str(current))
    metadata.write_text(json.dumps(expected_metadata, sort_keys=True, separators=(",", ":")))
    os.chmod(metadata, 0o444)
    (release / "artisan").write_text("tampered")
    with pytest.raises(
        artifact_program.ArtifactFailure, match="artifact_tree_digest_mismatch"
    ):
        artifact_program.inspect_live_release(apps_root, str(current))
    assert list(workspace_root.iterdir()) == []


def test_corrupt_exact_package_fails_resolution(tmp_path: Path, monkeypatch) -> None:
    fake = FakeS3()
    build_id, manifest, _ = seed_artifact(tmp_path, fake)
    package_key, _, _ = artifact_program.object_keys("example", build_id)
    fake.add(package_key, str(manifest["package_version"]), b"corrupt")
    monkeypatch.setattr(artifact_program, "s3_client", lambda *_arguments: fake)

    with pytest.raises(artifact_program.ArtifactFailure, match="artifact_checksum_invalid"):
        artifact_program.resolve_artifact(
            {"application": "example", "build_id": build_id, "store": STORE}, "-"
        )


def test_tree_mismatch_removes_incomplete_release(tmp_path: Path, monkeypatch) -> None:
    fake = FakeS3()
    build_id, manifest, manifest_version = seed_artifact(tmp_path, fake)
    manifest["tree_digest"] = "d" * 64
    _, manifest_key, _ = artifact_program.object_keys("example", build_id)
    fake.add(manifest_key, manifest_version, json.dumps(manifest).encode())
    monkeypatch.setattr(artifact_program, "s3_client", lambda *_arguments: fake)
    artifact = artifact_program.resolve_artifact(
        {"application": "example", "build_id": build_id, "store": STORE}, "-"
    )
    apps_root = tmp_path / "apps"
    release = apps_root / "deployments" / "example" / "releases" / "1"
    release.mkdir(parents=True)
    workspace_root = artifact_program.checked_root(str(apps_root))

    with pytest.raises(artifact_program.ArtifactFailure, match="artifact_tree_digest_mismatch"):
        artifact_program.materialize_artifact(
            materialize_request(artifact),
            "-",
            workspace_root,
            apps_root,
            str(release),
        )
    assert not release.exists()


def test_interruption_removes_download_and_incomplete_release(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeS3()
    build_id, _, _ = seed_artifact(tmp_path, fake)
    monkeypatch.setattr(artifact_program, "s3_client", lambda *_arguments: fake)
    artifact = artifact_program.resolve_artifact(
        {"application": "example", "build_id": build_id, "store": STORE}, "-"
    )
    apps_root = tmp_path / "apps"
    release = apps_root / "deployments" / "example" / "releases" / "1"
    release.mkdir(parents=True)
    workspace_root = artifact_program.checked_root(str(apps_root))
    def interrupt(*_arguments):
        raise KeyboardInterrupt

    monkeypatch.setattr(artifact_program, "extract_release", interrupt)

    with pytest.raises(KeyboardInterrupt):
        artifact_program.materialize_artifact(
            materialize_request(artifact),
            "-",
            workspace_root,
            apps_root,
            str(release),
        )

    assert not release.exists()
    assert list(workspace_root.iterdir()) == []


def test_reader_denial_is_redacted_and_never_materializes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    secret = "private-reader-credential"

    class DeniedS3:
        def get_object(self, **_arguments):
            raise ClientError(
                {
                    "Error": {"Code": "AccessDenied", "Message": secret},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                "GetObject",
            )

    monkeypatch.setattr(artifact_program, "s3_client", lambda *_arguments: DeniedS3())
    build_id = "build_v1_" + "a" * 64
    with pytest.raises(ClientError) as caught:
        artifact_program.resolve_artifact(
            {"application": "example", "build_id": build_id, "store": STORE}, "-"
        )

    artifact_program.safe_exception_hook(ClientError, caught.value, None)
    output = capsys.readouterr().err
    assert output == "GIMME_ARTIFACT_ERROR|artifact_operation_failed\n"
    assert secret not in output


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class DesiredState:
    def __init__(self, state: ControlState, root: Path):
        self.state = state
        self.secrets_path = root / "secrets.enc.json"

    def load(self):
        return self.state


class ExpectedBuild:
    def __init__(self, state: ControlState):
        self.state = state

    def expected_build(self, name: str):
        deployment = self.state.deployments[name]
        application = self.state.applications[deployment.application]
        build = application.build
        assert build is not None
        return {
            "state": self.state,
            "deployment": deployment,
            "application": application,
            "build": build,
            "target": self.state.targets[build.target],
            "definition": self.state.artifact_stores[build.artifact_store],
            "identity": {
                "commit": deployment.source.ref,
                "capability": {"system": "linux", "machine": "x86_64"},
            },
            "build_id": "build_v1_" + "a" * 64,
            "build_secret_versions": [],
        }


class ResolveRunner:
    def __init__(self, artifact: dict[str, object]):
        self.artifact = artifact
        self.calls = []

    def run(self, task, _server, **kwargs):
        self.calls.append((task, kwargs))
        encoded = base64.b64encode(json.dumps(self.artifact).encode()).decode()
        return CommandResult([], 0, "GIMME_ARTIFACT_RESULT|" + encoded)


def artifact_state() -> ControlState:
    document = json.loads(EXAMPLE.read_text())
    document["deployments"]["example-local"]["release_mode"] = "artifact"
    document["deployments"]["example-local"]["source"] = {
        "kind": "commit", "ref": "a" * 40,
    }
    document["applications"]["example"]["frontend"] = None
    document["deployments"]["example-local"]["runtimes"] = {
        "php": {"provider": "system", "version": "8.4.1"},
        "composer": {"provider": "system", "version": "2.8.4"},
    }
    return ControlState.model_validate(document)


def resolved_artifact(status: str = "ready") -> dict[str, object]:
    common = {
        "status": status,
        "application": "example",
        "build_id": "build_v1_" + "a" * 64,
    }
    if status == "missing":
        return common
    return {
        **common,
        "commit": "a" * 40,
        "schema_version": 2,
        "format": "laravel_v1",
        "artifact_digest": "b" * 64,
        "tree_digest": "c" * 64,
        "bytes": 4096,
        "package_version": "package-v1",
        "manifest_version": "manifest-v1",
        "build_secrets_used": False,
        "build_secret_count": 0,
    }


@pytest.mark.parametrize("status", ["ready", "missing"])
def test_artifact_resolution_uses_destination_reader_and_returns_bounded_evidence(
    tmp_path: Path, status: str
) -> None:
    state = artifact_state()
    runner = ResolveRunner(resolved_artifact(status))
    operations = ArtifactDeploymentOrchestrator(
        store=DesiredState(state, tmp_path),
        runner=runner,
        build_orchestrator=ExpectedBuild(state),
        legacy_server=lambda target: target,
    )

    context = operations.context("example-local")
    assert context["artifact"] == resolved_artifact(status)
    task, kwargs = runner.calls[0]
    assert task == "gimme:artifact:run"
    assert kwargs["artifact_request"]["operation"] == "resolve"
    assert kwargs["artifact_request"]["application"] == "example"
    assert kwargs["artifact_request"]["build_id"] == "build_v1_" + "a" * 64
    assert kwargs["artifact_request"]["store"]["bucket"] == (
        state.artifact_stores["primary"].bucket
    )
    assert "repository" not in kwargs["artifact_request"]


class ArtifactSupport:
    def __init__(self, state: ControlState, status: str = "ready"):
        self.state = state
        self.status = status

    def context(self, name: str):
        deployment = self.state.deployments[name]
        application = self.state.applications[deployment.application]
        build = application.build
        assert build is not None
        return {
            "state": self.state,
            "deployment": deployment,
            "application": application,
            "build": build,
            "target": self.state.targets[build.target],
            "definition": self.state.artifact_stores[build.artifact_store],
            "identity": {
                "commit": deployment.source.ref,
                "capability": {"system": "linux", "machine": "x86_64"},
            },
            "build_id": "build_v1_" + "a" * 64,
            "build_secret_versions": [],
            "destination_target": self.state.targets[deployment.target],
            "reader_credential_versions": [],
            "reader_credentials": {},
            "artifact": resolved_artifact(self.status),
        }

    def materialize_request(self, context):
        return {
            "operation": "materialize",
            "artifact": context["artifact"],
            "store": STORE,
            "build_identity": context["identity"],
            "release_contract": RELEASE_CONTRACT,
        }

    def apply_arguments(self, context):
        return self.materialize_request(context), nullcontext(None)


def release_operations(state: ControlState, support: ArtifactSupport, calls: list):
    @contextmanager
    def lock(_name):
        yield

    def run(task, name, **kwargs):
        calls.append((task, name, kwargs))
        if task == "gimme:preflight:processes":
            output = "\n".join([
                "GIMME_PROCESS_HELPER|ready",
                "GIMME_PCNTL|ready",
                "GIMME_POSIX|ready",
            ])
        elif task == "gimme:preflight:artifact-runtimes":
            output = "\n".join([
                "GIMME_RUNTIME|php|8.4.1",
                "GIMME_PLATFORM|linux|x86_64",
                *(
                    f"GIMME_PHP_EXTENSION|{extension}|ready"
                    for extension in state.applications["example"].php_extensions
                ),
            ])
        elif task == "deploy" and kwargs.get("arguments") == ("--plan",):
            output = "artifact deployment task graph"
        else:
            output = task
        return CommandResult([], 0, output)

    def assert_plan(expected, actual):
        if expected["plan_id"] != actual:
            raise ValueError("plan does not match current state")

    return DeploymentReleaseOrchestrator(
        store=type("Store", (), {"deployment": lambda _self, name: state.deployments[name]})(),
        context=lambda name: (
            state,
            state.deployments[name],
            state.targets[state.deployments[name].target],
            state.applications[state.deployments[name].application],
        ),
        run_deployment=run,
        secret_plan=lambda *_arguments: ([], []),
        dns_issues=lambda *_arguments: [],
        managed_database_issues=lambda *_arguments: [],
        valkey_runtime=lambda *_arguments: (None, None, None, []),
        deployment_resource_lock=lock,
        deployment_resource_locks=None,
        assert_plan=assert_plan,
        replace=None,
        result=lambda value: value.as_dict(),
        artifact_deployment=support,
    )


def test_artifact_release_plan_binds_versions_and_apply_uses_materialization() -> None:
    state = artifact_state()
    calls: list = []
    operations = release_operations(state, ArtifactSupport(state), calls)

    plan = operations.plan_deployment("example-local")
    assert plan["ready"] is True
    assert plan["release_mode"] == "artifact"
    assert plan["artifact"] == {
        "expected_build_id": "build_v1_" + "a" * 64,
        "reader_credential_versions": [],
        "publication": resolved_artifact(),
    }
    assert plan["runtimes"]["declared"] == {
        "php": {"provider": "system", "version": "8.4.1"},
        "php_extensions": state.applications["example"].php_extensions,
    }

    result = operations.apply_deployment("example-local", plan["plan_id"])
    assert result["output"] == "deploy"
    applied = [call for call in calls if call[0] == "deploy" and not call[2].get("arguments")]
    assert applied[-1][2]["artifact_request"]["operation"] == "materialize"
    assert applied[-1][2]["artifact_secret_file"] is None


def test_missing_artifact_is_an_inspectable_not_ready_plan() -> None:
    state = artifact_state()
    calls: list = []
    operations = release_operations(state, ArtifactSupport(state, "missing"), calls)

    plan = operations.plan_deployment("example-local")
    assert plan["ready"] is False
    assert plan["readiness_issues"] == ["artifact_missing"]
    assert not any(call[0] in {"gimme:preflight:artifact-runtimes", "deploy"} for call in calls)
    with pytest.raises(ValueError, match="not ready"):
        operations.apply_deployment("example-local", plan["plan_id"])


def test_changed_publication_rejects_stale_plan_before_deploy() -> None:
    state = artifact_state()
    calls: list = []
    support = ArtifactSupport(state)
    operations = release_operations(state, support, calls)
    plan = operations.plan_deployment("example-local")
    support.status = "missing"

    with pytest.raises(ValueError, match="plan does not match current state"):
        operations.apply_deployment("example-local", plan["plan_id"])

    applied = [call for call in calls if call[0] == "deploy" and not call[2].get("arguments")]
    assert applied == []


def test_runtime_incompatibility_stops_before_plan_render_or_deploy() -> None:
    state = artifact_state()
    calls: list = []
    operations = release_operations(state, ArtifactSupport(state), calls)
    original_run = operations.run_deployment

    def incompatible(task, name, **kwargs):
        if task == "gimme:preflight:artifact-runtimes":
            return CommandResult([], 0, "\n".join([
                "GIMME_RUNTIME|php|8.4.1",
                "GIMME_PLATFORM|linux|aarch64",
                *(
                    f"GIMME_PHP_EXTENSION|{extension}|ready"
                    for extension in state.applications["example"].php_extensions
                ),
            ]))
        return original_run(task, name, **kwargs)

    operations = DeploymentReleaseOrchestrator(
        **{
            **operations.__dict__,
            "run_deployment": incompatible,
        }
    )

    with pytest.raises(RuntimeError, match="artifact_runtime_incompatible"):
        operations.plan_deployment("example-local")

    assert not any(call[0] == "deploy" for call in calls)


@pytest.mark.parametrize(
    "kind",
    [
        "absolute",
        "case_conflict",
        "duplicate",
        "hardlink",
        "metadata_collision",
        "noncanonical",
        "special",
        "traversal",
        "unsafe_symlink",
    ],
)
def test_malicious_archive_is_rejected_and_incomplete_release_removed(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    fake = FakeS3()
    build_id, manifest, manifest_version = seed_artifact(tmp_path, fake)
    package_key, manifest_key, _ = artifact_program.object_keys("example", build_id)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        first = tarfile.TarInfo("App.php")
        first.size = 1
        archive.addfile(first, io.BytesIO(b"a"))
        second = tarfile.TarInfo({
            "absolute": "/escape.php",
            "hardlink": "linked.php",
            "case_conflict": "app.php",
            "duplicate": "App.php",
            "metadata_collision": ".GIMME-ARTIFACT.JSON",
            "noncanonical": "./App.php",
            "special": "device",
            "traversal": "../escape.php",
            "unsafe_symlink": "linked.php",
        }[kind])
        if kind == "hardlink":
            second.type = tarfile.LNKTYPE
            second.linkname = "App.php"
            archive.addfile(second)
        elif kind == "unsafe_symlink":
            second.type = tarfile.SYMTYPE
            second.mode = 0o777
            second.linkname = "../../escape.php"
            archive.addfile(second)
        elif kind == "special":
            second.type = tarfile.CHRTYPE
            archive.addfile(second)
        else:
            second.size = 1
            archive.addfile(second, io.BytesIO(b"b"))
    package = buffer.getvalue()
    manifest.update({
        "artifact_digest": hashlib.sha256(package).hexdigest(),
        "bytes": len(package),
        "tree_digest": "c" * 64,
    })
    fake.add(package_key, str(manifest["package_version"]), package)
    fake.add(manifest_key, manifest_version, json.dumps(manifest).encode())
    monkeypatch.setattr(artifact_program, "s3_client", lambda *_arguments: fake)
    artifact = artifact_program.resolve_artifact(
        {"application": "example", "build_id": build_id, "store": STORE}, "-"
    )
    apps_root = tmp_path / "apps"
    release = apps_root / "deployments" / "example" / "releases" / "1"
    release.mkdir(parents=True)
    workspace_root = artifact_program.checked_root(str(apps_root))

    expected_error = "unsafe_symlink" if kind == "unsafe_symlink" else "artifact_archive_unsafe"
    with pytest.raises(artifact_program.ArtifactFailure, match=expected_error):
        artifact_program.materialize_artifact(
            materialize_request(artifact),
            "-",
            workspace_root,
            apps_root,
            str(release),
        )
    assert not release.exists()
    assert list(workspace_root.iterdir()) == []


@pytest.mark.parametrize(
    ("limit", "code"),
    [
        ("MAX_FILES", "artifact_archive_too_many_files"),
        ("MAX_FILE_BYTES", "artifact_archive_too_large"),
        ("MAX_TREE_BYTES", "artifact_archive_too_large"),
    ],
)
def test_extractor_enforces_archive_bounds(
    tmp_path: Path, monkeypatch, limit: str, code: str
) -> None:
    fake = FakeS3()
    build_id, _, _ = seed_artifact(tmp_path, fake)
    monkeypatch.setattr(artifact_program, "s3_client", lambda *_arguments: fake)
    artifact = artifact_program.resolve_artifact(
        {"application": "example", "build_id": build_id, "store": STORE}, "-"
    )
    apps_root = tmp_path / "apps"
    release = apps_root / "deployments" / "example" / "releases" / "1"
    release.mkdir(parents=True)
    workspace_root = artifact_program.checked_root(str(apps_root))
    monkeypatch.setattr(artifact_program, limit, 0)

    with pytest.raises(artifact_program.ArtifactFailure, match=code):
        artifact_program.materialize_artifact(
            materialize_request(artifact),
            "-",
            workspace_root,
            apps_root,
            str(release),
        )

    assert not release.exists()
    assert list(workspace_root.iterdir()) == []


def write_artifact_release(root: Path, name: str, marker: str) -> dict[str, object]:
    release = root / "releases" / name
    release.mkdir(parents=True)
    artisan = release / "artisan"
    artisan.write_text(marker)
    os.chmod(artisan, 0o755)
    tree_digest = artifact_program.tree_digest([("artisan", artisan)])
    metadata = {
        "application": "example",
        "commit": marker * 40,
        "build_id": "build_v1_" + marker * 64,
        "artifact_digest": marker * 64,
        "tree_digest": tree_digest,
        "bytes": 100,
        "manifest_version": f"manifest-v{name}",
        "package_version": f"package-v{name}",
        "schema_version": 2,
        "build_secrets_used": False,
        "build_secret_count": 0,
        "packaging_schema": "laravel_v1",
        "release_mode": "artifact",
        "promotion_seed": {
            field: BUILD_IDENTITY[field]
            for field in sorted({
                "repository_fingerprint", "composer_lock_sha256",
                "composer_lock_bytes", "frontend_lock", "capability",
            })
        },
        "release_contract": RELEASE_CONTRACT,
    }
    metadata_path = release / ".gimme-artifact.json"
    metadata_path.write_text(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
    os.chmod(metadata_path, 0o444)
    return metadata


@pytest.mark.parametrize("release_mode", ["source", "artifact"])
def test_rollback_inventory_selects_and_verifies_exact_predecessor(
    tmp_path: Path, release_mode: str
) -> None:
    apps_root = tmp_path / "apps"
    deploy_path = apps_root / "deployments" / "example"
    (deploy_path / "releases").mkdir(parents=True)
    expected = {}
    for name, marker in (("1", "a"), ("2", "b")):
        release = deploy_path / "releases" / name
        if release_mode == "artifact":
            expected[name] = write_artifact_release(deploy_path, name, marker)
        else:
            release.mkdir()
            (release / "REVISION").write_text(marker * 40)
            expected[name] = {"commit": marker * 40, "release_mode": "source"}
    (deploy_path / ".dep").mkdir()
    (deploy_path / ".dep" / "releases_log").write_text(
        '\n'.join(json.dumps({"release_name": name}) for name in ("1", "2"))
    )
    (deploy_path / "current").symlink_to(deploy_path / "releases" / "2")

    result = artifact_program.rollback_inventory(
        apps_root, str(deploy_path), release_mode
    )

    assert result["status"] == "ready"
    assert result["current"] == {"release": "2", "identity": expected["2"]}
    assert result["target"] == {"release": "1", "identity": expected["1"]}
    assert len(result["inventory_sha256"]) == 64

    stale_request = {
        "operation": "rollback",
        "release_mode": release_mode,
        "expected": {
            "inventory_sha256": "0" * 64,
            "current_release": "2",
            "target_release": "1",
            "current_metadata_sha256": "0" * 64,
            "target_metadata_sha256": "0" * 64,
        },
    }
    encoded = base64.b64encode(json.dumps(stale_request).encode()).decode()
    with pytest.raises(artifact_program.ArtifactFailure, match="rollback_plan_stale"):
        artifact_program.main([
            "artifact.py", encoded, "-", str(apps_root), str(deploy_path)
        ])

    bad = deploy_path / "releases" / "1" / "BAD_RELEASE"
    bad.write_text("bad")
    with pytest.raises(artifact_program.ArtifactFailure, match="rollback_release_missing"):
        artifact_program.rollback_inventory(apps_root, str(deploy_path), release_mode)
    bad.unlink()

    if release_mode == "artifact":
        (deploy_path / "releases" / "1" / "artisan").write_text("tampered")
        with pytest.raises(
            artifact_program.ArtifactFailure, match="artifact_tree_digest_mismatch"
        ):
            artifact_program.rollback_inventory(
                apps_root, str(deploy_path), release_mode
            )


def promotion_state() -> ControlState:
    state = artifact_state()
    original = state.deployments["example-local"]
    source = original.model_copy(update={
        "placement": original.placement.model_copy(update={
            "instance": "artifact-source",
            "relative_path": "deployments/artifact-source",
            "database_identifier": "artifact_source",
            "cache_prefix": "gimme:artifact-source:",
            "site_host": "artifact-source.devbox.local",
        }),
    })
    destination = original.model_copy(update={
        "source": original.source.model_copy(update={"kind": "branch", "ref": "main"}),
        "placement": original.placement.model_copy(update={
            "instance": "artifact-destination",
            "relative_path": "deployments/artifact-destination",
            "database_identifier": "artifact_destination",
            "cache_prefix": "gimme:artifact-destination:",
            "site_host": "artifact-destination.devbox.local",
        }),
    })
    return state.model_copy(update={
        "deployments": {"artifact-source": source, "artifact-destination": destination}
    })


def test_promotion_build_identity_is_recomputed_without_build_target(tmp_path: Path) -> None:
    state = promotion_state()
    deployment = state.deployments["artifact-destination"]
    application = state.applications[deployment.application]
    build = application.build
    assert build is not None
    seed = {
        "repository_fingerprint": "repo_" + "1" * 64,
        "composer_lock_sha256": "2" * 64,
        "composer_lock_bytes": 100,
        "frontend_lock": None,
        "capability": {
            "php": "8.4.1",
            "composer": "2.8.4",
            "php_extensions": application.php_extensions,
            "system": "linux",
            "machine": "x86_64",
            "frontend": None,
        },
    }
    identity = {
        "application": deployment.application,
        **seed,
        "commit": "a" * 40,
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
        "frontend": None,
        "packaging_version": "laravel_v1",
        "execution_fingerprint": execution_fingerprint(),
    }
    metadata = {
        "application": deployment.application,
        "commit": "a" * 40,
        "build_id": ArtifactBuildOrchestrator.build_id(identity),
        "promotion_seed": seed,
    }
    operations = ArtifactDeploymentOrchestrator(
        store=DesiredState(state, tmp_path),
        runner=None,
        build_orchestrator=ArtifactBuildOrchestrator,
        legacy_server=lambda target: target,
    )

    expected = operations.expected_from_release("artifact-destination", metadata)

    assert expected["identity"] == identity
    assert expected["build_id"] == metadata["build_id"]


class MemoryStore:
    def __init__(self, state: ControlState):
        self.state = state

    def load(self):
        return self.state

    def save(self, state):
        self.state = state

    def deployment(self, name):
        return self.state.deployments[name]


class PromotionSupport:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.compatible = True
        self.reader_denied = False
        self.manifest_version = "manifest-v1"
        self.resolve_calls = 0

    def live_release(self, name):
        state = self.store.load()
        deployment = state.deployments[name]
        application = state.applications[deployment.application]
        return {
            "application": deployment.application,
            "commit": "a" * 40,
            "build_id": "build_v1_" + "a" * 64,
            "artifact_digest": "b" * 64,
            "tree_digest": "c" * 64,
            "bytes": 4096,
            "manifest_version": self.manifest_version,
            "package_version": "package-v1",
            "schema_version": 2,
            "build_secrets_used": False,
            "build_secret_count": 0,
            "packaging_schema": "laravel_v1",
            "release_mode": "artifact",
            "promotion_seed": {
                "repository_fingerprint": "repo_" + "1" * 64,
                "composer_lock_sha256": "2" * 64,
                "composer_lock_bytes": 100,
                "frontend_lock": None,
                "capability": {
                    "php": "8.4.1",
                    "composer": "2.8.4",
                    "php_extensions": application.php_extensions,
                    "system": "linux",
                    "machine": "x86_64",
                    "frontend": None,
                },
            },
            "release_contract": release_contract(deployment, application),
        }

    def expected_from_release(self, name, metadata):
        state = self.store.load()
        deployment = state.deployments[name]
        application = state.applications[deployment.application]
        build = application.build
        assert build is not None
        return {
            "state": state,
            "deployment": deployment,
            "application": application,
            "build": build,
            "target": state.targets[build.target],
            "definition": state.artifact_stores[build.artifact_store],
            "identity": {
                "commit": metadata["commit"],
                "capability": {"system": "linux", "machine": "x86_64"},
            },
            "build_id": (
                metadata["build_id"] if self.compatible else "build_v1_" + "f" * 64
            ),
            "build_secret_versions": [],
        }

    def resolve_expected(self, _name, expected):
        self.resolve_calls += 1
        if self.reader_denied:
            raise RuntimeError("artifact_operation_failed")
        metadata = self.live_release("artifact-source")
        return {
            **expected,
            "destination_target": self.store.load().targets[expected["deployment"].target],
            "reader_credential_versions": [],
            "reader_credentials": {},
            "artifact": DeploymentReleaseOrchestrator._metadata_artifact(metadata),
        }

    def materialize_request(self, context):
        return {
            "operation": "materialize",
            "artifact": context["artifact"],
            "store": STORE,
            "build_identity": context["identity"],
            "release_contract": release_contract(
                context["deployment"], context["application"]
            ),
        }

    def apply_arguments(self, context):
        return self.materialize_request(context), nullcontext(None)


class RollbackSupport(PromotionSupport):
    def __init__(self, store: MemoryStore, mode: str):
        super().__init__(store)
        self.mode = mode
        self.inventory_version = "1" * 64

    def rollback_inventory(self, name):
        if self.mode == "artifact":
            target = self.live_release(name)
            current = {**target, "commit": "b" * 40}
        else:
            target = {"commit": "a" * 40, "release_mode": "source"}
            current = {"commit": "b" * 40, "release_mode": "source"}
        return {
            "status": "ready",
            "release_mode": self.mode,
            "inventory_sha256": self.inventory_version,
            "current": {"release": "2", "identity": current},
            "target": {"release": "1", "identity": target},
        }


def promotion_operations(store: MemoryStore, support: PromotionSupport, calls: list):
    @contextmanager
    def locks(*_names):
        yield

    def run(task, name, **kwargs):
        calls.append((task, name, kwargs))
        if task == "gimme:preflight:artifact-runtimes":
            output = "\n".join([
                "GIMME_RUNTIME|php|8.4.1",
                "GIMME_PLATFORM|linux|x86_64",
                *(
                    f"GIMME_PHP_EXTENSION|{extension}|ready"
                    for extension in store.load().applications["example"].php_extensions
                ),
            ])
        elif task == "gimme:preflight:runtimes":
            deployment = store.load().deployments[name]
            output = "\n".join([
                *(
                    f"GIMME_RUNTIME|{runtime}|{pin.version}"
                    for runtime, pin in deployment.runtimes.items()
                ),
                *(
                    f"GIMME_PHP_EXTENSION|{extension}|ready"
                    for extension in store.load().applications["example"].php_extensions
                ),
                "GIMME_PLATFORM|linux|x86_64",
            ])
        elif task == "deploy" and kwargs.get("arguments") == ("--plan",):
            output = "artifact promotion task graph"
        else:
            output = "deployed"
        return CommandResult([], 0, output)

    def assert_plan(expected, actual):
        if expected["plan_id"] != actual:
            raise ValueError("plan does not match current state")

    return DeploymentReleaseOrchestrator(
        store=store,
        context=lambda name: (
            store.load(),
            store.load().deployments[name],
            store.load().targets[store.load().deployments[name].target],
            store.load().applications[store.load().deployments[name].application],
        ),
        run_deployment=run,
        secret_plan=lambda *_arguments: ([], []),
        dns_issues=lambda *_arguments: [],
        managed_database_issues=lambda *_arguments: [],
        valkey_runtime=lambda *_arguments: (None, None, None, []),
        deployment_resource_lock=locks,
        deployment_resource_locks=locks,
        assert_plan=assert_plan,
        replace=lambda state, _field, name, deployment: state.model_copy(update={
            "deployments": {**state.deployments, name: deployment}
        }),
        result=lambda value: value.as_dict(),
        artifact_deployment=support,
    )


def test_artifact_promotion_reuses_live_publication_without_build_target() -> None:
    store = MemoryStore(promotion_state())
    support = PromotionSupport(store)
    calls: list = []
    operations = promotion_operations(store, support, calls)

    plan = operations.plan_promotion("artifact-source", "artifact-destination")
    assert plan["ready"] is True
    assert plan["compatibility"] == {
        "status": "compatible",
        "build_id_matches": True,
        "release_contract_matches": True,
    }
    assert plan["artifact"]["manifest_version"] == "manifest-v1"
    result = operations.promote_deployment(
        "artifact-source", "artifact-destination", plan["plan_id"]
    )

    assert result["output"] == "deployed"
    assert store.deployment("artifact-destination").source.ref == "a" * 40
    applied = [call for call in calls if call[0] == "deploy" and not call[2].get("arguments")]
    assert applied[-1][2]["artifact_request"]["artifact"] == plan["artifact"]


def test_incompatible_artifact_promotion_is_inspectable_and_does_not_resolve_reader() -> None:
    store = MemoryStore(promotion_state())
    support = PromotionSupport(store)
    support.compatible = False
    calls: list = []
    operations = promotion_operations(store, support, calls)

    plan = operations.plan_promotion("artifact-source", "artifact-destination")

    assert plan["ready"] is False
    assert plan["readiness_issues"] == ["artifact_incompatible"]
    assert support.resolve_calls == 0
    assert calls == []


def test_artifact_promotion_rejects_changed_health_contract_without_remote_mutation() -> None:
    store = MemoryStore(promotion_state())
    destination = store.deployment("artifact-destination").model_copy(
        update={"health": None}
    )
    store.state = store.state.model_copy(update={
        "deployments": {
            **store.state.deployments,
            "artifact-destination": destination,
        }
    })
    support = PromotionSupport(store)
    calls: list = []

    plan = promotion_operations(store, support, calls).plan_promotion(
        "artifact-source", "artifact-destination"
    )

    assert plan["compatibility"]["release_contract_matches"] is False
    assert plan["readiness_issues"] == ["artifact_incompatible"]
    assert support.resolve_calls == 0
    assert calls == []


def test_artifact_promotion_rejects_stale_source_metadata_before_activation() -> None:
    store = MemoryStore(promotion_state())
    support = PromotionSupport(store)
    calls: list = []
    operations = promotion_operations(store, support, calls)
    plan = operations.plan_promotion("artifact-source", "artifact-destination")
    support.manifest_version = "manifest-v2"

    with pytest.raises(ValueError, match="plan does not match current state"):
        operations.promote_deployment(
            "artifact-source", "artifact-destination", plan["plan_id"]
        )
    assert not any(call[0] == "deploy" and not call[2].get("arguments") for call in calls)


def test_artifact_promotion_reader_denial_is_fail_closed() -> None:
    store = MemoryStore(promotion_state())
    support = PromotionSupport(store)
    support.reader_denied = True
    calls: list = []

    with pytest.raises(RuntimeError, match="^artifact_operation_failed$"):
        promotion_operations(store, support, calls).plan_promotion(
            "artifact-source", "artifact-destination"
        )
    assert not any(call[0] == "deploy" for call in calls)


def test_failed_artifact_promotion_does_not_update_destination_source() -> None:
    store = MemoryStore(promotion_state())
    support = PromotionSupport(store)
    calls: list = []
    operations = promotion_operations(store, support, calls)
    plan = operations.plan_promotion("artifact-source", "artifact-destination")
    before = store.deployment("artifact-destination").source
    original_run = operations.run_deployment

    def fail_activation(task, name, **kwargs):
        if task == "deploy" and not kwargs.get("arguments"):
            raise RuntimeError("health_failed")
        return original_run(task, name, **kwargs)

    operations = DeploymentReleaseOrchestrator(
        **{**operations.__dict__, "run_deployment": fail_activation}
    )

    with pytest.raises(RuntimeError, match="health_failed"):
        operations.promote_deployment(
            "artifact-source", "artifact-destination", plan["plan_id"]
        )
    assert store.deployment("artifact-destination").source == before


@pytest.mark.parametrize("release_mode", ["source", "artifact"])
def test_content_addressed_rollback_plans_and_applies_exact_release(
    release_mode: str,
) -> None:
    state = promotion_state()
    deployment = state.deployments["artifact-destination"].model_copy(
        update={"release_mode": release_mode}
    )
    state = state.model_copy(update={
        "deployments": {**state.deployments, "artifact-destination": deployment}
    })
    store = MemoryStore(state)
    support = RollbackSupport(store, release_mode)
    calls: list = []
    operations = promotion_operations(store, support, calls)

    plan = operations.plan_rollback_deployment("artifact-destination")
    assert plan["ready"] is True
    assert plan["current"]["release"] == "2"
    assert plan["target"]["release"] == "1"
    assert "promotion_seed" not in plan["target"]["identity"]
    with pytest.raises(ValueError, match="ROLLBACK artifact-destination TO 1"):
        operations.rollback_deployment(
            "artifact-destination", plan["plan_id"], "wrong"
        )

    result = operations.rollback_deployment(
        "artifact-destination",
        plan["plan_id"],
        "ROLLBACK artifact-destination TO 1",
    )

    assert result["status"] == "rolled_back"
    assert "manifest_version" not in result["to"]["identity"]
    assert "package_version" not in result["to"]["identity"]
    applied = [call for call in calls if call[0] == "gimme:rollback"]
    assert applied[-1][2]["rollback_release"] == "1"
    assert applied[-1][2]["artifact_request"]["expected"]["target_release"] == "1"


def test_rollback_rejects_changed_inventory_before_switch() -> None:
    store = MemoryStore(promotion_state())
    support = RollbackSupport(store, "artifact")
    calls: list = []
    operations = promotion_operations(store, support, calls)
    plan = operations.plan_rollback_deployment("artifact-destination")
    support.inventory_version = "2" * 64

    with pytest.raises(ValueError, match="plan does not match current state"):
        operations.rollback_deployment(
            "artifact-destination",
            plan["plan_id"],
            "ROLLBACK artifact-destination TO 1",
        )
    assert not any(call[0] == "gimme:rollback" for call in calls)


def test_artifact_rollback_rejects_incompatible_retained_build() -> None:
    store = MemoryStore(promotion_state())
    support = RollbackSupport(store, "artifact")
    support.compatible = False

    with pytest.raises(RuntimeError, match="rollback_release_incompatible"):
        promotion_operations(store, support, []).plan_rollback_deployment(
            "artifact-destination"
        )


def test_interrupted_rollback_can_retry_the_same_reviewed_plan() -> None:
    store = MemoryStore(promotion_state())
    support = RollbackSupport(store, "artifact")
    calls: list = []
    operations = promotion_operations(store, support, calls)
    plan = operations.plan_rollback_deployment("artifact-destination")
    original_run = operations.run_deployment
    interrupted = True

    def interrupt_once(task, name, **kwargs):
        nonlocal interrupted
        if task == "gimme:rollback" and interrupted:
            interrupted = False
            raise KeyboardInterrupt
        return original_run(task, name, **kwargs)

    operations = DeploymentReleaseOrchestrator(
        **{**operations.__dict__, "run_deployment": interrupt_once}
    )
    confirmation = "ROLLBACK artifact-destination TO 1"

    with pytest.raises(KeyboardInterrupt):
        operations.rollback_deployment(
            "artifact-destination", plan["plan_id"], confirmation
        )
    assert operations.rollback_deployment(
        "artifact-destination", plan["plan_id"], confirmation
    )["status"] == "rolled_back"
