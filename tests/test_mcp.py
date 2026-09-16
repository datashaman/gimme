from pathlib import Path

from fastmcp import Client
import pytest

from gimme.config import ArtisanConfig, HealthCheckConfig, StackConfig
from gimme.control import (
    ApplicationConfig,
    ControlState,
    DeploymentConfig,
    DeploymentRegistration,
    DeploymentSource,
    Placement,
    StateStore,
    TargetConfig,
    TargetNetwork,
)
from gimme.deployer import CommandResult
import gimme.server as server_module
from gimme.server import mcp


def sample_state() -> ControlState:
    target = TargetConfig(
        host_alias="devbox",
        bootstrap_hostname="192.0.2.10",
        hostname="devbox.local",
        system_hostname="devbox",
        remote_user="deployer",
        apps_root="/srv/gimme/apps",
        network=TargetNetwork(mode="local_mdns", mdns_name="devbox"),
        stack=StackConfig(package_manager="apt", packages=["git"], services=[]),
    )
    application = ApplicationConfig(
        repository="git@github.com:example/example-app.git",
        framework="laravel",
        artisan=ArtisanConfig(allowed_commands=["about", "migrate"]),
        default_health=HealthCheckConfig(path="/up"),
    )
    deployment = DeploymentConfig(
        application="example-app",
        target="devbox",
        stage="local",
        source=DeploymentSource(kind="branch", ref="main"),
        app_env="local",
        placement=Placement(
            instance="example-app",
            relative_path="deployments/example-app",
            database_identifier="gimme_example_app",
            cache_prefix="gimme:example-app:",
            site_host="example-app.devbox.local",
        ),
    )
    return ControlState(
        targets={"devbox": target},
        applications={"example-app": application},
        deployments={"example-app": deployment},
    )


def use_store(tmp_path: Path, monkeypatch) -> StateStore:
    selected = StateStore(tmp_path / "state")
    selected.save(sample_state())
    monkeypatch.setattr(server_module, "store", selected)
    return selected


async def test_hard_v2_tool_surface() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        templates = await client.list_resource_templates()

    names = {tool.name for tool in tools}
    assert "register_environment" not in names
    assert "register_app" not in names
    assert names >= {
        "plan_state_migration",
        "apply_state_migration",
        "register_target",
        "register_application",
        "register_deployment",
        "plan_target_stack",
        "apply_target_stack",
        "plan_deployment_resources",
        "apply_deployment_resources",
        "plan_deployment",
        "apply_deployment",
        "plan_promotion",
        "promote_deployment",
        "plan_artisan",
        "run_artisan",
    }
    assert {str(resource.uri) for resource in resources} == {"gimme://state"}
    assert {template.uriTemplate for template in templates} == {
        "gimme://targets/{name}",
        "gimme://applications/{name}",
        "gimme://deployments/{name}",
    }
    assert all(tool.annotations is not None for tool in tools)


def test_register_deployment_allocates_immutable_placement(tmp_path, monkeypatch) -> None:
    selected = use_store(tmp_path, monkeypatch)
    definition = DeploymentRegistration(
        application="example-app",
        target="devbox",
        stage="preview",
        source=DeploymentSource(kind="branch", ref="feature/demo"),
        app_env="local",
    )
    result = server_module.register_deployment("example-preview", definition)
    placement = selected.deployment("example-preview").placement

    assert result["placement"] == placement.model_dump(mode="json")
    assert placement.relative_path == "deployments/example-preview"
    update = definition.model_copy(update={"source": DeploymentSource(kind="branch", ref="next")})
    plan = server_module.plan_update_deployment("example-preview", update)
    server_module.update_deployment("example-preview", update, str(plan["plan_id"]))
    assert selected.deployment("example-preview").placement == placement


def test_deployment_update_rejects_stale_plan(tmp_path, monkeypatch) -> None:
    use_store(tmp_path, monkeypatch)
    current = server_module.store.deployment("example-app")
    definition = DeploymentRegistration.from_deployment(current).model_copy(
        update={"source": DeploymentSource(kind="branch", ref="next")}
    )
    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.update_deployment("example-app", definition, "plan_" + "0" * 20)


def test_deploy_rechecks_revision_and_rendered_plan(tmp_path, monkeypatch) -> None:
    use_store(tmp_path, monkeypatch)
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_run(task, *args, **kwargs):
        arguments = tuple(kwargs.get("arguments", ()))
        calls.append((task, arguments))
        if task == "gimme:resolve-revision":
            return CommandResult(["dep"], 0, "GIMME_REVISION|" + "a" * 40)
        if arguments == ("--plan",):
            return CommandResult(["dep"], 0, "candidate -> health -> symlink -> live")
        return CommandResult(["dep"], 0, "deployed")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_deployment("example-app")
    result = server_module.apply_deployment("example-app", str(plan["plan_id"]))

    assert result["output"] == "deployed"
    assert calls[-1] == ("deploy", ())


def test_artisan_is_deployment_scoped_and_plan_gated(tmp_path, monkeypatch) -> None:
    use_store(tmp_path, monkeypatch)
    calls: list[dict] = []

    def fake_run(task, *args, **kwargs):
        calls.append({"task": task, **kwargs})
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_artisan("example-app", "migrate", ["--force"])
    server_module.run_artisan("example-app", "migrate", str(plan["plan_id"]), ["--force"])
    assert calls[-1]["artisan_command"] == "migrate"
    assert calls[-1]["artisan_arguments"] == ["--force"]


def test_non_artisan_deployment_does_not_receive_partial_artisan_context(
    tmp_path, monkeypatch
) -> None:
    use_store(tmp_path, monkeypatch)
    captured: dict = {}

    def fake_run(task, *args, **kwargs):
        captured.update(kwargs)
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    server_module._run_deployment("gimme:preflight:frontend", "example-app")
    assert captured["artisan_command"] is None
    assert captured["artisan_arguments"] is None
    assert captured["artisan_allowed_commands"] is None


def test_state_resource_does_not_decrypt_secrets(tmp_path, monkeypatch) -> None:
    use_store(tmp_path, monkeypatch)
    value = server_module.desired_state()
    assert value["schema_version"] == 2
    assert "deployments" in value
