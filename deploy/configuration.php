<?php

declare(strict_types=1);

namespace Deployer;

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
        $path = dirname(__DIR__) . "/config/{$file}.json";
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

function configured_process_object(string $environment): ?array
{
    if (!in_array($environment, ['GIMME_WORKERS_JSON', 'GIMME_SCHEDULER_JSON'], true)) {
        throw new \RuntimeException('Unknown process configuration');
    }
    $decoded = json_decode(required_env($environment), true, flags: JSON_THROW_ON_ERROR);
    if ($decoded !== null && (!is_array($decoded) || array_is_list($decoded))) {
        throw new \RuntimeException("{$environment} must be an object or null");
    }
    return $decoded;
}
function configured_runtimes(): array
{
    $raw = getenv('GIMME_RUNTIMES_JSON') ?: '{}';
    $decoded = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
    if (!is_array($decoded) || (array_is_list($decoded) && trim($raw) !== '{}')) {
        throw new \RuntimeException('GIMME_RUNTIMES_JSON must be an object');
    }
    foreach ($decoded as $name => $pin) {
        if (!in_array($name, [
            'php', 'composer', 'node', 'npm', 'pnpm', 'yarn', 'bun',
            'python', 'ruby', 'go', 'java',
        ], true) || !is_array($pin) || array_is_list($pin) ||
            array_diff(array_keys($pin), ['provider', 'version']) !== [] ||
            count($pin) !== 2 ||
            !in_array($pin['provider'] ?? null, ['system', 'mise', 'bundled'], true) ||
            !is_string($pin['version'] ?? null) ||
            !preg_match('/^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][a-zA-Z0-9.-]+)?$/', $pin['version'])) {
            throw new \RuntimeException('Unsafe runtime declaration');
        }
    }
    return $decoded;
}

function configured_php_extensions(): array
{
    $decoded = json_decode(getenv('GIMME_PHP_EXTENSIONS_JSON') ?: '[]', true, flags: JSON_THROW_ON_ERROR);
    if (!is_array($decoded) || !array_is_list($decoded) || count($decoded) > 64) {
        throw new \RuntimeException('GIMME_PHP_EXTENSIONS_JSON must be a bounded list');
    }
    foreach ($decoded as $extension) {
        if (!is_string($extension) || !preg_match('/^[a-z][a-z0-9_]{0,47}$/', $extension)) {
            throw new \RuntimeException('Unsafe PHP extension name');
        }
    }
    return $decoded;
}

function configured_resources(): array
{
    $raw = getenv('GIMME_RESOURCES_JSON') ?: '{}';
    $decoded = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
    if (!is_array($decoded) || (array_is_list($decoded) && trim($raw) !== '{}')) {
        throw new \RuntimeException('GIMME_RESOURCES_JSON must be an object');
    }
    foreach ($decoded as $binding => $resource) {
        if (!in_array($binding, ['database', 'cache'], true) || !is_array($resource) ||
            array_diff(array_keys($resource), ['target', 'kind', 'provider', 'version']) !== [] ||
            count($resource) !== 4 ||
            !is_string($resource['target'] ?? null) ||
            !preg_match('/^[a-z][a-z0-9-]{0,31}$/', $resource['target']) ||
            !in_array($resource['kind'] ?? null, ['postgres', 'valkey'], true) ||
            ($binding === 'database' && $resource['kind'] !== 'postgres') ||
            ($binding === 'cache' && $resource['kind'] !== 'valkey') ||
            ($resource['provider'] ?? null) !== 'target_local' ||
            !is_string($resource['version'] ?? null) ||
            !preg_match('/^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][a-zA-Z0-9.-]+)?$/', $resource['version'])) {
            throw new \RuntimeException('Unsafe resource declaration');
        }
    }
    return $decoded;
}

