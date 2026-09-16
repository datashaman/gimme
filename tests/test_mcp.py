import json
from pathlib import Path

from fastmcp import Client
import pytest

from gimme.config import ConfigStore
from gimme.deployer import CommandResult
import gimme.server as server_module
from gimme.server import mcp


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value))


def _use_test_store(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_json(
        config_dir / "server.json",
        {
            "host_alias": "devbox",
            "bootstrap_hostname": "192.0.2.10",
            "hostname": "devbox.local",
            "mdns_name": "devbox",
            "remote_user": "deployer",
            "apps_root": "/srv/gimme/apps",
            "keep_releases": 5,
        },
    )
    _write_json(
        config_dir / "stack.json",
        {"package_manager": "apt", "packages": ["git"], "services": []},
    )
    _write_json(
        config_dir / "apps.json",
        {
            "apps": {
                "example-app": {
                    "branch": "main",
                    "framework": "laravel",
                    "repository": "git@github.com:example/example-app.git",
                }
            }
        },
    )
    monkeypatch.setattr(server_module, "store", ConfigStore(tmp_path))


async def test_tool_surface_and_annotations() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    assert len(tools) == 24
    assert {tool.name for tool in tools} >= {
        "inspect_host",
        "plan_stack",
        "provision_stack",
        "register_app",
        "plan_app_resources",
        "provision_app_resources",
        "plan_deploy",
        "deploy_app",
        "rollback_app",
        "plan_artisan",
        "run_artisan",
        "plan_app_processes",
        "provision_app_processes",
        "app_process_status",
        "configure_app_processes",
        "configure_app_health",
        "list_environments",
        "register_environment",
        "configure_environment_health",
        "plan_remove_environment",
        "remove_environment",
    }
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.title
        assert tool.annotations.readOnlyHint is not None
        assert tool.annotations.destructiveHint is not None

    register = next(tool for tool in tools if tool.name == "register_app")
    worker_schema = register.inputSchema["properties"]["workers"]
    assert "standard queue worker or Horizon" in worker_schema["description"]


async def test_register_and_list_isolated_environment(tmp_path, monkeypatch) -> None:
    _use_test_store(tmp_path, monkeypatch)

    async with Client(mcp) as client:
        registered = await client.call_tool(
            "register_environment",
            {
                "name": "example-app",
                "environment": "feature-x",
                "branch": "feature/worktrees",
            },
        )
        listed = await client.call_tool(
            "list_environments", {"name": "example-app"}
        )

    assert registered.data["site_url"] == (
        "https://feature-x.example-app.devbox.local"
    )
    assert registered.data["deploy_path"] == (
        "/srv/gimme/apps/example-app/environments/feature-x"
    )
    assert listed.data["environments"]["default"]["branch"] == "main"
    assert listed.data["environments"]["feature-x"]["branch"] == (
        "feature/worktrees"
    )
    assert listed.data["environments"]["feature-x"]["workers"] is None


def test_remove_environment_requires_exact_plan_and_unregisters_after_cleanup(
    tmp_path, monkeypatch
) -> None:
    _use_test_store(tmp_path, monkeypatch)
    server_module.register_environment(
        "example-app", "feature-x", "feature/worktrees"
    )
    plan = server_module.plan_remove_environment("example-app", "feature-x")
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_run(task, *args, **kwargs) -> CommandResult:
        calls.append((task, kwargs))
        return CommandResult(["dep", task, "devbox"], 0, "ok")

    monkeypatch.setattr("gimme.server.runner.run", fake_run)
    result = server_module.remove_environment(
        "example-app",
        "feature-x",
        plan["plan_id"],
        "REMOVE example-app/feature-x",
    )

    assert result["removed"] is True
    assert "feature-x" not in server_module.store.app("example-app").environments
    assert [task for task, _kwargs in calls] == [
        "gimme:reconcile:sites",
        "gimme:remove:environment",
    ]
    assert calls[0][1]["exclude_instance"] == "example-app--feature-x"


def test_environment_resource_apply_reconciles_route_before_database(
    tmp_path, monkeypatch
) -> None:
    _use_test_store(tmp_path, monkeypatch)
    server_module.register_environment("example-app", "feature-x", "feature/x")
    plan = server_module.plan_app_resources("example-app", "feature-x")
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_run(task, *args, **kwargs) -> CommandResult:
        calls.append((task, kwargs))
        return CommandResult(["dep", task, "devbox"], 0, "ok")

    monkeypatch.setattr("gimme.server.runner.run", fake_run)
    server_module.provision_app_resources(
        "example-app", plan["plan_id"], "feature-x"
    )

    assert [task for task, _kwargs in calls] == [
        "gimme:reconcile:sites",
        "gimme:provision:app",
    ]
    assert calls[1][1]["environment_name"] == "feature-x"


def test_default_environment_cannot_be_removed(tmp_path, monkeypatch) -> None:
    _use_test_store(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="default environment"):
        server_module.plan_remove_environment("example-app", "default")


async def test_configure_health_through_mcp_is_local_and_preserves_app(
    tmp_path, monkeypatch
) -> None:
    _use_test_store(tmp_path, monkeypatch)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "configure_app_health",
            {
                "name": "example-app",
                "health": {
                    "path": "/up",
                    "expected_status": 200,
                    "attempts": 5,
                    "delay_seconds": 1,
                    "timeout_seconds": 3,
                },
            },
        )

    assert result.data == {
        "application": "example-app",
        "changed": True,
        "health": {
            "path": "/up",
            "expected_status": 200,
            "attempts": 5,
            "delay_seconds": 1,
            "timeout_seconds": 3,
        },
    }
    assert server_module.store.app("example-app").repository == (
        "git@github.com:example/example-app.git"
    )


