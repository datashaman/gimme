import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


DEPLOYER_SOURCES = (
    ROOT / "deploy" / "configuration.php",
    ROOT / "deploy" / "programs.php",
    ROOT / "deploy" / "state.php",
    ROOT / "deploy.php",
)


def deployer_source() -> str:
    return "\n".join(path.read_text() for path in DEPLOYER_SOURCES)


def test_deployer_recipe_loads_cohesive_support_modules() -> None:
    recipe = (ROOT / "deploy.php").read_text()

    assert "require __DIR__ . '/deploy/configuration.php';" in recipe
    assert "require __DIR__ . '/deploy/programs.php';" in recipe
    assert "require __DIR__ . '/deploy/state.php';" in recipe
    assert "function required_env" not in recipe
    assert "function laravel_environment_reconcile_script" not in recipe
    assert "function process_state_write_command" not in recipe


def laravel_environment_reconciler() -> str:
    recipe = deployer_source()
    return recipe.split("return <<<'PYTHON'", 1)[1].split("\nPYTHON;", 1)[0]


def managed_postgres_bind_script() -> str:
    recipe = deployer_source()
    return recipe.split("function managed_postgres_bind_script", 1)[1].split(
        "return <<<'PYTHON'", 1
    )[1].split("\nPYTHON;", 1)[0]


