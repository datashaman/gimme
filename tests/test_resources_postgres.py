from pathlib import Path

import pytest

from gimme.control import (
    AWSNetwork,
    AWSProviderAccount,
    AWSRDSPostgresResource,
    AWSSecretsManagerStore,
)
from gimme.resources_postgres import (
    BotoRDSAdapter,
    InstanceObservation,
    ResourceError,
    _provider_error,
    apply_provision,
    derive_instance_identifier,
    generate_workload_password,
    load_observed,
    persist_binding,
)


def account() -> AWSProviderAccount:
    return AWSProviderAccount(
        account_id="123456789012",
        inspection_role_arn="arn:aws:iam::123456789012:role/gimme-inspect",
        resolver_role_arn="arn:aws:iam::123456789012:role/gimme-resolve",
    )


def network() -> AWSNetwork:
    return AWSNetwork(
        provider_account="main",
        region="us-east-1",
        vpc_id="vpc-0123456789abcdef0",
        private_subnet_ids=["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"],
    )


def resource() -> AWSRDSPostgresResource:
    return AWSRDSPostgresResource(
        aws_network="primary",
        administration_target="adminbox",
        engine_version="17.2",
        instance_class="db.t3.medium",
        allocated_storage_gb=20,
        administration_security_group_id="sg-0123456789abcdef0",
        deployment_security_group_ids={"devbox": "sg-0123456789abcdef1"},
        workload_secret_store="workload-secrets",
    )


def workload_store() -> AWSSecretsManagerStore:
    return AWSSecretsManagerStore(
        provider_account="main", region="us-east-1", prefix="gimme/workload"
    )


class FakeRDSAdapter:
    def __init__(self, *, ready_after: int = 0) -> None:
        self.instances: dict[str, InstanceObservation] = {}
        self.create_calls = 0
        self.describe_calls = 0
        self.secret_versions: dict[str, int] = {}
        self.secret_payloads: dict[str, dict[str, str]] = {}
        self._pending_polls: dict[str, int] = {}
        self._ready_after = ready_after

    def describe_instance(self, account, network, aws_instance_identifier):
        self.describe_calls += 1
        instance = self.instances.get(aws_instance_identifier)
        if instance is None:
            return None
        if instance.status != "available":
            seen = self._pending_polls.get(aws_instance_identifier, 0)
            self._pending_polls[aws_instance_identifier] = seen + 1
            if seen >= self._ready_after:
                instance = InstanceObservation(
                    identity=instance.identity,
                    status="available",
                    engine_version=instance.engine_version,
                    endpoint="db.example.test",
                    port=5432,
                    master_secret_arn=instance.master_secret_arn,
                )
                self.instances[aws_instance_identifier] = instance
        return instance

    def create_instance(
        self, account, network, resource, resource_name, aws_instance_identifier, security_group_ids
    ):
        self.create_calls += 1
        status = "creating" if self._ready_after > 0 else "available"
        observation = InstanceObservation(
            identity=f"arn:aws:rds:us-east-1:123456789012:db:{aws_instance_identifier}",
            status=status,
            engine_version=resource.engine_version,
            endpoint="db.example.test" if status == "available" else None,
            port=5432 if status == "available" else None,
            master_secret_arn=(
                f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{aws_instance_identifier}-master"
            ),
        )
        self.instances[aws_instance_identifier] = observation
        return observation

    def resolve_master_credential(self, account, region, secret_arn):
        return "gimme_admin", "master-plaintext-password"

    def create_workload_secret(self, account, store, name, tags, payload):
        version = self.secret_versions.get(name, 0) + 1
        self.secret_versions[name] = version
        self.secret_payloads[name] = payload
        return (
            f"arn:aws:secretsmanager:{store.region}:{account.account_id}:secret:{name}",
            f"v{version}",
        )


class ProviderFailureAdapter(FakeRDSAdapter):
    def create_instance(self, *args, **kwargs):
        # A compliant adapter has already translated the raw provider exception into a
        # bounded ResourceError by the time it reaches orchestration code (see
        # BotoRDSAdapter._provider_error); this fake models that contract directly.
        raise ResourceError("aws_rds_create_access_denied")


