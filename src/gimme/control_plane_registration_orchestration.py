from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from gimme.control import (
    AWSProviderAccount,
    AWSSecretsManagerStore,
    S3ArtifactStore,
    S3BackupDestination,
    SecretStore,
)
from gimme.control_plans import exact_plan
from gimme.artifact_public import public_store_policy
from gimme.recovery import preflight_backup_destination
from gimme.secrets import validate_aws_account, validate_aws_store


@dataclass(frozen=True)
class ControlPlaneRegistrationOrchestrator:
    """Own registration policy for control-plane provider and storage definitions."""

    store: Any
    aws_secrets: Any
    backup_s3: Any
    assert_plan: Callable[[dict[str, object], str], None]
    replace: Callable[..., Any]
    delete: Callable[..., Any]
    backup_destination_credentials: Callable[..., Any]

    def account_registration_plan(
        self, name: str, definition: AWSProviderAccount, *, update: bool
    ) -> dict[str, object]:
        state = self.store.load()
        exists = name in state.provider_accounts
        if update != exists:
            message = (
                "provider account already exists" if exists else "provider account missing"
            )
            raise ValueError(message)
        validate_aws_account(definition, self.aws_secrets)
        proposed = self.replace(state, "provider_accounts", name, definition)
        return exact_plan(
            {
                "kind": (
                    "provider_account_update"
                    if update
                    else "provider_account_registration"
                ),
                "name": name,
                "current": (
                    state.provider_accounts[name].model_dump(mode="json")
                    if exists
                    else None
                ),
                "proposed": proposed.provider_accounts[name].model_dump(mode="json"),
                "identity_verified": True,
                "effects": ["replace local desired state only", "make no AWS changes"],
            }
        )

    def plan_register_provider_account(
        self, name: str, definition: AWSProviderAccount
    ) -> dict[str, object]:
        return self.account_registration_plan(name, definition, update=False)

    def register_provider_account(
        self, name: str, definition: AWSProviderAccount, plan_id: str
    ) -> dict[str, object]:
        expected = self.account_registration_plan(name, definition, update=False)
        self.assert_plan(expected, plan_id)
        self.store.save(
            self.replace(self.store.load(), "provider_accounts", name, definition)
        )
        return {"changed": True, "provider_account": name}

    def plan_update_provider_account(
        self, name: str, definition: AWSProviderAccount
    ) -> dict[str, object]:
        return self.account_registration_plan(name, definition, update=True)

    def update_provider_account(
        self, name: str, definition: AWSProviderAccount, plan_id: str
    ) -> dict[str, object]:
        expected = self.account_registration_plan(name, definition, update=True)
        self.assert_plan(expected, plan_id)
        self.store.save(
            self.replace(self.store.load(), "provider_accounts", name, definition)
        )
        return {"changed": True, "provider_account": name}

    def plan_remove_provider_account(self, name: str) -> dict[str, object]:
        state = self.store.load()
        if name not in state.provider_accounts:
            raise KeyError("provider account is not registered")
        stores = sorted(
            store_name
            for store_name, value in state.secret_stores.items()
            if isinstance(value, AWSSecretsManagerStore)
            and value.provider_account == name
        )
        if stores:
            raise ValueError("provider account is still referenced by a secret store")
        return exact_plan(
            {
                "kind": "provider_account_removal",
                "name": name,
                "effects": ["remove local desired state only", "make no AWS changes"],
            }
        )

    def remove_provider_account(self, name: str, plan_id: str) -> dict[str, object]:
        expected = self.plan_remove_provider_account(name)
        self.assert_plan(expected, plan_id)
        self.store.save(self.delete(self.store.load(), "provider_accounts", name))
        return {"changed": True, "provider_account": name}

    def secret_store_registration_plan(
        self, name: str, definition: SecretStore, *, update: bool
    ) -> dict[str, object]:
        if name == "local-sops":
            raise ValueError("the built-in local-sops store cannot be registered or updated")
        state = self.store.load()
        exists = name in state.secret_stores
        if update != exists:
            raise ValueError("secret store already exists" if exists else "secret store missing")
        if not isinstance(definition, AWSSecretsManagerStore):
            raise ValueError("only AWS Secrets Manager stores can be registered")
        account = state.provider_accounts.get(definition.provider_account)
        if account is None:
            raise ValueError("secret store references an unknown provider account")
        validate_aws_store(definition, self.aws_secrets)
        self.aws_secrets.verify_role(account, account.inspection_role_arn)
        proposed = self.replace(state, "secret_stores", name, definition)
        return exact_plan(
            {
                "kind": (
                    "secret_store_update" if update else "secret_store_registration"
                ),
                "name": name,
                "current": (
                    state.secret_stores[name].model_dump(mode="json") if exists else None
                ),
                "proposed": proposed.secret_stores[name].model_dump(mode="json"),
                "ownership_tag": f"gimme:secret-store={name}",
                "identity_verified": True,
                "effects": ["replace local desired state only", "make no AWS changes"],
            }
        )

    def plan_register_secret_store(
        self, name: str, definition: SecretStore
    ) -> dict[str, object]:
        return self.secret_store_registration_plan(name, definition, update=False)

    def register_secret_store(
        self, name: str, definition: SecretStore, plan_id: str
    ) -> dict[str, object]:
        expected = self.secret_store_registration_plan(name, definition, update=False)
        self.assert_plan(expected, plan_id)
        self.store.save(self.replace(self.store.load(), "secret_stores", name, definition))
        return {"changed": True, "secret_store": name}

    def plan_update_secret_store(
        self, name: str, definition: SecretStore
    ) -> dict[str, object]:
        return self.secret_store_registration_plan(name, definition, update=True)

    def update_secret_store(
        self, name: str, definition: SecretStore, plan_id: str
    ) -> dict[str, object]:
        expected = self.secret_store_registration_plan(name, definition, update=True)
        self.assert_plan(expected, plan_id)
        self.store.save(self.replace(self.store.load(), "secret_stores", name, definition))
        return {"changed": True, "secret_store": name}

    def plan_remove_secret_store(self, name: str) -> dict[str, object]:
        if name == "local-sops":
            raise ValueError("the built-in local-sops store cannot be removed")
        state = self.store.load()
        if name not in state.secret_stores:
            raise KeyError("secret store is not registered")
        if any(
            reference.store == name
            for deployment in state.deployments.values()
            for reference in deployment.secrets.values()
        ):
            raise ValueError("secret store is still referenced by a deployment")
        return exact_plan(
            {
                "kind": "secret_store_removal",
                "name": name,
                "effects": ["remove local desired state only", "make no AWS changes"],
            }
        )

    def remove_secret_store(self, name: str, plan_id: str) -> dict[str, object]:
        expected = self.plan_remove_secret_store(name)
        self.assert_plan(expected, plan_id)
        self.store.save(self.delete(self.store.load(), "secret_stores", name))
        return {"changed": True, "secret_store": name}

    def backup_destination_registration_plan(
        self, name: str, definition: S3BackupDestination, *, update: bool
    ) -> dict[str, object]:
        """Diff locally; a plan never contacts the caller-supplied endpoint."""
        state = self.store.load()
        exists = name in state.backup_destinations
        if update != exists:
            message = (
                "backup destination already exists"
                if exists
                else "backup destination missing"
            )
            raise ValueError(message)
        proposed = self.replace(state, "backup_destinations", name, definition)
        return exact_plan(
            {
                "kind": (
                    "backup_destination_update"
                    if update
                    else "backup_destination_registration"
                ),
                "name": name,
                "current": (
                    state.backup_destinations[name].model_dump(mode="json")
                    if exists
                    else None
                ),
                "proposed": proposed.backup_destinations[name].model_dump(mode="json"),
                "preflight_verified": False,
                "preflight": "deferred to apply; plan performs no live destination calls",
                "effects": [
                    "replace local desired state only",
                    "make no destination changes",
                ],
            }
        )

    def backup_destination_apply(
        self,
        name: str,
        definition: S3BackupDestination,
        plan_id: str,
        *,
        update: bool,
    ) -> dict[str, object]:
        expected = self.backup_destination_registration_plan(
            name, definition, update=update
        )
        self.assert_plan(expected, plan_id)
        state = self.store.load()
        _, credentials = self.backup_destination_credentials(state, definition)
        preflight_backup_destination(definition, credentials, self.backup_s3)
        self.store.save(
            self.replace(self.store.load(), "backup_destinations", name, definition)
        )
        return {"changed": True, "backup_destination": name}

    def plan_register_backup_destination(
        self, name: str, definition: S3BackupDestination
    ) -> dict[str, object]:
        return self.backup_destination_registration_plan(name, definition, update=False)

    def register_backup_destination(
        self, name: str, definition: S3BackupDestination, plan_id: str
    ) -> dict[str, object]:
        return self.backup_destination_apply(
            name, definition, plan_id, update=False
        )

    def plan_update_backup_destination(
        self, name: str, definition: S3BackupDestination
    ) -> dict[str, object]:
        return self.backup_destination_registration_plan(name, definition, update=True)

    def update_backup_destination(
        self, name: str, definition: S3BackupDestination, plan_id: str
    ) -> dict[str, object]:
        return self.backup_destination_apply(name, definition, plan_id, update=True)

    def plan_remove_backup_destination(self, name: str) -> dict[str, object]:
        state = self.store.load()
        if name not in state.backup_destinations:
            raise KeyError("backup destination is not registered")
        if any(
            deployment.recovery is not None
            and deployment.recovery.destination == name
            for deployment in state.deployments.values()
        ):
            raise ValueError("backup destination is still referenced by a deployment")
        return exact_plan(
            {
                "kind": "backup_destination_removal",
                "name": name,
                "effects": [
                    "remove local desired state only",
                    "make no destination changes",
                ],
            }
        )

    def remove_backup_destination(self, name: str, plan_id: str) -> dict[str, object]:
        expected = self.plan_remove_backup_destination(name)
        self.assert_plan(expected, plan_id)
        self.store.save(self.delete(self.store.load(), "backup_destinations", name))
        return {"changed": True, "backup_destination": name}

    def artifact_store_registration_plan(
        self, name: str, definition: S3ArtifactStore, *, update: bool
    ) -> dict[str, object]:
        state = self.store.load()
        exists = name in state.artifact_stores
        if update != exists:
            raise ValueError(
                "artifact store already exists" if exists else "artifact store missing"
            )
        proposed = self.replace(state, "artifact_stores", name, definition)
        return exact_plan({
            "kind": "artifact_store_update" if update else "artifact_store_registration",
            "name": name,
            "current": (
                public_store_policy(state.artifact_stores[name]) if exists else None
            ),
            "proposed": public_store_policy(proposed.artifact_stores[name]),
            "effects": [
                "replace local desired state only",
                "make no Target or object-store changes",
            ],
        })

    def plan_register_artifact_store(
        self, name: str, definition: S3ArtifactStore
    ) -> dict[str, object]:
        return self.artifact_store_registration_plan(name, definition, update=False)

    def register_artifact_store(
        self, name: str, definition: S3ArtifactStore, plan_id: str
    ) -> dict[str, object]:
        expected = self.plan_register_artifact_store(name, definition)
        self.assert_plan(expected, plan_id)
        self.store.save(self.replace(self.store.load(), "artifact_stores", name, definition))
        return {"changed": True, "artifact_store": name}

    def plan_update_artifact_store(
        self, name: str, definition: S3ArtifactStore
    ) -> dict[str, object]:
        return self.artifact_store_registration_plan(name, definition, update=True)

    def update_artifact_store(
        self, name: str, definition: S3ArtifactStore, plan_id: str
    ) -> dict[str, object]:
        expected = self.plan_update_artifact_store(name, definition)
        self.assert_plan(expected, plan_id)
        self.store.save(self.replace(self.store.load(), "artifact_stores", name, definition))
        return {"changed": True, "artifact_store": name}

    def plan_remove_artifact_store(self, name: str) -> dict[str, object]:
        state = self.store.load()
        if name not in state.artifact_stores:
            raise KeyError("artifact store is not registered")
        if any(
            application.build is not None and application.build.artifact_store == name
            for application in state.applications.values()
        ):
            raise ValueError("artifact store is still referenced by an application")
        return exact_plan({
            "kind": "artifact_store_removal",
            "name": name,
            "effects": [
                "remove local desired state only",
                "make no Target or object-store changes",
            ],
        })

    def remove_artifact_store(self, name: str, plan_id: str) -> dict[str, object]:
        expected = self.plan_remove_artifact_store(name)
        self.assert_plan(expected, plan_id)
        self.store.save(self.delete(self.store.load(), "artifact_stores", name))
        return {"changed": True, "artifact_store": name}
