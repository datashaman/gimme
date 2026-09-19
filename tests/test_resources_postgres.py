import dataclasses
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
    modification_for,
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
        ("DBParameterGroupAlreadyExists", "already_exists"),
        ("DBParameterGroupAlreadyExistsFault", "already_exists"),
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


def test_observation_parses_the_secret_free_fields_used_for_drift() -> None:
    identifier = derive_instance_identifier("devbox-postgres")
    response = {
        **_instance_response(identifier, tag="devbox-postgres"),
        "DBInstanceClass": "db.t3.medium", "AllocatedStorage": 20,
        "VpcSecurityGroups": [
            {"VpcSecurityGroupId": "sg-0123456789abcdef1", "Status": "active"},
            {"VpcSecurityGroupId": "sg-0123456789abcdef0", "Status": "active"},
        ],
        "PendingModifiedValues": {"DBInstanceClass": "db.m6g.large"},
    }

    observed = BotoRDSAdapter._observation(response, identifier)

    assert observed.instance_class == "db.t3.medium"
    assert observed.allocated_storage_gb == 20
    assert observed.security_group_ids == ("sg-0123456789abcdef0", "sg-0123456789abcdef1")
    assert observed.modification_pending is True
    bare = BotoRDSAdapter._observation(
        _instance_response(identifier, tag="devbox-postgres"), identifier
    )
    assert (bare.instance_class, bare.security_group_ids, bare.modification_pending) == (
        None, None, False,
    )


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


def _parameter_group_request(identifier: str, major: int) -> dict[str, object]:
    return {
        "DBParameterGroupName": f"{identifier}-params",
        "DBParameterGroupFamily": f"postgres{major}",
        "Description": "Gimme-managed parameter group for devbox-postgres",
        "Tags": [{"Key": "gimme:resource", "Value": "devbox-postgres"}],
    }


def _owned_group_stubs(
    stub, identifier: str, *, family: str = "postgres17", owner: str | None = "devbox-postgres"
) -> None:
    arn = f"arn:aws:rds:us-east-1:123456789012:pg:{identifier}-params"
    stub.add_response(
        "describe_db_parameter_groups",
        {"DBParameterGroups": [{"DBParameterGroupFamily": family, "DBParameterGroupArn": arn}]},
        {"DBParameterGroupName": f"{identifier}-params"},
    )
    stub.add_response(
        "list_tags_for_resource",
        {"TagList": [] if owner is None else [{"Key": "gimme:resource", "Value": owner}]},
        {"ResourceName": arn},
    )


def _force_ssl_request(identifier: str) -> dict[str, object]:
    return {
        "DBParameterGroupName": f"{identifier}-params",
        "Parameters": [
            {
                "ParameterName": "rds.force_ssl",
                "ParameterValue": "1",
                "ApplyMethod": "pending-reboot",
            }
        ],
    }


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
    stub.add_response("create_db_parameter_group", {}, _parameter_group_request(identifier, 17))
    stub.add_response("modify_db_parameter_group", {}, _force_ssl_request(identifier))
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
            "DBParameterGroupName": f"{identifier}-params",
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


@pytest.mark.parametrize(
    "parameter_group_code", ["DBParameterGroupAlreadyExists", "DBParameterGroupAlreadyExistsFault"]
)
def test_create_instance_is_idempotent_on_the_real_already_exists_wire_codes(
    monkeypatch, parameter_group_code: str
) -> None:
    identifier = derive_instance_identifier("devbox-postgres")
    rds, stub = _stubbed("rds")
    stub.add_client_error("create_db_subnet_group", service_error_code="DBSubnetGroupAlreadyExists")
    stub.add_client_error("create_db_parameter_group", service_error_code=parameter_group_code)
    # A resumed create verifies ownership, then converges the group onto rds.force_ssl.
    _owned_group_stubs(stub, identifier)
    stub.add_response("modify_db_parameter_group", {}, _force_ssl_request(identifier))
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


