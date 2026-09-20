import base64
import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from gimme.artifact_build_orchestration import ArtifactBuildOrchestrator
from gimme.control import ControlState, SecretReference
from gimme.deployer import CommandResult


ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "config" / "state.example.json"


def load_artifact_program():
    spec = importlib.util.spec_from_file_location(
        "gimme_target_artifact", ROOT / "deploy" / "artifact.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


artifact_program = load_artifact_program()


def backend_state() -> ControlState:
    document = json.loads(EXAMPLE.read_text())
    application = document["applications"]["example"]
    application["frontend"] = None
    deployment = document["deployments"]["example-local"]
    deployment["release_mode"] = "artifact"
    deployment["source"] = {"kind": "commit", "ref": "a" * 40}
    deployment["runtimes"] = {
        "php": {"provider": "system", "version": "8.4.1"},
        "composer": {"provider": "system", "version": "2.8.4"},
    }
    return ControlState.model_validate(document)


class DesiredState:
    def __init__(self, state: ControlState, root: Path):
        self.state = state
        self.secrets_path = root / "secrets.enc.json"

    def load(self):
        return self.state


def encoded(value: dict[str, object]) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


class PlanningRunner:
    def __init__(self):
        self.lock_digest = "b" * 64
        self.capability = {
            "php": "8.4.1",
            "composer": "2.8.4",
            "php_extensions": ["curl", "json"],
            "system": "linux",
            "machine": "x86_64",
        }
        self.publication = "absent"
        self.calls = []

    def run(self, task, _server, **kwargs):
        self.calls.append((task, kwargs))
        request = kwargs.get("artifact_request")
        if task == "gimme:artifact:run" and request["operation"] == "inspect":
            value = {
                "status": "ready",
                "commit": request["commit"],
                "repository_fingerprint": "repo_" + "c" * 64,
                "composer_lock_sha256": self.lock_digest,
                "composer_lock_bytes": 2048,
                "capability": self.capability,
            }
        elif task == "gimme:artifact:run" and request["operation"] == "publication":
            value = {"status": self.publication, "build_id": request["build_id"]}
        elif task == "gimme:artifact:run" and request["operation"] == "build":
            value = {
                "status": "published",
                "application": request["application"],
                "build_id": request["build_id"],
                "artifact_digest": "d" * 64,
                "tree_digest": "e" * 64,
                "bytes": 4096,
            }
        elif task == "gimme:artifact:run" and request["operation"] == "inventory":
            value = {"application": request["application"], "artifacts": []}
        else:
            raise AssertionError((task, kwargs))
        return CommandResult([], 0, "GIMME_ARTIFACT_RESULT|" + encoded(value))


def orchestrator(tmp_path: Path, runner: PlanningRunner, state=None):
    def assert_plan(expected, actual):
        if expected["plan_id"] != actual:
            raise ValueError("stale plan")

    return ArtifactBuildOrchestrator(
        store=DesiredState(state or backend_state(), tmp_path),
        runner=runner,
        assert_plan=assert_plan,
        legacy_server=lambda value: value,
        legacy_app=lambda application, deployment: (application, deployment),
    )


def test_build_plan_binds_every_reviewed_identity_input(tmp_path: Path) -> None:
    runner = PlanningRunner()
    plan = orchestrator(tmp_path, runner).plan_build_artifact("example-local")

    assert plan["build_id"].startswith("build_v1_")
    assert plan["identity"] == {
        "application": "example",
        "repository_fingerprint": "repo_" + "c" * 64,
        "commit": "a" * 40,
        "composer_lock_sha256": "b" * 64,
        "composer_lock_bytes": 2048,
        "build_policy": {
            "target": "buildbox",
            "artifact_store": "primary",
            "packaging": "laravel_v1",
            "secrets": {},
        },
        "runtimes": {
            "composer": {"provider": "system", "version": "2.8.4"},
            "php": {"provider": "system", "version": "8.4.1"},
        },
        "php_extensions": backend_state().applications["example"].php_extensions,
        "capability": runner.capability,
        "packaging_version": "laravel_v1",
        "execution_fingerprint": plan["execution_fingerprint"],
    }
    assert "git@github.com:example/example.git" not in json.dumps(plan)
    assert plan["publication"] == {"status": "absent", "build_id": plan["build_id"]}


@pytest.mark.parametrize("changed", ["lock", "capability", "commit"])
def test_build_identity_changes_with_reviewed_inputs(tmp_path: Path, changed: str) -> None:
    runner = PlanningRunner()
    selected = backend_state()
    operations = orchestrator(tmp_path, runner, selected)
    first = operations.plan_build_artifact("example-local")["build_id"]
    if changed == "lock":
        runner.lock_digest = "f" * 64
    elif changed == "capability":
        runner.capability = {**runner.capability, "machine": "aarch64"}
    else:
        deployment = selected.deployments["example-local"].model_copy(update={
            "source": selected.deployments["example-local"].source.model_copy(
                update={"ref": "9" * 40}
            )
        })
        selected = selected.model_copy(update={"deployments": {"example-local": deployment}})
        operations = orchestrator(tmp_path, runner, selected)
    assert operations.plan_build_artifact("example-local")["build_id"] != first


def test_changed_lock_makes_apply_plan_stale_before_build(tmp_path: Path) -> None:
    runner = PlanningRunner()
    operations = orchestrator(tmp_path, runner)
    plan = operations.plan_build_artifact("example-local")
    runner.lock_digest = "f" * 64

    with pytest.raises(ValueError, match="stale plan"):
        operations.build_artifact("example-local", plan["plan_id"])
    assert not any(
        call[1].get("artifact_request", {}).get("operation") == "build"
        for call in runner.calls
    )


def test_frontend_and_build_secrets_fail_before_remote_work(tmp_path: Path) -> None:
    runner = PlanningRunner()
    frontend = backend_state().model_dump(mode="json")
    frontend["applications"]["example"]["frontend"] = {
        "package_manager": "npm", "build_script": "build", "output_dir": "public/build"
    }
    frontend["deployments"]["example-local"]["runtimes"].update({
        "node": {"provider": "system", "version": "22.12.0"},
        "npm": {"provider": "bundled", "version": "10.9.0"},
    })
    with pytest.raises(ValueError, match="artifact_frontend_not_supported"):
        orchestrator(tmp_path, runner, ControlState.model_validate(frontend)).plan_build_artifact(
            "example-local"
        )
    assert runner.calls == []

    secret = backend_state().model_dump(mode="json")
    secret["applications"]["example"]["build"]["secrets"] = {
        "NPM_TOKEN": {
            "store": "local-sops", "secret": "example/build", "field": "NPM_TOKEN"
        }
    }
    with pytest.raises(ValueError, match="artifact_build_secrets_not_supported"):
        orchestrator(tmp_path, runner, ControlState.model_validate(secret)).plan_build_artifact(
            "example-local"
        )
    assert runner.calls == []


def test_build_secret_shape_never_accepts_plaintext() -> None:
    policy = backend_state().applications["example"].build
    assert policy is not None
    reference = SecretReference(
        store="local-sops", secret="example/build", field="NPM_TOKEN"
    )
    assert policy.model_copy(update={"secrets": {"NPM_TOKEN": reference}}).secrets
    with pytest.raises(ValidationError):
        type(policy).model_validate({
            "target": "buildbox",
            "artifact_store": "primary",
            "packaging": "laravel_v1",
            "secrets": {"NPM_TOKEN": "plaintext"},
        })


def test_empty_inventory_is_bounded_and_secret_safe(tmp_path: Path) -> None:
    runner = PlanningRunner()
    value = orchestrator(tmp_path, runner).list_artifacts("example")
    assert value == {"application": "example", "artifacts": []}
    request = runner.calls[-1][1]["artifact_request"]
    assert "repository" not in request and "credentials" not in request


def test_deterministic_archive_and_tree_digest(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "artisan").write_text("#!/usr/bin/env php\n")
    os.chmod(root / "artisan", 0o755)
    (root / "vendor").mkdir()
    (root / "vendor" / "autoload.php").write_text("<?php\n")
    entries = [("artisan", root / "artisan"), ("vendor", root / "vendor"),
               ("vendor/autoload.php", root / "vendor" / "autoload.php")]
    artifact_program.validate_tree(entries, root)
    digest = artifact_program.tree_digest(entries)
    first, second = tmp_path / "first.tar.gz", tmp_path / "second.tar.gz"
    artifact_program.create_archive(entries, first)
    artifact_program.create_archive(entries, second)

    assert first.read_bytes() == second.read_bytes()
    (tmp_path / "verify-one").mkdir()
    (tmp_path / "verify-two").mkdir()
    artifact_program.verify_archive(first, digest, tmp_path / "verify-one")
    artifact_program.verify_archive(second, digest, tmp_path / "verify-two")


def test_malicious_links_and_archive_members_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(artifact_program.ArtifactFailure, match="unsafe_symlink"):
        artifact_program.safe_link("vendor/link", "../../outside")

    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("escape")
        member.type = tarfile.LNKTYPE
        member.linkname = "../../outside"
        output.addfile(member)
    verification = tmp_path / "verification"
    verification.mkdir()
    with pytest.raises(artifact_program.ArtifactFailure, match="artifact_archive_unsafe"):
        artifact_program.verify_archive(archive, "0" * 64, verification)


class Body(io.BytesIO):
    pass


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.counter = 0

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, **kwargs):
        if IfNoneMatch == "*" and Key in self.objects:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"},
                 "ResponseMetadata": {"HTTPStatusCode": 412}},
                "PutObject",
            )
        self.counter += 1
        version = f"v{self.counter}"
        value = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects.setdefault(Key, {})[version] = value
        self.objects[Key]["current"] = version
        return {"VersionId": version, "ServerSideEncryption": "AES256"}

    def get_object(self, Bucket, Key, VersionId=None):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey"},
                 "ResponseMetadata": {"HTTPStatusCode": 404}},
                "GetObject",
            )
        version = VersionId or self.objects[Key]["current"]
        return {"Body": Body(self.objects[Key][version])}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys, **kwargs):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))[:MaxKeys]
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}


