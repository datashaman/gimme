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

import gimme.artifact_build_orchestration as artifact_build_module
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
            "frontend": None,
        }
        self.publication = "absent"
        self.calls = []

    def run(self, task, _server, **kwargs):
        self.calls.append((task, kwargs))
        request = kwargs.get("artifact_request")
        if task == "gimme:artifact:run" and request["operation"] == "inspect":
            frontend = request["frontend"]
            capability = self.capability
            frontend_lock = None
            if frontend is not None:
                manager = frontend["package_manager"]
                lockfile = {
                    "npm": "package-lock.json", "pnpm": "pnpm-lock.yaml",
                    "yarn": "yarn.lock", "bun": "bun.lock",
                }[manager]
                frontend_lock = {"filename": lockfile, "sha256": "9" * 64, "bytes": 1024}
                capability = {**capability, "frontend": {
                    "manager": manager,
                    "manager_version": request["runtimes"][manager]["version"],
                    "node_version": (
                        None if manager == "bun" else request["runtimes"]["node"]["version"]
                    ),
                }}
            value = {
                "status": "ready",
                "commit": request["commit"],
                "repository_fingerprint": "repo_" + "c" * 64,
                "composer_lock_sha256": self.lock_digest,
                "composer_lock_bytes": 2048,
                "frontend_lock": frontend_lock,
                "capability": capability,
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
        "frontend_lock": None,
        "build_policy": {
            "target": "buildbox",
            "artifact_store": "primary",
            "packaging": "laravel_v1",
            "build_secrets_used": False,
            "build_secret_count": 0,
            "build_secret_names_sha256": hashlib.sha256(b"[]").hexdigest(),
        },
        "runtimes": {
            "composer": {"provider": "system", "version": "2.8.4"},
            "php": {"provider": "system", "version": "8.4.1"},
        },
        "php_extensions": backend_state().applications["example"].php_extensions,
        "frontend": None,
        "capability": runner.capability,
        "packaging_version": "laravel_v1",
        "execution_fingerprint": plan["execution_fingerprint"],
    }
    assert "git@github.com:example/example.git" not in json.dumps(plan)
    assert plan["publication"] == {"status": "absent", "build_id": plan["build_id"]}


def test_exact_artifact_resource_status_is_bounded_and_version_free(tmp_path: Path) -> None:
    runner = PlanningRunner()
    build_id = "build_v1_" + "a" * 64

    assert orchestrator(tmp_path, runner).artifact_status("example", build_id) == {
        "status": "absent", "build_id": build_id,
    }
    with pytest.raises(ValueError, match="build_id is invalid"):
        orchestrator(tmp_path, runner).artifact_status("example", "latest")


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


def test_unsupported_frontend_output_fails_before_remote_work(tmp_path: Path) -> None:
    runner = PlanningRunner()
    frontend = backend_state().model_dump(mode="json")
    frontend["applications"]["example"]["frontend"] = {
        "package_manager": "npm", "build_script": "build", "output_dir": "dist"
    }
    frontend["deployments"]["example-local"]["runtimes"].update({
        "node": {"provider": "system", "version": "22.12.0"},
        "npm": {"provider": "bundled", "version": "10.9.0"},
    })
    with pytest.raises(ValueError, match="artifact_frontend_output_not_supported"):
        orchestrator(tmp_path, runner, ControlState.model_validate(frontend)).plan_build_artifact(
            "example-local"
        )
    assert runner.calls == []


@pytest.mark.parametrize(
    ("manager", "manager_version", "install"),
    [
        ("npm", "10.9.0", ["npm", "ci", "--no-audit", "--no-fund"]),
        ("pnpm", "9.15.0", ["pnpm", "install", "--frozen-lockfile"]),
        (
            "yarn", "1.22.22",
            ["yarn", "install", "--frozen-lockfile", "--non-interactive"],
        ),
        ("yarn", "4.5.3", ["yarn", "install", "--immutable"]),
        ("bun", "1.1.38", ["bun", "install", "--frozen-lockfile"]),
    ],
)
def test_frontend_commands_are_fixed(
    manager: str, manager_version: str, install: list[str]
) -> None:
    assert artifact_program.frontend_commands(manager, manager_version, "build:prod") == (
        install,
        [manager, "run", "build:prod"],
    )


def test_frontend_plan_binds_lockfile_and_runtime_capability(tmp_path: Path) -> None:
    document = backend_state().model_dump(mode="json")
    document["applications"]["example"]["frontend"] = {
        "package_manager": "npm", "build_script": "build", "output_dir": "public/build",
    }
    document["deployments"]["example-local"]["runtimes"].update({
        "node": {"provider": "system", "version": "22.12.0"},
        "npm": {"provider": "bundled", "version": "10.9.0"},
    })
    plan = orchestrator(
        tmp_path, PlanningRunner(), ControlState.model_validate(document)
    ).plan_build_artifact("example-local")

    assert plan["identity"]["frontend_lock"] == {
        "filename": "package-lock.json", "sha256": "9" * 64, "bytes": 1024,
    }
    assert plan["identity"]["capability"]["frontend"] == {
        "manager": "npm", "manager_version": "10.9.0", "node_version": "22.12.0",
    }