@pytest.mark.parametrize(("engine_version", "major"), [("14.13", 14), ("17.2", 17)])
def test_create_instance_derives_the_parameter_group_family_from_the_engine_version(
    monkeypatch, engine_version: str, major: int
) -> None:
    identifier = derive_instance_identifier("devbox-postgres")
    rds, stub = _stubbed("rds")
    stub.add_response("create_db_subnet_group", {})
    stub.add_response("create_db_parameter_group", {}, _parameter_group_request(identifier, major))
    stub.add_response("modify_db_parameter_group", {}, _force_ssl_request(identifier))
    stub.add_response("create_db_instance", {})
    stub.add_response(
        "describe_db_instances",
        {"DBInstances": [_instance_response(identifier, tag="devbox-postgres")]},
        {"DBInstanceIdentifier": identifier},
    )
    adapter = BotoRDSAdapter()
    monkeypatch.setattr(adapter, "_session", lambda *a, **k: _StubbedSession({"rds": rds}))

    with stub:
        adapter.create_instance(
            account(),
            network(),
            resource().model_copy(update={"engine_version": engine_version}),
            "devbox-postgres",
            identifier,
            ["sg-0123456789abcdef0", "sg-0123456789abcdef1"],
        )
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("failing_call", "code"),
    [
        ("create_db_parameter_group", "aws_rds_parameter_group_access_denied"),
        ("modify_db_parameter_group", "aws_rds_parameter_group_modify_access_denied"),
    ],
)
def test_create_instance_bounds_parameter_group_failures_and_never_creates_the_instance(
    monkeypatch, failing_call: str, code: str
) -> None:
    identifier = derive_instance_identifier("devbox-postgres")
    rds, stub = _stubbed("rds")
    stub.add_response("create_db_subnet_group", {})
    if failing_call == "modify_db_parameter_group":
        stub.add_response("create_db_parameter_group", {})
    stub.add_client_error(
        failing_call,
        service_error_code="AccessDenied",
        service_message="user arn:aws:iam::123456789012:user/x cannot touch the group",
    )
    adapter = BotoRDSAdapter()
    monkeypatch.setattr(adapter, "_session", lambda *a, **k: _StubbedSession({"rds": rds}))

    with stub, pytest.raises(ResourceError) as raised:
        adapter.create_instance(
            account(),
            network(),
            resource(),
            "devbox-postgres",
            identifier,
            ["sg-0123456789abcdef0", "sg-0123456789abcdef1"],
        )

    assert str(raised.value) == code
    assert "arn:aws" not in str(raised.value)
    assert "cannot touch" not in str(raised.value)


@pytest.mark.parametrize(
    ("family", "owner"),
    [
        ("postgres14", "devbox-postgres"),  # retained group from a different engine major
        ("postgres17", "other-postgres"),  # someone else's Resource
        ("postgres17", None),  # pre-created, untagged
    ],
)
def test_create_instance_refuses_an_existing_parameter_group_it_does_not_own(
    monkeypatch, family: str, owner: str | None
) -> None:
    identifier = derive_instance_identifier("devbox-postgres")
    rds, stub = _stubbed("rds")
    stub.add_response("create_db_subnet_group", {})
    stub.add_client_error(
        "create_db_parameter_group", service_error_code="DBParameterGroupAlreadyExists"
    )
    _owned_group_stubs(stub, identifier, family=family, owner=owner)
    adapter = BotoRDSAdapter()
    monkeypatch.setattr(adapter, "_session", lambda *a, **k: _StubbedSession({"rds": rds}))

    with stub, pytest.raises(ResourceError) as raised:
        adapter.create_instance(
            account(), network(), resource(), "devbox-postgres", identifier,
            ["sg-0123456789abcdef0", "sg-0123456789abcdef1"],
        )

    # No modify_db_parameter_group or create_db_instance was queued, so reaching either
    # would surface as a different (unclassified) error code.
    assert str(raised.value) == "aws_rds_parameter_group_ownership_mismatch"


def test_create_instance_bounds_parameter_group_verification_failures(monkeypatch) -> None:
    identifier = derive_instance_identifier("devbox-postgres")
    rds, stub = _stubbed("rds")
    stub.add_response("create_db_subnet_group", {})
    stub.add_client_error(
        "create_db_parameter_group", service_error_code="DBParameterGroupAlreadyExists"
    )
    stub.add_client_error(
        "describe_db_parameter_groups",
        service_error_code="AccessDenied",
        service_message="user arn:aws:iam::123456789012:user/x cannot describe the group",
    )
    adapter = BotoRDSAdapter()
    monkeypatch.setattr(adapter, "_session", lambda *a, **k: _StubbedSession({"rds": rds}))

    with stub, pytest.raises(ResourceError) as raised:
        adapter.create_instance(
            account(), network(), resource(), "devbox-postgres", identifier,
            ["sg-0123456789abcdef0", "sg-0123456789abcdef1"],
        )

    assert str(raised.value) == "aws_rds_parameter_group_verify_access_denied"
    assert "arn:aws" not in str(raised.value)