async def test_read_only_plan_through_mcp() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    plan_stack = next(tool for tool in tools if tool.name == "plan_stack")
    assert plan_stack.annotations.readOnlyHint is True


async def test_plan_artisan_through_mcp(tmp_path, monkeypatch) -> None:
    _use_test_store(tmp_path, monkeypatch)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "plan_artisan",
            {
                "name": "example-app",
                "command": "migrate",
                "arguments": ["--force"],
            },
        )

    assert result.data["kind"] == "artisan_command"
    assert result.data["argv"] == [
        "php",
        "artisan",
        "--no-interaction",
        "migrate",
        "--force",
    ]


async def test_remote_mutations_are_marked_destructive() -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in (
        "provision_stack",
        "provision_app_resources",
        "deploy_app",
        "rollback_app",
        "run_artisan",
        "provision_app_processes",
    ):
        assert tools[name].annotations.destructiveHint is True


async def test_static_resource_catalog_and_contents(tmp_path, monkeypatch) -> None:
    _use_test_store(tmp_path, monkeypatch)
    async with Client(mcp) as client:
        resources = await client.list_resources()
        server_contents = await client.read_resource("gimme://config/server")
        stack_contents = await client.read_resource("gimme://config/stack")
        apps_contents = await client.read_resource("gimme://config/apps")

    assert {str(resource.uri) for resource in resources} == {
        "gimme://config/server",
        "gimme://config/stack",
        "gimme://config/apps",
    }
    assert all(resource.mimeType == "application/json" for resource in resources)
    assert server_contents[0].mimeType == "application/json"
    assert stack_contents[0].mimeType == "application/json"
    assert apps_contents[0].mimeType == "application/json"
    assert json.loads(server_contents[0].text)["hostname"] == "devbox.local"
    assert "packages" in json.loads(stack_contents[0].text)
    assert "example-app" in json.loads(apps_contents[0].text)["apps"]


async def test_application_resource_templates(tmp_path, monkeypatch) -> None:
    _use_test_store(tmp_path, monkeypatch)
    def fake_run(*args, **kwargs) -> CommandResult:
        return CommandResult(["dep", "releases", "devbox"], 0, "release 8 (current)")

    monkeypatch.setattr("gimme.server.runner.run", fake_run)

    async with Client(mcp) as client:
        templates = await client.list_resource_templates()
        app_contents = await client.read_resource("gimme://apps/example-app")
        environments_contents = await client.read_resource(
            "gimme://apps/example-app/environments"
        )
        environment_contents = await client.read_resource(
            "gimme://apps/example-app/environments/default"
        )
        release_contents = await client.read_resource(
            "gimme://apps/example-app/releases"
        )

    assert {template.uriTemplate for template in templates} == {
        "gimme://apps/{name}",
        "gimme://apps/{name}/releases",
        "gimme://apps/{name}/environments",
        "gimme://apps/{name}/environments/{environment}",
        "gimme://apps/{name}/environments/{environment}/releases",
    }
    app = json.loads(app_contents[0].text)
    environments = json.loads(environments_contents[0].text)
    environment = json.loads(environment_contents[0].text)
    releases = json.loads(release_contents[0].text)
    assert app_contents[0].mimeType == "application/json"
    assert release_contents[0].mimeType == "application/json"
    assert app["name"] == "example-app"
    assert app["site_url"] == "https://example-app.devbox.local"
    assert environments["environments"]["default"]["branch"] == "main"
    assert environment["environment"] == "default"
    assert environment["site_url"] == "https://example-app.devbox.local"
    assert releases["application"] == "example-app"
    assert releases["output"] == "release 8 (current)"