def real_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "composer.json").write_text(json.dumps({
        "name": "example/backend",
        "description": "Artifact build fixture",
        "license": "MIT",
        "require": {},
    }))
    subprocess.run(
        ["composer", "update", "--no-install", "--no-interaction", "--no-ansi"],
        cwd=repository,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (repository / "artisan").write_text("#!/usr/bin/env php\n<?php\n")
    os.chmod(repository / "artisan", 0o755)
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Gimme", "-c", "user.email=gimme@example.test",
         "commit", "--quiet", "-m", "fixture"],
        cwd=repository,
        check=True,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    return repository, commit


def local_runtime_request(repository: Path, commit: str) -> dict[str, object]:
    php = subprocess.run(
        ["php", "-r", "echo PHP_VERSION;"], check=True, text=True, capture_output=True
    ).stdout
    composer = subprocess.run(
        ["composer", "--version", "--no-ansi"], check=True, text=True, capture_output=True
    ).stdout.split()[2]
    return {
        "repository": str(repository),
        "commit": commit,
        "runtimes": {
            "php": {"provider": "system", "version": php},
            "composer": {"provider": "system", "version": composer},
        },
        "php_extensions": [],
    }


def test_real_backend_build_is_reproducible_idempotent_and_cleans_workspace(
    tmp_path: Path, monkeypatch,
) -> None:
    repository, commit = real_repository(tmp_path)
    apps_root = tmp_path / "apps"
    apps_root.mkdir(mode=0o700)
    request = local_runtime_request(repository, commit)
    inspection = artifact_program.inspect_source(request, artifact_program.checked_root(
        str(apps_root)
    ))
    fake = FakeS3()
    monkeypatch.setattr(artifact_program, "s3_client", lambda *_: fake)
    build_request = {
        **request,
        "application": "example",
        "composer_lock_sha256": inspection["composer_lock_sha256"],
        "capability": inspection["capability"],
        "build_id": "build_v1_" + "a" * 64,
        "store": {
            "bucket": "gimme-artifacts",
            "region": "us-east-1",
            "endpoint": None,
            "addressing": "virtual_hosted",
            "encryption": {"method": "aes256"},
        },
    }
    root = artifact_program.checked_root(str(apps_root))
    first = artifact_program.build(build_request, "-", root)
    second = artifact_program.build(build_request, "-", root)

    assert first["status"] == "published"
    assert second == {**first, "status": "idempotent"}
    assert list(root.iterdir()) == []

    package_key, manifest_key, _ = artifact_program.object_keys(
        "example", build_request["build_id"]
    )
    alternate = b"different deterministic bytes"
    package = fake.put_object(
        Bucket="gimme-artifacts", Key=package_key, Body=alternate,
        ServerSideEncryption="AES256",
    )
    manifest = json.loads(fake.get_object(
        Bucket="gimme-artifacts", Key=manifest_key
    )["Body"].read())
    manifest.update({
        "artifact_digest": hashlib.sha256(alternate).hexdigest(),
        "tree_digest": "f" * 64,
        "bytes": len(alternate),
        "package_version": package["VersionId"],
    })
    fake.put_object(
        Bucket="gimme-artifacts", Key=manifest_key,
        Body=json.dumps(manifest).encode(), ServerSideEncryption="AES256",
    )
    conflict = artifact_program.build(build_request, "-", root)
    assert conflict == {
        "status": "non_reproducible_build",
        "application": "example",
        "build_id": build_request["build_id"],
    }
    assert list(root.iterdir()) == []