def test_create_instance_rejects_an_unusable_engine_version_before_any_aws_call(
    monkeypatch,
) -> None:
    adapter = BotoRDSAdapter()

    def _no_aws(*args, **kwargs):
        raise AssertionError("AWS must not be reached")

    monkeypatch.setattr(adapter, "_session", _no_aws)
    unusable = resource().model_copy(update={"engine_version": "latest"})

    with pytest.raises(ResourceError, match="aws_rds_engine_version_invalid"):
        adapter.create_instance(
            account(), network(), unusable, "devbox-postgres", "gimme-devbox-postgres", []
        )


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


IDENTIFIER = derive_instance_identifier("devbox-postgres")


def live_instance(**updates) -> InstanceObservation:
    values = {
        "identity": f"arn:aws:rds:us-east-1:123456789012:db:{IDENTIFIER}",
        "status": "available", "engine_version": "17.2", "endpoint": "db.example.test",
        "port": 5432, "master_secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:m",
        "instance_class": "db.t3.medium", "allocated_storage_gb": 20,
        "security_group_ids": ("sg-0123456789abcdef0", "sg-0123456789abcdef1"),
        "parameter_group_name": f"{IDENTIFIER}-params", "parameter_group_status": "in-sync",
    }
    values.update(updates)
    return InstanceObservation(**values)  # type: ignore[arg-type]


def desired(**updates) -> AWSRDSPostgresResource:
    return resource().model_copy(update=updates)


class ConvergingAdapter:
    """An existing instance. AWS applies a modification immediately: the changed values
    become pending and the status 'modifying' until `settle_polls` describes have passed."""

    _FIELDS = {
        "EngineVersion": ("engine_version", "pending_engine_version"),
        "DBInstanceClass": ("instance_class", "pending_instance_class"),
        "AllocatedStorage": ("allocated_storage_gb", "pending_allocated_storage_gb"),
    }

    def __init__(self, live: InstanceObservation, *, settle_polls: int = 0) -> None:
        self.live = live
        self.settle_polls = settle_polls
        self.modify_calls: list[dict[str, object]] = []
        self.reboot_calls = 0
        self.create_calls = 0
        self._polls = 0
        self._on_settle: dict[str, object] = {}

    def describe_instance(self, account, network, aws_instance_identifier):
        if self.live.status in ("modifying", "rebooting"):
            self._polls += 1
            if self._polls > self.settle_polls:
                self.live = dataclasses.replace(
                    self.live, status="available", modification_pending=False,
                    pending_engine_version=None, pending_instance_class=None,
                    pending_allocated_storage_gb=None, **self._on_settle,  # type: ignore[arg-type]
                )
                self._on_settle = {}
        return self.live

    def modify_instance(self, account, network, resource, name, identifier, changes):
        self.modify_calls.append(dict(changes))
        self._polls = 0
        pending: dict[str, object] = {}
        for field, (live_name, pending_name) in self._FIELDS.items():
            if field in changes:
                pending[pending_name] = changes[field]
                self._on_settle[live_name] = changes[field]
        if "VpcSecurityGroupIds" in changes:
            self._on_settle["security_group_ids"] = tuple(changes["VpcSecurityGroupIds"])  # type: ignore[arg-type]
        if "DBParameterGroupName" in changes:
            self._on_settle.update(
                parameter_group_name=changes["DBParameterGroupName"],
                parameter_group_status="pending-reboot",
            )
        self.live = dataclasses.replace(
            self.live, status="modifying", modification_pending=True, **pending  # type: ignore[arg-type]
        )
        return self.live

    def reboot_instance(self, account, network, identifier):
        self.reboot_calls += 1
        self._polls = 0
        self._on_settle = {"parameter_group_status": "in-sync"}
        self.live = dataclasses.replace(self.live, status="rebooting")
        return self.live

    def create_instance(self, *args, **kwargs):
        self.create_calls += 1
        raise AssertionError("an existing instance must not be recreated")


