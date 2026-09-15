<?php

declare(strict_types=1);

namespace Deployer;

$framework = getenv('GIMME_FRAMEWORK') ?: 'common';
$recipes = [
    'common' => __DIR__ . '/vendor/deployer/deployer/recipe/common.php',
    'laravel' => __DIR__ . '/vendor/deployer/deployer/recipe/laravel.php',
    'symfony' => __DIR__ . '/vendor/deployer/deployer/recipe/symfony.php',
    'wordpress' => __DIR__ . '/vendor/deployer/deployer/recipe/wordpress.php',
    'static' => __DIR__ . '/vendor/deployer/deployer/recipe/common.php',
];

if (!array_key_exists($framework, $recipes)) {
    throw new \RuntimeException("Unsupported framework recipe: {$framework}");
}

require $recipes[$framework];

function required_env(string $name): string
{
    $value = getenv($name);
    if ($value === false || $value === '') {
        throw new \RuntimeException("Missing required environment variable {$name}");
    }
    return $value;
}

function sudo_prefix(): string
{
    return getenv('GIMME_INTERACTIVE_SUDO') === '1' ? 'sudo' : 'sudo -n';
}

function valid_endpoint(string $value): bool
{
    if ($value === '' || str_starts_with($value, '-') || preg_match('/\\s/', $value)) {
        return false;
    }
    if (filter_var($value, FILTER_VALIDATE_IP) !== false) {
        return true;
    }
    return preg_match(
        '/^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\\.)*' .
        '[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$/',
        $value,
    ) === 1;
}

function valid_repository_path(string $path): bool
{
    if (!preg_match('/^[a-zA-Z0-9._~\\/-]+$/', $path)) {
        return false;
    }
    $parts = explode('/', trim($path, '/'));
    return !array_filter(
        $parts,
        static fn (string $part): bool => in_array($part, ['', '.', '..'], true),
    );
}

function valid_repository(string $value): bool
{
    if (preg_match('/[\\x00\\r\\n]/', $value)) {
        return false;
    }
    if (str_starts_with($value, 'git@')) {
        if (!preg_match(
            '/^git@(?<host>[a-zA-Z0-9.-]+):(?<path>[a-zA-Z0-9._~\\/-]+)$/',
            $value,
            $matches,
        )) {
            return false;
        }
        return valid_endpoint($matches['host']) && valid_repository_path($matches['path']);
    }
    $parts = parse_url($value);
    if (!is_array($parts) || !isset($parts['scheme'], $parts['host'], $parts['path'])) {
        return false;
    }
    if (!in_array($parts['scheme'], ['https', 'ssh'], true)) {
        return false;
    }
    if (isset($parts['pass']) || isset($parts['query']) || isset($parts['fragment'])) {
        return false;
    }
    if ($parts['scheme'] === 'https' && (isset($parts['user']) || isset($parts['port']))) {
        return false;
    }
    if (isset($parts['user']) &&
        (str_starts_with($parts['user'], '-') ||
        !preg_match('/^[a-zA-Z_][a-zA-Z0-9_-]{0,31}$/', $parts['user']))) {
        return false;
    }
    return valid_endpoint($parts['host']) && valid_repository_path($parts['path']);
}

function valid_git_branch(string $value): bool
{
    return $value !== '' && $value !== '@' &&
        !str_starts_with($value, '-') && !str_starts_with($value, '.') &&
        !str_starts_with($value, '/') && !str_ends_with($value, '.') &&
        !str_ends_with($value, '/') && !str_ends_with($value, '.lock') &&
        !str_contains($value, '..') && !str_contains($value, '@{') &&
        !str_contains($value, '//') &&
        !preg_match('/[\\x00-\\x20\\x7f~^:?*\[\\\\]/', $value);
}

function local_config(string $file): array
{
    static $cache = [];
    if (!array_key_exists($file, $cache)) {
        $path = __DIR__ . "/config/{$file}.json";
        $decoded = json_decode(file_get_contents($path), true, flags: JSON_THROW_ON_ERROR);
        if (!is_array($decoded)) {
            throw new \RuntimeException("Invalid local config file: {$path}");
        }
        $cache[$file] = $decoded;
    }
    return $cache[$file];
}

