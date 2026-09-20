import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from gimme.artifact_store_orchestration import ArtifactStoreOrchestrator
from gimme.artifact_public import public_state
from gimme.config import HealthCheckConfig, StackConfig
from gimme.control import (
    ApplicationBuildPolicy,
    ApplicationConfig,
    ControlState,
    DeploymentConfig,
    DeploymentSource,
    ResourceBindings,
    ResourceConfig,
    RuntimePin,
    S3ArtifactStore,
    SecretReference,
    SopsArtifactAuth,
    SSEAES256,
    StateStore,
    TargetConfig,
    TargetNetwork,
    explicit_placement_decision,
    new_placement,
)
from gimme.control_plane_registration_orchestration import (
    ControlPlaneRegistrationOrchestrator,
)
from gimme.control_plans import migration_plan
from gimme.deployer import CommandResult, DeployerError
from gimme.deployment_release_orchestration import DeploymentReleaseOrchestrator
import gimme.artifact_store_orchestration as artifact_module


ROOT = Path(__file__).parents[1]


def target(name: str, role: str = "deployment") -> TargetConfig:
    return TargetConfig(
        host_alias=name,
        bootstrap_hostname="192.0.2.10",
        hostname=f"{name}.local",
        system_hostname=name,
        remote_user="deployer",
        apps_root="/srv/gimme/apps",
        deployment_slots=0 if role == "administration" else 2,
        network=TargetNetwork(mode="local_mdns", mdns_name=name),
        stack=StackConfig(package_manager="apt", packages=["git"], services=[]),
        role=role,
    )


def store_definition(**updates) -> S3ArtifactStore:
    values = {
        "bucket": "gimme-artifacts",
        "region": "us-east-1",
        "encryption": SSEAES256(),
    }
    values.update(updates)
    return S3ArtifactStore.model_validate(values)


def build_policy() -> ApplicationBuildPolicy:
    return ApplicationBuildPolicy(
        target="builder",
        artifact_store="primary",
        packaging="laravel_v1",
    )


def application(build: bool = True) -> ApplicationConfig:
    return ApplicationConfig(
        repository="git@github.com:example/application.git",
        framework="laravel",
        build=build_policy() if build else None,
        default_health=HealthCheckConfig(path="/up"),
    )


def deployment(release_mode: str = "artifact", stage: str = "local") -> DeploymentConfig:
    deploy_target = target("deploy")
    return DeploymentConfig(
        application="example",
        target="deploy",
        stage=stage,
        release_mode=release_mode,
        source=DeploymentSource(kind="branch", ref="main"),
        app_env="local",
        app_debug=True,
        runtimes={
            "php": RuntimePin(provider="system", version="8.4.1"),
            "composer": RuntimePin(provider="system", version="2.8.4"),
        },
        resources=ResourceBindings(
            database="deploy-postgres",
            valkey={"resource": "deploy-valkey", "uses": ["cache"]},
        ),
        placement=new_placement("example-local", deploy_target),
        placement_decision=explicit_placement_decision("deploy", deploy_target),
    )


def state(*, include_application: bool = True) -> ControlState:
    return ControlState(
        artifact_stores={"primary": store_definition()},
        targets={"builder": target("builder"), "deploy": target("deploy")},
        applications={"example": application()} if include_application else {},
        resources={
            "deploy-postgres": ResourceConfig(
                target="deploy", kind="postgres", version="17.2"
            ),
            "deploy-valkey": ResourceConfig(
                target="deploy", kind="valkey", version="8.0.1"
            ),
        },
        deployments={"example-local": deployment()} if include_application else {},
    )


@pytest.mark.parametrize(
    "endpoint",
    ["http://objects.example.test", "objects.example.test/path", "user@objects.example.test"],
)
def test_artifact_store_rejects_non_host_or_insecure_endpoints(endpoint) -> None:
    with pytest.raises(ValidationError):
        store_definition(endpoint=endpoint)


def test_artifact_store_auth_is_reference_only_and_local_sops_bound() -> None:
    auth = SopsArtifactAuth(
        access_key_id=SecretReference(
            store="local-sops", secret="artifacts/publisher", field="ACCESS_KEY_ID"
        ),
        secret_access_key=SecretReference(
            store="local-sops", secret="artifacts/publisher", field="SECRET_ACCESS_KEY"
        ),
    )
    definition = store_definition(publisher_auth=auth, reader_auth=auth)
    value = definition.model_dump(mode="json")
    assert "plaintext" not in json.dumps(value)
    with pytest.raises(ValidationError, match="local-sops"):
        ControlState(
            artifact_stores={
                "primary": store_definition(
                    publisher_auth=auth.model_copy(update={
                        "access_key_id": auth.access_key_id.model_copy(
                            update={"store": "external"}
                        )
                    })
                )
            }
        )


