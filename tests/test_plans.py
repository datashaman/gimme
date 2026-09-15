from gimme.config import AppConfig, FrontendBuildConfig, ServerConfig, StackConfig
from gimme.plans import app_resource_plan, stack_plan


def server() -> ServerConfig:
    return ServerConfig(
        host_alias="devbox",
        bootstrap_hostname="192.0.2.10",
        hostname="devbox.local",
        mdns_name="devbox",
        remote_user="deployer",
        apps_root="/srv/gimme/apps",
        keep_releases=5,
    )


def test_stack_plan_is_stable_and_specific() -> None:
    stack = StackConfig(
        package_manager="apt",
        packages=["postgresql", "valkey-server"],
        services=["postgresql", "valkey-server"],
    )
    resolution = {
        "postgresql": {"installed": "missing", "candidate": "18.1"},
        "valkey-server": {"installed": "missing", "candidate": "9.0"},
    }
    first = stack_plan(server(), stack, resolution)
    second = stack_plan(server(), stack, resolution)

    assert first["plan_id"] == second["plan_id"]
    assert first["ready"] is True
    assert first["host"] == "192.0.2.10"
    assert first["managed_hostname"] == "devbox.local"
    assert first["packages"]["postgresql"]["candidate"] == "18.1"


def test_stack_plan_reports_mcp_privilege_readiness() -> None:
    stack = StackConfig(package_manager="apt", packages=["caddy"], services=["caddy"])
    resolution = {"caddy": {"installed": "2.6.2", "candidate": "2.6.2"}}

    before = stack_plan(
        server(), stack, resolution, privileged_helper="bootstrap_required"
    )
    after = stack_plan(server(), stack, resolution, privileged_helper="ready")

    assert before["ready"] is True
    assert before["mcp_apply_ready"] is False
    assert after["mcp_apply_ready"] is True


def test_stack_plan_includes_https_sites_for_registered_apps() -> None:
    stack = StackConfig(package_manager="apt", packages=["caddy"], services=["caddy"])
    apps = {
        "example-app": AppConfig(
            repository="git@example.test:acme/example-app.git", framework="laravel"
        )
    }

    plan = stack_plan(
        server(),
        stack,
        {"caddy": {"installed": "2.6.2", "candidate": "2.6.2"}},
        apps=apps,
    )

    assert plan["sites"]["example-app"] == {
        "url": "https://example-app.devbox.local",
        "document_root": "/srv/gimme/apps/example-app/current/public",
        "tls": "caddy-local-ca",
    }


def test_stack_plan_blocks_unavailable_package() -> None:
    stack = StackConfig(
        package_manager="apt",
        packages=["postgresql"],
        services=["postgresql"],
    )
    plan = stack_plan(
        server(),
        stack,
        {"postgresql": {"installed": "missing", "candidate": "unavailable"}},
    )

    assert plan["ready"] is False
    assert plan["unavailable_packages"] == ["postgresql"]


def test_stack_plan_blocks_active_package_manager() -> None:
    stack = StackConfig(
        package_manager="apt",
        packages=["postgresql"],
        services=["postgresql"],
    )
    plan = stack_plan(
        server(),
        stack,
        {"postgresql": {"installed": "18.1", "candidate": "18.1"}},
        [27708],
    )

    assert plan["ready"] is False
    assert plan["package_manager_processes"] == [27708]


def test_app_resources_use_safe_derived_names() -> None:
    plan = app_resource_plan(
        server(),
        "my-app",
        AppConfig(repository="git@example.test:me/my-app.git", framework="laravel"),
    )

    assert plan["database"] == "gimme_my_app"
    assert plan["database_role"] == "gimme_my_app"
    assert plan["cache"]["prefix"] == "gimme:my-app:"
    assert plan["environment_file"] == "/srv/gimme/apps/my-app/shared/.env"
    assert plan["site_url"] == "https://my-app.devbox.local"


def test_app_plan_changes_when_deployment_definition_changes() -> None:
    first = app_resource_plan(
        server(),
        "my-app",
        AppConfig(repository="git@example.test:me/my-app.git", branch="main"),
    )
    second = app_resource_plan(
        server(),
        "my-app",
        AppConfig(repository="git@example.test:me/my-app.git", branch="develop"),
    )

    assert first["plan_id"] != second["plan_id"]


def test_static_frontend_has_no_backend_resources() -> None:
    plan = app_resource_plan(
        server(),
        "dashboard",
        AppConfig(
            repository="https://example.test/dashboard.git",
            framework="static",
            frontend=FrontendBuildConfig(output_dir="dist"),
        ),
    )

    assert plan["database"] is None
    assert plan["cache"] is None
    assert plan["frontend"]["build_script"] == "build"