function env_or_config(string $environment, string $file, string $key): mixed
{
    $value = getenv($environment);
    if ($value !== false && $value !== '') {
        return $value;
    }
    $config = local_config($file);
    if (!array_key_exists($key, $config)) {
        throw new \RuntimeException("Missing config key {$file}.{$key}");
    }
    return $config[$key];
}

function configured_package_manager(): string
{
    $manager = env_or_config('GIMME_PACKAGE_MANAGER', 'stack', 'package_manager');
    if (!is_string($manager)) {
        throw new \RuntimeException('Configured package manager must be a string');
    }
    return $manager;
}

function configured_list(string $name): array
{
    $raw = getenv($name);
    if ($raw !== false && $raw !== '') {
        $value = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
    } else {
        $key = match ($name) {
            'GIMME_PACKAGES_JSON' => 'packages',
            'GIMME_SERVICES_JSON' => 'services',
            default => throw new \RuntimeException("Unknown configured list {$name}"),
        };
        $value = local_config('stack')[$key] ?? null;
    }
    if (!is_array($value) || !array_is_list($value)) {
        throw new \RuntimeException("{$name} must be a JSON list");
    }
    return $value;
}

function configured_packages(): array
{
    $packages = configured_list('GIMME_PACKAGES_JSON');
    foreach ($packages as $package) {
        if (!is_string($package) || !preg_match('/^[a-z0-9][a-z0-9+.-]{0,79}$/', $package)) {
            throw new \RuntimeException('Unsafe configured package name');
        }
    }
    return $packages;
}

function configured_services(): array
{
    $services = configured_list('GIMME_SERVICES_JSON');
    foreach ($services as $service) {
        if (!is_string($service) || !preg_match('/^[a-zA-Z0-9][a-zA-Z0-9@_.:-]{0,79}$/', $service)) {
            throw new \RuntimeException('Unsafe configured service name');
        }
    }
    return $services;
}

function configured_artisan_commands(): array
{
    $raw = required_env('GIMME_ARTISAN_ALLOWED_JSON');
    $commands = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
    if (!is_array($commands) || !array_is_list($commands) || count($commands) > 32) {
        throw new \RuntimeException('Artisan command allowlist must be a JSON list');
    }
    foreach ($commands as $command) {
        if (!is_string($command) ||
            !preg_match('/^[a-z][a-z0-9-]*(?::[a-z][a-z0-9-]*)*$/', $command)) {
            throw new \RuntimeException('Unsafe Artisan command allowlist');
        }
    }
    if (count($commands) !== count(array_unique($commands))) {
        throw new \RuntimeException('Artisan command allowlist contains duplicates');
    }
    return $commands;
}

function configured_artisan_arguments(): array
{
    $raw = required_env('GIMME_ARTISAN_ARGS_JSON');
    $arguments = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
    if (!is_array($arguments) || !array_is_list($arguments) || count($arguments) > 32) {
        throw new \RuntimeException('Artisan arguments must be a JSON list');
    }
    foreach ($arguments as $argument) {
        if (!is_string($argument) || $argument === '' || strlen($argument) > 256 ||
            preg_match('/[\x00-\x1f\x7f]/', $argument) || $argument === '--env' ||
            str_starts_with($argument, '--env=')) {
            throw new \RuntimeException('Unsafe Artisan argument');
        }
    }
    return $arguments;
}

function configured_apps(): array
{
    $registry = local_config('apps');
    $apps = $registry['apps'] ?? null;
    if (!is_array($apps) || array_is_list($apps)) {
        throw new \RuntimeException('Configured applications must be an object');
    }
    foreach ($apps as $name => $definition) {
        if (!is_string($name) || !preg_match('/^[a-z][a-z0-9-]{0,47}$/', $name)) {
            throw new \RuntimeException('Unsafe configured application name');
        }
        if (!is_array($definition)) {
            throw new \RuntimeException("Invalid application definition for {$name}");
        }
    }
    ksort($apps);
    return $apps;
}

function privileged_helper_source_hash(): string
{
    $source = file_get_contents(__DIR__ . '/scripts/gimme-provision-stack');
    if ($source === false) {
        throw new \RuntimeException('Missing privileged helper source');
    }
    return hash('sha256', $source);
}

