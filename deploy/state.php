<?php

declare(strict_types=1);

namespace Deployer;

function configured_workers(): ?array
{
    return configured_process_object('GIMME_WORKERS_JSON');
}

function configured_scheduler(): ?array
{
    return configured_process_object('GIMME_SCHEDULER_JSON');
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

function configured_sites(string $appsRoot, string $mdnsName): array
{
    $configured = getenv('GIMME_SITES_JSON');
    if ($configured !== false && $configured !== '') {
        $decoded = json_decode($configured, true, flags: JSON_THROW_ON_ERROR);
        if (!is_array($decoded) || !array_is_list($decoded)) {
            throw new \RuntimeException('GIMME_SITES_JSON must be a list');
        }
        $sites = [];
        foreach ($decoded as $definition) {
            if (!is_array($definition) || !isset($definition['instance']) ||
                !is_string($definition['instance'])) {
                throw new \RuntimeException('Invalid configured deployment site');
            }
            $instance = $definition['instance'];
            unset($definition['instance']);
            $sites[$instance] = $definition;
        }
        ksort($sites);
        return $sites;
    }
    $sites = [];
    foreach (configured_apps() as $name => $definition) {
        $framework = $definition['framework'] ?? 'common';
        $environments = $definition['environments'] ?? [
            'default' => ['branch' => $definition['branch'] ?? 'main'],
        ];
        if (!is_array($environments) || array_is_list($environments) ||
            !array_key_exists('default', $environments)) {
            throw new \RuntimeException("Invalid environments for {$name}");
        }
        foreach ($environments as $environment => $environmentDefinition) {
            if (!is_string($environment) ||
                !preg_match('/^[a-z][a-z0-9-]{0,31}$/', $environment) ||
                !is_array($environmentDefinition)) {
                throw new \RuntimeException("Invalid environment for {$name}");
            }
            $instance = $environment === 'default'
                ? $name
                : "{$name}--{$environment}--" . substr(
                    hash('sha256', "{$name}\0{$environment}"),
                    0,
                    10,
                );
            if ((getenv('GIMME_EXCLUDE_INSTANCE') ?: '') === $instance) {
                continue;
            }
            $deployPath = $environment === 'default'
                ? "{$appsRoot}/{$name}"
                : "{$appsRoot}/{$name}/environments/{$environment}";
            $siteHost = $environment === 'default'
                ? "{$name}.{$mdnsName}.local"
                : "{$environment}.{$name}.{$mdnsName}.local";
            $relativeRoot = match ($framework) {
                'laravel', 'symfony' => 'public',
                'static' => $definition['frontend']['output_dir'] ?? 'dist',
                default => '',
            };
            $documentRoot = "{$deployPath}/current";
            if ($relativeRoot !== '') {
                $documentRoot .= "/{$relativeRoot}";
            }
            $sites[$instance] = [
                'application' => $name,
                'environment' => $environment,
                'framework' => $framework,
                'site_host' => $siteHost,
                'document_root' => $documentRoot,
            ];
        }
    }
    ksort($sites);
    return $sites;
}

function stack_state_write_command(
    string $mode,
    string $statePath,
    string $appsRoot,
    string $hostname,
    string $mdnsName,
    string $remoteUser,
): string {
    if (!in_array($mode, ['stack', 'sites'], true)) {
        throw new \RuntimeException('Invalid stack reconciliation mode');
    }
    $state = json_encode([
        'version' => 1,
        'mode' => $mode,
        'package_manager' => 'apt',
        'packages' => configured_packages(),
        'services' => configured_services(),
        'hostname' => $hostname,
        'mdns_name' => $mdnsName,
        'network_mode' => getenv('GIMME_NETWORK_MODE') ?: 'local_mdns',
        'remote_user' => $remoteUser,
        'apps_root' => $appsRoot,
        'sites' => configured_sites($appsRoot, $mdnsName),
        'mise_version' => configured_mise_version(),
    ], JSON_THROW_ON_ERROR);
    $stateEncoded = escapeshellarg(base64_encode($state));
    $stateDirectory = escapeshellarg(dirname($statePath));
    $quotedStatePath = escapeshellarg($statePath);
    return <<<BASH
set -eu
install -d -m 0700 {$stateDirectory}
temporary={$quotedStatePath}.tmp.\$\$
trap 'rm -f "\$temporary"' EXIT
printf %s {$stateEncoded} | base64 -d > "\$temporary"
chmod 0600 "\$temporary"
mv "\$temporary" {$quotedStatePath}
trap - EXIT
BASH;
}

function process_state_write_command(
    string $statePath,
    string $instance,
    string $appsRoot,
    string $deployPath,
    string $remoteUser,
    ?array $workers,
    ?array $scheduler,
    string $phpBinary,
): string {
    $state = json_encode([
        'version' => 1,
        'application' => $instance,
        'framework' => 'laravel',
        'remote_user' => $remoteUser,
        'apps_root' => $appsRoot,
        'deploy_path' => $deployPath,
        'workers' => $workers,
        'scheduler' => $scheduler,
        'php_binary' => $phpBinary,
    ], JSON_THROW_ON_ERROR);
    $encoded = escapeshellarg(base64_encode($state));
    $directory = escapeshellarg(dirname($statePath));
    $path = escapeshellarg($statePath);
    return <<<BASH
set -eu
install -d -m 0700 {$directory}
temporary={$path}.tmp.\$\$
trap 'rm -f "\$temporary"' EXIT
printf %s {$encoded} | base64 -d > "\$temporary"
chmod 0600 "\$temporary"
mv "\$temporary" {$path}
trap - EXIT
BASH;
}

function recovery_schedule_state_write_command(
    string $statePath,
    string $deployment,
): string {
    if (!preg_match('/^[a-z][a-z0-9-]{0,63}$/', $deployment)) {
        throw new \RuntimeException('Unsafe Recovery Schedule Deployment identity');
    }
    $raw = getenv('GIMME_RECOVERY_SCHEDULE_JSON');
    if ($raw === false || strlen($raw) > 65536) {
        throw new \RuntimeException('Missing or oversized Recovery Schedule authority');
    }
    $state = json_decode($raw, true, flags: JSON_THROW_ON_ERROR);
    if (!is_array($state) || array_is_list($state) ||
        ($state['deployment'] ?? null) !== $deployment) {
        throw new \RuntimeException('Invalid Recovery Schedule authority');
    }
    $encoded = escapeshellarg(base64_encode(json_encode(
        $state,
        JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES,
    )));
    $directory = escapeshellarg(dirname($statePath));
    $path = escapeshellarg($statePath);
    return <<<BASH
set -eu
install -d -m 0700 {$directory}
temporary={$path}.tmp.\$\$
trap 'rm -f "\$temporary"' EXIT
printf %s {$encoded} | base64 -d > "\$temporary"
chmod 0600 "\$temporary"
mv "\$temporary" {$path}
trap - EXIT
BASH;
}

function privileged_helper_source_hashes(): array
{
    $hashes = [];
    foreach ([
        'scripts/gimme-provision-stack', 'scripts/gimme-provision-processes',
        'scripts/gimme-provision-recovery-schedule',
        'scripts/gimme-recovery-maintenance', 'scripts/gimme-postgres-restore-swap',
        'scripts/gimme-recovery-runner', 'scripts/gimme-capture-valkey',
        'src/gimme/target_capture.py',
    ] as $name) {
        $source = file_get_contents(dirname(__DIR__) . "/{$name}");
        if ($source === false) {
            throw new \RuntimeException("Missing Target execution source: {$name}");
        }
        $hashes[$name] = hash('sha256', $source);
    }
    return $hashes;
}

function privileged_helper_policy(
    array $packages,
    array $services,
    string $hostname,
    string $mdnsName,
    string $remoteUser,
    string $appsRoot,
    array $sites,
): string {
    return hash('sha256', json_encode([
        'helper_source_sha256' => privileged_helper_source_hashes(),
        'packages' => $packages,
        'services' => $services,
        'hostname' => $hostname,
        'mdns_name' => $mdnsName,
        'remote_user' => $remoteUser,
        'apps_root' => $appsRoot,
        'sites' => $sites,
    ], JSON_THROW_ON_ERROR));
}