def test_artisan_tools_require_an_exact_plan_and_pass_structured_context(
    tmp_path, monkeypatch
) -> None:
    _use_test_store(tmp_path, monkeypatch)
    plan = server_module.plan_artisan("example-app", "migrate", ["--force"])
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs) -> CommandResult:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return CommandResult(["dep", "gimme:artisan", "devbox"], 0, "Migrated")

    monkeypatch.setattr("gimme.server.runner.run", fake_run)

    result = server_module.run_artisan(
        "example-app", "migrate", plan["plan_id"], ["--force"]
    )

    assert result["output"] == "Migrated"
    assert captured["args"][0] == "gimme:artisan"
    assert captured["kwargs"]["artisan_command"] == "migrate"
    assert captured["kwargs"]["artisan_arguments"] == ["--force"]
    assert "migrate" in captured["kwargs"]["artisan_allowed_commands"]


def test_deploy_plan_includes_both_health_gates(tmp_path, monkeypatch) -> None:
    _use_test_store(tmp_path, monkeypatch)
    server_module.configure_app_health(
        "example-app",
        {
            "path": "/up",
            "expected_status": 200,
            "attempts": 5,
            "delay_seconds": 1,
            "timeout_seconds": 3,
        },
    )

    def fake_run(task, *args, **kwargs) -> CommandResult:
        output = (
            "GIMME_REVISION|" + "a" * 40
            if task == "gimme:resolve-revision"
            else "deployment tasks"
        )
        return CommandResult(["dep", task, "devbox"], 0, output)

    monkeypatch.setattr("gimme.server.runner.run", fake_run)

    plan = server_module.plan_deploy("example-app")

    assert plan["kind"] == "application_deploy"
    assert plan["health"]["pre_activation"]["failure"] == (
        "prevent_symlink_switch"
    )
    assert plan["health"]["post_activation"]["failure"] == (
        "rollback_previous_release"
    )
    assert plan["deployer_plan"] == "deployment tasks"


def test_deploy_rejects_plan_after_health_policy_changes(tmp_path, monkeypatch) -> None:
    _use_test_store(tmp_path, monkeypatch)
    calls: list[str] = []

    def fake_run(task, *args, **kwargs) -> CommandResult:
        calls.append(task)
        output = "GIMME_REVISION|" + "a" * 40 if task == "gimme:resolve-revision" else "ok"
        return CommandResult(["dep", task, "devbox"], 0, output)

    monkeypatch.setattr("gimme.server.runner.run", fake_run)
    server_module.configure_app_health("example-app", {"path": "/up"})
    plan = server_module.plan_deploy("example-app")
    server_module.configure_app_health("example-app", {"path": "/health"})

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.deploy_app("example-app", plan["plan_id"])

    assert calls == [
        "gimme:resolve-revision",
        "deploy",
        "gimme:resolve-revision",
        "deploy",
    ]


def test_deploy_rejects_plan_when_deployer_task_graph_changes(
    tmp_path, monkeypatch
) -> None:
    _use_test_store(tmp_path, monkeypatch)
    rendered_plans = iter(["candidate -> symlink -> live", "symlink -> live"])
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_run(task, *args, **kwargs) -> CommandResult:
        arguments = tuple(kwargs.get("arguments", ()))
        calls.append((task, arguments))
        if task == "gimme:resolve-revision":
            return CommandResult(
                ["dep", task, "devbox"], 0, "GIMME_REVISION|" + "a" * 40
            )
        if arguments == ("--plan",):
            return CommandResult(
                ["dep", task, "devbox", "--plan"], 0, next(rendered_plans)
            )
        return CommandResult(["dep", task, "devbox"], 0, "deployed")

    monkeypatch.setattr("gimme.server.runner.run", fake_run)
    server_module.configure_app_health("example-app", {"path": "/up"})
    plan = server_module.plan_deploy("example-app")

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.deploy_app("example-app", plan["plan_id"])

    assert calls == [
        ("gimme:resolve-revision", ()),
        ("deploy", ("--plan",)),
        ("gimme:resolve-revision", ()),
        ("deploy", ("--plan",)),
    ]


def test_deploy_rejects_plan_when_remote_branch_moves(tmp_path, monkeypatch) -> None:
    _use_test_store(tmp_path, monkeypatch)
    revisions = iter(["a" * 40, "b" * 40])
    calls: list[str] = []

    def fake_run(task, *args, **kwargs) -> CommandResult:
        calls.append(task)
        if task == "gimme:resolve-revision":
            return CommandResult(
                ["dep", task, "devbox"], 0, f"GIMME_REVISION|{next(revisions)}"
            )
        if kwargs.get("arguments") == ("--plan",):
            return CommandResult(["dep", task, "devbox"], 0, "same graph")
        raise AssertionError("stale revision must prevent deployment")

    monkeypatch.setattr("gimme.server.runner.run", fake_run)
    plan = server_module.plan_deploy("example-app")

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.deploy_app("example-app", plan["plan_id"])

    assert calls == [
        "gimme:resolve-revision",
        "deploy",
        "gimme:resolve-revision",
        "deploy",
    ]