def test_stale_lock_fails_and_cleans_workspace_before_dependency_execution(
    tmp_path: Path,
) -> None:
    repository, commit = real_repository(tmp_path)
    apps_root = tmp_path / "apps"
    apps_root.mkdir(mode=0o700)
    request = local_runtime_request(repository, commit)
    inspection = artifact_program.inspect_source(request, artifact_program.checked_root(
        str(apps_root)
    ))
    build_request = {
        **request,
        "application": "example",
        "composer_lock_sha256": "0" * 64,
        "capability": inspection["capability"],
        "build_id": "build_v1_" + "a" * 64,
        "store": {
            "bucket": "gimme-artifacts",
            "region": "us-east-1",
            "endpoint": None,
            "addressing": "virtual_hosted",
            "encryption": {"method": "aes256"},
        },
    }
    root = artifact_program.checked_root(str(apps_root))
    with pytest.raises(artifact_program.ArtifactFailure, match="build_plan_stale"):
        artifact_program.build(build_request, "-", root)
    assert list(root.iterdir()) == []


def test_inventory_reports_fixed_degradation_without_storage_identities(
    tmp_path: Path, monkeypatch,
) -> None:
    fake = FakeS3()
    monkeypatch.setattr(artifact_program, "s3_client", lambda *_: fake)
    build_id = "build_v1_" + "a" * 64
    package_key, manifest_key, _ = artifact_program.object_keys("example", build_id)
    package_body = b"archive"
    package = fake.put_object(
        Bucket="gimme-artifacts", Key=package_key, Body=package_body,
        ServerSideEncryption="AES256",
    )
    manifest = {
        "schema_version": 1,
        "application": "example",
        "build_id": build_id,
        "commit": "b" * 40,
        "format": "laravel_v1",
        "artifact_digest": hashlib.sha256(package_body).hexdigest(),
        "tree_digest": "c" * 64,
        "bytes": len(package_body),
        "package_version": package["VersionId"],
        "published_at": "2026-09-20T12:00:00+00:00",
    }
    fake.put_object(
        Bucket="gimme-artifacts", Key=manifest_key, Body=json.dumps(manifest).encode(),
        ServerSideEncryption="AES256",
    )
    request = {
        "application": "example",
        "store": {
            "bucket": "gimme-artifacts",
            "region": "us-east-1",
            "endpoint": None,
            "addressing": "virtual_hosted",
            "encryption": {"method": "aes256"},
        },
    }

    ready = artifact_program.inventory(request, "-")["artifacts"][0]
    assert ready["status"] == "ready"
    assert package_key not in json.dumps(ready) and manifest_key not in json.dumps(ready)

    fake.objects[package_key][package["VersionId"]] = b"tampered"
    assert artifact_program.inventory(request, "-")["artifacts"][0]["status"] == (
        "checksum_invalid"
    )
    del fake.objects[package_key]
    assert artifact_program.inventory(request, "-")["artifacts"][0]["status"] == "missing"

    current = fake.objects[manifest_key]["current"]
    fake.objects[manifest_key][current] = b"not-json"
    assert artifact_program.inventory(request, "-")["artifacts"][0]["status"] == "malformed"

    unsupported = {**manifest, "schema_version": 2}
    fake.objects[manifest_key][current] = json.dumps(unsupported).encode()
    assert artifact_program.inventory(request, "-")["artifacts"][0]["status"] == "unsupported"

    foreign = {**manifest, "application": "other"}
    fake.objects[manifest_key][current] = json.dumps(foreign).encode()
    assert artifact_program.inventory(request, "-")["artifacts"][0]["status"] == "foreign"