def test_public_state_and_plans_hide_artifact_auth_and_build_secret_references(
    tmp_path: Path,
) -> None:
    auth = SopsArtifactAuth(
        access_key_id=SecretReference(
            store="local-sops", secret="artifacts/reader", field="ACCESS_KEY_ID"
        ),
        secret_access_key=SecretReference(
            store="local-sops", secret="artifacts/reader", field="SECRET_ACCESS_KEY"
        ),
    )
    document = state().model_dump(mode="json")
    document["artifact_stores"]["primary"].update({
        "publisher_auth": auth.model_dump(mode="json"),
        "reader_auth": auth.model_dump(mode="json"),
    })
    document["applications"]["example"]["build"]["secrets"] = {
        "NPM_TOKEN": {
            "store": "local-sops", "secret": "example/build", "field": "TOKEN",
        }
    }
    selected = ControlState.model_validate(document)

    desired = StateStore(tmp_path)
    desired.save(ControlState())
    registration = registration_orchestrator(desired).plan_register_artifact_store(
        "primary", selected.artifact_stores["primary"]
    )

    for value in (public_state(selected), migration_plan(selected, "/state"), registration):
        encoded = json.dumps(value)
        for forbidden in (
            "artifacts/reader", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY",
            "example/build", "NPM_TOKEN", '"field": "TOKEN"',
        ):
            assert forbidden not in encoded
    assert public_state(selected)["artifact_stores"]["primary"]["publisher_auth"] == {
        "mode": "sops_reference"
    }
    assert public_state(selected)["applications"]["example"]["build"][
        "build_secret_count"
    ] == 1


def test_release_mode_policy_has_no_source_or_missing_build_fallback() -> None:
    candidate = state().model_dump(mode="json")
    candidate["deployments"]["example-local"].update({
        "stage": "staging",
        "release_mode": "source",
        "app_debug": False,
    })
    with pytest.raises(ValidationError, match="source release mode"):
        ControlState.model_validate(candidate)


def test_build_target_is_deployment_capable_and_packaging_is_bounded() -> None:
    candidate = state().model_dump(mode="json")
    candidate["applications"]["example"]["build"]["target"] = "deploy"
    assert ControlState.model_validate(candidate).applications["example"].build is not None

    with pytest.raises(ValidationError):
        ApplicationBuildPolicy(
            target="builder", artifact_store="primary", packaging="custom"
        )


def test_existing_source_release_workflow_refuses_artifact_mode() -> None:
    with pytest.raises(ValueError, match="artifact deployment workflow"):
        DeploymentReleaseOrchestrator._require_source_release(deployment())

    candidate = state().model_dump(mode="json")
    candidate["applications"]["example"]["build"] = None
    with pytest.raises(ValidationError, match="requires application build policy"):
        ControlState.model_validate(candidate)


def test_schema_v5_requires_explicit_complete_release_policy(tmp_path: Path) -> None:
    document = state().model_dump(mode="json")
    document["schema_version"] = 5
    document.pop("artifact_stores")
    document["applications"]["example"].pop("build")
    document["deployments"]["example-local"].pop("release_mode")
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "state.json").write_text(json.dumps(document))
    desired = StateStore(tmp_path)

    with pytest.raises(ValueError, match="every migrated deployment"):
        desired.state_migration({}, {})
    migrated = desired.state_migration(
        {},
        {"example-local": "artifact"},
        {"primary": store_definition()},
        {"example": build_policy()},
    )
    desired.save(migrated)

    assert migrated.schema_version == 7
    assert migrated.deployments["example-local"].release_mode == "artifact"
    with pytest.raises(ValueError, match="schema-v7 state already exists"):
        desired.state_migration({}, {"example-local": "artifact"})


def registration_orchestrator(desired: StateStore) -> ControlPlaneRegistrationOrchestrator:
    def replace(current, collection, name, definition):
        values = dict(getattr(current, collection))
        values[name] = definition
        return current.model_copy(update={collection: values})

    def delete(current, collection, name):
        values = dict(getattr(current, collection))
        del values[name]
        return current.model_copy(update={collection: values})

    def assert_plan(expected, plan_id):
        if expected["plan_id"] != plan_id:
            raise ValueError("plan_id does not match current desired state")

    return ControlPlaneRegistrationOrchestrator(
        store=desired,
        aws_secrets=object(),
        backup_s3=object(),
        assert_plan=assert_plan,
        replace=replace,
        delete=delete,
        backup_destination_credentials=lambda *_: (None, None),
    )