function privileged_helper_policy(
    array $packages,
    array $services,
    string $hostname,
    string $mdnsName,
    string $remoteUser,
    string $appsRoot,
): string {
    return hash('sha256', json_encode([
        'helper_source_sha256' => privileged_helper_source_hash(),
        'packages' => $packages,
        'services' => $services,
        'hostname' => $hostname,
        'mdns_name' => $mdnsName,
        'remote_user' => $remoteUser,
        'apps_root' => $appsRoot,
    ], JSON_THROW_ON_ERROR));
}

$hostname = (string) env_or_config('GIMME_HOSTNAME', 'server', 'hostname');
$bootstrapHostname = (string) env_or_config(
    'GIMME_BOOTSTRAP_HOSTNAME', 'server', 'bootstrap_hostname'
);
$bootstrapTasks = ['gimme:preflight:stack', 'gimme:provision:stack'];
$arguments = $_SERVER['argv'] ?? [];
$useBootstrapHostname = is_array($arguments) &&
    count(array_intersect($bootstrapTasks, $arguments)) > 0;
$sshHostname = getenv('GIMME_SSH_HOSTNAME') ?:
    ($useBootstrapHostname ? $bootstrapHostname : $hostname);
$hostAlias = (string) env_or_config('GIMME_HOST_ALIAS', 'server', 'host_alias');
$mdnsName = (string) env_or_config('GIMME_MDNS_NAME', 'server', 'mdns_name');
$remoteUser = (string) env_or_config('GIMME_REMOTE_USER', 'server', 'remote_user');
$appsRoot = rtrim((string) env_or_config('GIMME_APPS_ROOT', 'server', 'apps_root'), '/');
$app = getenv('GIMME_APP') ?: '';

if (!valid_endpoint($hostname) || !valid_endpoint($bootstrapHostname) || !valid_endpoint($sshHostname)) {
    throw new \RuntimeException('Unsafe host endpoint');
}
if (!preg_match('/^[a-zA-Z_][a-zA-Z0-9_-]{0,31}$/', $remoteUser)) {
    throw new \RuntimeException('Unsafe remote user');
}
if (!preg_match('/^[a-z][a-z0-9-]{0,47}$/', $hostAlias)) {
    throw new \RuntimeException('Unsafe host alias');
}
if (!preg_match('/^[a-z][a-z0-9-]{0,62}$/', $mdnsName)) {
    throw new \RuntimeException('Unsafe mDNS host name');
}
if (
    str_contains($appsRoot, '..') ||
    !array_filter(
        ['/srv/', '/var/www/', '/opt/', '/home/'],
        static fn (string $prefix): bool => str_starts_with($appsRoot, $prefix),
    )
) {
    throw new \RuntimeException('Unsafe application root');
}
if ($app !== '' && !preg_match('/^[a-z][a-z0-9-]{0,47}$/', $app)) {
    throw new \RuntimeException('Unsafe application name');
}

host($hostAlias)
    ->setHostname($sshHostname)
    ->setRemoteUser($remoteUser)
    ->setDeployPath($app === '' ? $appsRoot : "{$appsRoot}/{$app}");

set('keep_releases', (int) env_or_config('GIMME_KEEP_RELEASES', 'server', 'keep_releases'));
set('ssh_multiplexing', true);

if ($app !== '') {
    set('application', $app);
    $repository = required_env('GIMME_REPOSITORY');
    $branch = required_env('GIMME_BRANCH');
    if (!valid_repository($repository)) {
        throw new \RuntimeException('Unsafe Git repository URL');
    }
    if (!valid_git_branch($branch)) {
        throw new \RuntimeException('Unsafe Git branch name');
    }
    set('repository', $repository);
    set('branch', $branch);
    if ($framework !== 'static') {
        set('shared_files', array_values(array_unique([
            ...get('shared_files', []),
            '.env',
        ])));
    }
}

