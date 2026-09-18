import json
import stat
import subprocess
from pathlib import Path

import pytest

import gimme.secrets as secrets_module
from gimme.control import (
    AWSProviderAccount, AWSSecretsManagerStore, ControlState, SecretReference,
)
from gimme.secrets import (
    SecretError, SecretMetadata, load_applied_secret_manifest, plan_secret_references,
    protected_secret_file, resolve_planned_secret_references, resolve_secret_references,
    save_applied_secret_manifest,
)


class FakeAWS:
    def __init__(self, *, version: str = "version-1", value: str = '{"TOKEN":"value"}'):
        self.version = version
        self.value = value
        self.descriptions = 0
        self.resolutions: list[str] = []

    def known_regions(self) -> set[str]:
        return {"us-east-1"}

    def verify_role(self, account, role_arn) -> None:
        pass

    def describe(self, account, store_name, store, secret) -> SecretMetadata:
        self.descriptions += 1
        return SecretMetadata(self.version)

    def resolve(self, account, store_name, store, secret, version_id) -> str:
        self.resolutions.append(version_id)
        return self.value


class MissingAWS(FakeAWS):
    def describe(self, account, store_name, store, secret) -> SecretMetadata:
        raise SecretError("aws_secret_metadata_missing")


def aws_state() -> ControlState:
    return ControlState(
        provider_accounts={
            "production": AWSProviderAccount(
                account_id="123456789012",
                inspection_role_arn="arn:aws:iam::123456789012:role/gimme-inspect",
                resolver_role_arn="arn:aws:iam::123456789012:role/gimme-resolve",
            )
        },
        secret_stores={
            "local-sops": {"provider": "sops"},
            "application": AWSSecretsManagerStore(
                provider_account="production", region="us-east-1", prefix="gimme/application"
            ),
        },
    )


def test_secret_references_are_resolved_without_returning_document(
    tmp_path: Path, monkeypatch
) -> None:
    encrypted = tmp_path / "secrets.enc.json"
    encrypted.write_text("encrypted")
    monkeypatch.setattr(secrets_module.shutil, "which", lambda name: "/usr/bin/sops")
    monkeypatch.setattr(
        secrets_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps({"preview": {"TOKEN": "value"}}), ""
        ),
    )

    assert resolve_secret_references(encrypted, {"API_TOKEN": "preview/TOKEN"}) == {
        "API_TOKEN": "value"
    }


def test_missing_secret_reports_bounded_code_not_reference(tmp_path: Path, monkeypatch) -> None:
    encrypted = tmp_path / "secrets.enc.json"
    encrypted.write_text("encrypted")
    monkeypatch.setattr(secrets_module.shutil, "which", lambda name: "/usr/bin/sops")
    monkeypatch.setattr(
        secrets_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "{}", ""),
    )
    with pytest.raises(SecretError, match="sops_reference_missing"):
        resolve_secret_references(encrypted, {"API_TOKEN": "preview/TOKEN"})


def test_protected_secret_file_is_owner_only_and_removed() -> None:
    with protected_secret_file({"API_TOKEN": "value"}) as path:
        assert path is not None
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text()) == {"API_TOKEN": "value"}
        remembered = path
    assert not remembered.exists()


def test_aws_plan_uses_metadata_only_and_returns_fingerprints(tmp_path: Path) -> None:
    adapter = FakeAWS()
    reference = SecretReference(store="application", secret="payments/api", field="TOKEN")

    plan = plan_secret_references(
        aws_state(), tmp_path / "unused", {"PAYMENTS_TOKEN": reference}, adapter
    )

    assert adapter.descriptions == 1
    assert adapter.resolutions == []
    serialized = json.dumps(plan)
    assert "payments/api" not in serialized
    assert "version-1" not in serialized
    assert "value" not in serialized
    assert plan[0]["reference_fingerprint"].startswith("ref_")
    assert plan[0]["version_fingerprint"].startswith("ver_")


def test_aws_apply_resolves_exact_planned_version(tmp_path: Path) -> None:
    adapter = FakeAWS()
    reference = SecretReference(store="application", secret="payments/api", field="TOKEN")
    state = aws_state()
    plan = plan_secret_references(
        state, tmp_path / "unused", {"PAYMENTS_TOKEN": reference}, adapter
    )

    resolved = resolve_planned_secret_references(
        state, tmp_path / "unused", {"PAYMENTS_TOKEN": reference}, plan, adapter
    )

    assert resolved == {"PAYMENTS_TOKEN": "value"}
    assert adapter.resolutions == ["version-1"]