def test_artifact_store_crud_is_local_content_addressed_and_reference_safe(
    tmp_path: Path,
) -> None:
    desired = StateStore(tmp_path)
    desired.save(ControlState())
    operations = registration_orchestrator(desired)
    definition = store_definition()

    plan = operations.plan_register_artifact_store("primary", definition)
    assert plan["effects"][-1] == "make no Target or object-store changes"
    with pytest.raises(ValueError, match="plan_id"):
        operations.register_artifact_store("primary", definition, "plan_00000000000000000000")
    operations.register_artifact_store("primary", definition, plan["plan_id"])

    changed = definition.model_copy(update={"addressing": "path"})
    update = operations.plan_update_artifact_store("primary", changed)
    operations.update_artifact_store("primary", changed, update["plan_id"])
    assert desired.load().artifact_stores["primary"].addressing == "path"

    referenced = desired.load().model_copy(update={
        "targets": {"builder": target("builder")},
        "applications": {"example": application()},
    })
    desired.save(ControlState.model_validate(referenced.model_dump(mode="json")))
    with pytest.raises(ValueError, match="still referenced"):
        operations.plan_remove_artifact_store("primary")


class FakeRunner:
    def __init__(self, evidence=None, error=None):
        self.evidence = evidence
        self.error = error
        self.calls = []

    def run(self, task, server, **kwargs):
        self.calls.append((task, server, kwargs))
        if self.error is not None:
            raise self.error
        encoded = base64.b64encode(json.dumps(self.evidence).encode()).decode()
        return CommandResult([], 0, f"GIMME_ARTIFACT_STORE_RESULT|{encoded}\n")


def verification_orchestrator(tmp_path: Path, runner: FakeRunner) -> ArtifactStoreOrchestrator:
    desired = StateStore(tmp_path)
    desired.save(state(include_application=False))

    def assert_plan(expected, actual):
        if expected["plan_id"] != actual:
            raise ValueError("stale")

    return ArtifactStoreOrchestrator(
        store=desired,
        runner=runner,
        assert_plan=assert_plan,
        legacy_server=lambda selected: selected,
    )


def test_publisher_and_reader_verification_return_only_bounded_evidence(tmp_path: Path) -> None:
    publisher = {
        "status": "ready",
        "role": "publisher",
        "versioning": "enabled",
        "encryption": "aes256",
        "checksum": "a" * 64,
        "probe_deleted": True,
        "reader_version": "reader-v1",
    }
    runner = FakeRunner(publisher)
    operations = verification_orchestrator(tmp_path, runner)
    plan = operations.plan_verification("primary", "builder", "publisher")
    evidence = operations.verify("primary", "builder", "publisher", plan["plan_id"])

    assert evidence["reader_version"] == "reader-v1"
    assert "bucket" not in evidence and "output" not in evidence
    assert runner.calls[0][2]["artifact_probe_role"] == "publisher"

    runner.evidence = {
        "status": "ready",
        "role": "reader",
        "versioning": "exact-version-read",
        "checksum": "b" * 64,
    }
    plan = operations.plan_verification("primary", "deploy", "reader", "reader-v1")
    evidence = operations.verify(
        "primary", "deploy", "reader", plan["plan_id"], "reader-v1"
    )
    assert evidence["role"] == "reader"
    assert runner.calls[-1][2]["artifact_probe_role"] == "reader"
    assert runner.calls[-1][2]["artifact_reader_version"] == "reader-v1"


def test_verification_permission_failure_is_fail_closed(tmp_path: Path) -> None:
    operations = verification_orchestrator(
        tmp_path, FakeRunner(error=DeployerError("fixed permission failure"))
    )
    plan = operations.plan_verification("primary", "builder", "publisher")
    with pytest.raises(DeployerError, match="permission failure"):
        operations.verify("primary", "builder", "publisher", plan["plan_id"])