$hasFrontend = getenv('GIMME_FRONTEND') === '1';
if ($hasFrontend) {
    $packageManager = required_env('GIMME_FRONTEND_PACKAGE_MANAGER');
    $buildScript = required_env('GIMME_FRONTEND_BUILD_SCRIPT');
    $outputDir = required_env('GIMME_FRONTEND_OUTPUT_DIR');
    if ($packageManager !== 'npm') {
        throw new \RuntimeException('Unsupported frontend package manager');
    }
    if (!preg_match('/^[a-zA-Z0-9:_-]{1,64}$/', $buildScript)) {
        throw new \RuntimeException('Unsafe frontend build script');
    }
    if (!preg_match('/^[a-zA-Z0-9._-]+(?:\/[a-zA-Z0-9._-]+)*$/', $outputDir) ||
        str_starts_with($outputDir, '-') ||
        count(array_intersect(explode('/', $outputDir), ['.', '..'])) > 0) {
        throw new \RuntimeException('Unsafe frontend output directory');
    }
    if ($framework === 'static') {
        set('public_path', $outputDir);
    }

    task('gimme:frontend:install', function (): void {
        run('cd {{release_path}} && npm ci --no-audit --no-fund');
    });
    task('gimme:frontend:build', function () use ($buildScript): void {
        run('cd {{release_path}} && npm run ' . escapeshellarg($buildScript));
    });
    task('gimme:frontend', [
        'gimme:frontend:install',
        'gimme:frontend:build',
    ]);
    after('deploy:update_code', 'gimme:frontend:install');
    after('deploy:vendors', 'gimme:frontend:build');
}