def converge(adapter: ConvergingAdapter, tmp_path: Path, **resource_updates) -> dict[str, object]:
    clock = FakeClock()
    return apply_provision(
        adapter, tmp_path, account(), network(), desired(**resource_updates), "devbox-postgres",
        sleep=clock.sleep, now=clock.now,
    )


@pytest.mark.parametrize(
    ("live", "updates", "expected"),
    [
        ({}, {}, {}),
        ({}, {"engine_version": "17.5"}, {"EngineVersion": "17.5"}),
        ({}, {"instance_class": "db.m6g.large"}, {"DBInstanceClass": "db.m6g.large"}),
        ({}, {"allocated_storage_gb": 100}, {"AllocatedStorage": 100}),
        (
            {"security_group_ids": ("sg-0123456789abcdef0",)}, {},
            {"VpcSecurityGroupIds": ["sg-0123456789abcdef0", "sg-0123456789abcdef1"]},
        ),
        (
            {"parameter_group_name": "default.postgres17"}, {},
            {"DBParameterGroupName": f"{IDENTIFIER}-params"},
        ),
        (
            {}, {"engine_version": "17.5", "allocated_storage_gb": 40},
            {"EngineVersion": "17.5", "AllocatedStorage": 40},
        ),
        # Values AWS already has pending are treated as applied and never re-sent.
        (
            {"pending_engine_version": "17.5", "pending_allocated_storage_gb": 40},
            {"engine_version": "17.5", "allocated_storage_gb": 40}, {},
        ),
        # Fields the observation did not report are left alone.
        ({"instance_class": None, "allocated_storage_gb": None, "security_group_ids": None,
          "parameter_group_name": None}, {"instance_class": "db.m6g.large"}, {}),
    ],
)
def test_modification_sends_only_the_changed_fields(live, updates, expected) -> None:
    assert modification_for(desired(**updates), live_instance(**live), IDENTIFIER) == expected


@pytest.mark.parametrize(
    ("live", "updates", "code"),
    [
        ({"allocated_storage_gb": 100}, {"allocated_storage_gb": 50},
         "aws_rds_modify_forbidden_allocated_storage_gb"),
        ({"pending_allocated_storage_gb": 100}, {"allocated_storage_gb": 50},
         "aws_rds_modify_forbidden_allocated_storage_gb"),
        ({"engine_version": "17.5"}, {"engine_version": "17.2"},
         "aws_rds_modify_forbidden_engine_downgrade"),
        ({"engine_version": "16.4"}, {"engine_version": "17.2"},
         "aws_rds_modify_forbidden_engine_major"),
    ],
)
def test_apply_refuses_what_the_allowlist_forbids_before_any_modify_call(
    tmp_path: Path, live, updates, code
) -> None:
    adapter = ConvergingAdapter(live_instance(**live))

    with pytest.raises(ResourceError, match=f"^{code}$"):
        converge(adapter, tmp_path, **updates)

    assert adapter.modify_calls == [] and adapter.reboot_calls == 0


def test_apply_modifies_an_existing_instance_once_and_polls_to_ready(tmp_path: Path) -> None:
    adapter = ConvergingAdapter(live_instance(), settle_polls=2)

    first = converge(adapter, tmp_path, instance_class="db.m6g.large", allocated_storage_gb=40)
    second = converge(adapter, tmp_path, instance_class="db.m6g.large", allocated_storage_gb=40)

    assert adapter.modify_calls == [
        {"DBInstanceClass": "db.m6g.large", "AllocatedStorage": 40}
    ]
    assert first["modified_fields"] == ["AllocatedStorage", "DBInstanceClass"]
    assert first["phase"] == "ready" and first["rebooted"] is False
    assert second["modified_fields"] == [] and second["phase"] == "ready"
    assert adapter.create_calls == 0


def test_a_second_apply_during_a_modification_makes_no_modify_call_and_reports_pending(
    tmp_path: Path,
) -> None:
    adapter = ConvergingAdapter(live_instance(), settle_polls=1000)

    first = converge(adapter, tmp_path, engine_version="17.5")
    resumed = converge(adapter, tmp_path, engine_version="17.5")

    assert first["phase"] == "pending" and resumed["phase"] == "pending"
    assert adapter.modify_calls == [{"EngineVersion": "17.5"}]
    assert resumed["modified_fields"] == []
    observed = load_observed(tmp_path, "devbox-postgres")
    assert observed is not None and observed["phase"] == "pending"


