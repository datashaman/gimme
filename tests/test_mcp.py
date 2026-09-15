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

    assert len(tools) == 14
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
    }
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.title
        assert tool.annotations.readOnlyHint is not None
        assert tool.annotations.destructiveHint is not None


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
        release_contents = await client.read_resource(
            "gimme://apps/example-app/releases"
        )

    assert {template.uriTemplate for template in templates} == {
        "gimme://apps/{name}",
        "gimme://apps/{name}/releases",
    }
    app = json.loads(app_contents[0].text)
    releases = json.loads(release_contents[0].text)
    assert app_contents[0].mimeType == "application/json"
    assert release_contents[0].mimeType == "application/json"
    assert app["name"] == "example-app"
    assert app["site_url"] == "https://example-app.devbox.local"
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


def test_run_artisan_rejects_stale_plan_before_remote_execution(
    tmp_path, monkeypatch
) -> None:
    _use_test_store(tmp_path, monkeypatch)

    def unexpected_run(*args, **kwargs) -> CommandResult:
        raise AssertionError("remote runner must not execute for a stale plan")

    monkeypatch.setattr("gimme.server.runner.run", unexpected_run)

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.run_artisan("example-app", "about", "plan_invented", [])