def rendered_deploy_plan(health: dict[str, object], extra: dict[str, str] | None = None) -> str:
    environment = {
        **os.environ,
        "GIMME_HOSTNAME": "devbox.local",
        "GIMME_BOOTSTRAP_HOSTNAME": "192.0.2.10",
        "GIMME_SSH_HOSTNAME": "devbox.local",
        "GIMME_HOST_ALIAS": "devbox",
        "GIMME_MDNS_NAME": "devbox",
        "GIMME_REMOTE_USER": "deployer",
        "GIMME_APPS_ROOT": "/srv/gimme/apps",
        "GIMME_KEEP_RELEASES": "5",
        "GIMME_APP": "example-app",
        "GIMME_REPOSITORY": "git@example.test:acme/example-app.git",
        "GIMME_FRAMEWORK": "laravel",
        "GIMME_BRANCH": "main",
        "GIMME_WORKERS_JSON": "null",
        "GIMME_SCHEDULER_JSON": "null",
        "GIMME_HEALTH_JSON": json.dumps([health]),
        "GIMME_RUNTIMES_JSON": json.dumps({
            "php": {"provider": "system", "version": "8.4.1"},
            "composer": {"provider": "system", "version": "2.8.4"},
        }),
        "GIMME_RESOURCES_JSON": "{}",
        "GIMME_PHP_EXTENSIONS_JSON": "[]",
        **(extra or {}),
    }
    result = subprocess.run(
        [
            str(ROOT / "vendor" / "bin" / "dep"),
            "--file=deploy.php",
            "deploy",
            "devbox",
            "--no-interaction",
            "--plan",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_deployer_recipe_passes_static_analysis() -> None:
    result = subprocess.run(
        [
            str(ROOT / "vendor" / "bin" / "phpstan"),
            "analyse",
            "deploy.php",
            "deploy",
            "--no-progress",
            "--level=5",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_empty_environment_object_is_valid_but_empty_list_is_not() -> None:
    script = (
        "require 'deploy/configuration.php'; "
        "echo json_encode(Deployer\\configured_environment_values(), JSON_THROW_ON_ERROR);"
    )

    accepted = subprocess.run(
        ["php", "-r", script],
        cwd=ROOT,
        env={**os.environ, "GIMME_VARIABLES_JSON": "{}"},
        text=True,
        capture_output=True,
        check=False,
    )
    rejected = subprocess.run(
        ["php", "-r", script],
        cwd=ROOT,
        env={**os.environ, "GIMME_VARIABLES_JSON": "[]"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert accepted.stdout == "[]"
    assert rejected.returncode != 0
    assert "must be a bounded object" in rejected.stdout + rejected.stderr


def test_stack_includes_deployer_acl_dependency() -> None:
    state = json.loads((ROOT / "config" / "state.example.json").read_text())
    stack = state["targets"]["devbox"]["stack"]

    assert "acl" in stack["packages"]
    assert "avahi-utils" in stack["packages"]


def test_stack_bootstrap_uses_bootstrap_hostname() -> None:
    recipe = deployer_source()

    assert "'GIMME_BOOTSTRAP_HOSTNAME', 'server', 'bootstrap_hostname'" in recipe
    assert "['gimme:preflight:stack', 'gimme:provision:stack']" in recipe
    assert "$arguments = $_SERVER['argv'] ?? [];" in recipe
    assert "($useBootstrapHostname ? $bootstrapHostname : $hostname)" in recipe
    assert "($app !== '' && !preg_match('/^[a-z][a-z0-9-]{0,93}$/', $instance))" in recipe


def test_stack_includes_laravel_php_extensions() -> None:
    state = json.loads((ROOT / "config" / "state.example.json").read_text())
    stack = state["targets"]["devbox"]["stack"]

    assert "php-gd" in stack["packages"]
    assert "python3" in stack["packages"]


def test_apt_install_matches_preflight_and_allows_large_transactions() -> None:
    recipe = deployer_source()

    assert recipe.count("apt-get --simulate --no-install-recommends install") == 1
    assert "apt-get --no-install-recommends install -y" in recipe
    assert "forceOutput: true" in recipe
    assert "timeout: 1800" in recipe


def test_database_bootstrap_exposes_sudo_to_deployer() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:bootstrap:database-admin'", 1)[1].split(
        "task('gimme:provision:app'", 1
    )[0]

    assert 'run("{$sudo} -u postgres bash -c "' in task
    assert "if {$sudo} -u postgres" not in task


def test_host_inspection_reports_application_reachability() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:inspect'", 1)[1].split("task('gimme:preflight:stack'", 1)[0]

    assert "'.mdns_local_resolution='" in task
    assert "'.mdns_client_resolution=not_observable'" in task
    assert "'.alias='" not in task
    assert "getent hosts ' . escapeshellarg($siteHost)" in task
    assert "getent ahostsv4 ' . escapeshellarg($siteHost)" not in task
    assert "'.mdns_publisher='" in task
    assert "site.{$instance}.https_status=" in task
    assert "site.{$instance}.path_permissions=" in task
    assert "site.{$instance}.env_keys=" in task
    assert "site.{$instance}.laravel_log_files=" in task
    assert "site.{$instance}.laravel_errors=" not in task
    assert "site.{$instance}.laravel_log=" not in task
    assert "xargs -0 tail -n 30" not in task
    assert "site.{$instance}.runtime_path_permissions=" in task
    assert "php_fpm_socket=" in task
    assert "toolchain.{$tool}={$version}" in task
    assert "['node', 'npm', 'pnpm', 'yarn', 'bun']" in task
    assert "php_fpm_recent_log=" not in task
    assert "ssh_agent=forwarded" in task
    assert "ssh_agent=missing" in task
    assert "ssh_agent_identities=available" in task
    assert "ssh_agent_identities=empty" in task
    assert "ssh_agent_identities=unreachable" in task
    assert "ssh-add -l >/dev/null 2>&1" in task
    assert "ssh_agent_key=" not in task


def test_app_role_can_become_database_owner() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:provision:app'", 1)[1].split("task('gimme:service:status'", 1)[
        0
    ]

    grant = "GRANT {$role} TO CURRENT_USER WITH SET TRUE, INHERIT FALSE"
    assert grant in task
    assert task.index(grant) < task.index('createdb --owner="{$role}"')


def test_laravel_resources_include_required_application_environment() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:provision:app'", 1)[1].split("task('gimme:service:status'", 1)[
        0
    ]

    assert "APP_KEY=base64:" in task
    assert "GIMME_APP_ENV" in task
    assert "GIMME_APP_DEBUG" in task
    assert "APP_URL=https://{$siteHost}" in task
    assert "HORIZON_PREFIX={$horizonPrefix}" in task
    assert "'HORIZON_PREFIX' => horizon_prefix($cachePrefix)" in task
    assert "laravel_environment_reconcile_script" in task
    assert "GIMME_ENVIRONMENT_CHANGED|yes" in task
    assert "grep -q '^APP_KEY='" in task
    assert "{{bin/php}} artisan optimize:clear" in task
    assert "{{bin/php}} artisan optimize" in task
    assert 'if [ -L "\\$env_path" ]' in task
    assert 'chmod 0600 "\\$env_path"' in task


def test_laravel_runtime_reconciliation_is_atomic_and_process_aware() -> None:
    recipe = deployer_source()
    reconciler = recipe.split("function laravel_environment_reconcile_script", 1)[1].split(
        "function configured_workers", 1
    )[0]
    task = recipe.split("task('gimme:provision:app'", 1)[1].split("task('gimme:service:status'", 1)[
        0
    ]

    assert "tempfile.mkstemp" in reconciler
    assert "os.fsync" in reconciler
    assert "os.replace" in reconciler
    assert "path.is_symlink()" in reconciler
    assert ".gimme-secret-manifest.json" in reconciler
    assert ".gimme-env-backup-" in task
    assert ".gimme-manifest-backup-" in task
    assert "assert_laravel_configuration_health" in task
    assert "the prior protected state was restored" in task
    assert "process.units_changed=yes" in task
    assert "invoke('gimme:restart:workers')" in task


def test_runtime_reconciliation_upgrades_process_state_before_using_helper() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:provision:app'", 1)[1].split(
        "task('gimme:service:status'", 1
    )[0]

    state_write = task.index("process_state_write_command(")
    helper_call = task.index("sudo -n /usr/local/sbin/gimme-provision-processes")
    assert state_write < helper_call


def test_inspection_uses_content_bound_helper_readiness() -> None:
    recipe = deployer_source()
    inspect = recipe.split("task('gimme:inspect'", 1)[1].split(
        "task('gimme:inspect:runtimes'", 1
    )[0]

    assert "privileged_helper_policy(" in inspect
    assert "grep -Fqx {$policyLine} /usr/local/sbin/gimme-provision-stack" in inspect
    assert "grep -Fqx {$policyLine} /usr/local/sbin/gimme-provision-processes" in inspect
    assert "grep -Fqx {$policyLine} /usr/local/sbin/gimme-postgres-restore-swap" in inspect
    assert "sudo -n -l /usr/local/sbin/gimme-postgres-restore-swap swap probe probe" in inspect


def test_recovery_schedule_status_queries_only_the_derived_timer() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:recovery:schedule-status'", 1)[1].split(
        "task('gimme:restart:workers'", 1
    )[0]

    assert "required_env('GIMME_DEPLOYMENT')" in task
    assert '"gimme-recovery-{$deployment}.timer"' in task
    assert "systemctl show --no-pager" in task
    assert "--property=LoadState --value" in task
    assert "systemctl is-enabled --quiet" in task
    assert "systemctl is-active --quiet" in task
    assert "GIMME_RECOVERY_TIMER|" in task


def test_recovery_schedule_reconciliation_uses_fixed_protected_transfer() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:recovery:schedule-reconcile'", 1)[1].split(
        "task('gimme:restart:workers'", 1
    )[0]

    assert "recovery_schedule_state_write_command(" in task
    assert "invoke('gimme:recovery:runtime-status')" in task
    assert '"{$appsRoot}/.gimme/recovery-schedules"' in task
    assert '"{$directory}/{$deployment}.json"' in task
    assert '"{$directory}/{$deployment}.credentials"' in task
    assert '"{$directory}/{$deployment}.valkey-credentials"' in task
    assert "upload($localCredential, $remoteCredential)" in task
    assert "upload($localValkeyCredential, $remoteValkeyCredential)" in task
    assert "chmod 0600" in task
    assert "sudo -n /usr/local/sbin/gimme-provision-recovery-schedule" in task
    assert "finally" in task
    assert "rm -f" in task
    assert "GIMME_RECOVERY_SCHEDULE_VALKEY_FILE" in task
    assert task.index("invoke('gimme:recovery:runtime-status')") < task.index(
        "recovery_schedule_state_write_command("
    ) < task.index("try {")
    assert task.index("try {") < task.index("upload($localCredential") < task.index(
        "sudo -n /usr/local/sbin/gimme-provision-recovery-schedule"
    ) < task.index("finally")


