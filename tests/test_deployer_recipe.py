import base64
import json
import os
import subprocess
from pathlib import Path


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


def rendered_deploy_plan(health: dict[str, object]) -> str:
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
        "GIMME_HEALTH_JSON": json.dumps(health),
        "GIMME_RUNTIMES_JSON": json.dumps({
            "php": {"provider": "system", "version": "8.4.1"},
            "composer": {"provider": "system", "version": "2.8.4"},
        }),
        "GIMME_RESOURCES_JSON": "{}",
        "GIMME_PHP_EXTENSIONS_JSON": "[]",
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
    assert "HORIZON_PREFIX={$cachePrefix}horizon:" in task
    assert "laravel_environment_reconcile_script" in task
    assert "GIMME_RUNTIME_CHANGED|yes" in task
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
    assert changed.stdout.strip() == "GIMME_RUNTIME_CHANGED|yes"
    assert unchanged.stdout.strip() == "GIMME_RUNTIME_CHANGED|no"
    assert env_path.stat().st_ino == first_inode
    assert env_path.stat().st_mode & 0o777 == 0o600
    assert "APP_KEY=secret-value" in content
    assert "DB_PASSWORD=another-secret" in content
    assert "APP_ENV=local" in content
    assert "APP_DEBUG=true" in content
    assert "secret-value" not in changed.stdout


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
    assert '"{$sudo} bash -c %bootstrap%"' in task
    assert "secrets: ['bootstrap' => escapeshellarg($bootstrap)]" in task
    assert "sudo -n /usr/local/sbin/gimme-provision-stack" in task


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
    assert 'redis.call("SCAN"' in task


def test_frontend_build_runs_after_composer_dependencies() -> None:
    recipe = deployer_source()

    assert "after('deploy:update_code', 'gimme:frontend:install')" in recipe
    assert "after('deploy:vendors', 'gimme:frontend:build')" in recipe
    assert "after('deploy:update_code', 'gimme:frontend')" not in recipe


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


def test_deployment_health_gates_candidate_before_live_activation() -> None:
    plan = rendered_deploy_plan(
        {
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

    assert "len(sys.argv) != 1" in helper
    assert "len(sys.argv) != 2" in process_helper
    assert "ALLOWED_PACKAGES" in helper
    assert "ALLOWED_SERVICES" in helper
    assert "EXPECTED_HOSTNAME" in helper
    assert "GIMME_POLICY_ID" in helper
    assert "'helper_source_sha256' => privileged_helper_source_hashes()" in recipe
    assert "NOPASSWD: /usr/local/sbin/gimme-provision-processes" in recipe
    assert "shell_exec" not in helper
    assert "GIMME_HELPER|" in recipe
    assert "chown {$user}:{$user}" in recipe
    assert recipe.index('visudo -cf "\\$sudoers_tmp"') < recipe.index(
        'mv "\\$sudoers_tmp" /etc/sudoers.d/gimme-provision-stack'
    )
