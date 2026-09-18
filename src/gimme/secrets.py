from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from gimme.control import AWSProviderAccount, AWSSecretsManagerStore, ControlState, SecretReference


MAX_SECRET_VALUE_BYTES = 8 * 1024
MAX_RESOLVED_PAYLOAD_BYTES = 64 * 1024


class SecretError(RuntimeError):
    """A bounded error whose text is safe for plans, logs, and journals."""


@dataclass(frozen=True)
class SecretMetadata:
    version_id: str
    identity: str = ""


class AWSSecretAdapter(Protocol):
    def known_regions(self) -> set[str]: ...
    def verify_role(self, account: AWSProviderAccount, role_arn: str) -> None: ...
    def describe(self, account: AWSProviderAccount, store_name: str,
                 store: AWSSecretsManagerStore, secret: str) -> SecretMetadata: ...
    def resolve(self, account: AWSProviderAccount, store_name: str,
                store: AWSSecretsManagerStore, secret: str, version_id: str) -> str: ...


def _provider_error(exc: Exception, operation: str) -> SecretError:
    code = "unavailable"
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        provider_code = error.get("Code") if isinstance(error, dict) else None
        mapping = {
            "AccessDenied": "access_denied", "AccessDeniedException": "access_denied",
            "ResourceNotFoundException": "missing",
            "InvalidRequestException": "disabled_or_deleting",
            "DecryptionFailure": "revoked", "DecryptionFailureException": "revoked",
            "Throttling": "throttled", "ThrottlingException": "throttled",
            "TooManyRequestsException": "throttled",
        }
        if isinstance(provider_code, str):
            code = mapping.get(provider_code, "unavailable")
    return SecretError(f"aws_secret_{operation}_{code}")


class BotoAWSSecretAdapter:
    """Narrow AWS boundary. Planning uses only the inspection role and DescribeSecret."""

    @staticmethod
    def _boto3():
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise SecretError("aws_sdk_unavailable") from exc
        return boto3

    def known_regions(self) -> set[str]:
        try:
            return set(self._boto3().session.Session().get_available_regions("secretsmanager"))
        except SecretError:
            raise
        except Exception as exc:
            raise _provider_error(exc, "region") from None

    def _session(self, account: AWSProviderAccount, role_arn: str, purpose: str):
        boto3 = self._boto3()
        try:
            response = boto3.client("sts").assume_role(
                RoleArn=role_arn, RoleSessionName=f"gimme-{purpose}", DurationSeconds=900,
            )
            credentials = response["Credentials"]
            session = boto3.session.Session(
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
            )
            if session.client("sts").get_caller_identity().get("Account") != account.account_id:
                raise SecretError("aws_role_account_mismatch")
            return session
        except SecretError:
            raise
        except Exception as exc:
            raise _provider_error(exc, "identity") from None

    def verify_role(self, account: AWSProviderAccount, role_arn: str) -> None:
        purpose = "inspect" if role_arn == account.inspection_role_arn else "resolve"
        self._session(account, role_arn, purpose)

    @staticmethod
    def _name(store: AWSSecretsManagerStore, secret: str) -> str:
        name = f"{store.prefix}/{secret}"
        if len(name.encode()) > 512:
            raise SecretError("aws_secret_name_too_long")
        return name

    @staticmethod
    def _validate_metadata(response: dict[str, object], account: AWSProviderAccount,
                           store_name: str, store: AWSSecretsManagerStore,
                           expected_name: str) -> SecretMetadata:
        arn, name = response.get("ARN"), response.get("Name")
        if not isinstance(arn, str) or not isinstance(name, str) or name != expected_name:
            raise SecretError("aws_secret_identity_mismatch")
        parts = arn.split(":", 5)
        if len(parts) != 6 or parts[2] != "secretsmanager" or parts[3] != store.region or (
            parts[4] != account.account_id
        ):
            raise SecretError("aws_secret_boundary_mismatch")
        if response.get("DeletedDate") is not None:
            raise SecretError("aws_secret_disabled_or_deleting")
        tags = response.get("Tags", [])
        ownership = {item.get("Key"): item.get("Value") for item in tags
                     if isinstance(item, dict)} if isinstance(tags, list) else {}
        if ownership.get("gimme:secret-store") != store_name:
            raise SecretError("aws_secret_ownership_mismatch")
        kms = response.get("KmsKeyId")
        if store.kms_key_arn is None:
            if kms not in {None, "alias/aws/secretsmanager"}:
                raise SecretError("aws_secret_kms_policy_mismatch")
        elif kms != store.kms_key_arn:
            raise SecretError("aws_secret_kms_policy_mismatch")
        versions = response.get("VersionIdsToStages")
        if not isinstance(versions, dict):
            raise SecretError("aws_secret_version_unknown")
        current = [version for version, stages in versions.items()
                   if isinstance(version, str) and isinstance(stages, list)
                   and "AWSCURRENT" in stages]
        if len(current) != 1:
            raise SecretError("aws_secret_version_unknown")
        return SecretMetadata(version_id=current[0], identity=arn)

    def describe(self, account: AWSProviderAccount, store_name: str,
                 store: AWSSecretsManagerStore, secret: str) -> SecretMetadata:
        session = self._session(account, account.inspection_role_arn, "inspect")
        expected_name = self._name(store, secret)
        try:
            response = session.client("secretsmanager", region_name=store.region).describe_secret(
                SecretId=expected_name
            )
        except Exception as exc:
            raise _provider_error(exc, "metadata") from None
        return self._validate_metadata(response, account, store_name, store, expected_name)

    def resolve(self, account: AWSProviderAccount, store_name: str,
                store: AWSSecretsManagerStore, secret: str, version_id: str) -> str:
        session = self._session(account, account.resolver_role_arn, "resolve")
        try:
            response = session.client("secretsmanager", region_name=store.region).get_secret_value(
                SecretId=self._name(store, secret), VersionId=version_id,
            )
        except Exception as exc:
            raise _provider_error(exc, "value") from None
        if response.get("VersionId") != version_id or "SecretBinary" in response:
            raise SecretError("aws_secret_value_invalid")
        value = response.get("SecretString")
        if not isinstance(value, str):
            raise SecretError("aws_secret_value_invalid")
        return value