def test_recovery_schedule_runtime_probe_is_fixed_and_bounded() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:recovery:runtime-status'", 1)[1].split(
        "task('gimme:recovery:schedule-reconcile'", 1
    )[0]

    assert "import boto3; print(boto3.__version__" in task
    assert "/usr/bin/python3 -c" in task
    assert "GIMME_RECOVERY_RUNTIME|boto3|" in task
    assert "^[0-9]+(?:\\.[0-9]+){1,3}$" in task
    assert "GIMME_RECOVERY_SCHEDULE_JSON" not in task


def test_recovery_schedule_status_reads_only_the_fixed_runner_marker() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:recovery:schedule-status'", 1)[1].split(
        "task('gimme:recovery:runtime-status'", 1
    )[0]

    assert "/usr/local/libexec/gimme-recovery-runner status" in task
    assert "GIMME_RECOVERY_STATUS" in task
    assert "[A-Za-z0-9+\\/=]{1,24576}" in task


def test_on_demand_recovery_uses_isolated_fixed_runner_transfer() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:recovery:on-demand'", 1)[1].split(
        "task('gimme:restart:workers'", 1
    )[0]

    assert "required_env('GIMME_RECOVERY_ON_DEMAND_REQUEST_ID')" in task
    assert "recovery-on-demand/{$deployment}/{$request}" in task
    assert "recovery_schedule_state_write_command(" in task
    assert "/usr/local/libexec/gimme-recovery-runner on-demand" in task
    assert "CREDENTIALS_DIRECTORY=" in task
    assert "STATE_DIRECTORY=" in task
    assert "GIMME_RECOVERY_RESULT" in task
    assert "[A-Za-z0-9+\\/=]{1,24576}" in task
    assert "upload($local, $remote)" in task
    assert "chmod 0600" in task
    assert "finally" in task
    assert "rm -f" in task
    assert "rmdir" in task


def test_recovery_schedule_state_writer_is_bounded_and_secret_free() -> None:
    state = (ROOT / "deploy/state.php").read_text().split(
        "function recovery_schedule_state_write_command", 1
    )[1].split("function privileged_helper_source_hashes", 1)[0]

    assert "GIMME_RECOVERY_SCHEDULE_JSON" in state
    assert "strlen($raw) > 65536" in state
    assert "($state['deployment'] ?? null) !== $deployment" in state
    assert "GIMME_SECRET_FILE" not in state
    assert "credentials" not in state
    assert "chmod 0600" in state


def test_laravel_runtime_reconciler_preserves_secrets_and_is_idempotent(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "APP_KEY=secret-value\nAPP_ENV=production\nAPP_DEBUG=false\n"
        "HORIZON_PREFIX=old:\nDB_PASSWORD=another-secret\n"
    )
    updates = base64.b64encode(
        json.dumps(
            {
                "APP_ENV": "local",
                "APP_DEBUG": "true",
                "HORIZON_PREFIX": "gimme:example:preview:horizon:",
            }
        ).encode()
    ).decode()

    changed = subprocess.run(
        ["python3", "-c", laravel_environment_reconciler(), str(env_path), updates],
        text=True,
        capture_output=True,
        check=True,
    )
    first_inode = env_path.stat().st_ino
    unchanged = subprocess.run(
        ["python3", "-c", laravel_environment_reconciler(), str(env_path), updates],
        text=True,
        capture_output=True,
        check=True,
    )

    content = env_path.read_text()
    assert changed.stdout.splitlines() == [
        "GIMME_RUNTIME_CHANGED|yes", "GIMME_ENVIRONMENT_CHANGED|yes",
    ]
    assert unchanged.stdout.splitlines() == [
        "GIMME_RUNTIME_CHANGED|no", "GIMME_ENVIRONMENT_CHANGED|no",
    ]
    assert env_path.stat().st_ino == first_inode
    assert env_path.stat().st_mode & 0o777 == 0o600
    assert "APP_KEY=secret-value" in content
    assert "DB_PASSWORD=another-secret" in content
    assert "APP_ENV=local" in content
    assert "APP_DEBUG=true" in content
    assert "secret-value" not in changed.stdout