task('gimme:inspect', function () use ($mdnsName): void {
    $script = <<<'BASH'
set -eu
. /etc/os-release
printf 'os=%s\nversion=%s\nhostname=%s\n' "$NAME" "$VERSION_ID" "$(hostname)"
if sudo -n true >/dev/null 2>&1; then
    printf 'passwordless_sudo=yes\n'
else
    printf 'passwordless_sudo=no\n'
fi
if [ -x /usr/local/sbin/gimme-provision-stack ] && \
   sudo -n -l /usr/local/sbin/gimme-provision-stack >/dev/null 2>&1; then
    printf 'privileged_helper=ready\n'
else
    printf 'privileged_helper=bootstrap_required\n'
fi
BASH;
    writeln(run("bash -c " . escapeshellarg($script)));
    foreach (configured_services() as $service) {
        $state = run(
            'systemctl is-active ' . escapeshellarg($service) . ' 2>/dev/null || true'
        );
        $state = $state === '' ? 'unknown' : $state;
        writeln("service.{$service}={$state}");
    }
    $resolved = run('getent hosts ' . escapeshellarg("{$mdnsName}.local") . ' 2>/dev/null || true');
    writeln('mdns=' . ($resolved === '' ? 'unresolved' : $resolved));
    foreach (configured_apps() as $name => $definition) {
        $framework = $definition['framework'] ?? 'common';
        $relativeRoot = match ($framework) {
            'laravel', 'symfony' => 'public',
            'static' => $definition['frontend']['output_dir'] ?? 'dist',
            default => '',
        };
        $documentRoot = get('deploy_path') === ''
            ? ''
            : rtrim((string) env_or_config('GIMME_APPS_ROOT', 'server', 'apps_root'), '/') .
                "/{$name}/current";
        if ($relativeRoot !== '') {
            $documentRoot .= "/{$relativeRoot}";
        }
        $siteHost = "{$name}.{$mdnsName}.local";
        // Local resolver behaviour is not authoritative for an Avahi record published
        // by this same host. Report it as a separate observation and allow IPv6-only
        // mDNS results; client resolution cannot be observed from the VM.
        $localResolution = run(
            'getent hosts ' . escapeshellarg($siteHost) . ' 2>/dev/null || true'
        );
        $index = test('[ -f ' . escapeshellarg("{$documentRoot}/index.php") . ' ]')
            || test('[ -f ' . escapeshellarg("{$documentRoot}/index.html") . ' ]');
        $path = run('namei -l ' . escapeshellarg($documentRoot) . ' 2>/dev/null || true');
        $http = run(
            'curl -skS --connect-timeout 3 --max-time 5 --resolve ' .
            escapeshellarg("{$siteHost}:443:127.0.0.1") .
            " -o /dev/null -w '%{http_code}' " .
            escapeshellarg("https://{$siteHost}/") . ' 2>/dev/null || true'
        );
        $publisher = run(
            'systemctl is-active ' . escapeshellarg("gimme-mdns-{$name}.service") .
            ' 2>/dev/null || true'
        );
        writeln("site.{$name}.hostname={$siteHost}");
        writeln(
            'site.' . $name . '.mdns_publisher=' .
            ($publisher === '' ? 'missing' : $publisher)
        );
        writeln(
            'site.' . $name . '.mdns_local_resolution=' .
            ($localResolution === '' ? 'unavailable' : $localResolution)
        );
        writeln('site.' . $name . '.mdns_client_resolution=not_observable');
        writeln("site.{$name}.document_root={$documentRoot}");
        writeln('site.' . $name . '.index=' . ($index ? 'present' : 'missing'));
        writeln("site.{$name}.https_status=" . ($http === '' ? 'unreachable' : $http));
        writeln("site.{$name}.path_permissions=\n{$path}");
        if ($framework === 'laravel') {
            $deployPath = rtrim(
                (string) env_or_config('GIMME_APPS_ROOT', 'server', 'apps_root'), '/'
            ) . "/{$name}";
            $envKeys = run(
                "sed -n 's/^\\([A-Z][A-Z0-9_]*\\)=.*/\\1/p' " .
                escapeshellarg("{$deployPath}/shared/.env") . ' 2>/dev/null || true'
            );
            $laravelLogFiles = run(
                'find ' . escapeshellarg("{$deployPath}/current/storage/logs") .
                " -maxdepth 1 -type f -name '*.log' -printf '.\\n' " .
                '2>/dev/null | wc -l'
            );
            $runtimePaths = run(
                'for path in ' .
                escapeshellarg("{$deployPath}/current/storage") . ' ' .
                escapeshellarg("{$deployPath}/current/storage/logs") . ' ' .
                escapeshellarg("{$deployPath}/current/storage/framework/sessions") . ' ' .
                escapeshellarg("{$deployPath}/current/bootstrap/cache") .
                '; do namei -l "$path" 2>/dev/null || true; done'
            );
            writeln("site.{$name}.env_keys=\n{$envKeys}");
            writeln("site.{$name}.laravel_log_files={$laravelLogFiles}");
            writeln("site.{$name}.runtime_path_permissions=\n{$runtimePaths}");
        }
    }
    $fpmSocket = run('readlink -f /run/php/php-fpm.sock 2>/dev/null || true');
    writeln('php_fpm_socket=' . ($fpmSocket === '' ? 'missing' : $fpmSocket));
    $exportedCa = rtrim(
        (string) env_or_config('GIMME_APPS_ROOT', 'server', 'apps_root'), '/'
    ) . '/.caddy-local-root.crt';
    $caFingerprint = run(
        'sha256sum ' . escapeshellarg($exportedCa) . " 2>/dev/null | awk '{ print \$1 }' || true"
    );
    writeln('caddy_ca=' . ($caFingerprint === '' ? 'missing' : $exportedCa));
    writeln('caddy_ca_sha256=' . ($caFingerprint === '' ? 'missing' : $caFingerprint));
    writeln('apps_root_acl=\n' . run(
        'getfacl -cp ' . escapeshellarg(
            rtrim((string) env_or_config('GIMME_APPS_ROOT', 'server', 'apps_root'), '/')
        ) . ' 2>/dev/null || true'
    ));
    writeln('php_fpm_processes=\n' . run(
        "ps -eo user=,comm= | awk '\$2 ~ /^php-fpm/ { print \$1, \$2 }'"
    ));
    writeln('avahi_managed_hosts=\n' . run(
        "sed -n '/^# BEGIN GIMME MANAGED HOSTS\$/,/^# END GIMME MANAGED HOSTS\$/p' " .
        '/etc/avahi/hosts 2>/dev/null || true'
    ));
    $agentScript = <<<'BASH'
if [ -S "${SSH_AUTH_SOCK:-}" ]; then
    printf 'ssh_agent=forwarded\n'
else
    printf 'ssh_agent=missing\n'
fi
BASH;
    writeln(run('bash -c ' . escapeshellarg($agentScript)));
});

