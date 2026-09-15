import json
from pathlib import Path

from fastmcp import Client

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

    assert len(tools) == 12
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