def _fingerprint(kind: str, *values: str) -> str:
    digest = hashlib.sha256(f"gimme-{kind}-v1\0".encode())
    for value in values:
        digest.update(value.encode())
        digest.update(b"\0")
    return f"{kind}_{digest.hexdigest()}"


def reference_fingerprint(reference: SecretReference) -> str:
    return _fingerprint("ref", reference.store, reference.secret, reference.field)


def _safe_sops_document(path: Path) -> object:
    if not path.is_file() or path.is_symlink():
        raise SecretError("sops_document_missing")
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SecretError("sops_document_invalid") from None


def _lookup(document: object, reference: str) -> object:
    value = document
    for part in reference.split("/"):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(reference)
        value = value[part]
    return value


def _sops_ciphertext(path: Path, reference: SecretReference) -> str:
    try:
        value = _lookup(_safe_sops_document(path), f"{reference.secret}/{reference.field}")
    except KeyError:
        raise SecretError("sops_reference_missing") from None
    if not isinstance(value, str) or not value.startswith("ENC["):
        raise SecretError("sops_reference_not_encrypted")
    return value


def validate_aws_account(account: AWSProviderAccount, adapter: AWSSecretAdapter) -> None:
    adapter.verify_role(account, account.inspection_role_arn)
    adapter.verify_role(account, account.resolver_role_arn)


def validate_aws_store(store: AWSSecretsManagerStore, adapter: AWSSecretAdapter) -> None:
    if store.region not in adapter.known_regions():
        raise SecretError("aws_region_unknown")


def plan_secret_references(state: ControlState, path: Path,
                           references: dict[str, SecretReference],
                           adapter: AWSSecretAdapter | None = None,
                           applied: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    aws = adapter or BotoAWSSecretAdapter()
    previous = {
        item["environment_key"]: item
        for item in (applied or [])
        if isinstance(item, dict) and isinstance(item.get("environment_key"), str)
    }
    planned: list[dict[str, str]] = []
    for environment_key, reference in sorted(references.items()):
        secret_store = state.secret_stores[reference.store]
        try:
            if secret_store.provider == "sops":
                version = _fingerprint("ver", _sops_ciphertext(path, reference))
            else:
                account = state.provider_accounts[secret_store.provider_account]
                metadata = aws.describe(account, reference.store, secret_store, reference.secret)
                version = _fingerprint("ver", metadata.identity, metadata.version_id)
        except SecretError as exc:
            code = str(exc)
            if "missing" not in code:
                raise
            status = "missing"
            planned.append({"environment_key": environment_key,
                            "reference_fingerprint": reference_fingerprint(reference),
                            "version_fingerprint": _fingerprint("ver", status),
                            "status": status})
            continue
        old = previous.get(environment_key)
        if old is None or old.get("reference_fingerprint") != reference_fingerprint(reference):
            status = "unknown"
        elif old.get("version_fingerprint") == version:
            status = "current"
        else:
            status = "rotated"
        planned.append({"environment_key": environment_key,
                        "reference_fingerprint": reference_fingerprint(reference),
                        "version_fingerprint": version, "status": status})
    return planned


def load_applied_secret_manifest(root: Path, deployment: str) -> list[dict[str, str]] | None:
    if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", deployment) is None:
        raise SecretError("secret_manifest_identity_invalid")
    path = root / "applied-secrets" / f"{deployment}.json"
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, list) or len(value) > 128:
        return None
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "environment_key", "reference_fingerprint", "version_fingerprint", "status"
        }:
            return None
        if (
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", item.get("environment_key", "")) is None
            or re.fullmatch(r"ref_[0-9a-f]{64}", item.get("reference_fingerprint", "")) is None
            or re.fullmatch(r"ver_[0-9a-f]{64}", item.get("version_fingerprint", "")) is None
        ):
            return None
    return value


