import pytest

from gimme.config import (
    AppConfig,
    FrontendBuildConfig,
    HorizonWorkerConfig,
    QueueWorkerConfig,
    SchedulerConfig,
    ServerConfig,
    StackConfig,
)
from gimme.plans import (
    app_process_plan,
    app_resource_plan,
    artisan_command_plan,
    stack_plan,
)


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


def test_artisan_command_plan_is_exact_and_stable() -> None:
    app = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
    )

    first = artisan_command_plan(server(), "my-app", app, "migrate", ["--force"])
    second = artisan_command_plan(server(), "my-app", app, "migrate", ["--force"])

    assert first == second
    assert first["kind"] == "artisan_command"
    assert first["working_directory"] == "/srv/gimme/apps/my-app/current"
    assert first["argv"] == ["php", "artisan", "--no-interaction", "migrate", "--force"]


def test_artisan_command_plan_enforces_framework_and_allowlist() -> None:
    laravel = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
    )
    symfony = AppConfig(
        repository="https://example.test/app.git",
        framework="symfony",
    )

    with pytest.raises(ValueError, match="not allowlisted"):
        artisan_command_plan(server(), "my-app", laravel, "tinker", [])

    with pytest.raises(ValueError, match="Laravel"):
        artisan_command_plan(server(), "my-app", symfony, "about", [])


def test_standard_worker_plan_contains_exact_units_and_argv() -> None:
    app = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
        workers=QueueWorkerConfig(
            processes=2,
            connection="database",
            queues=["high", "default"],
            timeout_seconds=90,
        ),
        scheduler=SchedulerConfig(),
    )

    plan = app_process_plan(
        server(),
        "my-app",
        app,
        helper="ready",
        current_release="ready",
        pcntl="ready",
        posix="not_required",
        horizon="not_required",
    )

    assert plan["ready"] is True
    assert plan["worker"]["driver"] == "queue"
    assert plan["worker"]["units"] == [
        "gimme-worker-my-app@1.service",
        "gimme-worker-my-app@2.service",
    ]
    assert plan["worker"]["argv"][:5] == [
        "/usr/bin/php",
        "artisan",
        "queue:work",
        "database",
        "--queue=high,default",
    ]
    assert plan["scheduler"]["timer"] == "gimme-scheduler-my-app.timer"


def test_horizon_plan_uses_one_master_and_reports_preflight_blockers() -> None:
    app = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
        workers=HorizonWorkerConfig(),
    )

    blocked = app_process_plan(
        server(),
        "my-app",
        app,
        helper="bootstrap_required",
        current_release="ready",
        pcntl="ready",
        posix="ready",
        horizon="missing",
    )
    ready = app_process_plan(
        server(),
        "my-app",
        app,
        helper="ready",
        current_release="ready",
        pcntl="ready",
        posix="ready",
        horizon="ready",
    )

    assert blocked["ready"] is False
    assert blocked["blockers"] == ["privileged_helper", "horizon"]
    assert ready["worker"] == {
        "driver": "horizon",
        "unit": "gimme-horizon-my-app.service",
        "argv": ["/usr/bin/php", "artisan", "horizon"],
        "stop_wait_seconds": 3600,
    }