def test_laravel_secret_reconciliation_marks_the_environment_changed(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("APP_ENV=production\nAPI_TOKEN=old-value\n")
    secret_path = tmp_path / "secrets.json"
    secret_path.write_text(json.dumps({"API_TOKEN": "rotated-value"}))
    updates = base64.b64encode(b'{}').decode()
    manifest = base64.b64encode(b'[]').decode()

    result = subprocess.run(
        [
            "python3", "-c", laravel_environment_reconciler(), str(env_path), updates,
            str(secret_path), manifest,
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    assert "GIMME_ENVIRONMENT_CHANGED|yes" in result.stdout
    assert "GIMME_RUNTIME_CHANGED|no" in result.stdout
    assert "API_TOKEN=rotated-value" in env_path.read_text()


def test_secret_environment_changes_restart_managed_workers() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:provision:app'", 1)[1].split(
        "task('gimme:service:status'", 1
    )[0]

    assert "GIMME_ENVIRONMENT_CHANGED|yes" in task
    assert "$environmentChanged && !$unitsChanged" in task
    assert "invoke('gimme:restart:workers')" in task


def test_laravel_runtime_reconciler_rejects_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("APP_ENV=production\n")
    env_path = tmp_path / ".env"
    env_path.symlink_to(target)
    updates = base64.b64encode(b'{"APP_ENV":"local","APP_DEBUG":"false"}').decode()

    result = subprocess.run(
        ["python3", "-c", laravel_environment_reconciler(), str(env_path), updates],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "non-regular environment file" in result.stderr
    assert target.read_text() == "APP_ENV=production\n"


def test_php_recipe_uses_the_collision_safe_environment_instance_identity() -> None:
    recipe = deployer_source()
    configured_sites = recipe.split("function configured_sites", 1)[1].split(
        "function stack_state_write_command", 1
    )[0]

    assert "hash('sha256', \"{$name}\\0{$environment}\")" in configured_sites
    assert '"{$name}--{$environment}--"' in configured_sites


def test_app_resources_allow_php_fpm_to_traverse_shared_directory() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:provision:app'", 1)[1].split("task('gimme:service:status'", 1)[
        0
    ]

    assert "setfacl -m u:www-data:x" in task
    assert "install -d -m 0700" in task


def test_stack_provisions_https_sites_and_mdns_aliases() -> None:
    recipe = deployer_source()
    helper = (ROOT / "scripts" / "gimme-provision-stack").read_text()
    task = recipe.split("task('gimme:provision:stack'", 1)[1].split(
        "task('gimme:bootstrap:database-admin'", 1
    )[0]

    assert "tls internal" in helper
    assert "php_fastcgi unix/{php_fpm_socket}" in helper
    assert "resolve_root_symlink" in helper
    assert "/etc/caddy/gimme" in helper
    assert '"caddy", "validate"' in helper
    assert "avahi-publish" in helper
    assert "avahi-publish -a -R" in helper
    assert "/etc/systemd/system/gimme-mdns-" in helper
    assert '"u:caddy:rx,u:www-data:rx"' in helper
    assert ".caddy-local-root.crt" in helper
    assert "| {$sudo} tee" not in task
    assert 'getenv("GIMME_INTERACTIVE_SUDO")' in task
    assert "tempnam(sys_get_temp_dir(), 'gimme-bootstrap-')" in task
    assert "mktemp /tmp/.gimme-bootstrap.XXXXXX" in task
    assert "upload($localBootstrap, $remoteBootstrap)" in task
    assert '"{$sudo} bash " . escapeshellarg($remoteBootstrap)' in task
    assert '"{$sudo} bash -c %bootstrap%"' not in task
    assert "@unlink($localBootstrap)" in task
    assert "rm -f " in task
    assert "sudo -n /usr/local/sbin/gimme-provision-stack" in task
    assert "/usr/local/sbin/gimme-postgres-restore-swap *" in task
    assert r'mv "\$postgres_swap_helper_tmp" /usr/local/sbin/gimme-postgres-restore-swap' in task


def test_environment_removal_is_bounded_to_non_default_environment_root() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:remove:environment'", 1)[1].split("if ($health === null)", 1)[
        0
    ]

    assert "$environmentName === 'default'" in task
    assert '"{$appsRoot}/{$app}/environments/{$environmentName}"' in task
    assert "get('deploy_path') !== $expectedPath" in task
    assert "Refusing to remove a symlinked environment root" in task
    assert "Refusing to remove an environment through a symlinked parent" in task
    assert 'rm -rf -- "\\$path"' in task
    assert "dropdb --if-exists --force" in task
    assert 'PGOPTIONS=' in task
    assert '"-c role={$database}"' in task
    assert 'redis.call("SCAN"' in task


def test_deployment_removal_assumes_only_its_derived_database_owner() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:remove:deployment'", 1)[1].split(
        "task('gimme:service:status'", 1
    )[0]

    assert "dropdb --if-exists --force" in task
    assert 'PGOPTIONS=' in task
    assert '"-c role={$database}"' in task
    assert "sudo -u postgres" not in task


def test_frontend_build_runs_after_composer_dependencies() -> None:
    recipe = deployer_source()

    assert "after('deploy:update_code', 'gimme:frontend:install')" in recipe
    assert "after('deploy:vendors', 'gimme:frontend:build')" in recipe
    assert "after('deploy:update_code', 'gimme:frontend')" not in recipe


def test_artifact_mode_replaces_source_and_dependency_tasks() -> None:
    recipe = deployer_source()

    assert "$hasFrontend = $releaseMode === 'source'" in recipe
    assert "task('deploy:update_code', static function (): void" in recipe
    assert "invoke('gimme:artifact:run')" in recipe
    assert "task('deploy:vendors', static function (): void" in recipe
    assert "task('gimme:preflight:artifact-runtimes'" in recipe
    artifact_preflight = recipe.split(
        "task('gimme:preflight:artifact-runtimes'", 1
    )[1].split("task('gimme:preflight:frontend'", 1)[0]
    assert "composer" not in artifact_preflight
    assert "node" not in artifact_preflight
    assert "packageManager" not in artifact_preflight
    assert "GIMME_ARTIFACT_SECRET_FILE" in recipe
    assert "{{release_path}}" in recipe
    assert "get('deploy_path') . '/current'" in recipe
    assert "GIMME_PLATFORM|" in artifact_preflight


def test_artifact_plan_preserves_health_activation_process_and_cleanup_order() -> None:
    plan = rendered_deploy_plan(
        {
            "name": "primary",
            "phases": ["candidate", "live"],
            "path": "/up",
            "expected_status": 200,
            "attempts": 1,
            "delay_seconds": 0,
            "timeout_seconds": 3,
        },
        {"GIMME_RELEASE_MODE": "artifact"},
    )

    ordered = [
        "deploy:update_code",
        "deploy:env",
        "deploy:shared",
        "artisan:optimize",
        "artisan:migrate",
        "gimme:health:candidate",
        "deploy:symlink",
        "gimme:health:live",
        "gimme:restart:workers",
        "deploy:cleanup",
    ]
    assert [plan.index(task) for task in ordered] == sorted(
        plan.index(task) for task in ordered
    )


def test_reviewed_rollback_uses_retained_release_health_and_restoration_only() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:rollback'", 1)[1].split(
        "task('gimme:preflight:stack'", 1
    )[0]

    assert task.index("artisan:optimize") < task.index("gimme:health:candidate")
    assert task.index("gimme:health:candidate") < task.index("{{bin/symlink}}")
    assert task.index("{{bin/symlink}}") < task.index("gimme:health:live")
    assert task.index("gimme:health:live") < task.index("gimme:restart:workers")
    assert "set('rollback_candidate', $current)" in task
    assert "if ($observed === $candidate)" in task
    assert "git " not in task
    assert "composer" not in task
    assert "invoke('gimme:artifact:run')" in task
    assert "materialize" not in task
    assert "deploy:update_code" not in task


def test_artisan_task_runs_only_allowlisted_escaped_arguments_in_current_release() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:artisan'", 1)[1].split("task('gimme:service:status'", 1)[0]

    assert "Application context is required" in task
    assert "Laravel application" in task
    assert "Artisan command is not allowlisted" in task
    assert '"{$currentPath}/artisan"' in task
    assert "array_map('escapeshellarg', $arguments)" in task
    assert "--no-interaction" in task
    assert "GIMME_ARTISAN_ARGS_JSON" in recipe
    assert "GIMME_ARTISAN_ALLOWED_JSON" in recipe


