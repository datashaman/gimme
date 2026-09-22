"""Ansible-backed Target stack adapter boundary (ADR 0011).

Non-mutating rendering seam only: the renderer here has no apply implementation, is
not wired into any MCP tool or CLI argument, and never contacts a Target. See the ADR
for the deferred apply, fact-cache, and inventory-transport decisions.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from gimme.config import PACKAGE_NAME, SERVICE_NAME, _valid_endpoint
from gimme.control import DEPLOYMENT_NAME, TARGET_NAME, VERSION, TargetConfig


class TargetStackAdapterError(ValueError):
    """Fixed, non-secret error for invalid or unsupported Target stack adapter input."""

    def __init__(self) -> None:
        super().__init__("target stack adapter input is invalid")


class TargetStackSite(BaseModel):
    """One derived Caddy site fact, mirroring `gimme.control.target_sites` output."""

    model_config = ConfigDict(extra="forbid")

    deployment: str = Field(pattern=DEPLOYMENT_NAME.pattern)
    instance: str
    framework: str
    site_host: str
    document_root: str
    php_fpm_socket: str
    database_identifier: str

    @field_validator("site_host")
    @classmethod
    def safe_site_host(cls, value: str) -> str:
        return _valid_endpoint(value)


class TargetStackRenderInput(BaseModel):
    """Canonical, secret-free Target stack facts.

    Derived only from one validated `TargetConfig` and its currently derived sites,
    network mode, and mise policy (`render_target_stack_input`). Accepts no
    caller-supplied playbook, variable, command, path, package, service, or secret.
    """

    model_config = ConfigDict(extra="forbid")

    target: str = Field(pattern=TARGET_NAME.pattern)
    packages: tuple[str, ...]
    services: tuple[str, ...]
    mise_version: str | None = None
    network_mode: Literal["local_mdns", "public_dns"]
    sites: tuple[TargetStackSite, ...]

    @field_validator("packages", "services")
    @classmethod
    def safe_names(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        pattern = PACKAGE_NAME if info.field_name == "packages" else SERVICE_NAME
        if any(pattern.fullmatch(name) is None for name in value):
            raise ValueError("adapter input contains an unsupported name")
        return value

    @field_validator("mise_version")
    @classmethod
    def exact_mise_version(cls, value: str | None) -> str | None:
        if value is not None and VERSION.fullmatch(value) is None:
            raise ValueError("mise_version must be an exact version")
        return value


def render_target_stack_input(
    name: str, target: TargetConfig, sites: list[dict[str, str]]
) -> TargetStackRenderInput:
    """Pure, deterministic derivation from already-validated Target desired state.

    Never contacts the Target and reads no operational state beyond its arguments.
    """
    ordered_sites = sorted(sites, key=lambda site: site["deployment"])
    try:
        return TargetStackRenderInput(
            target=name,
            packages=tuple(sorted(target.stack.packages)),
            services=tuple(sorted(target.stack.services)),
            mise_version=target.runtimes.mise_version,
            network_mode=target.network.mode,
            sites=tuple(TargetStackSite(**site) for site in ordered_sites),
        )
    except (ValidationError, KeyError):
        # No `from exc`: pydantic's ValidationError echoes the rejected value, and
        # chaining it would leave that value reachable via __cause__/the traceback
        # even though the raised error's own message is fixed.
        raise TargetStackAdapterError() from None


@runtime_checkable
class TargetStackAdapterRenderer(Protocol):
    """Deterministic, non-mutating rendering behavior only."""

    def render(self, render_input: TargetStackRenderInput) -> dict[str, Any]: ...


@runtime_checkable
class TargetStackAdapterApplier(Protocol):
    """Apply behavior. Deliberately unimplemented by the initial Ansible adapter."""

    def apply(self, render_input: TargetStackRenderInput) -> dict[str, Any]: ...


class AnsibleTargetStackRenderer:
    """Local-only Ansible input renderer for the `ubuntu-systemd` Target stack.

    Produces a canonical, secret-free vars/inventory representation equivalent to the
    current Target stack policy. Not selectable by any MCP tool, CLI argument, desired
    state, or environment configuration; has no apply implementation and never
    contacts a Target.
    """

    engine: Literal["ansible"] = "ansible"

    def render(self, render_input: TargetStackRenderInput) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "inventory_hostname": render_input.target,
            "host_vars": {
                "gimme_packages": list(render_input.packages),
                "gimme_services": list(render_input.services),
                "gimme_mise_version": render_input.mise_version,
                "gimme_network_mode": render_input.network_mode,
                "gimme_sites": [site.model_dump(mode="json") for site in render_input.sites],
            },
        }