class FakeClock:
    """A deterministic stand-in for time.monotonic/time.sleep so poll-loop tests run
    instantly instead of burning real wall-clock seconds."""

    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("AccessDenied", "access_denied"),
        ("DBInstanceNotFound", "missing"),
        ("DBInstanceNotFoundFault", "missing"),
        ("DBInstanceAlreadyExists", "already_exists"),
        ("DBInstanceAlreadyExistsFault", "already_exists"),
        ("DBSubnetGroupAlreadyExists", "already_exists"),
        ("ResourceExistsException", "already_exists"),
        ("InvalidDBInstanceState", "invalid_state"),
        ("Throttling", "throttled"),
        ("SomeUnmappedProviderCode", "unavailable"),
    ],
)
def test_provider_error_maps_raw_exceptions_to_bounded_codes(code: str, expected: str) -> None:
    class _Boto(Exception):
        response = {"Error": {"Code": code}}

    error = _provider_error(_Boto("raw provider detail that must never surface"), "create")

    assert str(error) == f"aws_rds_create_{expected}"
    assert "raw provider detail" not in str(error)


def test_derive_instance_identifier_is_deterministic_and_bounded() -> None:
    assert derive_instance_identifier("devbox-postgres") == "gimme-devbox-postgres"
    long_name = "a" * 60
    identifier = derive_instance_identifier(long_name)
    assert len(identifier) <= 63
    assert identifier.startswith("gimme-")


def test_apply_provision_creates_once_and_reconciles_idempotently(tmp_path: Path) -> None:
    adapter = FakeRDSAdapter()

    first = apply_provision(adapter, tmp_path, account(), network(), resource(), "devbox-postgres")
    second = apply_provision(adapter, tmp_path, account(), network(), resource(), "devbox-postgres")

    assert first["phase"] == "ready"
    assert second["phase"] == "ready"
    assert adapter.create_calls == 1
    assert adapter.describe_calls >= 2


def test_apply_provision_returns_pending_phase_within_bounded_poll(tmp_path: Path) -> None:
    adapter = FakeRDSAdapter(ready_after=100)
    clock = FakeClock()

    result = apply_provision(
        adapter,
        tmp_path,
        account(),
        network(),
        resource(),
        "devbox-postgres",
        sleep=clock.sleep,
        now=clock.now,
    )

    assert result["phase"] == "pending"
    observed = load_observed(tmp_path, "devbox-postgres")
    assert observed is not None
    assert observed["phase"] == "pending"


def test_apply_provision_resumes_a_pending_instance_without_recreating(tmp_path: Path) -> None:
    adapter = FakeRDSAdapter(ready_after=100)
    clock = FakeClock()

    pending = apply_provision(
        adapter,
        tmp_path,
        account(),
        network(),
        resource(),
        "devbox-postgres",
        sleep=clock.sleep,
        now=clock.now,
    )
    adapter._ready_after = 0
    resumed = apply_provision(
        adapter,
        tmp_path,
        account(),
        network(),
        resource(),
        "devbox-postgres",
        sleep=clock.sleep,
        now=clock.now,
    )

    assert pending["phase"] == "pending"
    assert resumed["phase"] == "ready"
    assert adapter.create_calls == 1


def test_apply_provision_surfaces_a_bounded_error_on_provider_failure(tmp_path: Path) -> None:
    adapter = ProviderFailureAdapter()

    with pytest.raises(ResourceError, match="aws_rds_create_access_denied"):
        apply_provision(adapter, tmp_path, account(), network(), resource(), "devbox-postgres")


def test_persist_binding_never_returns_the_workload_credential(tmp_path: Path) -> None:
    adapter = FakeRDSAdapter()
    apply_provision(adapter, tmp_path, account(), network(), resource(), "devbox-postgres")
    password = generate_workload_password()

    result = persist_binding(
        adapter,
        tmp_path,
        account(),
        workload_store(),
        "workload-secrets",
        "devbox-postgres",
        "example-local",
        "gimme_example_local",
        "gimme_example_local",
        password,
        "db.example.test",
        5432,
    )

    assert "password" not in result
    assert password not in str(result)
    observed = load_observed(tmp_path, "devbox-postgres")
    assert observed is not None
    assert password not in str(observed)
    assert observed["allocations"]["example-local"]["status"] == "active"


def test_persist_binding_is_idempotent_and_rotates_the_secret_version(tmp_path: Path) -> None:
    adapter = FakeRDSAdapter()
    apply_provision(adapter, tmp_path, account(), network(), resource(), "devbox-postgres")

    first = persist_binding(
        adapter,
        tmp_path,
        account(),
        workload_store(),
        "workload-secrets",
        "devbox-postgres",
        "example-local",
        "role",
        "role",
        "pw-1",
        "db.example.test",
        5432,
    )
    second = persist_binding(
        adapter,
        tmp_path,
        account(),
        workload_store(),
        "workload-secrets",
        "devbox-postgres",
        "example-local",
        "role",
        "role",
        "pw-2",
        "db.example.test",
        5432,
    )

    assert first["secret_reference"] == second["secret_reference"]
    assert adapter.secret_versions["devbox-postgres/example-local"] == 2