function runtime_command(array $runtimes, array $names, string $appsRoot, array $command): string
{
    $mise = [];
    foreach ($names as $name) {
        $pin = $runtimes[$name] ?? null;
        if (!is_array($pin)) {
            throw new \RuntimeException("Missing runtime pin for {$name}");
        }
        if ($pin['provider'] === 'mise') {
            $mise[] = "{$name}@{$pin['version']}";
        }
    }
    $rendered = implode(' ', array_map('escapeshellarg', $command));
    if ($mise === []) {
        return $rendered;
    }
    return 'MISE_DATA_DIR=' . escapeshellarg("{$appsRoot}/.gimme/mise") .
        ' mise exec ' . implode(' ', array_map('escapeshellarg', $mise)) . ' -- ' . $rendered;
}

function configured_php_binary(): string
{
    $php = configured_runtimes()['php'] ?? null;
    if (!is_array($php) || $php['provider'] !== 'system') {
        throw new \RuntimeException('A system PHP runtime pin is required');
    }
    $parts = explode('.', $php['version']);
    if (count($parts) < 2) {
        throw new \RuntimeException('PHP version must include major and minor');
    }
    return "/usr/bin/php{$parts[0]}.{$parts[1]}";
}

function configured_mise_version(): ?string
{
    $version = getenv('GIMME_MISE_VERSION') ?: '';
    if ($version === '') {
        return null;
    }
    if (!preg_match('/^[0-9]+(?:\.[0-9]+){1,3}$/', $version)) {
        throw new \RuntimeException('Unsafe mise version');
    }
    return $version;
}

function configured_environment_values(): array
{
    $raw = getenv('GIMME_VARIABLES_JSON') ?: '{}';
    $decoded = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
    if (!is_array($decoded) ||
        (array_is_list($decoded) && trim($raw) !== '{}') ||
        count($decoded) > 128) {
        throw new \RuntimeException('GIMME_VARIABLES_JSON must be a bounded object');
    }
    foreach ($decoded as $key => $value) {
        if (!is_string($key) || !preg_match('/^[A-Z][A-Z0-9_]{0,63}$/', $key) ||
            !is_string($value) || strlen($value) > 4096 || preg_match('/[\x00\r\n]/', $value)) {
            throw new \RuntimeException('Unsafe declared environment value');
        }
    }
    return $decoded;
}

function configured_health(): ?array
{
    $decoded = json_decode(required_env('GIMME_HEALTH_JSON'), true, flags: JSON_THROW_ON_ERROR);
    if ($decoded === null) {
        return null;
    }
    if (!is_array($decoded) || array_is_list($decoded)) {
        throw new \RuntimeException('GIMME_HEALTH_JSON must be an object or null');
    }
    $expectedKeys = [
        'attempts',
        'delay_seconds',
        'expected_status',
        'path',
        'timeout_seconds',
    ];
    $actualKeys = array_keys($decoded);
    sort($actualKeys);
    if ($actualKeys !== $expectedKeys) {
        throw new \RuntimeException('Health configuration has unknown or missing fields');
    }
    $path = $decoded['path'];
    $segments = is_string($path) && $path !== '/'
        ? explode('/', trim($path, '/'))
        : [];
    if (!is_string($path) || strlen($path) > 200 || !str_starts_with($path, '/') ||
        str_starts_with($path, '//') || strpbrk($path, '?#%\\') !== false ||
        array_filter(
            $segments,
            static fn (string $segment): bool => $segment === '' ||
                in_array($segment, ['.', '..'], true) ||
                !preg_match('/^[a-zA-Z0-9._~-]+$/', $segment),
        )) {
        throw new \RuntimeException('Unsafe health path');
    }
    foreach ([
        'expected_status' => [200, 399],
        'attempts' => [1, 30],
        'delay_seconds' => [0, 30],
        'timeout_seconds' => [1, 30],
    ] as $key => [$minimum, $maximum]) {
        $value = $decoded[$key];
        if (!is_int($value) || $value < $minimum || $value > $maximum) {
            throw new \RuntimeException("Invalid health configuration field {$key}");
        }
    }
    return $decoded;
}