def test_an_available_instance_with_a_matching_pending_change_is_polled_not_remodified(
    tmp_path: Path,
) -> None:
    adapter = ConvergingAdapter(live_instance(
        pending_engine_version="17.5", modification_pending=True,
    ))

    result = converge(adapter, tmp_path, engine_version="17.5")

    assert adapter.modify_calls == []
    assert result["phase"] == "pending", "a pending managed change holds readiness"


def test_an_unsettled_instance_is_diffed_after_it_settles_not_reported_ready_with_drift(
    tmp_path: Path,
) -> None:
    adapter = ConvergingAdapter(live_instance(status="modifying"), settle_polls=1)

    result = converge(adapter, tmp_path, instance_class="db.m6g.large")

    assert adapter.modify_calls == [{"DBInstanceClass": "db.m6g.large"}]
    assert result["modified_fields"] == ["DBInstanceClass"] and result["phase"] == "ready"


def test_a_parameter_group_pending_reboot_triggers_exactly_one_reboot(tmp_path: Path) -> None:
    adapter = ConvergingAdapter(live_instance(parameter_group_status="pending-reboot"))

    first = converge(adapter, tmp_path)
    second = converge(adapter, tmp_path)

    assert adapter.reboot_calls == 1
    assert first["rebooted"] is True and first["phase"] == "ready"
    assert second["rebooted"] is False
    assert adapter.modify_calls == []


def test_attaching_the_parameter_group_reboots_once_after_the_modification_settles(
    tmp_path: Path,
) -> None:
    adapter = ConvergingAdapter(live_instance(parameter_group_name="default.postgres17"))

    result = converge(adapter, tmp_path)

    assert adapter.modify_calls == [{"DBParameterGroupName": f"{IDENTIFIER}-params"}]
    assert adapter.reboot_calls == 1
    assert result["modified_fields"] == ["DBParameterGroupName"] and result["rebooted"] is True


def test_no_reboot_while_the_instance_is_unsettled_or_a_modification_is_pending(
    tmp_path: Path,
) -> None:
    adapter = ConvergingAdapter(
        live_instance(parameter_group_status="pending-reboot", pending_engine_version="17.5"),
    )

    result = converge(adapter, tmp_path, engine_version="17.5")

    assert adapter.reboot_calls == 0 and result["rebooted"] is False
    assert result["phase"] == "pending"


def test_a_modify_failure_surfaces_a_bounded_code_and_persists_nothing_new(
    tmp_path: Path,
) -> None:
    adapter = ConvergingAdapter(live_instance())

    def denied(*args, **kwargs):
        raise ResourceError("aws_rds_modify_access_denied")

    adapter.modify_instance = denied  # type: ignore[method-assign]

    with pytest.raises(ResourceError, match="^aws_rds_modify_access_denied$"):
        converge(adapter, tmp_path, instance_class="db.m6g.large")

    assert load_observed(tmp_path, "devbox-postgres") is None


def _modify_stub(monkeypatch):
    rds, stub = _stubbed("rds")
    adapter = BotoRDSAdapter()
    monkeypatch.setattr(adapter, "_session", lambda *a, **k: _StubbedSession({"rds": rds}))
    return adapter, stub


def _describe_stub(stub) -> None:
    stub.add_response(
        "describe_db_instances",
        {"DBInstances": [_instance_response(IDENTIFIER, tag="devbox-postgres")]},
        {"DBInstanceIdentifier": IDENTIFIER},
    )


def test_modify_instance_sends_only_the_changed_field_immediately_and_never_a_major_upgrade(
    monkeypatch,
) -> None:
    adapter, stub = _modify_stub(monkeypatch)
    stub.add_response(
        "modify_db_instance", {},
        {
            "DBInstanceIdentifier": IDENTIFIER, "ApplyImmediately": True,
            "DBInstanceClass": "db.m6g.large",
        },
    )
    _describe_stub(stub)

    with stub:
        adapter.modify_instance(
            account(), network(), desired(instance_class="db.m6g.large"), "devbox-postgres",
            IDENTIFIER, {"DBInstanceClass": "db.m6g.large"},
        )
        stub.assert_no_pending_responses()