def test_aws_rotation_rejects_stale_plan_before_value_retrieval(tmp_path: Path) -> None:
    adapter = FakeAWS()
    reference = SecretReference(store="application", secret="payments/api", field="TOKEN")
    state = aws_state()
    plan = plan_secret_references(
        state, tmp_path / "unused", {"PAYMENTS_TOKEN": reference}, adapter
    )
    adapter.version = "version-2"

    with pytest.raises(SecretError, match="secret_plan_stale"):
        resolve_planned_secret_references(
            state, tmp_path / "unused", {"PAYMENTS_TOKEN": reference}, plan, adapter
        )

    assert adapter.resolutions == []


def test_missing_aws_reference_is_classified_without_value_retrieval(tmp_path: Path) -> None:
    adapter = MissingAWS()
    reference = SecretReference(store="application", secret="payments/api", field="TOKEN")

    plan = plan_secret_references(
        aws_state(), tmp_path / "unused", {"PAYMENTS_TOKEN": reference}, adapter
    )

    assert plan[0]["status"] == "missing"
    assert adapter.resolutions == []


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ('{"TOKEN":"first","TOKEN":"second"}', "aws_secret_duplicate_field"),
        ('{"TOKEN":["nested"]}', "secret_field_not_string"),
        ('{"OTHER":"value"}', "secret_field_missing"),
        ('{"TOKEN":"line\\nvalue"}', "secret_value_not_single_line"),
        ('{"TOKEN":"' + "x" * 8193 + '"}', "secret_value_too_large"),
    ],
)
def test_aws_value_failures_are_bounded(tmp_path: Path, value: str, code: str) -> None:
    adapter = FakeAWS(value=value)
    reference = SecretReference(store="application", secret="payments/api", field="TOKEN")
    state = aws_state()
    plan = plan_secret_references(
        state, tmp_path / "unused", {"PAYMENTS_TOKEN": reference}, adapter
    )

    with pytest.raises(SecretError, match=code) as failure:
        resolve_planned_secret_references(
            state, tmp_path / "unused", {"PAYMENTS_TOKEN": reference}, plan, adapter
        )

    assert "first" not in str(failure.value)
    assert "line\nvalue" not in str(failure.value)


def test_sops_plan_fingerprints_ciphertext_without_decrypting(
    tmp_path: Path, monkeypatch
) -> None:
    encrypted = tmp_path / "secrets.enc.json"
    encrypted.write_text(json.dumps({"preview": {"TOKEN": "ENC[AES256_GCM,data:cipher]"}}))
    monkeypatch.setattr(
        secrets_module.subprocess, "run",
        lambda *args, **kwargs: pytest.fail("planning must not invoke SOPS"),
    )
    reference = SecretReference(store="local-sops", secret="preview", field="TOKEN")

    plan = plan_secret_references(ControlState(), encrypted, {"API_TOKEN": reference})

    assert plan[0]["version_fingerprint"].startswith("ver_")
    assert "cipher" not in json.dumps(plan)


def test_applied_manifest_classifies_current_and_rotated_without_values(tmp_path: Path) -> None:
    adapter = FakeAWS()
    reference = SecretReference(store="application", secret="payments/api", field="TOKEN")
    state = aws_state()
    first = plan_secret_references(
        state, tmp_path / "unused", {"PAYMENTS_TOKEN": reference}, adapter
    )
    assert first[0]["status"] == "unknown"
    save_applied_secret_manifest(tmp_path, "checkout", first)
    applied = load_applied_secret_manifest(tmp_path, "checkout")

    current = plan_secret_references(
        state, tmp_path / "unused", {"PAYMENTS_TOKEN": reference}, adapter, applied
    )
    adapter.version = "version-2"
    rotated = plan_secret_references(
        state, tmp_path / "unused", {"PAYMENTS_TOKEN": reference}, adapter, applied
    )

    assert current[0]["status"] == "current"
    assert rotated[0]["status"] == "rotated"
    manifest = (tmp_path / "applied-secrets" / "checkout.json")
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert "value" not in manifest.read_text()