task('gimme:preflight:stack', function () use ($hostname, $mdnsName, $remoteUser, $appsRoot): void {
    if (configured_package_manager() !== 'apt') {
        throw new \RuntimeException('Configured package manager is not supported');
    }
    if (!test('command -v apt-get >/dev/null && command -v apt-cache >/dev/null && command -v dpkg-query >/dev/null')) {
        throw new \RuntimeException('The configured host does not provide the apt toolchain');
    }
    $packageProcesses = run(
        "pgrep -x apt-get 2>/dev/null | paste -sd, - || true"
    );
    writeln('GIMME_APT_BUSY|' . ($packageProcesses === '' ? 'no' : $packageProcesses));
    $policy = privileged_helper_policy(
        configured_packages(),
        configured_services(),
        $hostname,
        $mdnsName,
        $remoteUser,
        $appsRoot,
    );
    $policyLine = escapeshellarg("# GIMME_POLICY_ID={$policy}");
    $helperReady = test(
        '[ -x /usr/local/sbin/gimme-provision-stack ] && ' .
        "grep -Fqx {$policyLine} /usr/local/sbin/gimme-provision-stack && " .
        'sudo -n -l /usr/local/sbin/gimme-provision-stack >/dev/null 2>&1'
    );
    writeln('GIMME_HELPER|' . ($helperReady ? 'ready' : 'bootstrap_required'));
    $packages = configured_packages();
    $services = configured_services();
    foreach ($packages as $package) {
        $quoted = escapeshellarg($package);
        $installed = run(
            "dpkg-query -W -f='\${Version}' {$quoted} 2>/dev/null || printf missing"
        );
        $candidate = run(
            "apt-cache policy {$quoted} | sed -n 's/^  Candidate: //p' | head -n 1"
        );
        if ($candidate === '' || $candidate === '(none)') {
            $candidate = 'unavailable';
        }
        writeln("GIMME_PACKAGE|{$package}|{$installed}|{$candidate}");
    }
    run(
        'apt-get --simulate --no-install-recommends install ' .
        implode(' ', array_map('escapeshellarg', $packages)) .
        ' >/dev/null'
    );
});