def test_modify_instance_attaching_the_parameter_group_first_ensures_the_owned_group(
    monkeypatch,
) -> None:
    adapter, stub = _modify_stub(monkeypatch)
    stub.add_response("create_db_parameter_group", {}, _parameter_group_request(IDENTIFIER, 17))
    stub.add_response("modify_db_parameter_group", {}, _force_ssl_request(IDENTIFIER))
    stub.add_response(
        "modify_db_instance", {},
        {
            "DBInstanceIdentifier": IDENTIFIER, "ApplyImmediately": True,
            "DBParameterGroupName": f"{IDENTIFIER}-params",
        },
    )
    _describe_stub(stub)

    with stub:
        adapter.modify_instance(
            account(), network(), resource(), "devbox-postgres", IDENTIFIER,
            {"DBParameterGroupName": f"{IDENTIFIER}-params"},
        )
        stub.assert_no_pending_responses()


def test_modify_instance_refuses_fields_outside_the_allowlist_without_any_aws_call(
    monkeypatch,
) -> None:
    adapter = BotoRDSAdapter()
    monkeypatch.setattr(
        adapter, "_session", lambda *a, **k: pytest.fail("must not reach AWS")
    )

    for changes in ({}, {"AllowMajorVersionUpgrade": True}, {"MultiAZ": False},
                    {"DBInstanceClass": "db.m6g.large", "DeletionProtection": False}):
        with pytest.raises(ResourceError, match="^aws_rds_modify_field_forbidden$"):
            adapter.modify_instance(
                account(), network(), resource(), "devbox-postgres", IDENTIFIER, changes
            )


@pytest.mark.parametrize(
    ("provider_code", "expected"),
    [("AccessDenied", "aws_rds_modify_access_denied"),
     ("InvalidDBInstanceState", "aws_rds_modify_invalid_state"),
     ("InvalidParameterCombination", "aws_rds_modify_unavailable")],
)
def test_modify_instance_bounds_provider_failures(monkeypatch, provider_code, expected) -> None:
    adapter, stub = _modify_stub(monkeypatch)
    stub.add_client_error(
        "modify_db_instance", service_error_code=provider_code,
        service_message="arn:aws:rds:secret-detail must never leak",
    )

    with stub, pytest.raises(ResourceError) as raised:
        adapter.modify_instance(
            account(), network(), resource(), "devbox-postgres", IDENTIFIER,
            {"AllocatedStorage": 40},
        )

    assert str(raised.value) == expected


def test_reboot_instance_never_forces_a_failover(monkeypatch) -> None:
    adapter, stub = _modify_stub(monkeypatch)
    stub.add_response(
        "reboot_db_instance", {}, {"DBInstanceIdentifier": IDENTIFIER, "ForceFailover": False}
    )
    _describe_stub(stub)

    with stub:
        adapter.reboot_instance(account(), network(), IDENTIFIER)
        stub.assert_no_pending_responses()


def test_observation_parses_pending_values_and_the_parameter_group_status() -> None:
    response = {
        **_instance_response(IDENTIFIER, tag="devbox-postgres"),
        "PendingModifiedValues": {
            "EngineVersion": "17.5", "DBInstanceClass": "db.m6g.large",
            "AllocatedStorage": 40, "BackupRetentionPeriod": 14,
        },
        "DBParameterGroups": [
            {"DBParameterGroupName": f"{IDENTIFIER}-params",
             "ParameterApplyStatus": "pending-reboot"}
        ],
    }

    observed = BotoRDSAdapter._observation(response, IDENTIFIER)

    assert (observed.pending_engine_version, observed.pending_instance_class,
            observed.pending_allocated_storage_gb) == ("17.5", "db.m6g.large", 40)
    assert (observed.parameter_group_name, observed.parameter_group_status) == (
        f"{IDENTIFIER}-params", "pending-reboot",
    )
    assert observed.converging is True
    other = BotoRDSAdapter._observation(
        {**_instance_response(IDENTIFIER, tag="devbox-postgres"),
         "PendingModifiedValues": {"BackupRetentionPeriod": 14}},
        IDENTIFIER,
    )
    assert other.modification_pending is True and other.converging is False