def test_persist_binding_requires_a_provisioned_resource(tmp_path: Path) -> None:
    adapter = FakeRDSAdapter()

    with pytest.raises(ResourceError, match="observed_resource_missing"):
        persist_binding(
            adapter,
            tmp_path,
            account(),
            workload_store(),
            "workload-secrets",
            "devbox-postgres",
            "example-local",
            "role",
            "role",
            "pw",
            "db.example.test",
            5432,
        )


def _instance_response(identifier: str, *, tag: str | None, status: str = "available") -> dict:
    return {
        "DBInstanceIdentifier": identifier,
        "DBInstanceArn": f"arn:aws:rds:us-east-1:123456789012:db:{identifier}",
        "DBInstanceStatus": status,
        "EngineVersion": "17.2",
        "Endpoint": {"Address": "db.example.test", "Port": 5432},
        "MasterUserSecret": {
            "SecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds-master"
        },
        "TagList": [] if tag is None else [{"Key": "gimme:resource", "Value": tag}],
    }


@pytest.mark.parametrize(
    ("tag", "allowed"),
    [("devbox-postgres", True), ("someone-elses-db", False), ("Not Valid!", False), (None, False)],
)
def test_observation_requires_the_ownership_tag_to_derive_the_instance_identifier(
    tag: str | None, allowed: bool
) -> None:
    identifier = derive_instance_identifier("devbox-postgres")
    response = _instance_response(identifier, tag=tag)

    if allowed:
        observed = BotoRDSAdapter._observation(response, identifier)
        assert observed.status == "available"
        assert observed.master_secret_arn is not None
    else:
        with pytest.raises(ResourceError, match="aws_rds_instance_ownership_mismatch"):
            BotoRDSAdapter._observation(response, identifier)


class _StubbedSession:
    def __init__(self, clients: dict[str, object]) -> None:
        self._clients = clients

    def client(self, service: str, region_name: str | None = None) -> object:
        return self._clients[service]


def _stubbed(service: str):
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        service, region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
    )
    return client, Stubber(client)


def test_create_instance_sends_a_hardened_botocore_valid_request(monkeypatch) -> None:
    identifier = derive_instance_identifier("devbox-postgres")
    rds, stub = _stubbed("rds")
    stub.add_response(
        "create_db_subnet_group",
        {},
        {
            "DBSubnetGroupName": f"{identifier}-subnets",
            "DBSubnetGroupDescription": "Gimme-managed subnet group for devbox-postgres",
            "SubnetIds": ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"],
            "Tags": [{"Key": "gimme:resource", "Value": "devbox-postgres"}],
        },
    )
    stub.add_response(
        "create_db_instance",
        {},
        {
            "DBInstanceIdentifier": identifier,
            "Engine": "postgres",
            "EngineVersion": "17.2",
            "DBInstanceClass": "db.t3.medium",
            "AllocatedStorage": 20,
            "StorageType": "gp3",
            "StorageEncrypted": True,
            "MultiAZ": True,
            "PubliclyAccessible": False,
            "DBSubnetGroupName": f"{identifier}-subnets",
            "VpcSecurityGroupIds": ["sg-0123456789abcdef0", "sg-0123456789abcdef1"],
            "ManageMasterUserPassword": True,
            "MasterUsername": "gimme_admin",
            "BackupRetentionPeriod": 7,
            "AutoMinorVersionUpgrade": False,
            "DeletionProtection": True,
            "Tags": [{"Key": "gimme:resource", "Value": "devbox-postgres"}],
        },
    )
    stub.add_response(
        "describe_db_instances",
        {"DBInstances": [_instance_response(identifier, tag="devbox-postgres")]},
        {"DBInstanceIdentifier": identifier},
    )
    adapter = BotoRDSAdapter()
    monkeypatch.setattr(adapter, "_session", lambda *a, **k: _StubbedSession({"rds": rds}))

    with stub:
        observed = adapter.create_instance(
            account(),
            network(),
            resource(),
            "devbox-postgres",
            identifier,
            ["sg-0123456789abcdef0", "sg-0123456789abcdef1"],
        )
        stub.assert_no_pending_responses()

    assert observed.endpoint == "db.example.test"


def test_describe_instance_treats_a_missing_instance_as_absent(monkeypatch) -> None:
    rds, stub = _stubbed("rds")
    stub.add_client_error("describe_db_instances", service_error_code="DBInstanceNotFound")
    adapter = BotoRDSAdapter()
    monkeypatch.setattr(adapter, "_session", lambda *a, **k: _StubbedSession({"rds": rds}))

    with stub:
        assert adapter.describe_instance(account(), network(), "gimme-devbox-postgres") is None