def test_process_tasks_are_planned_reconciled_and_restarted_safely() -> None:
    recipe = deployer_source()

    assert "task('gimme:preflight:processes'" in recipe
    assert "task('gimme:provision:processes'" in recipe
    assert "task('gimme:processes:status'" in recipe
    assert "LoadState,LoadError,ActiveState,SubState,MainPID,NRestarts" in recipe
    assert "/usr/bin/systemd-analyze verify" in recipe
    assert "LoadState=bad-setting" in recipe
    assert "GIMME_PROCESS_HELPER|" in recipe
    assert "GIMME_CURRENT_RELEASE|" in recipe
    assert "GIMME_PCNTL|" in recipe
    assert "GIMME_POSIX|" in recipe
    assert "GIMME_HORIZON|" in recipe
    assert "sudo -n /usr/local/sbin/gimme-provision-processes" in recipe
    assert "'queue' => 'queue:restart'" in recipe
    assert "'horizon' => 'horizon:terminate'" in recipe
    assert "after('deploy:symlink', 'gimme:restart:workers')" in recipe
    assert "after('rollback', 'gimme:restart:workers')" in recipe
    assert "QUEUE_CONNECTION=redis" in recipe
    assert "artisan --no-interaction config:clear" in recipe


def test_runtime_inspection_emits_remote_observations_to_the_control_plane() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:inspect:runtimes'", 1)[1].split(
        "task('gimme:preflight:stack'", 1
    )[0]

    assert "GIMME_RUNTIME|%s|%s" in task
    assert "writeln(run('bash -c '" in task


def test_mise_bootstrap_uses_only_the_fixed_official_ubuntu_ppa() -> None:
    recipe = deployer_source()
    helper = (ROOT / "scripts" / "gimme-provision-stack").read_text()

    assert "add-apt-repository -y ppa:jdxcode/mise" in recipe
    assert '["add-apt-repository", "-y", "ppa:jdxcode/mise"]' in helper
    assert '"mise": "/usr/bin/mise"' in helper
    assert "installed mise version does not match desired state" in helper


def test_target_bootstrap_streams_bounded_phase_progress() -> None:
    recipe = (ROOT / "deploy.php").read_text()
    bootstrap = recipe.split("$bootstrap = <<<BASH", 1)[1].split("\nBASH;", 1)[0]

    stages = [
        "preflight", "packages", "mise", "state", "helpers", "policy", "reconcile", "complete"
    ]
    assert all(f"GIMME_BOOTSTRAP|{stage}|" in bootstrap for stage in stages)


def test_target_bootstrap_installs_content_bound_recovery_runtime() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:provision:stack'", 1)[1].split(
        "task('gimme:bootstrap:database-admin'", 1
    )[0]
    state = (ROOT / "deploy/state.php").read_text()

    for source in (
        "scripts/gimme-recovery-runner", "src/gimme/target_capture.py",
        "scripts/gimme-capture-valkey",
    ):
        assert source in state
    assert "__GIMME_RUNNER_SHA256__" in task
    assert "hash('sha256', $recoveryRunner)" in task
    assert "hash('sha256', $targetCapture)" in task
    assert "hash('sha256', $valkeyCapture)" in task
    assert r'mv "\$recovery_runner_tmp" /usr/local/libexec/gimme-recovery-runner' in task
    assert r'mv "\$target_capture_tmp" /usr/local/libexec/gimme_target_capture.py' in task
    assert r'mv "\$valkey_capture_tmp" /usr/local/libexec/gimme-capture-valkey' in task


def test_deployment_health_gates_candidate_before_live_activation() -> None:
    plan = rendered_deploy_plan(
        {
            "name": "primary",
            "phases": ["candidate", "live"],
            "path": "/up",
            "expected_status": 200,
            "attempts": 5,
            "delay_seconds": 1,
            "timeout_seconds": 3,
        }
    )

    assert plan.index("gimme:health:candidate") < plan.index("deploy:symlink")
    assert plan.index("deploy:symlink") < plan.index("gimme:health:live")
    assert plan.index("gimme:health:live") < plan.index("gimme:restart:workers")


VALKEY_PROBE = json.dumps({"host": "cache.example.internal", "port": 6379})
CANDIDATE_HEALTH = {
    "name": "primary", "phases": ["candidate"], "path": "/up", "expected_status": 200,
    "attempts": 1, "delay_seconds": 0, "timeout_seconds": 3,
}


def test_valkey_probe_gates_the_switch_before_the_candidate_health_check() -> None:
    plan = rendered_deploy_plan(CANDIDATE_HEALTH, {"GIMME_VALKEY_PROBE_JSON": VALKEY_PROBE})

    assert plan.index("gimme:probe:valkey") < plan.index("gimme:health:candidate")
    assert plan.index("gimme:probe:valkey") < plan.index("deploy:symlink")


def test_no_valkey_probe_runs_without_a_managed_binding() -> None:
    assert "gimme:probe:valkey" not in rendered_deploy_plan(CANDIDATE_HEALTH)


def test_valkey_probe_reads_only_fixed_paths_and_reports_the_current_release_stays_live() -> None:
    recipe = (ROOT / "deploy.php").read_text()
    task = recipe.split("function run_valkey_probe", 1)[1].split(
        "if ($health !== []) {", 1
    )[0]

    assert "get('deploy_path') . '/shared/.env'" in task
    assert "run_valkey_probe('{{release_path}}/composer.lock')" in task
    assert "the current release stays live" in task
    assert "ssl" not in task and "cafile" not in task.lower()


def test_current_release_probe_reads_only_the_live_lock_and_skips_a_never_deployed_one() -> None:
    recipe = (ROOT / "deploy.php").read_text()
    task = recipe.split("task('gimme:probe:valkey:current'", 1)[1].split("});", 1)[0]

    assert "get('deploy_path') . '/current/composer.lock'" in task
    assert "valkey_probe=skipped_no_release" in task
    assert "run_valkey_probe($lock)" in task
    assert "release_path" not in task