def test_secret_reference_value_does_not_change_build_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        artifact_build_module,
        "plan_secret_references",
        lambda *_arguments: [{"status": "current", "version_fingerprint": "ver_" + "a" * 64}],
    )

    def state(field: str) -> ControlState:
        document = backend_state().model_dump(mode="json")
        document["applications"]["example"]["frontend"] = {
            "package_manager": "npm", "build_script": "build",
            "output_dir": "public/build",
        }
        document["applications"]["example"]["build"]["secrets"] = {
            "NPM_TOKEN": {
                "store": "local-sops", "secret": "example/build", "field": field,
            }
        }
        document["deployments"]["example-local"]["runtimes"].update({
            "node": {"provider": "system", "version": "22.12.0"},
            "npm": {"provider": "bundled", "version": "10.9.0"},
        })
        return ControlState.model_validate(document)

    first = orchestrator(tmp_path, PlanningRunner(), state("FIRST")).plan_build_artifact(
        "example-local"
    )
    second = orchestrator(tmp_path, PlanningRunner(), state("SECOND")).plan_build_artifact(
        "example-local"
    )
    assert first["build_id"] == second["build_id"]
    assert first["identity"]["build_policy"] == {
        "target": "buildbox", "artifact_store": "primary", "packaging": "laravel_v1",
        "build_secrets_used": True, "build_secret_count": 1,
        "build_secret_names_sha256": hashlib.sha256(
            json.dumps(["NPM_TOKEN"], separators=(",", ":")).encode()
        ).hexdigest(),
    }
    encoded = json.dumps(first)
    for forbidden in ("NPM_TOKEN", "example/build", "FIRST", "ver_" + "a" * 64):
        assert forbidden not in encoded
    assert len(first["build_secret_versions_sha256"]) == 64


def test_build_secrets_are_resolved_only_after_plan_acceptance(
    tmp_path: Path, monkeypatch
) -> None:
    document = backend_state().model_dump(mode="json")
    document["applications"]["example"]["frontend"] = {
        "package_manager": "npm", "build_script": "build", "output_dir": "public/build",
    }
    document["applications"]["example"]["build"]["secrets"] = {
        "NPM_TOKEN": {
            "store": "local-sops", "secret": "example/build", "field": "TOKEN",
        }
    }
    document["deployments"]["example-local"]["runtimes"].update({
        "node": {"provider": "system", "version": "22.12.0"},
        "npm": {"provider": "bundled", "version": "10.9.0"},
    })
    selected = ControlState.model_validate(document)
    planned = [{"status": "current", "version_fingerprint": "ver_" + "a" * 64}]
    resolutions: list[dict[str, SecretReference]] = []
    monkeypatch.setattr(
        artifact_build_module, "plan_secret_references", lambda *_arguments: planned
    )

    def resolve(_state, _path, references, observed):
        assert observed == planned
        resolutions.append(references)
        return {"NPM_TOKEN": "protected-value"}

    monkeypatch.setattr(artifact_build_module, "resolve_planned_secret_references", resolve)
    runner = PlanningRunner()
    operations = orchestrator(tmp_path, runner, selected)
    plan = operations.plan_build_artifact("example-local")
    assert resolutions == []

    result = operations.build_artifact("example-local", plan["plan_id"])
    assert result["status"] == "published"
    assert len(resolutions) == 1


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