def test_provider_exception_hook_never_renders_exception_details(capsys) -> None:
    artifact_program.safe_exception_hook(RuntimeError, RuntimeError("credential-value"), None)
    assert capsys.readouterr().err == "GIMME_ARTIFACT_ERROR|artifact_operation_failed\n"


def test_timed_out_command_terminates_its_process_group(monkeypatch) -> None:
    killed: list[tuple[int, signal.Signals]] = []
    original_killpg = os.killpg
    monkeypatch.setattr(
        artifact_program.os,
        "killpg",
        lambda pid, sent_signal: (
            killed.append((pid, sent_signal)), original_killpg(pid, sent_signal)
        )[-1],
    )

    with pytest.raises(artifact_program.ArtifactFailure, match="build_command_timeout"):
        artifact_program.command(["/bin/sleep", "5"], timeout=0.01)

    assert killed and killed[0][1] == signal.SIGKILL
    assert artifact_program.active_process is None


def test_interrupted_build_cleans_workspace(tmp_path: Path, monkeypatch) -> None:
    apps_root = tmp_path / "apps"
    apps_root.mkdir(mode=0o700)
    root = artifact_program.checked_root(str(apps_root))

    def interrupted_clone(_request, workspace_root):
        workspace = Path(tempfile.mkdtemp(prefix="build-", dir=workspace_root))
        source = workspace / "source"
        source.mkdir()
        (source / "composer.lock").write_text("{}")
        return workspace, ["composer.lock"]

    monkeypatch.setattr(artifact_program, "clone_exact", interrupted_clone)
    monkeypatch.setattr(
        artifact_program,
        "runtime_capability",
        lambda _request: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    request = {
        "repository": "https://example.test/repository.git",
        "commit": "a" * 40,
        "runtimes": {
            "php": {"provider": "system", "version": "8.4.1"},
            "composer": {"provider": "system", "version": "2.8.4"},
        },
        "php_extensions": [],
        "application": "example",
        "composer_lock_sha256": hashlib.sha256(b"{}").hexdigest(),
        "capability": {},
        "build_id": "build_v1_" + "a" * 64,
        "store": {
            "bucket": "gimme-artifacts",
            "region": "us-east-1",
            "endpoint": None,
            "addressing": "virtual_hosted",
            "encryption": {"method": "aes256"},
        },
    }

    with pytest.raises(KeyboardInterrupt):
        artifact_program.build(request, "-", root)

    assert list(root.iterdir()) == []


def test_failed_checkout_cleans_workspace(tmp_path: Path, monkeypatch) -> None:
    apps_root = tmp_path / "apps"
    apps_root.mkdir(mode=0o700)
    root = artifact_program.checked_root(str(apps_root))
    monkeypatch.setattr(
        artifact_program,
        "checkout_exact",
        lambda *_arguments: (_ for _ in ()).throw(
            artifact_program.ArtifactFailure("build_command_failed")
        ),
    )

    with pytest.raises(artifact_program.ArtifactFailure, match="build_command_failed"):
        artifact_program.clone_exact(
            {
                "repository": "https://example.test/repository.git",
                "commit": "a" * 40,
            },
            root,
        )

    assert list(root.iterdir()) == []