def test_environment_deploy_uses_exact_revision_and_environment_context(
    tmp_path, monkeypatch
) -> None:
    _use_test_store(tmp_path, monkeypatch)
    server_module.register_environment("example-app", "feature-x", "feature/x")
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_run(task, *args, **kwargs) -> CommandResult:
        calls.append((task, kwargs))
        if task == "gimme:resolve-revision":
            output = "GIMME_REVISION|" + "c" * 40
        elif kwargs.get("arguments") == ("--plan",):
            output = "candidate -> symlink -> live"
        else:
            output = "deployed"
        return CommandResult(["dep", task, "devbox"], 0, output)

    monkeypatch.setattr("gimme.server.runner.run", fake_run)
    plan = server_module.plan_deploy("example-app", "feature-x")
    result = server_module.deploy_app(
        "example-app", plan["plan_id"], "feature-x"
    )

    assert result["output"] == "deployed"
    deploy_call = calls[-1]
    assert deploy_call[0] == "deploy"
    assert deploy_call[1]["environment_name"] == "feature-x"
    assert deploy_call[1]["revision"] == "c" * 40


def test_run_artisan_rejects_stale_plan_before_remote_execution(
    tmp_path, monkeypatch
) -> None:
    _use_test_store(tmp_path, monkeypatch)

    def unexpected_run(*args, **kwargs) -> CommandResult:
        raise AssertionError("remote runner must not execute for a stale plan")

    monkeypatch.setattr("gimme.server.runner.run", unexpected_run)

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.run_artisan("example-app", "about", "plan_invented", [])


def test_horizon_process_plan_and_apply_use_preflight_and_exact_plan(
    tmp_path, monkeypatch
) -> None:
    _use_test_store(tmp_path, monkeypatch)
    _write_json(
        tmp_path / "config" / "apps.json",
        {
            "apps": {
                "example-app": {
                    "branch": "main",
                    "framework": "laravel",
                    "repository": "git@github.com:example/example-app.git",
                    "workers": {"driver": "horizon", "enabled": True},
                    "scheduler": {"enabled": True},
                }
            }
        },
    )
    calls: list[str] = []

    def fake_run(task, *args, **kwargs) -> CommandResult:
        calls.append(task)
        if task == "gimme:preflight:processes":
            return CommandResult(
                ["dep", task, "devbox"],
                0,
                "\n".join(
                    [
                        "[devbox] GIMME_PROCESS_HELPER|ready",
                        "[devbox] GIMME_CURRENT_RELEASE|ready",
                        "[devbox] GIMME_PCNTL|ready",
                        "[devbox] GIMME_POSIX|ready",
                        "[devbox] GIMME_HORIZON|ready",
                    ]
                ),
            )
        return CommandResult(["dep", task, "devbox"], 0, "reconciled")

    monkeypatch.setattr("gimme.server.runner.run", fake_run)

    plan = server_module.plan_app_processes("example-app")
    result = server_module.provision_app_processes("example-app", plan["plan_id"])

    assert plan["ready"] is True
    assert plan["worker"]["driver"] == "horizon"
    assert plan["scheduler"]["timer"] == "gimme-scheduler-example-app.timer"
    assert result["output"] == "reconciled"
    assert calls == [
        "gimme:preflight:processes",
        "gimme:preflight:processes",
        "gimme:provision:processes",
    ]


def test_process_apply_rejects_unready_plan_before_mutation(tmp_path, monkeypatch) -> None:
    _use_test_store(tmp_path, monkeypatch)
    calls: list[str] = []

    def fake_run(task, *args, **kwargs) -> CommandResult:
        calls.append(task)
        return CommandResult(
            ["dep", task, "devbox"],
            0,
            "\n".join(
                [
                    "GIMME_PROCESS_HELPER|bootstrap_required",
                    "GIMME_CURRENT_RELEASE|ready",
                    "GIMME_PCNTL|not_required",
                    "GIMME_POSIX|not_required",
                    "GIMME_HORIZON|not_required",
                ]
            ),
        )

    monkeypatch.setattr("gimme.server.runner.run", fake_run)
    plan = server_module.plan_app_processes("example-app")

    with pytest.raises(ValueError, match="privileged_helper"):
        server_module.provision_app_processes("example-app", plan["plan_id"])

    assert calls == ["gimme:preflight:processes", "gimme:preflight:processes"]
