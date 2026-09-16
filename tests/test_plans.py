import pytest

from gimme.config import (
    AppConfig,
    EnvironmentConfig,
    FrontendBuildConfig,
    HealthCheckConfig,
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
    deployment_plan,
    environment_database_identifier,
    environment_instance,
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

    before = stack_plan(server(), stack, resolution, privileged_helper="bootstrap_required")
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


def test_stack_plan_includes_each_registered_environment_site() -> None:
    stack = StackConfig(package_manager="apt", packages=["caddy"], services=["caddy"])
    apps = {
        "example-app": AppConfig(
            repository="git@example.test:acme/example-app.git",
            framework="laravel",
            environments={
                "default": EnvironmentConfig(branch="main"),
                "feature-x": EnvironmentConfig(branch="feature/x"),
            },
        )
    }

    plan = stack_plan(
        server(),
        stack,
        {"caddy": {"installed": "2.6.2", "candidate": "2.6.2"}},
        apps=apps,
    )

    assert plan["sites"]["example-app/feature-x"] == {
        "url": "https://feature-x.example-app.devbox.local",
        "document_root": ("/srv/gimme/apps/example-app/environments/feature-x/current/public"),
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


def test_additional_environment_has_isolated_paths_url_database_and_cache() -> None:
    app = AppConfig(
        repository="git@example.test:me/my-app.git",
        framework="laravel",
        environments={
            "default": EnvironmentConfig(branch="main"),
            "feature-x": EnvironmentConfig(
                branch="feature/worktrees", app_env="local", app_debug=True
            ),
        },
    )

    plan = app_resource_plan(server(), "my-app", app, "feature-x")

    assert plan["environment"] == "feature-x"
    assert plan["branch"] == "feature/worktrees"
    assert plan["database"] == "gimme_my_app_feature_x_a98b6c775d"
    assert plan["database_role"] == "gimme_my_app_feature_x_a98b6c775d"
    assert plan["cache"]["prefix"] == "gimme:my-app:feature-x:"
    assert plan["environment_file"] == ("/srv/gimme/apps/my-app/environments/feature-x/shared/.env")
    assert plan["site_url"] == "https://feature-x.my-app.devbox.local"
    assert plan["runtime"] == {
        "app_env": "local",
        "app_debug": True,
        "warning": ("APP_DEBUG=true can expose sensitive diagnostics to the local network"),
    }


def test_app_resource_plan_changes_with_runtime_policy() -> None:
    production = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
        environments={"default": EnvironmentConfig(app_env="production", app_debug=False)},
    )
    local = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
        environments={"default": EnvironmentConfig(app_env="local", app_debug=True)},
    )

    production_plan = app_resource_plan(server(), "my-app", production)
    local_plan = app_resource_plan(server(), "my-app", local)

    assert production_plan["plan_id"] != local_plan["plan_id"]
    assert production_plan["runtime"]["warning"] is None
    assert local_plan["runtime"]["warning"].startswith("APP_DEBUG=true")


def test_long_environment_database_identifiers_are_bounded_and_collision_safe() -> None:
    name = "application-" + "a" * 36
    app = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
        environments={
            "default": EnvironmentConfig(),
            "feature-" + "x" * 24: EnvironmentConfig(branch="feature/x"),
            "feature-" + "y" * 24: EnvironmentConfig(branch="feature/y"),
        },
    )

    first = app_resource_plan(server(), name, app, "feature-" + "x" * 24)
    second = app_resource_plan(server(), name, app, "feature-" + "y" * 24)

    assert len(first["database"]) <= 63
    assert len(first["database_role"]) <= 63
    assert first["database"] != second["database"]


def test_internal_environment_identifiers_cannot_alias_other_applications() -> None:
    assert environment_instance("foo--bar", "baz") != environment_instance("foo", "bar--baz")
    assert environment_database_identifier("foo-bar", "default") != environment_database_identifier(
        "foo", "bar"
    )


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


def test_deployment_plan_exposes_candidate_and_live_health_gates() -> None:
    app = AppConfig(
        repository="git@example.test:me/my-app.git",
        framework="laravel",
        branch="stable",
        health=HealthCheckConfig(
            path="/up",
            expected_status=204,
            attempts=5,
            delay_seconds=2,
            timeout_seconds=3,
        ),
    )

    plan = deployment_plan(server(), "my-app", app)

    assert plan["kind"] == "application_deploy"
    assert plan["repository"] == app.repository
    assert plan["branch"] == "stable"
    assert plan["health"]["pre_activation"] == {
        "target": "candidate_release",
        "path": "/up",
        "expected_status": 204,
        "attempts": 5,
        "delay_seconds": 2,
        "timeout_seconds": 3,
        "failure": "prevent_symlink_switch",
    }
    assert plan["health"]["post_activation"] == {
        "target": "https://my-app.devbox.local/up",
        "expected_status": 204,
        "attempts": 5,
        "delay_seconds": 2,
        "timeout_seconds": 3,
        "failure": "rollback_previous_release",
    }

    changed = deployment_plan(
        server(),
        "my-app",
        app.model_copy(update={"health": HealthCheckConfig(path="/health")}),
    )
    assert changed["plan_id"] != plan["plan_id"]


def test_environment_deployment_plan_uses_branch_url_and_health_override() -> None:
    app = AppConfig(
        repository="git@example.test:me/my-app.git",
        framework="laravel",
        health=HealthCheckConfig(path="/up"),
        environments={
            "default": EnvironmentConfig(branch="main"),
            "feature-x": EnvironmentConfig(
                branch="feature/x",
                health=HealthCheckConfig(path="/health", expected_status=204),
            ),
        },
    )

    plan = deployment_plan(server(), "my-app", app, "feature-x", revision="a" * 40)

    assert plan["environment"] == "feature-x"
    assert plan["branch"] == "feature/x"
    assert plan["revision"] == "a" * 40
    assert plan["deploy_path"] == ("/srv/gimme/apps/my-app/environments/feature-x")
    assert plan["site_url"] == "https://feature-x.my-app.devbox.local"
    assert plan["health"]["pre_activation"]["path"] == "/health"
    assert plan["health"]["post_activation"]["target"] == (
        "https://feature-x.my-app.devbox.local/health"
    )


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


def test_environment_processes_use_isolated_units_and_working_directory() -> None:
    app = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
        environments={
            "default": EnvironmentConfig(branch="main"),
            "feature-x": EnvironmentConfig(
                branch="feature/x",
                workers=HorizonWorkerConfig(),
                scheduler=SchedulerConfig(),
            ),
        },
    )

    plan = app_process_plan(
        server(),
        "my-app",
        app,
        "feature-x",
        helper="ready",
        current_release="ready",
        pcntl="ready",
        posix="ready",
        horizon="ready",
    )

    assert plan["working_directory"] == ("/srv/gimme/apps/my-app/environments/feature-x/current")
    assert plan["worker"]["unit"] == ("gimme-horizon-my-app--feature-x--a98b6c775d.service")
    assert plan["scheduler"]["timer"] == ("gimme-scheduler-my-app--feature-x--a98b6c775d.timer")
