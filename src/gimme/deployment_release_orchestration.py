from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from gimme.control import (
    ApplicationConfig,
    DeploymentConfig,
    DeploymentSource,
)
from gimme.control_plans import deployment_release_plan, exact_plan


@dataclass(frozen=True)
class DeploymentReleaseOrchestrator:
    """Own the exact, locked Deployment release lifecycle."""

    store: Any
    context: Callable[..., Any]
    run_deployment: Callable[..., Any]
    secret_plan: Callable[..., Any]
    dns_issues: Callable[..., Any]
    managed_database_issues: Callable[..., Any]
    valkey_runtime: Callable[..., Any]
    deployment_resource_lock: Callable[..., Any]
    deployment_resource_locks: Callable[..., Any]
    assert_plan: Callable[..., Any]
    replace: Callable[..., Any]
    result: Callable[..., Any]
    artifact_deployment: Any = None

    @staticmethod
    def _require_source_release(deployment: DeploymentConfig) -> None:
        if deployment.release_mode != "source":
            raise ValueError(
                "artifact release operations require the artifact deployment workflow"
            )

    def _revision(self, name: str) -> str:
        deployment = self.store.deployment(name)
        if deployment.source.kind == "commit":
            return deployment.source.ref
        resolved = self.run_deployment("gimme:resolve-revision", name, timeout=60)
        for raw in resolved.output.splitlines():
            line = raw.split("] ", 1)[-1].strip()
            if line.startswith("GIMME_REVISION|"):
                revision = line.split("|", 1)[1]
                if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", revision):
                    return revision
        raise RuntimeError("source did not resolve to one exact Git revision")

    def _process_preflight(
        self,
        name: str,
        deployment: DeploymentConfig,
        application: ApplicationConfig,
    ) -> tuple[dict[str, object], list[str]]:
        managed = application.framework == "laravel" and (
            deployment.workers is not None or deployment.scheduler is not None
        )
        if not managed:
            return {"required": False, "observed": {}}, []
        preflight = self.run_deployment("gimme:preflight:processes", name, timeout=60)
        observed: dict[str, str] = {}
        for raw in preflight.output.splitlines():
            line = raw.split("] ", 1)[-1].strip()
            if line.startswith("GIMME_") and "|" in line:
                key, value = line.split("|", 1)
                observed[key.removeprefix("GIMME_").lower()] = value
        issues = []
        if observed.get("process_helper") != "ready":
            issues.append("privileged process helper requires target bootstrap")
        if observed.get("pcntl") not in {"ready", "not_required"}:
            issues.append("PHP pcntl extension is required for managed workers")
        if observed.get("posix") not in {"ready", "not_required"}:
            issues.append("PHP posix extension is required for Horizon")
        return {"required": True, "observed": observed}, issues

    def _release_plan(self, name: str, revision: str | None = None) -> dict[str, Any]:
        state, deployment, target, application = self.context(name)
        if deployment.release_mode == "artifact":
            return self._artifact_release_plan(
                name, state, deployment, target, application
            )
        self._require_source_release(deployment)
        _, secret_issues = self.secret_plan(name, state, deployment)
        issues = (
            secret_issues
            + self.dns_issues(deployment, target)
            + self.managed_database_issues(state, deployment)
            + self.valkey_runtime(name, state, deployment)[3]
        )
        if issues:
            raise ValueError("deployment is not ready: " + "; ".join(issues))
        selected = revision or self._revision(name)
        preflight = self.run_deployment(
            "gimme:preflight:runtimes", name, revision=selected, timeout=60
        )
        processes, process_issues = self._process_preflight(
            name, deployment, application
        )
        rendered = self.run_deployment(
            "deploy", name, revision=selected, arguments=("--plan",), timeout=60
        )
        return deployment_release_plan(
            name,
            deployment,
            target,
            application,
            selected,
            rendered.output,
            {
                "declared": {
                    key: value.model_dump(mode="json")
                    for key, value in deployment.runtimes.items()
                },
                "preflight": preflight.output,
            },
            processes,
            process_issues,
        )

    def _artifact_release_plan(
        self, name: str, state, deployment, target, application
    ) -> dict[str, Any]:
        _, secret_issues = self.secret_plan(name, state, deployment)
        issues = (
            secret_issues
            + self.dns_issues(deployment, target)
            + self.managed_database_issues(state, deployment)
            + self.valkey_runtime(name, state, deployment)[3]
        )
        artifact_context = self.artifact_deployment.context(name)
        artifact = artifact_context["artifact"]
        if artifact["status"] == "missing":
            issues.append("artifact_missing")
            processes = {"required": False, "observed": {}}
            process_issues: list[str] = []
            runtime = None
            rendered = ""
        else:
            preflight = self.run_deployment(
                "gimme:preflight:artifact-runtimes", name, timeout=60
            )
            runtime = {
                "declared": {
                    "php": deployment.runtimes["php"].model_dump(mode="json"),
                    "php_extensions": application.php_extensions,
                },
                "preflight": preflight.output,
            }
            processes, process_issues = self._process_preflight(
                name, deployment, application
            )
            request = self.artifact_deployment.materialize_request(artifact_context)
            rendered = self.run_deployment(
                "deploy",
                name,
                revision=str(artifact["commit"]),
                artifact_request=request,
                arguments=("--plan",),
                timeout=60,
            ).output
        return deployment_release_plan(
            name,
            deployment,
            target,
            application,
            str(artifact.get("commit", artifact_context["identity"]["commit"])),
            rendered,
            runtime,
            processes,
            issues + process_issues,
            artifact={
                "expected_build_id": artifact_context["build_id"],
                "reader_credential_versions": artifact_context[
                    "reader_credential_versions"
                ],
                "publication": artifact,
            },
        )

    @staticmethod
    def _manages_processes(
        deployment: DeploymentConfig, application: ApplicationConfig
    ) -> bool:
        return application.framework == "laravel" and (
            deployment.workers is not None or deployment.scheduler is not None
        )

    def plan_deployment(self, name: str) -> dict[str, object]:
        return self._release_plan(name)

    def apply_deployment(self, name: str, plan_id: str) -> dict[str, object]:
        with self.deployment_resource_lock(name):
            expected = self._release_plan(name)
            self.assert_plan(expected, plan_id)
            if not expected["ready"]:
                raise ValueError("deployment is not ready; inspect readiness_issues")
            _, deployment, _, application = self.context(name)
            if deployment.release_mode == "artifact":
                artifact_context = self.artifact_deployment.context(name)
                artifact_plan = expected.get("artifact")
                if (
                    not isinstance(artifact_plan, dict)
                    or artifact_context["artifact"] != artifact_plan["publication"]
                    or artifact_context["reader_credential_versions"]
                    != artifact_plan["reader_credential_versions"]
                ):
                    raise ValueError("artifact deployment plan is stale")
                request, secret_context = self.artifact_deployment.apply_arguments(
                    artifact_context
                )
                with secret_context as credential_file:
                    applied = self.run_deployment(
                        "deploy",
                        name,
                        revision=str(expected["revision"]),
                        artifact_request=request,
                        artifact_secret_file=credential_file,
                        timeout=1800,
                    )
            else:
                applied = self.run_deployment(
                    "deploy", name, revision=str(expected["revision"]), timeout=1800
                )
            if self._manages_processes(deployment, application):
                self.run_deployment("gimme:provision:processes", name, timeout=1800)
            return self.result(applied)

    def list_releases(self, name: str) -> dict[str, object]:
        return self.result(self.run_deployment("releases", name))

    def rollback_deployment(self, name: str, confirmation: str) -> dict[str, object]:
        self._require_source_release(self.store.deployment(name))
        expected = f"ROLLBACK {name}"
        if confirmation != expected:
            raise ValueError(f"confirmation must exactly equal '{expected}'")
        with self.deployment_resource_lock(name):
            return self.result(self.run_deployment("rollback", name))

    def plan_promotion(self, source: str, destination: str) -> dict[str, object]:
        state, source_deployment, _, _ = self.context(source)
        destination_deployment = state.deployments[destination]
        self._require_source_release(source_deployment)
        self._require_source_release(destination_deployment)
        if source_deployment.application != destination_deployment.application:
            raise ValueError("promotion requires deployments of the same application")
        current = self.run_deployment("gimme:current-revision", source, timeout=60)
        revision = ""
        for raw in current.output.splitlines():
            line = raw.split("] ", 1)[-1].strip()
            if line.startswith("GIMME_CURRENT_REVISION|"):
                revision = line.split("|", 1)[1]
        if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", revision) is None:
            raise RuntimeError("source deployment has no exact current revision")
        return exact_plan(
            {
                "kind": "promotion",
                "source": source,
                "destination": destination,
                "revision": revision,
                "release": self._release_plan(destination, revision),
            }
        )

    def promote_deployment(
        self, source: str, destination: str, plan_id: str
    ) -> dict[str, object]:
        with self.deployment_resource_locks(source, destination):
            expected = self.plan_promotion(source, destination)
            self.assert_plan(expected, plan_id)
            release = expected["release"]
            if not isinstance(release, dict) or not release["ready"]:
                raise ValueError(
                    "destination deployment is not ready; inspect release readiness_issues"
                )
            revision = str(expected["revision"])
            applied = self.run_deployment(
                "deploy", destination, revision=revision, timeout=1800
            )
            state = self.store.load()
            deployment = state.deployments[destination].model_copy(
                update={"source": DeploymentSource(kind="commit", ref=revision)}
            )
            application = state.applications[deployment.application]
            if self._manages_processes(deployment, application):
                self.run_deployment(
                    "gimme:provision:processes", destination, timeout=1800
                )
            self.store.save(self.replace(state, "deployments", destination, deployment))
            return self.result(applied)