def save_applied_secret_manifest(root: Path, deployment: str,
                                 manifest: list[dict[str, str]]) -> None:
    directory = root / "applied-secrets"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{deployment}.", dir=directory)
    target = directory / f"{deployment}.json"
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(manifest, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)


def _decrypt_sops(path: Path) -> object:
    executable = shutil.which("sops")
    if executable is None:
        raise SecretError("sops_unavailable")
    if not path.is_file() or path.is_symlink():
        raise SecretError("sops_document_missing")
    result = subprocess.run(  # nosec B603
        [executable, "--decrypt", "--output-type", "json", str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=30, check=False,
        env={name: os.environ[name] for name in ("PATH", "SOPS_AGE_KEY", "SOPS_AGE_KEY_FILE")
             if name in os.environ},
    )
    if result.returncode != 0:
        raise SecretError("sops_decryption_failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SecretError("sops_plaintext_invalid") from None


def _json_object(value: str) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise SecretError("aws_secret_duplicate_field")
            result[key] = item
        return result
    try:
        document = json.loads(value, object_pairs_hook=unique)
    except SecretError:
        raise
    except (json.JSONDecodeError, UnicodeError):
        raise SecretError("aws_secret_json_invalid") from None
    if not isinstance(document, dict):
        raise SecretError("aws_secret_json_invalid")
    return document


def _validate_value(value: object) -> str:
    if not isinstance(value, str):
        raise SecretError("secret_field_not_string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise SecretError("secret_value_invalid") from None
    if b"\0" in encoded or b"\r" in encoded or b"\n" in encoded:
        raise SecretError("secret_value_not_single_line")
    if len(encoded) > MAX_SECRET_VALUE_BYTES:
        raise SecretError("secret_value_too_large")
    return value


def resolve_planned_secret_references(state: ControlState, path: Path,
                                      references: dict[str, SecretReference],
                                      planned: list[dict[str, str]],
                                      adapter: AWSSecretAdapter | None = None) -> dict[str, str]:
    aws = adapter or BotoAWSSecretAdapter()
    expected = {item["environment_key"]: item for item in planned}
    sops_document: object | None = None
    aws_documents: dict[tuple[str, str], dict[str, object]] = {}
    resolved: dict[str, str] = {}
    total = 0
    for environment_key, reference in sorted(references.items()):
        item = expected.get(environment_key)
        if item is None or item.get("reference_fingerprint") != reference_fingerprint(reference):
            raise SecretError("secret_plan_stale")
        secret_store = state.secret_stores[reference.store]
        if secret_store.provider == "sops":
            ciphertext = _sops_ciphertext(path, reference)
            if item.get("version_fingerprint") != _fingerprint("ver", ciphertext):
                raise SecretError("secret_plan_stale")
            if sops_document is None:
                sops_document = _decrypt_sops(path)
            try:
                value = _lookup(sops_document, f"{reference.secret}/{reference.field}")
            except KeyError:
                raise SecretError("sops_reference_missing") from None
        else:
            account = state.provider_accounts[secret_store.provider_account]
            metadata = aws.describe(account, reference.store, secret_store, reference.secret)
            if item.get("version_fingerprint") != _fingerprint(
                "ver", metadata.identity, metadata.version_id
            ):
                raise SecretError("secret_plan_stale")
            cache_key = (reference.store, reference.secret)
            if cache_key not in aws_documents:
                plaintext = aws.resolve(account, reference.store, secret_store,
                                        reference.secret, metadata.version_id)
                aws_documents[cache_key] = _json_object(plaintext)
            document = aws_documents[cache_key]
            if reference.field not in document:
                raise SecretError("secret_field_missing")
            value = document[reference.field]
        selected = _validate_value(value)
        total += len(selected.encode())
        if total > MAX_RESOLVED_PAYLOAD_BYTES:
            raise SecretError("secret_payload_too_large")
        resolved[environment_key] = selected
    return resolved


def resolve_secret_references(path: Path, references: dict[str, str]) -> dict[str, str]:
    """Compatibility helper for direct local-SOPS callers."""
    document = _decrypt_sops(path)
    resolved: dict[str, str] = {}
    for key, reference in references.items():
        try:
            resolved[key] = _validate_value(_lookup(document, reference))
        except KeyError:
            raise SecretError("sops_reference_missing") from None
    return resolved


@contextmanager
def protected_secret_file(values: dict[str, str]) -> Iterator[Path | None]:
    if not values:
        yield None
        return
    descriptor, temporary = tempfile.mkstemp(prefix="gimme-secrets-", suffix=".json")
    path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(values, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
        yield path
    finally:
        path.unlink(missing_ok=True)