def test_workload_secret_is_written_tagged_and_versioned_without_leaking_errors(
    monkeypatch,
) -> None:
    secrets_client, stub = _stubbed("secretsmanager")
    payload = {
        "username": "u",
        "password": "workload-pw",
        "host": "h",
        "port": "5432",
        "dbname": "d",
    }
    body = '{"dbname":"d","host":"h","password":"workload-pw","port":"5432","username":"u"}'
    tags = [
        {"Key": "gimme:deployment", "Value": "example-local"},
        {"Key": "gimme:resource", "Value": "devbox-postgres"},
        {"Key": "gimme:secret-store", "Value": "workload-secrets"},
    ]
    stub.add_response(
        "create_secret",
        {},
        {
            "Name": "gimme/workload/devbox-postgres/example-local",
            "SecretString": body,
            "Tags": tags,
        },
    )
    stub.add_response(
        "put_secret_value",
        {
            "ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:x",
            "VersionId": "6f1f3f0e-7c3a-4b8e-9c55-0a1b2c3d4e5f",
        },
        {"SecretId": "gimme/workload/devbox-postgres/example-local", "SecretString": body},
    )
    adapter = BotoRDSAdapter()
    monkeypatch.setattr(
        adapter, "_session", lambda *a, **k: _StubbedSession({"secretsmanager": secrets_client})
    )

    with stub:
        arn, version = adapter.create_workload_secret(
            account(),
            workload_store(),
            "devbox-postgres/example-local",
            {item["Key"]: item["Value"] for item in tags},
            payload,
        )

    assert version == "6f1f3f0e-7c3a-4b8e-9c55-0a1b2c3d4e5f" and arn.startswith(
        "arn:aws:secretsmanager:"
    )


def test_workload_secret_failure_surfaces_only_a_bounded_code(monkeypatch) -> None:
    secrets_client, stub = _stubbed("secretsmanager")
    stub.add_client_error(
        "create_secret",
        service_error_code="AccessDeniedException",
        service_message="user arn:aws:iam::1:user/x cannot create workload-pw",
    )
    adapter = BotoRDSAdapter()
    monkeypatch.setattr(
        adapter, "_session", lambda *a, **k: _StubbedSession({"secretsmanager": secrets_client})
    )

    with stub, pytest.raises(ResourceError) as raised:
        adapter.create_workload_secret(
            account(),
            workload_store(),
            "devbox-postgres/example-local",
            {},
            {"password": "workload-pw"},
        )

    assert str(raised.value) == "aws_rds_workload_secret_create_access_denied"
    assert "workload-pw" not in str(raised.value)


def test_create_instance_is_idempotent_on_the_real_already_exists_wire_codes(monkeypatch) -> None:
    identifier = derive_instance_identifier("devbox-postgres")
    rds, stub = _stubbed("rds")
    stub.add_client_error("create_db_subnet_group", service_error_code="DBSubnetGroupAlreadyExists")
    stub.add_client_error("create_db_instance", service_error_code="DBInstanceAlreadyExists")
    stub.add_response(
        "describe_db_instances",
        {"DBInstances": [_instance_response(identifier, tag="devbox-postgres")]},
        {"DBInstanceIdentifier": identifier},
    )
    adapter = BotoRDSAdapter()
    monkeypatch.setattr(adapter, "_session", lambda *a, **k: _StubbedSession({"rds": rds}))

    with stub:
        observed = adapter.create_instance(
            account(),
            network(),
            resource(),
            "devbox-postgres",
            identifier,
            ["sg-0123456789abcdef0", "sg-0123456789abcdef1"],
        )

    assert observed.status == "available"


def test_workload_secret_rotation_writes_a_new_version_when_the_secret_exists(monkeypatch) -> None:
    secrets_client, stub = _stubbed("secretsmanager")
    stub.add_client_error("create_secret", service_error_code="ResourceExistsException")
    stub.add_response(
        "put_secret_value",
        {
            "ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:x",
            "VersionId": "6f1f3f0e-7c3a-4b8e-9c55-0a1b2c3d4e5f",
        },
    )
    adapter = BotoRDSAdapter()
    monkeypatch.setattr(
        adapter, "_session", lambda *a, **k: _StubbedSession({"secretsmanager": secrets_client})
    )

    with stub:
        arn, version = adapter.create_workload_secret(
            account(), workload_store(), "devbox-postgres/example-local", {}, {"password": "pw"}
        )

    assert version.startswith("6f1f3f0e")
