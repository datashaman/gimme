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

from gimme.artifact_deployment_orchestration import ArtifactDeploymentOrchestrator
from gimme.control import ControlState
from gimme.deployer import CommandResult
from gimme.deployment_release_orchestration import DeploymentReleaseOrchestrator


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
    build_id = "build_v1_" + "a" * 64
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
        {"artifact": artifact, "store": STORE},
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
    assert json.loads(metadata.read_text()) == {
        "application": "example",
        "commit": manifest["commit"],
        "build_id": build_id,
        "artifact_digest": manifest["artifact_digest"],
        "tree_digest": manifest["tree_digest"],
        "manifest_version": manifest_version,
        "packaging_schema": "laravel_v1",
        "release_mode": "artifact",
    }
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
            {"artifact": artifact, "store": STORE},
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
            {"artifact": artifact, "store": STORE},
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
            "identity": {"commit": deployment.source.ref},
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
            "identity": {"commit": deployment.source.ref},
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
            raise RuntimeError("runtime_incompatible")
        return original_run(task, name, **kwargs)

    operations = DeploymentReleaseOrchestrator(
        **{
            **operations.__dict__,
            "run_deployment": incompatible,
        }
    )

    with pytest.raises(RuntimeError, match="runtime_incompatible"):
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
            {"artifact": artifact, "store": STORE},
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
            {"artifact": artifact, "store": STORE},
            "-",
            workspace_root,
            apps_root,
            str(release),
        )

    assert not release.exists()
    assert list(workspace_root.iterdir()) == []
