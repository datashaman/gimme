import ast
import json
from pathlib import Path

import pytest

from gimme.config import StackConfig
from gimme.control import TargetConfig, TargetNetwork
from gimme.target_stack_adapter import (
    AnsibleTargetStackRenderer,
    TargetStackAdapterApplier,
    TargetStackAdapterError,
    TargetStackAdapterRenderer,
    render_target_stack_input,
)

ADAPTER_SOURCE = Path("src/gimme/target_stack_adapter.py")


def sample_target() -> TargetConfig:
    return TargetConfig(
        host_alias="web1",
        bootstrap_hostname="192.168.1.10",
        hostname="web1.local",
        system_hostname="web1",
        remote_user="deploy",
        apps_root="/srv/apps",
        deployment_slots=4,
        network=TargetNetwork(mode="local_mdns", mdns_name="web1"),
        stack=StackConfig(
            package_manager="apt",
            packages=["caddy", "avahi-daemon"],
            services=["caddy"],
        ),
    )


def sample_sites() -> list[dict[str, str]]:
    return [
        {
            "deployment": "blog-prod",
            "instance": "prod",
            "framework": "laravel",
            "site_host": "blog.example.com",
            "document_root": "/srv/apps/blog/current/public",
            "php_fpm_socket": "/run/php/php8.3-fpm.sock",
            "database_identifier": "blog_prod",
        }
    ]


def test_render_target_stack_input_is_deterministic() -> None:
    target = sample_target()
    sites = sample_sites()

    first = render_target_stack_input("web1", target, sites)
    second = render_target_stack_input("web1", target, sites)

    assert first == second

    renderer = AnsibleTargetStackRenderer()
    assert renderer.render(first) == renderer.render(second)


def test_render_output_is_secret_free_and_matches_the_fixed_shape() -> None:
    target = sample_target()
    render_input = render_target_stack_input("web1", target, sample_sites())

    output = AnsibleTargetStackRenderer().render(render_input)

    # Round-trips through JSON: plain, secret-free data only.
    assert json.loads(json.dumps(output)) == output
    assert output == {
        "engine": "ansible",
        "inventory_hostname": "web1",
        "host_vars": {
            "gimme_packages": ["avahi-daemon", "caddy"],
            "gimme_services": ["caddy"],
            "gimme_mise_version": None,
            "gimme_network_mode": "local_mdns",
            "gimme_sites": [
                {
                    "deployment": "blog-prod",
                    "instance": "prod",
                    "framework": "laravel",
                    "site_host": "blog.example.com",
                    "document_root": "/srv/apps/blog/current/public",
                    "php_fpm_socket": "/run/php/php8.3-fpm.sock",
                    "database_identifier": "blog_prod",
                }
            ],
        },
    }


@pytest.mark.parametrize(
    "override",
    [
        {"deployment": "bad name!"},
        {"site_host": "not a host"},
    ],
)
def test_render_rejects_invalid_site_values_with_a_fixed_non_secret_error(
    override: dict[str, str],
) -> None:
    target = sample_target()
    sites = [{**sample_sites()[0], **override}]

    with pytest.raises(TargetStackAdapterError) as excinfo:
        render_target_stack_input("web1", target, sites)

    assert str(excinfo.value) == "target stack adapter input is invalid"
    for value in override.values():
        assert value not in str(excinfo.value)


def test_renderer_has_no_apply_implementation() -> None:
    renderer = AnsibleTargetStackRenderer()

    assert not hasattr(renderer, "apply")
    assert isinstance(renderer, TargetStackAdapterRenderer)
    assert not isinstance(renderer, TargetStackAdapterApplier)


def test_adapter_module_never_imports_a_target_transport() -> None:
    tree = ast.parse(ADAPTER_SOURCE.read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    forbidden = {"subprocess", "socket", "paramiko", "fabric", "os"}
    assert not imported & forbidden
    assert "deployer" not in imported
    assert "control_plans" not in imported


def test_adapter_is_not_selectable_by_any_mcp_tool_or_cli_argument() -> None:
    server_source = Path("src/gimme/server.py").read_text().lower()
    cli_source = Path("src/gimme/cli.py").read_text().lower()

    assert "ansible" not in server_source
    assert "ansible" not in cli_source
    assert "target_stack_adapter" not in server_source
    assert "target_stack_adapter" not in cli_source


def test_legacy_target_stack_orchestration_is_unchanged() -> None:
    orchestration_source = Path("src/gimme/target_runtime_orchestration.py").read_text()

    assert "ansible" not in orchestration_source.lower()
    assert "target_stack_adapter" not in orchestration_source