def test_horizon_prefix_follows_a_managed_valkey_contract_only() -> None:
    script = (
        "namespace Deployer; require 'deploy/configuration.php';"
        "putenv('GIMME_VARIABLES_JSON=' . $argv[1]);"
        "echo horizon_prefix('gimme:shop:');"
    )

    def prefix(variables: dict[str, str]) -> str:
        return subprocess.run(
            ["php", "-r", script, json.dumps(variables)],
            cwd=ROOT, text=True, capture_output=True, check=True,
        ).stdout

    assert prefix({}) == "gimme:shop:horizon:"
    assert prefix({"HORIZON_PREFIX": "{other}:"}) == "gimme:shop:horizon:"
    assert prefix({
        "GIMME_VALKEY_CONTRACT": "laravel-cluster-v1", "HORIZON_PREFIX": "{gimme:shop}:horizon:"
    }) == "{gimme:shop}:horizon:"


def test_health_gates_apply_each_probe_only_at_declared_phases() -> None:
    recipe = (ROOT / "deploy.php").read_text()
    health = recipe.split("if ($health !== []) {", 1)[1].split(
        "task('gimme:inspect'", 1
    )[0]

    assert "in_array('candidate', $probe['phases'], true)" in health
    assert "in_array('live', $probe['phases'], true)" in health
    assert 'health.candidate.{$probe[\'name\']}' in health
    assert 'health.live.{$probe[\'name\']}' in health


def test_health_probes_are_local_bounded_and_do_not_return_response_bodies() -> None:
    recipe = deployer_source()
    health = recipe.split("function laravel_candidate_health_script", 1)[1].split(
        "task('gimme:inspect'", 1
    )[0]

    assert "/usr/bin/timeout --signal=TERM" in health
    assert "CURLOPT_CAINFO" in health
    assert "CURLOPT_FOLLOWLOCATION => false" in health
    assert 'CURLOPT_RESOLVE => ["{$host}:443:127.0.0.1"]' in health
    assert "CURLOPT_WRITEFUNCTION" in health
    assert "GIMME_HEALTH_STATUS|exception" in health
    assert "invoke('rollback')" in health
    assert "response->getContent" not in health


def test_privileged_helper_is_narrowly_allowlisted() -> None:
    recipe = deployer_source()
    helper = (ROOT / "scripts" / "gimme-provision-stack").read_text()

    assert "NOPASSWD: /usr/local/sbin/gimme-provision-stack" in recipe
    assert "NOPASSWD: ALL" not in recipe
    assert "SUDO_USER" in helper
    process_helper = (ROOT / "scripts" / "gimme-provision-processes").read_text()
    schedule_helper = (ROOT / "scripts" / "gimme-provision-recovery-schedule").read_text()
    recovery_helper = (ROOT / "scripts" / "gimme-recovery-maintenance").read_text()

    assert "len(sys.argv) != 1" in helper
    assert "len(sys.argv) != 2" in process_helper
    assert "len(sys.argv) != 2" in schedule_helper
    assert "len(sys.argv) != 4" in recovery_helper
    assert "ALLOWED_PACKAGES" in helper
    assert "ALLOWED_SERVICES" in helper
    assert "EXPECTED_HOSTNAME" in helper
    assert "GIMME_POLICY_ID" in helper
    assert "'helper_source_sha256' => privileged_helper_source_hashes()" in recipe
    assert "NOPASSWD: /usr/local/sbin/gimme-provision-processes" in recipe
    assert "NOPASSWD: /usr/local/sbin/gimme-provision-recovery-schedule *" in recipe
    assert "NOPASSWD: /usr/local/sbin/gimme-recovery-maintenance *" in recipe
    assert "maintenance is owned by another request" in recovery_helper
    assert 'action not in {"enter", "resume", "quiesce", "exit"}' in recovery_helper
    assert "shell_exec" not in helper
    assert "GIMME_HELPER|" in recipe
    assert "chown {$user}:{$user}" in recipe
    assert recipe.index('visudo -cf "\\$sudoers_tmp"') < recipe.index(
        'mv "\\$sudoers_tmp" /etc/sudoers.d/gimme-provision-stack'
    )


def test_managed_postgres_bind_uses_no_sudo_and_shreds_its_secret_file() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:resource:bind-postgres'", 1)[1].split(
        "task('gimme:backup:dump-postgres'", 1
    )[0]

    assert "sudo" not in task
    assert "valid_endpoint($host)" in task
    assert "is_link($localSecretFile)" in task
    assert (
        "rm -f ' . escapeshellarg($remoteSecretFile) . ' ' . escapeshellarg($remoteBundleFile)"
        in task
    )
    assert task.index("try {") < task.index("upload($localSecretFile") < task.index(
        "} finally {"
    ), "uploads must sit inside the try so a failed upload still cleans up"
    assert "upload(__DIR__ . '/deploy/aws-rds-global-bundle.pem', $remoteBundleFile)" in task
    assert "managed_postgres_bind_script" in task


def test_valkey_recovery_capture_is_binary_safe_prefix_bounded_and_non_global() -> None:
    recipe = deployer_source()
    program = (ROOT / "scripts" / "gimme-capture-valkey").read_text()
    task = recipe.split("task('gimme:backup:capture-valkey'", 1)[1].split(
        "task('gimme:provision:app'", 1
    )[0]

    assert 'session.call("SCAN", cursor, "MATCH", prefix + b"*", "COUNT", 1000)' in program
    assert "redis.call('DUMP',KEYS[1])" in program
    assert "redis.call('PEXPIRETIME',KEYS[1])" in program
    assert "base64.b64encode(result[0])" in program
    assert "key.startswith(prefix)" in program
    assert "GIMME_VALKEY_BACKUP|" in program
    assert 'session.call("EVAL", script, 1, key)' in program
    assert 'redis.call("SCAN"' not in program
    prohibited = (
        "KEYS", "SAVE", "BGSAVE", "FLUSHALL", "FLUSHDB", "SHUTDOWN",
        "CONFIG", "SCRIPT",
    )
    assert all(
        re.search(rf"\.call\(\s*['\"]{command}['\"]", program) is None
        for command in prohibited
    )
    assert "download($remotePath, $localPath)" in task
    assert "rm -f " in task