def test_frontend_lockfile_must_be_exact_and_uncontested(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text("lock")
    request = {
        "frontend": {
            "package_manager": "npm", "build_script": "build",
            "output_dir": "public/build",
        }
    }
    policy = artifact_program.frontend_policy(request, tmp_path)
    assert policy["lockfile"] == "package-lock.json"
    (tmp_path / "yarn.lock").write_text("conflict")
    with pytest.raises(artifact_program.ArtifactFailure, match="frontend_lockfile_invalid"):
        artifact_program.frontend_policy(request, tmp_path)


def test_frontend_output_rejects_dependency_and_credential_material(tmp_path: Path) -> None:
    (tmp_path / "vendor").mkdir()
    output = tmp_path / "public" / "build" / "node_modules"
    output.mkdir(parents=True)
    (output / "dependency.js").write_text("unexpected")
    with pytest.raises(artifact_program.ArtifactFailure, match="frontend_output_invalid"):
        artifact_program.collect_tree(
            tmp_path, [], {"output_dir": "public/build"}
        )


def test_secret_scan_rejects_selected_tree_and_final_archive(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    leaked = root / "compiled.js"
    leaked.write_text("window.token = 'exact-secret-value';")
    entries = [("compiled.js", leaked)]
    with pytest.raises(artifact_program.ArtifactFailure, match="secret_leak_detected"):
        artifact_program.scan_secret_values(entries, {"TOKEN": "exact-secret-value"})

    archive = tmp_path / "artifact.tar.gz"
    artifact_program.create_archive(entries, archive)
    with pytest.raises(artifact_program.ArtifactFailure, match="secret_leak_detected"):
        artifact_program.scan_archive_secrets(archive, {"TOKEN": "exact-secret-value"})

    leaked.write_text("safe")
    excluded = root / "node_modules" / "dependency"
    excluded.parent.mkdir()
    excluded.write_text("exact-secret-value")
    with pytest.raises(artifact_program.ArtifactFailure, match="secret_leak_detected"):
        artifact_program.scan_workspace_secrets(root, {"TOKEN": "exact-secret-value"})


def test_artifact_credential_file_is_removed_immediately_after_read(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({
        "store": {},
        "build": {"NPM_TOKEN": "protected-value"},
    }))
    path.chmod(0o600)

    assert artifact_program.credentials(str(path)) == ({}, {"NPM_TOKEN": "protected-value"})
    assert not path.exists()


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


def real_repository(tmp_path: Path, *, frontend: bool = False) -> tuple[Path, str]:
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
    if frontend:
        (repository / "package.json").write_text(json.dumps({
            "name": "example-frontend",
            "version": "1.0.0",
            "scripts": {
                "build": "mkdir -p public/build && printf compiled > public/build/app.js"
            },
        }))
        subprocess.run(
            ["npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit"],
            cwd=repository,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
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


def local_runtime_request(
    repository: Path, commit: str, *, frontend: bool = False
) -> dict[str, object]:
    php = subprocess.run(
        ["php", "-r", "echo PHP_VERSION;"], check=True, text=True, capture_output=True
    ).stdout
    composer = subprocess.run(
        ["composer", "--version", "--no-ansi"], check=True, text=True, capture_output=True
    ).stdout.split()[2]
    request = {
        "repository": str(repository),
        "commit": commit,
        "runtimes": {
            "php": {"provider": "system", "version": php},
            "composer": {"provider": "system", "version": composer},
        },
        "php_extensions": [],
        "frontend": None,
    }
    if frontend:
        node = subprocess.run(
            ["node", "--version"], check=True, text=True, capture_output=True
        ).stdout.strip().removeprefix("v")
        npm = subprocess.run(
            ["npm", "--version"], check=True, text=True, capture_output=True
        ).stdout.strip()
        request["runtimes"].update({
            "node": {"provider": "system", "version": node},
            "npm": {"provider": "bundled", "version": npm},
        })
        request["frontend"] = {
            "package_manager": "npm", "build_script": "build",
            "output_dir": "public/build",
        }
    return request


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
        "frontend_lock": None,
        "capability": inspection["capability"],
        "build_id": "build_v1_" + "a" * 64,
        "build_secret_names": [],
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


def test_real_npm_build_includes_only_fixed_compiled_output(tmp_path: Path, monkeypatch) -> None:
    repository, commit = real_repository(tmp_path, frontend=True)
    apps_root = tmp_path / "apps"
    apps_root.mkdir(mode=0o700)
    request = local_runtime_request(repository, commit, frontend=True)
    root = artifact_program.checked_root(str(apps_root))
    inspection = artifact_program.inspect_source(request, root)
    fake = FakeS3()
    monkeypatch.setattr(artifact_program, "s3_client", lambda *_: fake)
    build_request = {
        **request,
        "application": "example",
        "composer_lock_sha256": inspection["composer_lock_sha256"],
        "frontend_lock": inspection["frontend_lock"],
        "capability": inspection["capability"],
        "build_id": "build_v1_" + "b" * 64,
        "build_secret_names": [],
        "store": {
            "bucket": "gimme-artifacts", "region": "us-east-1", "endpoint": None,
            "addressing": "virtual_hosted", "encryption": {"method": "aes256"},
        },
    }

    result = artifact_program.build(build_request, "-", root)
    assert result["status"] == "published"
    package_key, _, _ = artifact_program.object_keys("example", build_request["build_id"])
    body = fake.objects[package_key][fake.objects[package_key]["current"]]
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        names = {member.name for member in archive.getmembers()}
    assert "public/build/app.js" in names
    assert not any(name == "node_modules" or name.startswith("node_modules/") for name in names)
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
        "frontend_lock": None,
        "capability": inspection["capability"],
        "build_id": "build_v1_" + "a" * 64,
        "build_secret_names": [],
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
        "schema_version": 2,
        "application": "example",
        "build_id": build_id,
        "commit": "b" * 40,
        "format": "laravel_v1",
        "artifact_digest": hashlib.sha256(package_body).hexdigest(),
        "tree_digest": "c" * 64,
        "bytes": len(package_body),
        "package_version": package["VersionId"],
        "published_at": "2026-09-20T12:00:00+00:00",
        "build_secrets_used": False,
        "build_secret_count": 0,
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

    unsupported = {**manifest, "schema_version": 1}
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
        lambda *_arguments: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    request = {
        "repository": "https://example.test/repository.git",
        "commit": "a" * 40,
        "runtimes": {
            "php": {"provider": "system", "version": "8.4.1"},
            "composer": {"provider": "system", "version": "2.8.4"},
        },
        "php_extensions": [],
        "frontend": None,
        "application": "example",
        "composer_lock_sha256": hashlib.sha256(b"{}").hexdigest(),
        "frontend_lock": None,
        "capability": {},
        "build_id": "build_v1_" + "a" * 64,
        "build_secret_names": [],
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