task('gimme:provision:stack', function () use ($appsRoot, $hostname, $remoteUser, $mdnsName): void {
    if (configured_package_manager() !== 'apt') {
        throw new \RuntimeException('Configured package manager is not supported');
    }

    $packages = configured_packages();
    $services = configured_services();
    $statePath = "{$appsRoot}/.gimme/stack.json";
    $state = json_encode([
        'version' => 1,
        'package_manager' => 'apt',
        'packages' => $packages,
        'services' => $services,
        'hostname' => $hostname,
        'mdns_name' => $mdnsName,
        'remote_user' => $remoteUser,
        'apps_root' => $appsRoot,
        'apps' => configured_apps(),
    ], JSON_THROW_ON_ERROR);
    $stateEncoded = escapeshellarg(base64_encode($state));
    $stateDirectory = escapeshellarg(dirname($statePath));
    $quotedStatePath = escapeshellarg($statePath);
    $writeState = <<<BASH
set -eu
install -d -m 0700 {$stateDirectory}
temporary={$quotedStatePath}.tmp.\$\$
trap 'rm -f "\$temporary"' EXIT
printf %s {$stateEncoded} | base64 -d > "\$temporary"
chmod 0600 "\$temporary"
mv "\$temporary" {$quotedStatePath}
trap - EXIT
BASH;

    if (getenv("GIMME_INTERACTIVE_SUDO") !== '1') {
        run('bash -c ' . escapeshellarg($writeState));
        run(
            'sudo -n /usr/local/sbin/gimme-provision-stack',
            forceOutput: true,
            timeout: 1800,
        );
        return;
    }

    $helperTemplate = file_get_contents(__DIR__ . '/scripts/gimme-provision-stack');
    if ($helperTemplate === false) {
        throw new \RuntimeException('Missing privileged helper source');
    }
    $helper = str_replace(
        '"__GIMME_STATE_PATH__"',
        json_encode($statePath, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES),
        $helperTemplate,
    );
    $helper = str_replace(
        '"__GIMME_ALLOWED_PACKAGES_JSON__"',
        json_encode(json_encode($packages, JSON_THROW_ON_ERROR), JSON_THROW_ON_ERROR),
        $helper,
    );
    $helper = str_replace(
        '"__GIMME_HOSTNAME__"',
        json_encode($hostname, JSON_THROW_ON_ERROR),
        $helper,
    );
    $helper = str_replace(
        '"__GIMME_MDNS_NAME__"',
        json_encode($mdnsName, JSON_THROW_ON_ERROR),
        $helper,
    );
    $helper = str_replace(
        '__GIMME_POLICY_ID__',
        privileged_helper_policy(
            $packages,
            $services,
            $hostname,
            $mdnsName,
            $remoteUser,
            $appsRoot,
        ),
        $helper,
    );
    $helperEncoded = escapeshellarg(base64_encode($helper));
    $packageWords = implode(' ', array_map('escapeshellarg', $packages));
    $user = escapeshellarg($remoteUser);
    $sudoers = escapeshellarg(
        "{$remoteUser} ALL=(root) NOPASSWD: /usr/local/sbin/gimme-provision-stack\n"
    );
    $rootWriteState = str_replace(
        'install -d -m 0700',
        "install -d -m 0700 -o {$user} -g {$user}",
        $writeState,
    );
    $rootWriteState = str_replace(
        'chmod 0600 "$temporary"',
        "chmod 0600 \"\$temporary\"\nchown {$user}:{$user} \"\$temporary\"",
        $rootWriteState,
    );
    $bootstrap = <<<BASH
set -euo pipefail
packages=({$packageWords})
missing=()
for package in "\${packages[@]}"; do
    status=\$(dpkg-query -W -f='\${Status}' "\$package" 2>/dev/null || true)
    if [ "\$status" != 'install ok installed' ]; then
        missing+=("\$package")
    fi
done
if (( \${#missing[@]} )); then
    apt-get update
    env DEBIAN_FRONTEND=noninteractive apt-get --no-install-recommends install -y "\${missing[@]}"
fi

{$rootWriteState}
helper_tmp=\$(mktemp /usr/local/sbin/.gimme-provision-stack.XXXXXX)
sudoers_tmp=\$(mktemp /etc/sudoers.d/.gimme-provision-stack.XXXXXX)
trap 'rm -f "\$helper_tmp" "\$sudoers_tmp"' EXIT
printf %s {$helperEncoded} | base64 -d > "\$helper_tmp"
chown root:root "\$helper_tmp"
chmod 0755 "\$helper_tmp"
printf %s {$sudoers} > "\$sudoers_tmp"
chown root:root "\$sudoers_tmp"
chmod 0440 "\$sudoers_tmp"
visudo -cf "\$sudoers_tmp"
mv "\$helper_tmp" /usr/local/sbin/gimme-provision-stack
mv "\$sudoers_tmp" /etc/sudoers.d/gimme-provision-stack
trap - EXIT
/usr/local/sbin/gimme-provision-stack
BASH;
    $sudo = sudo_prefix();
    run(
        "{$sudo} bash -c %bootstrap%",
        secrets: ['bootstrap' => escapeshellarg($bootstrap)],
        forceOutput: true,
        timeout: 1800,
    );
});

task('gimme:bootstrap:database-admin', function () use ($remoteUser): void {
    $sudo = sudo_prefix();
    $user = escapeshellarg($remoteUser);
    $role = str_replace("'", "''", $remoteUser);
    $script = <<<BASH
set -eu
if psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='{$role}'" | grep -qx 1; then
    psql -v ON_ERROR_STOP=1 -c "ALTER ROLE {$role} CREATEDB CREATEROLE"
else
    createuser --createdb --createrole {$user}
fi
BASH;
    run("{$sudo} -u postgres bash -c " . escapeshellarg($script));
});

task('gimme:provision:app', function () use ($app, $mdnsName): void {
    if ($app === '') {
        throw new \RuntimeException('Application context is required');
    }
    if ((getenv('GIMME_FRAMEWORK') ?: 'common') === 'static') {
        writeln('Static frontend requires no PostgreSQL database or Valkey namespace.');
        return;
    }

    $identifier = str_replace('-', '_', $app);
    $database = "gimme_{$identifier}";
    $role = "gimme_{$identifier}";
    $deployPath = get('deploy_path');
    $sharedPath = "{$deployPath}/shared";
    $envPath = "{$sharedPath}/.env";

    run('install -d -m 0700 ' . escapeshellarg($sharedPath));
    run('setfacl -m u:www-data:x ' . escapeshellarg($sharedPath));

    $script = <<<BASH
set -eu
env_path="{$envPath}"
if [ -L "\$env_path" ]; then
    printf 'Refusing to manage symlinked environment file: %s\n' "\$env_path" >&2
    exit 1
fi
if [ ! -f "\$env_path" ]; then
    password=\$(openssl rand -hex 32)
    if ! psql -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='{$role}'" | grep -qx 1; then
        psql -d postgres -v ON_ERROR_STOP=1 -c "CREATE ROLE {$role} LOGIN PASSWORD '\$password'"
    else
        psql -d postgres -v ON_ERROR_STOP=1 -c "ALTER ROLE {$role} PASSWORD '\$password'"
    fi
    psql -d postgres -v ON_ERROR_STOP=1 -c "GRANT {$role} TO CURRENT_USER WITH SET TRUE, INHERIT FALSE"
    if ! psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='{$database}'" | grep -qx 1; then
        createdb --owner="{$role}" "{$database}"
    fi
    umask 077
    {
        printf 'DB_CONNECTION=pgsql\n'
        printf 'DB_HOST=127.0.0.1\n'
        printf 'DB_PORT=5432\n'
        printf 'DB_DATABASE=%s\n' '{$database}'
        printf 'DB_USERNAME=%s\n' '{$role}'
        printf 'DB_PASSWORD=%s\n' "\$password"
        printf 'CACHE_STORE=redis\n'
        printf 'REDIS_HOST=127.0.0.1\n'
        printf 'REDIS_PORT=6379\n'
        printf 'REDIS_PREFIX=%s\n' 'gimme:{$app}:'
    } > "\$env_path"
fi
chmod 0600 "\$env_path"
BASH;

    if ((getenv('GIMME_FRAMEWORK') ?: 'common') === 'laravel') {
        $script .= <<<BASH

if ! grep -q '^APP_NAME=' "\$env_path"; then
    printf 'APP_NAME=%s\n' '{$app}' >> "\$env_path"
fi
if ! grep -q '^APP_ENV=' "\$env_path"; then
    printf 'APP_ENV=production\n' >> "\$env_path"
fi
if ! grep -q '^APP_DEBUG=' "\$env_path"; then
    printf 'APP_DEBUG=false\n' >> "\$env_path"
fi
if ! grep -q '^APP_URL=' "\$env_path"; then
    printf 'APP_URL=https://{$app}.{$mdnsName}.local\n' >> "\$env_path"
fi
if ! grep -q '^APP_KEY=' "\$env_path"; then
    app_key=\$(openssl rand -base64 32 | tr -d '\n')
    printf 'APP_KEY=base64:%s\n' "\$app_key" >> "\$env_path"
fi
BASH;
    }

    run('bash -c ' . escapeshellarg($script));
    if ((getenv('GIMME_FRAMEWORK') ?: 'common') === 'laravel') {
        $currentPath = get('deploy_path') . '/current';
        if (test('[ -f ' . escapeshellarg("{$currentPath}/artisan") . ' ]')) {
            run(
                'cd ' . escapeshellarg($currentPath) .
                ' && php artisan optimize:clear && php artisan optimize'
            );
        }
    }
});

task('gimme:artisan', function () use ($app): void {
    if ($app === '') {
        throw new \RuntimeException('Application context is required');
    }
    if ((getenv('GIMME_FRAMEWORK') ?: 'common') !== 'laravel') {
        throw new \RuntimeException('Artisan commands require a Laravel application');
    }
    $command = required_env('GIMME_ARTISAN_COMMAND');
    if (!preg_match('/^[a-z][a-z0-9-]*(?::[a-z][a-z0-9-]*)*$/', $command)) {
        throw new \RuntimeException('Unsafe Artisan command');
    }
    if (!in_array($command, configured_artisan_commands(), true)) {
        throw new \RuntimeException('Artisan command is not allowlisted');
    }
    $currentPath = get('deploy_path') . '/current';
    if (!test('[ -f ' . escapeshellarg("{$currentPath}/artisan") . ' ]')) {
        throw new \RuntimeException('Current release does not contain an Artisan executable');
    }
    $arguments = [
        'php',
        'artisan',
        '--no-interaction',
        $command,
        ...configured_artisan_arguments(),
    ];
    run(
        'cd ' . escapeshellarg($currentPath) . ' && ' .
        implode(' ', array_map('escapeshellarg', $arguments)),
        forceOutput: true,
    );
});

task('gimme:service:status', function (): void {
    $service = get('gimme_service');
    $allowed = ['postgresql', 'valkey-server', 'caddy'];
    if (!in_array($service, $allowed, true)) {
        throw new \RuntimeException('Service is not allowlisted');
    }
    writeln(run('systemctl status --no-pager ' . escapeshellarg($service)));
});

after('deploy:failed', 'deploy:unlock');
