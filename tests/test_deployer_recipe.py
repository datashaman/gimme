import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deployer_recipe_passes_static_analysis() -> None:
    result = subprocess.run(
        [
            str(ROOT / "vendor" / "bin" / "phpstan"),
            "analyse",
            "deploy.php",
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
    stack = json.loads((ROOT / "config" / "stack.example.json").read_text())

    assert "acl" in stack["packages"]
    assert "avahi-utils" in stack["packages"]


def test_stack_bootstrap_uses_bootstrap_hostname() -> None:
    recipe = (ROOT / "deploy.php").read_text()

    assert "'GIMME_BOOTSTRAP_HOSTNAME', 'server', 'bootstrap_hostname'" in recipe
    assert "['gimme:preflight:stack', 'gimme:provision:stack']" in recipe
    assert "$arguments = $_SERVER['argv'] ?? [];" in recipe
    assert "($useBootstrapHostname ? $bootstrapHostname : $hostname)" in recipe


def test_stack_includes_laravel_php_extensions() -> None:
    stack = json.loads((ROOT / "config" / "stack.example.json").read_text())

    assert "php-gd" in stack["packages"]
    assert "python3" in stack["packages"]


def test_apt_install_matches_preflight_and_allows_large_transactions() -> None:
    recipe = (ROOT / "deploy.php").read_text()

    assert recipe.count("apt-get --simulate --no-install-recommends install") == 1
    assert "apt-get --no-install-recommends install -y" in recipe
    assert "forceOutput: true" in recipe
    assert "timeout: 1800" in recipe


def test_database_bootstrap_exposes_sudo_to_deployer() -> None:
    recipe = (ROOT / "deploy.php").read_text()
    task = recipe.split("task('gimme:bootstrap:database-admin'", 1)[1].split(
        "task('gimme:provision:app'", 1
    )[0]

    assert 'run("{$sudo} -u postgres bash -c "' in task
    assert "if {$sudo} -u postgres" not in task


def test_host_inspection_reports_application_reachability() -> None:
    recipe = (ROOT / "deploy.php").read_text()
    task = recipe.split("task('gimme:inspect'", 1)[1].split(
        "task('gimme:preflight:stack'", 1
    )[0]

    assert "'.alias='" in task
    assert "'.mdns_publisher='" in task
    assert "site.{$name}.https_status=" in task
    assert "site.{$name}.path_permissions=" in task
    assert "site.{$name}.env_keys=" in task
    assert "site.{$name}.laravel_log_files=" in task
    assert "site.{$name}.laravel_errors=" not in task
    assert "site.{$name}.laravel_log=" not in task
    assert "xargs -0 tail -n 30" not in task
    assert "site.{$name}.runtime_path_permissions=" in task
    assert "php_fpm_socket=" in task
    assert "php_fpm_recent_log=" not in task
    assert "ssh_agent=forwarded" in task
    assert "ssh_agent=missing" in task
    assert "ssh_agent_key=" not in task


def test_app_role_can_become_database_owner() -> None:
    recipe = (ROOT / "deploy.php").read_text()
    task = recipe.split("task('gimme:provision:app'", 1)[1].split(
        "task('gimme:service:status'", 1
    )[0]

    grant = "GRANT {$role} TO CURRENT_USER WITH SET TRUE, INHERIT FALSE"
    assert grant in task
    assert task.index(grant) < task.index('createdb --owner="{$role}"')


def test_laravel_resources_include_required_application_environment() -> None:
    recipe = (ROOT / "deploy.php").read_text()
    task = recipe.split("task('gimme:provision:app'", 1)[1].split(
        "task('gimme:service:status'", 1
    )[0]

    assert "APP_KEY=base64:" in task
    assert "APP_ENV=production" in task
    assert "APP_DEBUG=false" in task
    assert "APP_URL=https://{$app}.{$mdnsName}.local" in task
    assert "grep -q '^APP_KEY='" in task
    assert "php artisan optimize:clear" in task
    assert "php artisan optimize" in task
    assert 'if [ -L "\\$env_path" ]' in task
    assert 'chmod 0600 "\\$env_path"' in task


def test_app_resources_allow_php_fpm_to_traverse_shared_directory() -> None:
    recipe = (ROOT / "deploy.php").read_text()
    task = recipe.split("task('gimme:provision:app'", 1)[1].split(
        "task('gimme:service:status'", 1
    )[0]

    assert "setfacl -m u:www-data:x" in task
    assert "install -d -m 0700" in task


def test_stack_provisions_https_sites_and_mdns_aliases() -> None:
    recipe = (ROOT / "deploy.php").read_text()
    helper = (ROOT / "scripts" / "gimme-provision-stack").read_text()
    task = recipe.split("task('gimme:provision:stack'", 1)[1].split(
        "task('gimme:bootstrap:database-admin'", 1
    )[0]

    assert "tls internal" in helper
    assert "php_fastcgi unix//run/php/php-fpm.sock" in helper
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


def test_frontend_build_runs_after_composer_dependencies() -> None:
    recipe = (ROOT / "deploy.php").read_text()

    assert "after('deploy:update_code', 'gimme:frontend:install')" in recipe
    assert "after('deploy:vendors', 'gimme:frontend:build')" in recipe
    assert "after('deploy:update_code', 'gimme:frontend')" not in recipe


def test_privileged_helper_is_narrowly_allowlisted() -> None:
    recipe = (ROOT / "deploy.php").read_text()
    helper = (ROOT / "scripts" / "gimme-provision-stack").read_text()

    assert "NOPASSWD: /usr/local/sbin/gimme-provision-stack" in recipe
    assert "NOPASSWD: ALL" not in recipe
    assert "SUDO_USER" in helper
    assert "len(sys.argv) != 1" in helper
    assert "ALLOWED_PACKAGES" in helper
    assert "ALLOWED_SERVICES" in helper
    assert "EXPECTED_HOSTNAME" in helper
    assert "GIMME_POLICY_ID" in helper
    assert "'helper_source_sha256' => privileged_helper_source_hash()" in recipe
    assert "shell_exec" not in helper
    assert "GIMME_HELPER|" in recipe
    assert "chown {$user}:{$user}" in recipe
    assert recipe.index('visudo -cf "\\$sudoers_tmp"') < recipe.index(
        'mv "\\$sudoers_tmp" /etc/sudoers.d/gimme-provision-stack'
    )