def test_reader_resolves_only_reader_references_without_publisher_fallback(
    tmp_path: Path, monkeypatch,
) -> None:
    def auth(secret: str) -> SopsArtifactAuth:
        return SopsArtifactAuth(
            access_key_id=SecretReference(
                store="local-sops", secret=secret, field="ACCESS_KEY_ID"
            ),
            secret_access_key=SecretReference(
                store="local-sops", secret=secret, field="SECRET_ACCESS_KEY"
            ),
        )

    desired = StateStore(tmp_path)
    selected = state(include_application=False).model_dump(mode="json")
    selected["artifact_stores"]["primary"]["publisher_auth"] = auth(
        "artifacts/publisher"
    ).model_dump(mode="json")
    selected["artifact_stores"]["primary"]["reader_auth"] = auth(
        "artifacts/reader"
    ).model_dump(mode="json")
    desired.save(ControlState.model_validate(selected))
    observed = []

    def planned(_state, _path, references):
        observed.append({value.secret for value in references.values()})
        return [
            {
                "environment_key": key,
                "reference_fingerprint": "ref_" + "a" * 64,
                "version_fingerprint": "ver_" + "b" * 64,
                "status": "current",
            }
            for key in references
        ]

    def resolved(_state, _path, references, _planned):
        observed.append({value.secret for value in references.values()})
        return {key: "bounded-value" for key in references}

    monkeypatch.setattr(artifact_module, "plan_secret_references", planned)
    monkeypatch.setattr(artifact_module, "resolve_planned_secret_references", resolved)
    runner = FakeRunner({
        "status": "ready",
        "role": "reader",
        "versioning": "exact-version-read",
        "checksum": "c" * 64,
    })

    def assert_plan(expected, actual):
        if expected["plan_id"] != actual:
            raise ValueError("stale")

    operations = ArtifactStoreOrchestrator(
        store=desired,
        runner=runner,
        assert_plan=assert_plan,
        legacy_server=lambda selected: selected,
    )
    plan = operations.plan_verification("primary", "deploy", "reader", "reader-v1")
    operations.verify("primary", "deploy", "reader", plan["plan_id"], "reader-v1")

    assert observed == [{"artifacts/reader"}, {"artifacts/reader"}, {"artifacts/reader"}]
    assert runner.calls[0][2]["secret_file"] is not None


def test_target_probe_cleans_exact_version_and_redacts_interrupted_provider_error(
    tmp_path: Path,
) -> None:
    program = subprocess.run(
        [
            "php",
            "-r",
            "require 'deploy/programs.php'; echo Deployer\\artifact_store_probe_script();",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    modules = tmp_path / "modules"
    (modules / "botocore").mkdir(parents=True)
    trace = tmp_path / "trace.jsonl"
    (modules / "botocore" / "__init__.py").write_text("")
    (modules / "botocore" / "config.py").write_text(
        "class Config:\n    def __init__(self, **kwargs): pass\n"
    )
    (modules / "botocore" / "exceptions.py").write_text(
        "class ClientError(Exception):\n    pass\n"
    )
    (modules / "boto3.py").write_text(
        "import json, os\n"
        "class Body:\n"
        "    def read(self): raise RuntimeError('provider-secret-detail')\n"
        "    def close(self): pass\n"
        "class Client:\n"
        "    calls = 0\n"
        "    def get_bucket_versioning(self, **kwargs): return {'Status':'Enabled'}\n"
        "    def put_object(self, **kwargs):\n"
        "        self.calls += 1\n"
        "        return {'VersionId': 'probe-v1' if '/publisher/' in kwargs['Key'] "
        "else 'reader-v1', "
        "'ServerSideEncryption':'AES256'}\n"
        "    def get_object(self, **kwargs): return {'Body': Body()}\n"
        "    def delete_object(self, **kwargs):\n"
        "        open(os.environ['TRACE'], 'a').write(json.dumps(kwargs) + '\\n')\n"
        "def client(*args, **kwargs): return Client()\n"
    )
    policy = base64.b64encode(json.dumps({
        "name": "primary",
        "bucket": "gimme-artifacts",
        "region": "us-east-1",
        "endpoint": None,
        "addressing": "virtual_hosted",
        "encryption": {"method": "aes256"},
    }).encode()).decode()
    result = subprocess.run(
        [sys.executable, "-", policy, "publisher", "-", "-"],
        input=program,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(modules), "TRACE": str(trace)},
        check=False,
    )

    deleted = json.loads(trace.read_text())
    assert result.returncode != 0
    assert "provider-secret-detail" not in result.stderr
    assert "GIMME_ARTIFACT_STORE_ERROR|verification_failed" in result.stderr
    assert deleted["VersionId"] == "probe-v1"
    assert "/publisher/" in deleted["Key"]


def test_empty_schema_v6_state_is_valid() -> None:
    empty = ControlState()
    assert empty.artifact_stores == {}
    assert empty.deployments == {}