def test_postgres_restore_preflight_uses_only_fixed_catalog_inspection() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:recovery:inspect-postgres'", 1)[1].split(
        "task('gimme:recovery:maintenance'", 1
    )[0]

    assert "pg_class" in task
    assert "pg_proc" in task
    assert "pg_type" in task
    assert "pg_extension" in task
    assert "GIMME_POSTGRES_RESTORE_PREFLIGHT|$result" in task
    assert "required_env('GIMME_RESTORE_SOURCE_BYTES')" in task
    assert "gimme-postgres-restore-swap capacity" in task
    assert "GIMME_POSTGRES_RESTORE_CAPACITY|ready" in task
    assert "GIMME_POSTGRES_RESTORE_CAPACITY|insufficient" in task
    assert 'printf "%s\\\\n"' in task
    assert "writeln($output)" in task
    assert "DROP " not in task
    assert "ALTER " not in task


def test_postgres_restore_task_uses_request_scoped_protected_atomic_state() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:recovery:postgres'", 1)[1].split(
        "task('gimme:recovery:maintenance'", 1
    )[0]

    assert "required_env('GIMME_POSTGRES_RESTORE_REQUEST_ID')" in task
    assert "install -d -m 0700" in task
    assert "umask 077; printf %s" in task
    assert "&& mv " in task
    assert "chmod 0600" in task
    assert "upload($localPath, $artifactPath)" in task
    assert "python3 - " in task
    assert "escapeshellarg($database)" in task
    assert "escapeshellarg($sha256)" in task
    assert "escapeshellarg($bytes)" in task


def test_valkey_restore_task_is_request_scoped_prefix_bounded_and_cleans_up() -> None:
    recipe = deployer_source()
    program = (ROOT / "scripts" / "gimme-restore-valkey").read_text()
    task = recipe.split("task('gimme:recovery:valkey'", 1)[1].split(
        "task('gimme:recovery:verify-application'", 1
    )[0]

    assert "required_env('GIMME_VALKEY_RESTORE_REQUEST_ID')" in task
    assert "required_env('GIMME_CACHE_PREFIX')" in task
    assert "required_env('GIMME_VALKEY_RESTORE_SHA256')" in task
    assert "required_env('GIMME_VALKEY_RESTORE_RECORDS')" in task
    assert "upload($localPath, $remotePath)" in task
    assert "chmod 0600" in task
    assert "scripts/gimme-restore-valkey" in task
    assert "python3 - " in task
    assert "rm -f " in task
    assert task.index("try {") < task.index("upload($localPath") < task.index("} finally {")
    assert 'session.call("SCAN", cursor, "MATCH", prefix + b"*", "COUNT", 1000)' in program
    assert 'session.call("UNLINK", *keys[offset:offset + UNLINK_BATCH])' in program
    assert 'arguments = ("RESTORE", key, 0 if expiry is None else expiry' in program
    assert 'session.call("DUMP", key)' in program
    assert 'session.call("PEXPIRETIME", key)' in program
    assert 'session.call("TIME")' in program
    prohibited = (
        "KEYS", "SAVE", "BGSAVE", "FLUSHALL", "FLUSHDB", "SHUTDOWN",
        "CONFIG", "SCRIPT", "EVAL",
    )
    assert all(
        re.search(rf"\.call\(\s*['\"]{command}['\"]", program) is None
        for command in prohibited
    )


def test_restore_verification_runs_database_and_health_checks_behind_maintenance() -> None:
    recipe = deployer_source()
    task = recipe.split("task('gimme:recovery:verify-application'", 1)[1].split(
        "task('gimme:recovery:maintenance'", 1
    )[0]

    assert "migrate:status" in task
    assert "laravel_candidate_health_script()" in task
    assert "in_array('live', $probe['phases'], true)" in task
    assert "GIMME_RESTORE_VERIFY|ready" in task
    assert "laravel_live_health_script()" not in task


def _fake_psql(tmp_path: Path, *, fail: bool = False, message: str | None = None) -> Path:
    """A stand-in psql that records argv and stdin. It cannot judge SQL semantics, so the
    statements it receives are additionally exercised against a real server by hand; these
    tests pin the contract: statements arrive on stdin and secrets never reach argv."""
    log_path = tmp_path / "psql-calls.jsonl"
    script = tmp_path / "psql"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "stdin = sys.stdin.read()\n"
        f"with open({str(log_path)!r}, 'a') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:], 'stdin': stdin,\n"
        "        'env': {k: v for k, v in os.environ.items() if k[:5] == 'PGSSL'}}) + chr(10))\n"
        f"if {fail!r}:\n"
        f"    sys.stdout.write({message!r} if {message!r} else 'ERROR: ' + stdin)\n"
        "    sys.exit(3)\n"
        "sys.exit(0)\n"
    )
    script.chmod(0o700)
    return log_path


def _bundle(tmp_path: Path) -> tuple[Path, str]:
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("test bundle\n")
    bundle.chmod(0o600)
    return bundle, hashlib.sha256(bundle.read_bytes()).hexdigest()


def _run_bind_script(tmp_path: Path, secret: dict[str, str], *,
                     fail: bool = False, message: str | None = None,
                     digest: str | None = None) -> subprocess.CompletedProcess[str]:
    secret_path = tmp_path / "secret.json"
    secret_path.write_text(json.dumps(secret))
    secret_path.chmod(0o600)
    log_path = _fake_psql(tmp_path, fail=fail, message=message)
    bundle, bundle_digest = _bundle(tmp_path)
    result = subprocess.run(
        [
            "python3", "-c", managed_postgres_bind_script(),
            "db.example.test", "5432", "gimme_example", str(secret_path),
            str(bundle), digest or bundle_digest,
        ],
        text=True, capture_output=True, check=False,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    result.calls = [  # type: ignore[attr-defined]
        json.loads(line) for line in log_path.read_text().splitlines()
    ] if log_path.exists() else []
    return result


BIND_SECRET = {
    "master_username": "gimme_admin",
    "master_password": "s3cr3t-master",
    "workload_password": "s3cr3t-workload",
}


def test_managed_postgres_bind_script_sends_statements_over_stdin_never_argv(
    tmp_path: Path,
) -> None:
    result = _run_bind_script(tmp_path, BIND_SECRET)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "GIMME_RESOURCE_BOUND|gimme_example"
    calls = result.calls  # type: ignore[attr-defined]
    assert len(calls) == 2
    for call in calls:
        argv = " ".join(call["argv"])
        assert "-c" not in call["argv"]
        assert "s3cr3t-workload" not in argv
        assert "s3cr3t-master" not in argv
    role_sql, database_sql = calls[0]["stdin"], calls[1]["stdin"]
    assert "\\set role 'gimme_example'" in role_sql
    assert "CREATE ROLE %I LOGIN" in role_sql and "\\gexec" in role_sql
    assert "ALTER ROLE %I PASSWORD %L" in role_sql
    assert "CREATE DATABASE %I OWNER %I" in database_sql and "\\gexec" in database_sql
    assert "DO $" not in role_sql + database_sql


def test_managed_postgres_bind_script_redacts_secrets_from_failure_output(
    tmp_path: Path,
) -> None:
    result = _run_bind_script(tmp_path, BIND_SECRET, fail=True)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "role reconciliation failed" in output
    assert "s3cr3t-workload" not in output
    assert "s3cr3t-master" not in output
    assert "[redacted]" in output


def test_managed_postgres_bind_script_rejects_unsafe_workload_password_and_database(
    tmp_path: Path,
) -> None:
    weak = _run_bind_script(tmp_path, {**BIND_SECRET, "workload_password": "bad'; drop"})
    assert weak.returncode != 0
    assert "unexpected format" in weak.stdout + weak.stderr
    assert weak.calls == []  # type: ignore[attr-defined]

    secret_path = tmp_path / "secret.json"
    unsafe = subprocess.run(
        [
            "python3", "-c", managed_postgres_bind_script(),
            "db.example.test", "5432", "x'; drop database postgres; --", str(secret_path),
            str(_bundle(tmp_path)[0]), "0" * 64,
        ],
        text=True, capture_output=True, check=False,
    )
    assert unsafe.returncode != 0
    assert "unsafe database identifier" in unsafe.stdout + unsafe.stderr


def test_managed_postgres_bind_script_rejects_an_unexpected_secret_shape(
    tmp_path: Path,
) -> None:
    result = _run_bind_script(tmp_path, {"master_username": "gimme_admin"})

    assert result.returncode != 0
    assert "unexpected shape" in (result.stdout + result.stderr)


def test_managed_postgres_bind_script_rejects_a_symlinked_secret_file(tmp_path: Path) -> None:
    real_secret = tmp_path / "real-secret.json"
    real_secret.write_text(json.dumps({
        "master_username": "gimme_admin", "master_password": "x", "workload_password": "y",
    }))
    link = tmp_path / "linked-secret.json"
    link.symlink_to(real_secret)
    _fake_psql(tmp_path)

    result = subprocess.run(
        [
            "python3", "-c", managed_postgres_bind_script(),
            "db.example.test", "5432", "gimme_example", str(link),
            str(_bundle(tmp_path)[0]), "0" * 64,
        ],
        text=True, capture_output=True, check=False,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )

    assert result.returncode != 0
    assert "non-regular secret document" in (result.stdout + result.stderr)


def test_managed_postgres_bind_script_verifies_the_server_certificate_against_the_bundle(
    tmp_path: Path,
) -> None:
    result = _run_bind_script(tmp_path, BIND_SECRET)

    assert result.returncode == 0, result.stdout + result.stderr
    for call in result.calls:  # type: ignore[attr-defined]
        assert call["env"] == {
            "PGSSLMODE": "verify-full", "PGSSLROOTCERT": str(tmp_path / "bundle.pem"),
        }


def test_managed_postgres_bind_script_refuses_a_bundle_digest_mismatch_before_connecting(
    tmp_path: Path,
) -> None:
    result = _run_bind_script(tmp_path, BIND_SECRET, digest="0" * 64)

    assert result.returncode != 0
    assert "trust bundle digest mismatch" in result.stdout + result.stderr
    assert result.calls == []  # type: ignore[attr-defined]


def test_managed_postgres_bind_script_rejects_a_symlinked_trust_bundle(tmp_path: Path) -> None:
    secret_path = tmp_path / "secret.json"
    secret_path.write_text(json.dumps(BIND_SECRET))
    bundle, digest = _bundle(tmp_path)
    link = tmp_path / "linked-bundle.pem"
    link.symlink_to(bundle)

    result = subprocess.run(
        [
            "python3", "-c", managed_postgres_bind_script(),
            "db.example.test", "5432", "gimme_example", str(secret_path), str(link), digest,
        ],
        text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert "non-regular trust bundle" in result.stdout + result.stderr


def test_managed_postgres_bind_script_classifies_certificate_failures_without_psql_output(
    tmp_path: Path,
) -> None:
    for message in (
        'psql: error: connection to server at "db.example.test" (10.0.0.5), port 5432 '
        'failed: SSL error: certificate verify failed\n' + BIND_SECRET["master_password"],
        'psql: error: connection to server at "db.example.test" (10.0.0.5), port 5432 '
        'failed: server certificate for "other.example.test" does not match host name '
        '"db.example.test"',
    ):
        result = _run_bind_script(tmp_path, BIND_SECRET, fail=True, message=message)

        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert "TLS certificate verification failed" in output
        assert "role reconciliation failed" not in output
        assert "psql" not in output and "10.0.0.5" not in output
        assert "s3cr3t-master" not in output and "s3cr3t-workload" not in output


def test_pinned_rds_trust_bundle_matches_its_digest_and_holds_only_root_cas() -> None:
    from gimme.resources_postgres import RDS_TRUST_BUNDLE_SHA256

    bundle = ROOT / "deploy" / "aws-rds-global-bundle.pem"
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == RDS_TRUST_BUNDLE_SHA256
    assert RDS_TRUST_BUNDLE_SHA256 in (ROOT / "deploy" / "aws-rds-global-bundle.md").read_text()

    if shutil.which("openssl") is None:
        pytest.skip("openssl is needed to inspect the bundle's certificates")
    certificates = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", bundle.read_text(), re.S
    )
    assert certificates
    for pem in certificates:
        described = subprocess.run(
            ["openssl", "x509", "-noout", "-subject", "-issuer", "-ext", "basicConstraints"],
            input=pem, text=True, capture_output=True, check=True,
        ).stdout
        subject = re.search(r"subject=(.*)", described).group(1)  # type: ignore[union-attr]
        issuer = re.search(r"issuer=(.*)", described).group(1)  # type: ignore[union-attr]
        assert subject == issuer and "CA:TRUE" in described, "the bundle must hold roots only"
