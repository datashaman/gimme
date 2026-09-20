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

require __DIR__ . '/deploy/configuration.php';
require __DIR__ . '/deploy/programs.php';
require __DIR__ . '/deploy/state.php';

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
$environmentName = getenv('GIMME_ENVIRONMENT') ?: 'default';
$instance = getenv('GIMME_INSTANCE') ?: $app;
$deployPath = getenv('GIMME_DEPLOY_PATH') ?: ($app === '' ? $appsRoot : "{$appsRoot}/{$app}");
$siteHost = getenv('GIMME_SITE_HOST') ?: ($app === '' ? '' : "{$app}.{$mdnsName}.local");
$health = $app === '' ? [] : configured_health();

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
    !preg_match('#^/(?:[a-zA-Z0-9._-]+/)*[a-zA-Z0-9._-]+$#', $appsRoot) ||
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
if (!preg_match('/^[a-z][a-z0-9-]{0,31}$/', $environmentName) ||
    ($app !== '' && !preg_match('/^[a-z][a-z0-9-]{0,93}$/', $instance))) {
    throw new \RuntimeException('Unsafe environment identity');
}
if ($app !== '') {
    $controlV3 = getenv('GIMME_CONTROL_V3') === '1';
    $expectedInstance = $environmentName === 'default'
        ? $app
        : "{$app}--{$environmentName}--" . substr(
            hash('sha256', "{$app}\0{$environmentName}"),
            0,
            10,
        );
    $expectedDeployPath = $environmentName === 'default'
        ? "{$appsRoot}/{$app}"
        : "{$appsRoot}/{$app}/environments/{$environmentName}";
    $expectedSiteHost = $environmentName === 'default'
        ? "{$app}.{$mdnsName}.local"
        : "{$environmentName}.{$app}.{$mdnsName}.local";
    $safeV3Boundary = $controlV3 &&
        preg_match('/^[a-z][a-z0-9-]{0,93}$/', $instance) &&
        str_starts_with($deployPath, "{$appsRoot}/") &&
        !str_starts_with($deployPath, "{$appsRoot}/.") &&
        $deployPath !== "{$appsRoot}/deployments" &&
        !str_contains($deployPath, '..') &&
        preg_match('#^/(?:[a-zA-Z0-9._-]+/)*[a-zA-Z0-9._-]+$#', $deployPath) &&
        valid_endpoint($siteHost);
    $safeLegacyBoundary = !$controlV3 &&
        $instance === $expectedInstance &&
        $deployPath === $expectedDeployPath &&
        $siteHost === $expectedSiteHost &&
        valid_endpoint($siteHost);
    if (!$safeV3Boundary && !$safeLegacyBoundary) {
        throw new \RuntimeException('Unsafe environment deployment boundary');
    }
}
if ($health !== [] && $framework !== 'laravel') {
    throw new \RuntimeException('Deployment health gates require a Laravel application');
}

host($hostAlias)
    ->setHostname($sshHostname)
    ->setRemoteUser($remoteUser)
    ->setDeployPath($deployPath);

set('keep_releases', (int) env_or_config('GIMME_KEEP_RELEASES', 'server', 'keep_releases'));
set('ssh_multiplexing', true);

if ($app !== '') {
    set('application', $app);
    $repository = required_env('GIMME_REPOSITORY');
    $branch = required_env('GIMME_BRANCH');
    $revision = getenv('GIMME_REVISION') ?: '';
    if (!valid_repository($repository)) {
        throw new \RuntimeException('Unsafe Git repository URL');
    }
    if (!valid_git_branch($branch)) {
        throw new \RuntimeException('Unsafe Git branch name');
    }
    if ($revision !== '' && !preg_match('/^[0-9a-f]{40,64}$/', $revision)) {
        throw new \RuntimeException('Unsafe Git revision');
    }
    set('repository', $repository);
    set('branch', $revision !== '' ? $revision : $branch);
    if ($framework !== 'static') {
        set('shared_files', array_values(array_unique([
            ...get('shared_files', []),
            '.env',
        ])));
        $runtimes = configured_runtimes();
        $php = $runtimes['php'] ?? null;
        $composer = $runtimes['composer'] ?? null;
        if (!is_array($php) || $php['provider'] !== 'system' ||
            !preg_match('/^(\d+)\.(\d+)\./', $php['version'], $phpParts) ||
            !is_array($composer)) {
            throw new \RuntimeException('PHP applications require exact PHP and Composer pins');
        }
        set('php_version', "{$phpParts[1]}.{$phpParts[2]}");
        if ($composer['provider'] !== 'system') {
            throw new \RuntimeException('Composer currently requires the system provider');
        }
        set('composer_version', $composer['version']);
    }
}

$hasFrontend = getenv('GIMME_FRONTEND') === '1';
if ($hasFrontend) {
    $packageManager = required_env('GIMME_FRONTEND_PACKAGE_MANAGER');
    $buildScript = required_env('GIMME_FRONTEND_BUILD_SCRIPT');
    $outputDir = required_env('GIMME_FRONTEND_OUTPUT_DIR');
    if (!in_array($packageManager, ['npm', 'pnpm', 'yarn', 'bun'], true)) {
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

    $runtimes = configured_runtimes();
    $managerPin = $runtimes[$packageManager] ?? null;
    $nodePin = $runtimes['node'] ?? null;
    if (!is_array($managerPin) || ($packageManager !== 'bun' && !is_array($nodePin))) {
        throw new \RuntimeException('Deployment must declare exact frontend runtime pins');
    }
    $managerVersion = $managerPin['version'];
    $nodeVersion = $nodePin['version'] ?? null;
    $runtimeNames = $packageManager === 'bun'
        ? ['bun']
        : ['node', $packageManager];
    $install = match ($packageManager) {
        'npm' => ['npm', 'ci', '--no-audit', '--no-fund'],
        'pnpm' => ['pnpm', 'install', '--frozen-lockfile'],
        'yarn' => ((int) explode('.', $managerVersion)[0]) === 1
            ? ['yarn', 'install', '--frozen-lockfile', '--non-interactive']
            : ['yarn', 'install', '--immutable'],
        'bun' => ['bun', 'install', '--frozen-lockfile'],
    };
    $lockfiles = match ($packageManager) {
        'npm' => ['package-lock.json'],
        'pnpm' => ['pnpm-lock.yaml'],
        'yarn' => ['yarn.lock'],
        'bun' => ['bun.lock', 'bun.lockb'],
    };
    $allLockfiles = ['package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'bun.lock', 'bun.lockb'];
    $quotedAll = implode(' ', array_map('escapeshellarg', $allLockfiles));
    $quotedAllowed = implode(' ', array_map('escapeshellarg', $lockfiles));
    $installCommand = implode(' ', array_map('escapeshellarg', $install));
    task('gimme:frontend:install', function () use (
        $packageManager,
        $managerVersion,
        $nodeVersion,
        $quotedAll,
        $quotedAllowed,
        $installCommand,
        $runtimeNames,
        $runtimes,
        $appsRoot,
    ): void {
        $script = 'set -eu; cd {{release_path}}; ' .
            'command -v ' . escapeshellarg($packageManager) . ' >/dev/null; ' .
            'test "$(' . escapeshellarg($packageManager) . ' --version)" = ' .
            escapeshellarg($managerVersion) . '; ';
        if ($packageManager !== 'bun') {
            $script .= 'command -v node >/dev/null; ' .
                'test "$(node --version | sed s/^v//)" = ' . escapeshellarg((string) $nodeVersion) . '; ';
        }
        $script .= 'test "$(for f in ' . $quotedAll .
            '; do test -f "$f" && printf x; done)" = x; ' .
            'found=0; for f in ' . $quotedAllowed .
            '; do test -f "$f" && found=$((found + 1)); done; test "$found" = 1; ' .
            $installCommand;
        run(runtime_command($runtimes, $runtimeNames, $appsRoot, [
            'bash', '-c', $script,
        ]));
    });
    task('gimme:frontend:build', function () use (
        $packageManager, $buildScript, $runtimeNames, $runtimes, $appsRoot,
    ): void {
        run('cd {{release_path}} && ' . runtime_command(
            $runtimes, $runtimeNames, $appsRoot,
            [$packageManager, 'run', $buildScript],
        ));
    });
    task('gimme:frontend', [
        'gimme:frontend:install',
        'gimme:frontend:build',
    ]);
    after('deploy:update_code', 'gimme:frontend:install');
    after('deploy:vendors', 'gimme:frontend:build');
}

task('gimme:resolve-revision', function (): void {
    $repository = required_env('GIMME_REPOSITORY');
    $branch = required_env('GIMME_BRANCH');
    $kind = getenv('GIMME_SOURCE_KIND') ?: 'branch';
    if (!in_array($kind, ['branch', 'tag'], true)) {
        throw new \RuntimeException('Revision resolution requires a branch or tag source');
    }
    $reference = $kind === 'branch' ? "refs/heads/{$branch}" : "refs/tags/{$branch}";
    $references = escapeshellarg($reference);
    if ($kind === 'tag') {
        $references .= ' ' . escapeshellarg("{$reference}^{}");
    }
    $output = run(
        'GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND=' .
        escapeshellarg('ssh -o StrictHostKeyChecking=accept-new') .
        ' git ls-remote --exit-code ' . escapeshellarg($repository) . ' ' .
        $references . ' 2>/dev/null'
    );
    $lines = array_values(array_filter(explode("\n", trim($output))));
    $selected = end($lines);
    $revision = is_string($selected) ? (preg_split('/\s+/', $selected)[0] ?? '') : '';
    if (!preg_match('/^[0-9a-f]{40,64}$/', $revision)) {
        throw new \RuntimeException('Remote branch did not resolve to one Git revision');
    }
    writeln("GIMME_REVISION|{$revision}");
});

task('gimme:preflight:runtimes', function () use ($appsRoot): void {
    $runtimes = configured_runtimes();
    $miseVersion = getenv('GIMME_MISE_VERSION') ?: '';
    if (array_filter($runtimes, static fn (array $pin): bool => $pin['provider'] === 'mise')) {
        $actualMise = trim(run('mise --version'));
        if (!preg_match('/(?:^|\s)v?([0-9]+(?:\.[0-9]+){1,3})/', $actualMise, $match) ||
            $match[1] !== $miseVersion) {
            throw new \RuntimeException('mise version does not match desired state');
        }
        writeln("GIMME_RUNTIME|mise|{$match[1]}");
    }
    foreach ($runtimes as $name => $pin) {
        $names = $pin['provider'] === 'bundled' && $name === 'npm' ? ['node'] : [$name];
        $command = match ($name) {
            'php' => ['/usr/bin/php' . implode('.', array_slice(explode('.', $pin['version']), 0, 2)), '-r', 'echo PHP_VERSION;'],
            'composer' => ['composer', '--version', '--no-ansi'],
            'node' => ['node', '--version'],
            'npm', 'pnpm', 'yarn', 'bun', 'python', 'ruby', 'java' => [$name, '--version'],
            'go' => ['go', 'version'],
            default => throw new \RuntimeException('Unsupported runtime'),
        };
        $output = trim(run(runtime_command($runtimes, $names, $appsRoot, $command)));
        if ($name === 'composer' && preg_match('/Composer version ([^ ]+)/', $output, $match)) {
            $actual = $match[1];
        } elseif ($name === 'ruby' && preg_match('/ruby ([^ ]+)/', $output, $match)) {
            $actual = $match[1];
        } elseif ($name === 'python' && preg_match('/Python ([^ ]+)/', $output, $match)) {
            $actual = $match[1];
        } elseif ($name === 'go' && preg_match('/go version go([^ ]+)/', $output, $match)) {
            $actual = $match[1];
        } elseif ($name === 'java' && preg_match('/^[^ ]+ ([^ ]+)/', $output, $match)) {
            $actual = $match[1];
        } else {
            $actual = ltrim($output, 'v');
        }
        if ($actual !== $pin['version']) {
            throw new \RuntimeException("{$name} version does not match desired state");
        }
        writeln("GIMME_RUNTIME|{$name}|{$actual}");
    }
    $php = $runtimes['php'] ?? null;
    if (is_array($php)) {
        $phpBinary = '/usr/bin/php' . implode('.', array_slice(explode('.', $php['version']), 0, 2));
        foreach (configured_php_extensions() as $extension) {
            run($phpBinary . ' -r ' . escapeshellarg(
                "exit(extension_loaded('{$extension}') ? 0 : 1);",
            ));
            writeln("GIMME_PHP_EXTENSION|{$extension}|ready");
        }
    }
    foreach (configured_resources() as $resource) {
        $kind = $resource['kind'];
        $command = $kind === 'postgres' ? 'psql --version' : 'valkey-server --version';
        $output = trim(run($command));
        if (!preg_match('/(?:PostgreSQL\)? |v=)([0-9]+(?:\.[0-9]+){0,3})/', $output, $match) ||
            $match[1] !== $resource['version']) {
            throw new \RuntimeException("{$kind} version does not match desired state");
        }
        writeln("GIMME_RESOURCE|{$kind}|{$match[1]}");
    }
});

task('gimme:preflight:frontend', ['gimme:preflight:runtimes']);

task('gimme:provision:runtimes', function () use ($appsRoot): void {
    $runtimes = configured_runtimes();
    $pins = [];
    foreach ($runtimes as $name => $pin) {
        if ($pin['provider'] === 'mise') {
            $pins[] = "{$name}@{$pin['version']}";
        }
    }
    if ($pins === []) {
        writeln('GIMME_RUNTIME_PROVISION|not_required');
        return;
    }
    $miseVersion = getenv('GIMME_MISE_VERSION') ?: '';
    $actualMise = trim(run('mise --version'));
    if (!preg_match('/(?:^|\s)v?([0-9]+(?:\.[0-9]+){1,3})/', $actualMise, $match) ||
        $match[1] !== $miseVersion) {
        throw new \RuntimeException('mise version does not match desired state');
    }
    run('mkdir -p ' . escapeshellarg("{$appsRoot}/.gimme/mise"));
    foreach ($pins as $pin) {
        run('MISE_DATA_DIR=' . escapeshellarg("{$appsRoot}/.gimme/mise") .
            ' mise install ' . escapeshellarg($pin));
        writeln("GIMME_RUNTIME_PROVISION|{$pin}|ready");
    }
});

task('gimme:current-revision', function (): void {
    $currentPath = get('deploy_path') . '/current';
    if (!test('[ -d ' . escapeshellarg($currentPath) . ' ]')) {
        throw new \RuntimeException('Current release is missing');
    }
    $revision = trim(run(
        'cd ' . escapeshellarg($currentPath) . ' && git rev-parse --verify HEAD'
    ));
    if (!preg_match('/^[0-9a-f]{40,64}$/', $revision)) {
        throw new \RuntimeException('Current release has no valid Git revision');
    }
    writeln("GIMME_CURRENT_REVISION|{$revision}");
});

task('gimme:diagnose:deployment', function () use (
    $app,
    $framework,
    $health,
    $siteHost,
    $appsRoot,
): void {
    if ($app === '') {
        throw new \RuntimeException('Deployment diagnostics require an application');
    }
    if ($framework !== 'laravel') {
        throw new \RuntimeException('Deployment diagnostics currently require Laravel');
    }
    $currentPath = get('deploy_path') . '/current';
    if (!test('[ -d ' . escapeshellarg($currentPath) . ' ]')) {
        writeln('GIMME_DIAGNOSTIC|release|missing|none');
        return;
    }
    $revision = trim(run(
        'cd ' . escapeshellarg($currentPath) . ' && git rev-parse --verify HEAD'
    ));
    $revisionDetail = preg_match('/^[0-9a-f]{40,64}$/', $revision) ? $revision : 'invalid';
    writeln("GIMME_DIAGNOSTIC|release|ready|{$revisionDetail}");

    $php = escapeshellarg(configured_php_binary());
    $artisan = escapeshellarg("{$currentPath}/artisan");
    $artisanStatus = trim(run(
        "if {$php} {$artisan} --no-interaction about >/dev/null 2>&1; " .
        "then printf ready; else printf failed; fi"
    ));
    writeln("GIMME_DIAGNOSTIC|artisan|{$artisanStatus}|none");
    $databaseStatus = trim(run(
        "if {$php} {$artisan} --no-interaction migrate:status >/dev/null 2>&1; " .
        "then printf ready; else printf failed; fi"
    ));
    writeln("GIMME_DIAGNOSTIC|database|{$databaseStatus}|none");

    $writable = test('[ -w ' . escapeshellarg("{$currentPath}/storage") . ' ]') &&
        test('[ -w ' . escapeshellarg("{$currentPath}/bootstrap/cache") . ' ]');
    writeln('GIMME_DIAGNOSTIC|writable|' . ($writable ? 'ready' : 'failed') . '|none');
    $versionParts = explode('.', configured_runtimes()['php']['version']);
    $fpmSocket = "/run/php/php{$versionParts[0]}.{$versionParts[1]}-fpm.sock";
    writeln('GIMME_DIAGNOSTIC|php-fpm|' .
        (test('[ -S ' . escapeshellarg($fpmSocket) . ' ]') ? 'ready' : 'missing') . '|none');

    $logSummary = trim(run(
        'cd ' . escapeshellarg($currentPath) . ' && ' .
        "latest=\$(find storage/logs -maxdepth 1 -type f -name 'laravel*.log' " .
        "-printf '%T@ %p\\n' 2>/dev/null | sort -n | tail -n 1 | cut -d' ' -f2-); " .
        "if [ -z \"\$latest\" ]; then printf missing; else " .
        "bytes=\$(stat -c %s \"\$latest\"); now=\$(date +%s); " .
        "modified=\$(stat -c %Y \"\$latest\"); " .
        "errors=\$(tail -n 200 \"\$latest\" | " .
        "grep -Ec '\\.(ERROR|CRITICAL|ALERT|EMERGENCY):' || true); " .
        "printf 'bytes=%s,age_seconds=%s,errors=%s' \"\$bytes\" " .
        "\"\$((now-modified))\" \"\$errors\"; fi"
    ));
    if ($logSummary === 'missing') {
        writeln('GIMME_DIAGNOSTIC|laravel-log|ready|none');
    } elseif (preg_match('/^bytes=\d+,age_seconds=\d+,errors=\d+$/', $logSummary)) {
        writeln("GIMME_DIAGNOSTIC|laravel-log|ready|{$logSummary}");
    } else {
        writeln('GIMME_DIAGNOSTIC|laravel-log|failed|invalid-metadata');
    }

    foreach ($health as $probe) {
        if (!in_array('live', $probe['phases'], true)) {
            continue;
        }
        $url = "https://{$siteHost}{$probe['path']}";
        $command =
            'GIMME_HEALTH_URL=' . escapeshellarg($url) . ' ' .
            'GIMME_HEALTH_HOST=' . escapeshellarg($siteHost) . ' ' .
            'GIMME_HEALTH_CA=' . escapeshellarg("{$appsRoot}/.caddy-local-root.crt") . ' ' .
            'GIMME_HEALTH_EXPECTED=' . escapeshellarg((string) $probe['expected_status']) . ' ' .
            'GIMME_HEALTH_TIMEOUT=' . escapeshellarg((string) $probe['timeout_seconds']) . ' ' .
            '{{bin/php}} -d display_errors=0 -r %health_script% 2>/dev/null || true';
        $output = trim(run($command, secrets: [
            'health_script' => escapeshellarg(laravel_live_health_script()),
        ]));
        $status = str_starts_with($output, 'GIMME_HEALTH_STATUS|')
            ? substr($output, strlen('GIMME_HEALTH_STATUS|'))
            : 'exception';
        $ready = $status === (string) $probe['expected_status'];
        $detail = preg_match('/^(?:[1-5][0-9]{2}|exception)$/', $status)
            ? "status={$status}"
            : 'status=exception';
        writeln("GIMME_DIAGNOSTIC|health.{$probe['name']}|" .
            ($ready ? 'ready' : 'failed') . "|{$detail}");
    }
});

function run_valkey_probe(string $lockPath): void
{
    $config = json_decode(getenv('GIMME_VALKEY_PROBE_JSON') ?: '', true, flags: JSON_THROW_ON_ERROR);
    if (!is_array($config) || !is_string($config['host'] ?? null) ||
        !preg_match('/^[a-zA-Z0-9.-]{1,255}$/', $config['host']) ||
        !is_int($config['port'] ?? null) || $config['port'] < 1 || $config['port'] > 65535) {
        throw new \RuntimeException('Unsafe Valkey probe input');
    }
    try {
        $output = run(
            'printf %s ' . escapeshellarg(base64_encode(valkey_probe_script())) .
            ' | base64 -d | python3 - ' .
            escapeshellarg(get('deploy_path') . '/shared/.env') . ' ' .
            escapeshellarg($lockPath) . ' ' .
            escapeshellarg(base64_encode(json_encode($config, JSON_THROW_ON_ERROR)))
        );
    } catch (\Throwable $failure) {
        throw new \RuntimeException(
            'Valkey activation probe failed; the current release stays live',
            previous: $failure,
        );
    }
    writeln($output);
}

task('gimme:probe:valkey', function (): void {
    run_valkey_probe('{{release_path}}/composer.lock');
});

// The same probe against the live release, for recovery: a restored group has a new endpoint
// and a rotated credential is a new secret version, and neither passes through a deploy.
task('gimme:probe:valkey:current', function (): void {
    $lock = get('deploy_path') . '/current/composer.lock';
    if (!test('[ -f ' . escapeshellarg($lock) . ' ]')) {
        writeln('valkey_probe=skipped_no_release');
        return;
    }
    run_valkey_probe($lock);
});

if ($health !== []) {
    task('gimme:health:candidate', function () use ($health, $siteHost): void {
        $host = $siteHost;
        foreach ($health as $probe) {
            if (!in_array('candidate', $probe['phases'], true)) {
                continue;
            }
            $expected = $probe['expected_status'];
            $command = 'cd {{release_path}} && ' .
                'GIMME_HEALTH_PATH=' . escapeshellarg($probe['path']) . ' ' .
                'GIMME_HEALTH_HOST=' . escapeshellarg($host) . ' ' .
                'GIMME_HEALTH_EXPECTED=' . escapeshellarg((string) $expected) . ' ' .
                '/usr/bin/timeout --signal=TERM ' .
                escapeshellarg((string) $probe['timeout_seconds']) . 's ' .
                '{{bin/php}} -d display_errors=0 -r %health_script% 2>/dev/null || true';
            for ($attempt = 1; $attempt <= $probe['attempts']; $attempt++) {
                $output = run($command, secrets: [
                    'health_script' => escapeshellarg(laravel_candidate_health_script()),
                ]);
                if (trim($output) === "GIMME_HEALTH_STATUS|{$expected}") {
                    writeln("health.candidate.{$probe['name']}=ready attempts={$attempt}");
                    continue 2;
                }
                if ($attempt < $probe['attempts'] && $probe['delay_seconds'] > 0) {
                    run('/usr/bin/sleep ' . escapeshellarg((string) $probe['delay_seconds']));
                }
            }
            throw new \RuntimeException(
                "Candidate health probe {$probe['name']} failed before activation after " .
                "{$probe['attempts']} attempts"
            );
        }
    });

    task('gimme:health:live', function () use (
        $health,
        $siteHost,
        $appsRoot,
    ): void {
        $host = $siteHost;
        foreach ($health as $probe) {
            if (!in_array('live', $probe['phases'], true)) {
                continue;
            }
            $url = "https://{$host}{$probe['path']}";
            $expected = $probe['expected_status'];
            $command =
                'GIMME_HEALTH_URL=' . escapeshellarg($url) . ' ' .
                'GIMME_HEALTH_HOST=' . escapeshellarg($host) . ' ' .
                'GIMME_HEALTH_CA=' . escapeshellarg("{$appsRoot}/.caddy-local-root.crt") . ' ' .
                'GIMME_HEALTH_EXPECTED=' . escapeshellarg((string) $expected) . ' ' .
                'GIMME_HEALTH_TIMEOUT=' .
                escapeshellarg((string) $probe['timeout_seconds']) . ' ' .
                '{{bin/php}} -d display_errors=0 -r %health_script% 2>/dev/null || true';
            for ($attempt = 1; $attempt <= $probe['attempts']; $attempt++) {
                $output = run($command, secrets: [
                    'health_script' => escapeshellarg(laravel_live_health_script()),
                ]);
                if (trim($output) === "GIMME_HEALTH_STATUS|{$expected}") {
                    writeln("health.live.{$probe['name']}=ready attempts={$attempt}");
                    continue 2;
                }
                if ($attempt < $probe['attempts'] && $probe['delay_seconds'] > 0) {
                    run('/usr/bin/sleep ' . escapeshellarg((string) $probe['delay_seconds']));
                }
            }
            try {
                invoke('rollback');
            } catch (\Throwable $rollbackError) {
                throw new \RuntimeException(
                    "Live health probe {$probe['name']} failed and automatic rollback also failed",
                    previous: $rollbackError,
                );
            }
            throw new \RuntimeException(
                "Live health probe {$probe['name']} failed; the previous release was restored"
            );
        }
    });

    before('deploy:symlink', 'gimme:health:candidate');
    after('deploy:symlink', 'gimme:health:live');
}

// Deployer runs the most recently registered `before` hook first, so the probe is registered
// after the health gate and runs ahead of it.
if (getenv('GIMME_VALKEY_PROBE_JSON')) {
    before('deploy:symlink', 'gimme:probe:valkey');
}

task('gimme:inspect', function () use (
    $appsRoot,
    $hostname,
    $mdnsName,
    $remoteUser,
): void {
    $script = <<<'BASH'
set -eu
. /etc/os-release
printf 'os=%s\nversion=%s\nhostname=%s\n' "$NAME" "$VERSION_ID" "$(hostname)"
if sudo -n true >/dev/null 2>&1; then
    printf 'passwordless_sudo=yes\n'
else
    printf 'passwordless_sudo=no\n'
fi
BASH;
    writeln(run("bash -c " . escapeshellarg($script)));
    $policy = privileged_helper_policy(
        configured_packages(),
        configured_services(),
        $hostname,
        $mdnsName,
        $remoteUser,
        $appsRoot,
        configured_sites($appsRoot, $mdnsName),
    );
    $policyLine = escapeshellarg("# GIMME_POLICY_ID={$policy}");
    $helperReady = test(
        '[ -x /usr/local/sbin/gimme-provision-stack ] && ' .
        '[ -x /usr/local/sbin/gimme-provision-processes ] && ' .
        '[ -x /usr/local/sbin/gimme-provision-recovery-schedule ] && ' .
        '[ -x /usr/local/sbin/gimme-recovery-maintenance ] && ' .
        '[ -x /usr/local/sbin/gimme-postgres-restore-swap ] && ' .
        '[ -x /usr/local/libexec/gimme-recovery-runner ] && ' .
        '[ -f /usr/local/libexec/gimme_target_capture.py ] && ' .
        '[ -x /usr/local/libexec/gimme-capture-valkey ] && ' .
        "grep -Fqx {$policyLine} /usr/local/sbin/gimme-provision-stack && " .
        "grep -Fqx {$policyLine} /usr/local/sbin/gimme-provision-processes && " .
        "grep -Fqx {$policyLine} /usr/local/sbin/gimme-provision-recovery-schedule && " .
        "grep -Fqx {$policyLine} /usr/local/sbin/gimme-recovery-maintenance && " .
        "grep -Fqx {$policyLine} /usr/local/sbin/gimme-postgres-restore-swap && " .
        'sudo -n -l /usr/local/sbin/gimme-provision-stack >/dev/null 2>&1 && ' .
        'sudo -n -l /usr/local/sbin/gimme-provision-processes >/dev/null 2>&1 && ' .
        'sudo -n -l /usr/local/sbin/gimme-provision-recovery-schedule probe ' .
        '>/dev/null 2>&1 && ' .
        'sudo -n -l /usr/local/sbin/gimme-recovery-maintenance enter probe probe ' .
        '>/dev/null 2>&1 && ' .
        'sudo -n -l /usr/local/sbin/gimme-postgres-restore-swap swap probe probe ' .
        '>/dev/null 2>&1'
    );
    writeln('privileged_helper=' . ($helperReady ? 'ready' : 'bootstrap_required'));
    foreach (configured_services() as $service) {
        $state = run(
            'systemctl is-active ' . escapeshellarg($service) . ' 2>/dev/null || true'
        );
        $state = $state === '' ? 'unknown' : $state;
        writeln("service.{$service}={$state}");
    }
    $resolved = run('getent hosts ' . escapeshellarg("{$mdnsName}.local") . ' 2>/dev/null || true');
    writeln('mdns=' . ($resolved === '' ? 'unresolved' : $resolved));
    $configuredAppsRoot = rtrim(
        (string) env_or_config('GIMME_APPS_ROOT', 'server', 'apps_root'), '/'
    );
    foreach (configured_sites($configuredAppsRoot, $mdnsName) as $instance => $site) {
        $framework = $site['framework'];
        $documentRoot = $site['document_root'];
        $siteHost = $site['site_host'];
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
            'systemctl is-active ' . escapeshellarg("gimme-mdns-{$instance}.service") .
            ' 2>/dev/null || true'
        );
        writeln("site.{$instance}.hostname={$siteHost}");
        writeln(
            'site.' . $instance . '.mdns_publisher=' .
            ($publisher === '' ? 'missing' : $publisher)
        );
        writeln(
            'site.' . $instance . '.mdns_local_resolution=' .
            ($localResolution === '' ? 'unavailable' : $localResolution)
        );
        writeln('site.' . $instance . '.mdns_client_resolution=not_observable');
        writeln("site.{$instance}.document_root={$documentRoot}");
        writeln('site.' . $instance . '.index=' . ($index ? 'present' : 'missing'));
        writeln("site.{$instance}.https_status=" . ($http === '' ? 'unreachable' : $http));
        writeln("site.{$instance}.path_permissions=\n{$path}");
        if ($framework === 'laravel') {
            $deployPath = dirname(dirname($documentRoot));
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
            writeln("site.{$instance}.env_keys=\n{$envKeys}");
            writeln("site.{$instance}.laravel_log_files={$laravelLogFiles}");
            writeln("site.{$instance}.runtime_path_permissions=\n{$runtimePaths}");
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
    if ssh-add -l >/dev/null 2>&1; then
        printf 'ssh_agent_identities=available\n'
    elif [ "$?" -eq 1 ]; then
        printf 'ssh_agent_identities=empty\n'
    else
        printf 'ssh_agent_identities=unreachable\n'
    fi
else
    printf 'ssh_agent=missing\n'
    printf 'ssh_agent_identities=unreachable\n'
fi
BASH;
    writeln(run('bash -c ' . escapeshellarg($agentScript)));
    foreach (['node', 'npm', 'pnpm', 'yarn', 'bun'] as $tool) {
        if (!test('command -v ' . escapeshellarg($tool) . ' >/dev/null 2>&1')) {
            writeln("toolchain.{$tool}=missing");
            continue;
        }
        $version = trim(run(escapeshellarg($tool) . ' --version 2>/dev/null'));
        if ($tool === 'node') {
            $version = ltrim($version, 'v');
        }
        if (!preg_match('/^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][a-zA-Z0-9.-]+)?$/', $version)) {
            writeln("toolchain.{$tool}=unrecognized");
            continue;
        }
        writeln("toolchain.{$tool}={$version}");
    }
});

task('gimme:inspect:runtimes', function (): void {
    $script = <<<'BASH'
set -eu
emit() { test -n "$2" && printf 'GIMME_RUNTIME|%s|%s\n' "$1" "$2"; }
if command -v php >/dev/null 2>&1; then emit php "$(php -r 'echo PHP_VERSION;')"; fi
if command -v composer >/dev/null 2>&1; then
    emit composer "$(composer --version --no-ansi | awk '{print $3}')"
fi
if command -v node >/dev/null 2>&1; then emit node "$(node --version | sed 's/^v//')"; fi
for tool in npm pnpm yarn bun; do
    if command -v "$tool" >/dev/null 2>&1; then emit "$tool" "$($tool --version)"; fi
done
if command -v python >/dev/null 2>&1; then emit python "$(python --version | awk '{print $2}')"; fi
if command -v ruby >/dev/null 2>&1; then emit ruby "$(ruby --version | awk '{print $2}')"; fi
if command -v go >/dev/null 2>&1; then
    emit go "$(go version | sed -E 's/.* go([^ ]+).*/\1/')"
fi
if command -v java >/dev/null 2>&1; then
    emit java "$(java --version 2>&1 | head -n 1 | awk '{print $2}')"
fi
if command -v mise >/dev/null 2>&1; then
    emit mise "$(mise --version | sed -E 's/.*v?([0-9]+\.[0-9]+\.[0-9]+).*/\1/')"
fi
if command -v psql >/dev/null 2>&1; then
    emit postgres "$(psql --version | sed -E 's/.* ([0-9]+(\.[0-9]+){0,3}).*/\1/')"
fi
if command -v valkey-server >/dev/null 2>&1; then
    emit valkey "$(valkey-server --version | sed -E 's/.*v=([0-9]+(\.[0-9]+){0,3}).*/\1/')"
fi
BASH;
    writeln(run('bash -c ' . escapeshellarg($script)));
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
        configured_sites($appsRoot, $mdnsName),
    );
    $policyLine = escapeshellarg("# GIMME_POLICY_ID={$policy}");
    $helperReady = test(
        '[ -x /usr/local/sbin/gimme-provision-stack ] && ' .
        '[ -x /usr/local/sbin/gimme-provision-processes ] && ' .
        '[ -x /usr/local/sbin/gimme-provision-recovery-schedule ] && ' .
        '[ -x /usr/local/libexec/gimme-recovery-runner ] && ' .
        '[ -f /usr/local/libexec/gimme_target_capture.py ] && ' .
        '[ -x /usr/local/libexec/gimme-capture-valkey ] && ' .
        "grep -Fqx {$policyLine} /usr/local/sbin/gimme-provision-stack && " .
        "grep -Fqx {$policyLine} /usr/local/sbin/gimme-provision-processes && " .
        "grep -Fqx {$policyLine} /usr/local/sbin/gimme-provision-recovery-schedule && " .
        'sudo -n -l /usr/local/sbin/gimme-provision-stack >/dev/null 2>&1 && ' .
        'sudo -n -l /usr/local/sbin/gimme-provision-processes >/dev/null 2>&1 && ' .
        'sudo -n -l /usr/local/sbin/gimme-provision-recovery-schedule probe ' .
        '>/dev/null 2>&1'
    );
    writeln('GIMME_HELPER|' . ($helperReady ? 'ready' : 'bootstrap_required'));
    $packages = configured_packages();
    $services = configured_services();
    $miseVersion = configured_mise_version();
    foreach ($packages as $package) {
        $quoted = escapeshellarg($package);
        $installed = run(
            "dpkg-query -W -f='\${Version}' {$quoted} 2>/dev/null || printf missing"
        );
        $candidate = run(
            "apt-cache policy {$quoted} | sed -n 's/^  Candidate: //p' | head -n 1"
        );
        if ($candidate === '' || $candidate === '(none)') {
            $candidate = $package === 'mise' && $miseVersion !== null
                ? 'official_ppa'
                : 'unavailable';
        }
        writeln("GIMME_PACKAGE|{$package}|{$installed}|{$candidate}");
    }
    $simulatedPackages = array_values(array_filter(
        $packages,
        static fn (string $package): bool => $package !== 'mise' || $miseVersion === null,
    ));
    run(
        'apt-get --simulate --no-install-recommends install ' .
        implode(' ', array_map('escapeshellarg', $simulatedPackages)) .
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
    $writeState = stack_state_write_command(
        'stack', $statePath, $appsRoot, $hostname, $mdnsName, $remoteUser
    );

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
    $processHelperTemplate = file_get_contents(
        __DIR__ . '/scripts/gimme-provision-processes'
    );
    $scheduleHelperTemplate = file_get_contents(
        __DIR__ . '/scripts/gimme-provision-recovery-schedule'
    );
    $recoveryHelperTemplate = file_get_contents(
        __DIR__ . '/scripts/gimme-recovery-maintenance'
    );
    $postgresSwapHelperTemplate = file_get_contents(
        __DIR__ . '/scripts/gimme-postgres-restore-swap'
    );
    $recoveryRunnerTemplate = file_get_contents(
        __DIR__ . '/scripts/gimme-recovery-runner'
    );
    $targetCapture = file_get_contents(__DIR__ . '/src/gimme/target_capture.py');
    $valkeyCapture = file_get_contents(__DIR__ . '/scripts/gimme-capture-valkey');
    if ($helperTemplate === false || $processHelperTemplate === false ||
        $scheduleHelperTemplate === false ||
        $recoveryHelperTemplate === false || $postgresSwapHelperTemplate === false ||
        $recoveryRunnerTemplate === false || $targetCapture === false ||
        $valkeyCapture === false) {
        throw new \RuntimeException('Missing Target execution source');
    }
    $policy = privileged_helper_policy(
        $packages,
        $services,
        $hostname,
        $mdnsName,
        $remoteUser,
        $appsRoot,
        configured_sites($appsRoot, $mdnsName),
    );
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
        $policy,
        $helper,
    );
    $processHelper = str_replace(
        '"__GIMME_APPS_ROOT__"',
        json_encode($appsRoot, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES),
        $processHelperTemplate,
    );
    $processHelper = str_replace('__GIMME_POLICY_ID__', $policy, $processHelper);
    $scheduleHelper = str_replace(
        '"__GIMME_APPS_ROOT__"',
        json_encode($appsRoot, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES),
        $scheduleHelperTemplate,
    );
    $scheduleHelper = str_replace('__GIMME_POLICY_ID__', $policy, $scheduleHelper);
    $recoveryHelper = str_replace(
        '"__GIMME_APPS_ROOT__"',
        json_encode($appsRoot, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES),
        $recoveryHelperTemplate,
    );
    $recoveryHelper = str_replace('__GIMME_POLICY_ID__', $policy, $recoveryHelper);
    $allowedDatabases = [];
    foreach (configured_sites($appsRoot, $mdnsName) as $instance => $site) {
        $allowedDatabases[$instance] = $site['database_identifier'];
    }
    $postgresSwapHelper = str_replace(
        '"__GIMME_APPS_ROOT__"',
        json_encode($appsRoot, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES),
        $postgresSwapHelperTemplate,
    );
    $postgresSwapHelper = str_replace(
        '"__GIMME_ALLOWED_DATABASES_JSON__"',
        json_encode(json_encode($allowedDatabases, JSON_THROW_ON_ERROR), JSON_THROW_ON_ERROR),
        $postgresSwapHelper,
    );
    $postgresSwapHelper = str_replace(
        '__GIMME_POLICY_ID__', $policy, $postgresSwapHelper
    );
    $recoveryRunner = str_replace(
        '"__GIMME_APPS_ROOT__"',
        json_encode($appsRoot, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES),
        $recoveryRunnerTemplate,
    );
    $recoveryRunner = str_replace(
        '__GIMME_TARGET_CAPTURE_SHA256__', hash('sha256', $targetCapture), $recoveryRunner
    );
    $recoveryRunner = str_replace(
        '__GIMME_VALKEY_CAPTURE_SHA256__', hash('sha256', $valkeyCapture), $recoveryRunner
    );
    $scheduleHelper = str_replace(
        '__GIMME_RUNNER_SHA256__', hash('sha256', $recoveryRunner), $scheduleHelper
    );
    $helperEncoded = escapeshellarg(base64_encode($helper));
    $processHelperEncoded = escapeshellarg(base64_encode($processHelper));
    $scheduleHelperEncoded = escapeshellarg(base64_encode($scheduleHelper));
    $recoveryHelperEncoded = escapeshellarg(base64_encode($recoveryHelper));
    $postgresSwapHelperEncoded = escapeshellarg(base64_encode($postgresSwapHelper));
    $recoveryRunnerEncoded = escapeshellarg(base64_encode($recoveryRunner));
    $targetCaptureEncoded = escapeshellarg(base64_encode($targetCapture));
    $valkeyCaptureEncoded = escapeshellarg(base64_encode($valkeyCapture));
    $packageWords = implode(' ', array_map('escapeshellarg', $packages));
    $miseVersion = escapeshellarg(configured_mise_version() ?? '');
    $user = escapeshellarg($remoteUser);
    $sudoers = escapeshellarg(
        "{$remoteUser} ALL=(root) NOPASSWD: /usr/local/sbin/gimme-provision-stack\n" .
        "{$remoteUser} ALL=(root) NOPASSWD: /usr/local/sbin/gimme-provision-processes\n" .
        "{$remoteUser} ALL=(root) NOPASSWD: /usr/local/sbin/gimme-provision-recovery-schedule *\n" .
        "{$remoteUser} ALL=(root) NOPASSWD: /usr/local/sbin/gimme-recovery-maintenance *\n"
        . "{$remoteUser} ALL=(root) NOPASSWD: /usr/local/sbin/gimme-postgres-restore-swap *\n"
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
printf 'GIMME_BOOTSTRAP|preflight|checking installed packages\n'
packages=({$packageWords})
mise_version={$miseVersion}
missing=()
for package in "\${packages[@]}"; do
    if [ "\$package" = mise ]; then
        continue
    fi
    status=\$(dpkg-query -W -f='\${Status}' "\$package" 2>/dev/null || true)
    if [ "\$status" != 'install ok installed' ]; then
        missing+=("\$package")
    fi
done
if (( \${#missing[@]} )); then
    printf 'GIMME_BOOTSTRAP|packages|updating apt metadata for %s missing packages\n' "\${#missing[@]}"
    apt-get update
    printf 'GIMME_BOOTSTRAP|packages|installing required packages\n'
    env DEBIAN_FRONTEND=noninteractive apt-get --no-install-recommends install -y "\${missing[@]}"
else
    printf 'GIMME_BOOTSTRAP|packages|required packages already installed\n'
fi
if [ -n "\$mise_version" ]; then
    printf 'GIMME_BOOTSTRAP|mise|reconciling pinned mise package\n'
    add-apt-repository -y ppa:jdxcode/mise
    apt-get update
    env DEBIAN_FRONTEND=noninteractive apt-get --no-install-recommends install -y mise
fi

printf 'GIMME_BOOTSTRAP|state|writing validated desired state\n'
{$rootWriteState}
printf 'GIMME_BOOTSTRAP|helpers|installing privileged helpers\n'
helper_tmp=\$(mktemp /usr/local/sbin/.gimme-provision-stack.XXXXXX)
process_helper_tmp=\$(mktemp /usr/local/sbin/.gimme-provision-processes.XXXXXX)
schedule_helper_tmp=\$(mktemp /usr/local/sbin/.gimme-provision-recovery-schedule.XXXXXX)
recovery_helper_tmp=\$(mktemp /usr/local/sbin/.gimme-recovery-maintenance.XXXXXX)
postgres_swap_helper_tmp=\$(mktemp /usr/local/sbin/.gimme-postgres-restore-swap.XXXXXX)
install -d -m 0755 /usr/local/libexec
recovery_runner_tmp=\$(mktemp /usr/local/libexec/.gimme-recovery-runner.XXXXXX)
target_capture_tmp=\$(mktemp /usr/local/libexec/.gimme_target_capture.py.XXXXXX)
valkey_capture_tmp=\$(mktemp /usr/local/libexec/.gimme-capture-valkey.XXXXXX)
sudoers_tmp=\$(mktemp /etc/sudoers.d/.gimme-provision-stack.XXXXXX)
trap 'rm -f "\$helper_tmp" "\$process_helper_tmp" "\$schedule_helper_tmp" "\$recovery_helper_tmp" "\$postgres_swap_helper_tmp" "\$recovery_runner_tmp" "\$target_capture_tmp" "\$valkey_capture_tmp" "\$sudoers_tmp"' EXIT
printf %s {$helperEncoded} | base64 -d > "\$helper_tmp"
chown root:root "\$helper_tmp"
chmod 0755 "\$helper_tmp"
printf %s {$processHelperEncoded} | base64 -d > "\$process_helper_tmp"
chown root:root "\$process_helper_tmp"
chmod 0755 "\$process_helper_tmp"
printf %s {$scheduleHelperEncoded} | base64 -d > "\$schedule_helper_tmp"
chown root:root "\$schedule_helper_tmp"
chmod 0755 "\$schedule_helper_tmp"
printf %s {$recoveryHelperEncoded} | base64 -d > "\$recovery_helper_tmp"
chown root:root "\$recovery_helper_tmp"
chmod 0755 "\$recovery_helper_tmp"
printf %s {$postgresSwapHelperEncoded} | base64 -d > "\$postgres_swap_helper_tmp"
chown root:root "\$postgres_swap_helper_tmp"
chmod 0755 "\$postgres_swap_helper_tmp"
printf %s {$recoveryRunnerEncoded} | base64 -d > "\$recovery_runner_tmp"
chown root:root "\$recovery_runner_tmp"
chmod 0755 "\$recovery_runner_tmp"
printf %s {$targetCaptureEncoded} | base64 -d > "\$target_capture_tmp"
chown root:root "\$target_capture_tmp"
chmod 0644 "\$target_capture_tmp"
printf %s {$valkeyCaptureEncoded} | base64 -d > "\$valkey_capture_tmp"
chown root:root "\$valkey_capture_tmp"
chmod 0755 "\$valkey_capture_tmp"
printf %s {$sudoers} > "\$sudoers_tmp"
chown root:root "\$sudoers_tmp"
chmod 0440 "\$sudoers_tmp"
printf 'GIMME_BOOTSTRAP|policy|validating sudo policy\n'
visudo -cf "\$sudoers_tmp"
mv "\$helper_tmp" /usr/local/sbin/gimme-provision-stack
mv "\$process_helper_tmp" /usr/local/sbin/gimme-provision-processes
mv "\$schedule_helper_tmp" /usr/local/sbin/gimme-provision-recovery-schedule
mv "\$recovery_helper_tmp" /usr/local/sbin/gimme-recovery-maintenance
mv "\$postgres_swap_helper_tmp" /usr/local/sbin/gimme-postgres-restore-swap
mv "\$recovery_runner_tmp" /usr/local/libexec/gimme-recovery-runner
mv "\$target_capture_tmp" /usr/local/libexec/gimme_target_capture.py
mv "\$valkey_capture_tmp" /usr/local/libexec/gimme-capture-valkey
mv "\$sudoers_tmp" /etc/sudoers.d/gimme-provision-stack
trap - EXIT
printf 'GIMME_BOOTSTRAP|reconcile|applying target desired state\n'
/usr/local/sbin/gimme-provision-stack
printf 'GIMME_BOOTSTRAP|complete|target bootstrap complete\n'
BASH;
    $sudo = sudo_prefix();
    $localBootstrap = tempnam(sys_get_temp_dir(), 'gimme-bootstrap-');
    if ($localBootstrap === false) {
        throw new \RuntimeException('Could not allocate local bootstrap transfer');
    }
    $remoteBootstrap = '';
    try {
        if (file_put_contents($localBootstrap, $bootstrap) === false ||
            !chmod($localBootstrap, 0600)) {
            throw new \RuntimeException('Could not prepare local bootstrap transfer');
        }
        $remoteBootstrap = trim(run('mktemp /tmp/.gimme-bootstrap.XXXXXX'));
        if (!preg_match('/^\/tmp\/\.gimme-bootstrap\.[A-Za-z0-9]{6}$/', $remoteBootstrap)) {
            throw new \RuntimeException('Unsafe remote bootstrap transfer path');
        }
        upload($localBootstrap, $remoteBootstrap);
        run('chmod 0600 ' . escapeshellarg($remoteBootstrap));
        run(
            "{$sudo} bash " . escapeshellarg($remoteBootstrap),
            forceOutput: true,
            timeout: 1800,
        );
    } finally {
        @unlink($localBootstrap);
        if ($remoteBootstrap !== '') {
            run('rm -f ' . escapeshellarg($remoteBootstrap));
        }
    }
});

task('gimme:reconcile:sites', function () use (
    $appsRoot,
    $hostname,
    $remoteUser,
    $mdnsName,
): void {
    $statePath = "{$appsRoot}/.gimme/stack.json";
    $writeState = stack_state_write_command(
        'sites', $statePath, $appsRoot, $hostname, $mdnsName, $remoteUser
    );
    run('bash -c ' . escapeshellarg($writeState));
    run(
        'sudo -n /usr/local/sbin/gimme-provision-stack',
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

task('gimme:resource:bind-postgres', function (): void {
    $localSecretFile = getenv('GIMME_SECRET_FILE') ?: '';
    if ($localSecretFile === '') {
        throw new \RuntimeException(
            'A secret file is required to bind a managed PostgreSQL resource'
        );
    }
    if (!is_file($localSecretFile) || is_link($localSecretFile)) {
        throw new \RuntimeException('Unsafe local secret transfer file');
    }
    $host = required_env('GIMME_RESOURCE_ENDPOINT');
    $port = required_env('GIMME_RESOURCE_PORT');
    $database = required_env('GIMME_DATABASE_IDENTIFIER');
    if (
        !valid_endpoint($host) ||
        !preg_match('/^[1-9][0-9]{0,4}$/', $port) || (int) $port > 65535 ||
        !preg_match('/^[a-z][a-z0-9_]{0,62}$/', $database)
    ) {
        throw new \RuntimeException('Unsafe managed PostgreSQL binding identity');
    }

    $bundleDigest = required_env('GIMME_RESOURCE_TRUST_BUNDLE_SHA256');
    if (!preg_match('/^[0-9a-f]{64}$/', $bundleDigest)) {
        throw new \RuntimeException('Unsafe trust bundle digest');
    }

    $remoteDirectory = '/tmp/.gimme-resource-bind';
    $suffix = bin2hex(random_bytes(8));
    $remoteSecretFile = "{$remoteDirectory}/.{$suffix}.json";
    $remoteBundleFile = "{$remoteDirectory}/.{$suffix}.pem";
    run('install -d -m 0700 ' . escapeshellarg($remoteDirectory));

    $bindProgram = escapeshellarg(base64_encode(managed_postgres_bind_script()));
    try {
        upload($localSecretFile, $remoteSecretFile);
        upload(__DIR__ . '/deploy/aws-rds-global-bundle.pem', $remoteBundleFile);
        run('chmod 0600 ' . escapeshellarg($remoteSecretFile) . ' ' . escapeshellarg($remoteBundleFile));
        run(
            'printf %s ' . $bindProgram . ' | base64 -d | python3 - ' .
            escapeshellarg($host) . ' ' . escapeshellarg($port) . ' ' .
            escapeshellarg($database) . ' ' . escapeshellarg($remoteSecretFile) . ' ' .
            escapeshellarg($remoteBundleFile) . ' ' . escapeshellarg($bundleDigest),
            timeout: 120,
        );
    } finally {
        run('rm -f ' . escapeshellarg($remoteSecretFile) . ' ' . escapeshellarg($remoteBundleFile));
    }
});

task('gimme:backup:dump-postgres', function () use ($appsRoot): void {
    $database = required_env('GIMME_DATABASE_IDENTIFIER');
    $localPath = required_env('GIMME_BACKUP_LOCAL_PATH');
    if (!preg_match('/^[a-z][a-z0-9_]{0,62}$/', $database)) {
        throw new \RuntimeException('Unsafe database identity');
    }
    $backupDirectory = "{$appsRoot}/.gimme/backups";
    $remotePath = "{$backupDirectory}/." . bin2hex(random_bytes(8)) . '.dump';
    run('install -d -m 0700 ' . escapeshellarg($backupDirectory));
    $sha256 = '';
    $bytes = '';
    try {
        run(
            'pg_dump --format=custom --no-owner --no-privileges --no-acl --role=' .
            escapeshellarg($database) . ' -d ' .
            escapeshellarg($database) . ' -f ' . escapeshellarg($remotePath)
        );
        run('chmod 0600 ' . escapeshellarg($remotePath));
        $sha256 = trim(run('sha256sum ' . escapeshellarg($remotePath) . " | cut -d' ' -f1"));
        $bytes = trim(run('stat -c %s ' . escapeshellarg($remotePath)));
        if (!preg_match('/^[0-9a-f]{64}$/', $sha256) || !preg_match('/^[0-9]{1,15}$/', $bytes)) {
            throw new \RuntimeException('Unsafe backup dump metadata');
        }
        download($remotePath, $localPath);
    } finally {
        run('rm -f ' . escapeshellarg($remotePath));
    }
    writeln("GIMME_BACKUP|{$sha256}|{$bytes}");
});

task('gimme:recovery:inspect-postgres', function () use ($instance): void {
    $database = required_env('GIMME_DATABASE_IDENTIFIER');
    $sourceBytes = required_env('GIMME_RESTORE_SOURCE_BYTES');
    if (!preg_match('/^[a-z][a-z0-9_]{0,62}$/', $database) ||
        !preg_match('/^[0-9]{1,9}$/', $sourceBytes) || (int) $sourceBytes > 536870912) {
        throw new \RuntimeException('Unsafe PostgreSQL recovery identity');
    }
    $query = <<<'SQL'
SELECT CASE WHEN
    EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')
    ) OR EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
    ) OR EXISTS (
        SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%' AND t.typtype IN ('c','d','e','r')
    ) OR EXISTS (
        SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql'
    ) THEN 'nonempty' ELSE 'empty' END
SQL;
    $command = 'result=$(psql --no-psqlrc -Atq -d ' . escapeshellarg($database) .
        ' -c ' . escapeshellarg($query) . ' 2>/dev/null) || ' .
        '{ printf "%s\\n" GIMME_POSTGRES_RESTORE_PREFLIGHT_FAILED >&2; exit 1; }; ' .
        'case "$result" in empty|nonempty) printf "%s\\n" ' .
        '"GIMME_POSTGRES_RESTORE_PREFLIGHT|$result" ;; *) exit 1 ;; esac';
    $output = trim(run('bash -c ' . escapeshellarg($command), timeout: 60));
    writeln($output);
    $capacityOutput = trim(run(
        'sudo -n /usr/local/sbin/gimme-postgres-restore-swap capacity ' .
        escapeshellarg($instance) . ' ' . escapeshellarg($sourceBytes),
        forceOutput: true,
        timeout: 60,
    ));
    if (!in_array($capacityOutput, [
        'GIMME_POSTGRES_RESTORE_CAPACITY|ready',
        'GIMME_POSTGRES_RESTORE_CAPACITY|insufficient',
    ], true)) {
        throw new \RuntimeException('Invalid PostgreSQL restore capacity observation');
    }
    writeln($capacityOutput);
});

task('gimme:recovery:postgres', function () use ($appsRoot, $instance): void {
    $action = required_env('GIMME_POSTGRES_RESTORE_ACTION');
    $request = required_env('GIMME_POSTGRES_RESTORE_REQUEST_ID');
    $database = required_env('GIMME_DATABASE_IDENTIFIER');
    $sha256 = required_env('GIMME_POSTGRES_RESTORE_SHA256');
    $bytes = required_env('GIMME_POSTGRES_RESTORE_BYTES');
    if (!in_array($action, ['prepare', 'swap', 'cleanup'], true) ||
        !preg_match('/^[a-z0-9][a-z0-9-]{0,63}$/', $request) ||
        !preg_match('/^[a-z][a-z0-9_]{0,62}$/', $database) ||
        !preg_match('/^[0-9a-f]{64}$/', $sha256) ||
        !preg_match('/^[0-9]{1,9}$/', $bytes) || (int) $bytes > 536870912) {
        throw new \RuntimeException('Unsafe PostgreSQL restore request');
    }
    $directory = "{$appsRoot}/.gimme/restores/{$instance}";
    $statePath = "{$directory}/{$request}.json";
    $artifactPath = "{$directory}/{$request}.dump";
    run('install -d -m 0700 ' . escapeshellarg($directory));
    if ($action === 'prepare') {
        $localPath = required_env('GIMME_BACKUP_LOCAL_PATH');
        if (is_link($localPath) || !is_file($localPath)) {
            throw new \RuntimeException('Unsafe PostgreSQL restore artifact');
        }
        if (!test('[ -e ' . escapeshellarg($statePath) . ' ]')) {
            $document = json_encode([
                'schema_version' => 1, 'deployment' => $instance,
                'request_id' => $request, 'database' => $database, 'role' => $database,
                'sha256' => $sha256, 'bytes' => (int) $bytes, 'phase' => 'pending',
                'live_oid' => null, 'shadow_oid' => null,
            ], JSON_THROW_ON_ERROR);
            $temporaryStatePath = "{$statePath}.new";
            run(
                'umask 077; printf %s ' . escapeshellarg(base64_encode($document)) .
                ' | base64 -d > ' . escapeshellarg($temporaryStatePath) .
                ' && mv ' . escapeshellarg($temporaryStatePath) . ' ' .
                escapeshellarg($statePath)
            );
        }
        upload($localPath, $artifactPath);
        run('chmod 0600 ' . escapeshellarg($artifactPath));
    }
    $program = file_get_contents(__DIR__ . '/scripts/gimme-restore-postgres');
    if ($program === false) {
        throw new \RuntimeException('Missing PostgreSQL restore program');
    }
    $program = str_replace(
        '"__GIMME_APPS_ROOT__"',
        json_encode($appsRoot, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES),
        $program,
    );
    run(
        'printf %s ' . escapeshellarg(base64_encode($program)) .
        ' | base64 -d | python3 - ' . escapeshellarg($action) . ' ' .
        escapeshellarg($statePath) . ' ' . escapeshellarg($artifactPath) . ' ' .
        escapeshellarg($database) . ' ' . escapeshellarg($sha256) . ' ' .
        escapeshellarg($bytes),
        timeout: 3600,
    );
});

task('gimme:recovery:valkey', function () use ($appsRoot, $instance): void {
    $localPath = required_env('GIMME_BACKUP_LOCAL_PATH');
    $request = required_env('GIMME_VALKEY_RESTORE_REQUEST_ID');
    $sha256 = required_env('GIMME_VALKEY_RESTORE_SHA256');
    $bytes = required_env('GIMME_VALKEY_RESTORE_BYTES');
    $records = required_env('GIMME_VALKEY_RESTORE_RECORDS');
    $cachePrefix = required_env('GIMME_CACHE_PREFIX');
    if (!preg_match('/^[a-z0-9][a-z0-9-]{0,63}$/', $request) ||
        !preg_match('/^[0-9a-f]{64}$/', $sha256) ||
        !preg_match('/^[0-9]{1,9}$/', $bytes) || (int) $bytes > 536870912 ||
        !preg_match('/^[0-9]{1,6}$/', $records) || (int) $records > 100000 ||
        !preg_match(
            '/^(?:[a-zA-Z0-9:_-]{1,160}|\{gimme:[a-z][a-z0-9-]{0,63}\}:)$/',
            $cachePrefix,
        )) {
        throw new \RuntimeException('Unsafe Valkey restore request');
    }
    $probe = json_decode(getenv('GIMME_VALKEY_PROBE_JSON') ?: 'null', true);
    $host = is_array($probe) ? ($probe['host'] ?? null) : '127.0.0.1';
    $port = is_array($probe) ? ($probe['port'] ?? null) : 6379;
    $tls = is_array($probe) ? 'yes' : 'no';
    if (!is_string($host) || !valid_endpoint($host) || !is_int($port) ||
        $port < 1 || $port > 65535) {
        throw new \RuntimeException('Unsafe Valkey restore endpoint');
    }
    $directory = "{$appsRoot}/.gimme/restores/{$instance}";
    $remotePath = "{$directory}/{$request}.valkey";
    $remoteSecret = "{$directory}/{$request}.secret.json";
    $localSecret = getenv('GIMME_SECRET_FILE') ?: '';
    run('install -d -m 0700 ' . escapeshellarg($directory));
    try {
        if (is_link($localPath) || !is_file($localPath)) {
            throw new \RuntimeException('Unsafe Valkey restore artifact');
        }
        upload($localPath, $remotePath);
        run('chmod 0600 ' . escapeshellarg($remotePath));
        if ($localSecret !== '') {
            if (is_link($localSecret) || !is_file($localSecret)) {
                throw new \RuntimeException('Unsafe Valkey restore credential');
            }
            upload($localSecret, $remoteSecret);
            run('chmod 0600 ' . escapeshellarg($remoteSecret));
        }
        $program = file_get_contents(__DIR__ . '/scripts/gimme-restore-valkey');
        if ($program === false) {
            throw new \RuntimeException('Missing Valkey restore program');
        }
        $output = run(
            'printf %s ' . escapeshellarg(base64_encode($program)) .
            ' | base64 -d | python3 - ' . escapeshellarg($remotePath) . ' ' .
            escapeshellarg($cachePrefix) . ' ' . escapeshellarg($host) . ' ' .
            escapeshellarg((string) $port) . ' ' . escapeshellarg($tls) . ' ' .
            escapeshellarg($localSecret === '' ? '-' : $remoteSecret) . ' ' .
            escapeshellarg($sha256) . ' ' . escapeshellarg($bytes) . ' ' .
            escapeshellarg($records),
            timeout: 3600,
        );
        writeln($output);
    } finally {
        run('rm -f ' . escapeshellarg($remotePath) . ' ' . escapeshellarg($remoteSecret));
    }
});

task('gimme:recovery:verify-application', function () use (
    $app,
    $framework,
    $health,
    $siteHost,
): void {
    if ($app === '' || $framework !== 'laravel') {
        throw new \RuntimeException('Restore verification requires a Laravel application');
    }
    $currentPath = get('deploy_path') . '/current';
    $artisan = "{$currentPath}/artisan";
    if (!test('[ -f ' . escapeshellarg($artisan) . ' ]')) {
        throw new \RuntimeException('Restore verification requires a current release');
    }
    $php = escapeshellarg(configured_php_binary());
    $database = trim(run(
        'if ' . $php . ' ' . escapeshellarg($artisan) .
        ' --no-interaction migrate:status >/dev/null 2>&1; ' .
        'then printf ready; else printf failed; fi'
    ));
    if ($database !== 'ready') {
        throw new \RuntimeException('Restored database connectivity verification failed');
    }
    foreach ($health as $probe) {
        if (!in_array('live', $probe['phases'], true)) {
            continue;
        }
        $expected = $probe['expected_status'];
        $command = 'cd ' . escapeshellarg($currentPath) . ' && ' .
            'GIMME_HEALTH_PATH=' . escapeshellarg($probe['path']) . ' ' .
            'GIMME_HEALTH_HOST=' . escapeshellarg($siteHost) . ' ' .
            'GIMME_HEALTH_EXPECTED=' . escapeshellarg((string) $expected) . ' ' .
            '/usr/bin/timeout --signal=TERM ' .
            escapeshellarg((string) $probe['timeout_seconds']) . 's ' .
            '{{bin/php}} -d display_errors=0 -r %health_script% 2>/dev/null || true';
        for ($attempt = 1; $attempt <= $probe['attempts']; $attempt++) {
            $output = trim(run($command, secrets: [
                'health_script' => escapeshellarg(laravel_candidate_health_script()),
            ]));
            if ($output === "GIMME_HEALTH_STATUS|{$expected}") {
                continue 2;
            }
            if ($attempt < $probe['attempts'] && $probe['delay_seconds'] > 0) {
                run('/usr/bin/sleep ' . escapeshellarg(
                    (string) $probe['delay_seconds']
                ));
            }
        }
        throw new \RuntimeException(
            "Private restore health probe {$probe['name']} failed"
        );
    }
    writeln('GIMME_RESTORE_VERIFY|ready');
});

task('gimme:recovery:maintenance', function () use ($appsRoot, $instance): void {
    $action = required_env('GIMME_RECOVERY_ACTION');
    $request = required_env('GIMME_RECOVERY_REQUEST_ID');
    $wait = required_env('GIMME_RECOVERY_QUIESCE_WAIT');
    if (!in_array($action, ['enter', 'resume', 'quiesce', 'exit'], true) ||
        !preg_match('/^[a-z0-9][a-z0-9-]{0,63}$/', $request) ||
        !preg_match('/^(?:[1-9]|[1-9][0-9]|[12][0-9]{2}|300)$/', $wait)) {
        throw new \RuntimeException('Unsafe recovery maintenance request');
    }
    $directory = "{$appsRoot}/.gimme/recovery-requests";
    $path = "{$directory}/{$instance}.json";
    if ($action === 'enter') {
        $document = json_encode([
            'schema_version' => 1,
            'deployment' => $instance,
            'request_id' => $request,
            'quiesce_wait_seconds' => (int) $wait,
        ], JSON_THROW_ON_ERROR);
        $encoded = escapeshellarg(base64_encode($document));
        run('install -d -m 0700 ' . escapeshellarg($directory));
        run(
            'printf %s ' . $encoded . ' | base64 -d > ' . escapeshellarg($path) .
            ' && chmod 0600 ' . escapeshellarg($path)
        );
    }
    run(
        'sudo -n /usr/local/sbin/gimme-recovery-maintenance ' .
        escapeshellarg($action) . ' ' . escapeshellarg($instance) . ' ' .
        escapeshellarg($request),
        forceOutput: true,
        timeout: 900,
    );
    if ($action === 'exit') {
        run('rm -f ' . escapeshellarg($path));
    }
});

task('gimme:backup:capture-valkey', function () use ($appsRoot): void {
    $localPath = required_env('GIMME_BACKUP_LOCAL_PATH');
    $cachePrefix = required_env('GIMME_CACHE_PREFIX');
    if (!preg_match(
        '/^(?:[a-zA-Z0-9:_-]{1,160}|\{gimme:[a-z][a-z0-9-]{0,63}\}:)$/',
        $cachePrefix,
    )) {
        throw new \RuntimeException('Unsafe Valkey recovery prefix');
    }
    $probe = json_decode(getenv('GIMME_VALKEY_PROBE_JSON') ?: 'null', true);
    $host = is_array($probe) ? ($probe['host'] ?? null) : '127.0.0.1';
    $port = is_array($probe) ? ($probe['port'] ?? null) : 6379;
    $tls = is_array($probe) ? 'yes' : 'no';
    if (!is_string($host) || !valid_endpoint($host) || !is_int($port) ||
        $port < 1 || $port > 65535) {
        throw new \RuntimeException('Unsafe Valkey recovery endpoint');
    }
    $directory = "{$appsRoot}/.gimme/backups";
    $suffix = bin2hex(random_bytes(8));
    $remotePath = "{$directory}/.{$suffix}.valkey";
    $remoteSecret = "{$directory}/.{$suffix}.json";
    $localSecret = getenv('GIMME_SECRET_FILE') ?: '';
    run('install -d -m 0700 ' . escapeshellarg($directory));
    try {
        if ($localSecret !== '') {
            if (is_link($localSecret) || !is_file($localSecret)) {
                throw new \RuntimeException('Unsafe Valkey recovery credential');
            }
            upload($localSecret, $remoteSecret);
            run('chmod 0600 ' . escapeshellarg($remoteSecret));
        }
        $program = file_get_contents(__DIR__ . '/scripts/gimme-capture-valkey');
        if ($program === false) {
            throw new \RuntimeException('Missing Valkey recovery program');
        }
        $output = run(
            'printf %s ' . escapeshellarg(base64_encode($program)) .
            ' | base64 -d | python3 - ' . escapeshellarg($remotePath) . ' ' .
            escapeshellarg($cachePrefix) . ' ' . escapeshellarg($host) . ' ' .
            escapeshellarg((string) $port) . ' ' . escapeshellarg($tls) . ' ' .
            escapeshellarg($localSecret === '' ? '-' : $remoteSecret),
            timeout: 1800,
        );
        download($remotePath, $localPath);
        writeln($output);
    } finally {
        run('rm -f ' . escapeshellarg($remotePath) . ' ' . escapeshellarg($remoteSecret));
    }
});

task('gimme:provision:app', function () use (
    $app,
    $siteHost,
    $instance,
    $appsRoot,
    $hostname,
    $health,
    $mdnsName,
    $remoteUser,
): void {
    if ($app === '') {
        throw new \RuntimeException('Application context is required');
    }
    if ((getenv('GIMME_FRAMEWORK') ?: 'common') === 'static') {
        writeln('Static frontend requires no PostgreSQL database or Valkey namespace.');
        return;
    }

    $database = required_env('GIMME_DATABASE_IDENTIFIER');
    $role = $database;
    $cachePrefix = required_env('GIMME_CACHE_PREFIX');
    if (!preg_match('/^[a-z][a-z0-9_]{0,62}$/', $database) ||
        !preg_match('/^[a-zA-Z0-9:_-]{1,160}$/', $cachePrefix)) {
        throw new \RuntimeException('Unsafe environment resource identity');
    }
    $deployPath = get('deploy_path');
    $sharedPath = "{$deployPath}/shared";
    $envPath = "{$sharedPath}/.env";
    $framework = getenv('GIMME_FRAMEWORK') ?: 'common';
    $localSecretFile = getenv('GIMME_SECRET_FILE') ?: '';
    $remoteSecretFile = "{$sharedPath}/.gimme-secrets-" . bin2hex(random_bytes(8)) . '.json';
    $processStatePath = "{$appsRoot}/.gimme/processes/{$instance}.json";
    $hasProcessState = $framework === 'laravel' && test(
        '[ -f ' . escapeshellarg($processStatePath) . ' ]'
    );
    if ($hasProcessState) {
        $policy = privileged_helper_policy(
            configured_packages(),
            configured_services(),
            $hostname,
            $mdnsName,
            $remoteUser,
            $appsRoot,
            configured_sites($appsRoot, $mdnsName),
        );
        $policyLine = escapeshellarg("# GIMME_POLICY_ID={$policy}");
        $helperReady = test(
            '[ -x /usr/local/sbin/gimme-provision-processes ] && ' .
            "grep -Fqx {$policyLine} /usr/local/sbin/gimme-provision-processes && " .
            'sudo -n -l /usr/local/sbin/gimme-provision-processes >/dev/null 2>&1'
        );
        if (!$helperReady) {
            throw new \RuntimeException(
                'Managed processes require the current privileged helper; ' .
                'run the documented interactive stack bootstrap first'
            );
        }
        run('bash -c ' . escapeshellarg(process_state_write_command(
            $processStatePath,
            $instance,
            $appsRoot,
            $deployPath,
            $remoteUser,
            configured_workers(),
            configured_scheduler(),
            configured_php_binary(),
        )));
    }

    run('install -d -m 0700 ' . escapeshellarg($sharedPath));
    run('setfacl -m u:www-data:x ' . escapeshellarg($sharedPath));
    if ($localSecretFile !== '') {
        if (!is_file($localSecretFile) || is_link($localSecretFile)) {
            throw new \RuntimeException('Unsafe local secret transfer file');
        }
        upload($localSecretFile, $remoteSecretFile);
        run('chmod 0600 ' . escapeshellarg($remoteSecretFile));
    }

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
        printf 'REDIS_PREFIX=%s\n' '{$cachePrefix}'
    } > "\$env_path"
fi
chmod 0600 "\$env_path"
BASH;

    if ($framework === 'laravel') {
        $appEnv = required_env('GIMME_APP_ENV');
        $appDebug = required_env('GIMME_APP_DEBUG');
        if (!preg_match('/^[a-z][a-z0-9_-]{0,31}$/', $appEnv) ||
            !in_array($appDebug, ['true', 'false'], true)) {
            throw new \RuntimeException('Unsafe Laravel runtime environment policy');
        }
        $runtimeValues = json_encode([
            ...configured_environment_values(),
            'HORIZON_PREFIX' => horizon_prefix($cachePrefix),
            'APP_ENV' => $appEnv,
            'APP_DEBUG' => $appDebug,
        ], JSON_THROW_ON_ERROR);
        $runtimeProgram = escapeshellarg(base64_encode(laravel_environment_reconcile_script()));
        $runtimeEncoded = escapeshellarg(base64_encode($runtimeValues));
        $secretArgument = escapeshellarg(
            $localSecretFile === '' ? '-' : $remoteSecretFile
        );
        $manifestEncoded = escapeshellarg(base64_encode(json_encode(
            configured_secret_manifest(), JSON_THROW_ON_ERROR
        )));
        $script .= <<<BASH

if ! grep -q '^APP_NAME=' "\$env_path"; then
    printf 'APP_NAME=%s\n' '{$app}' >> "\$env_path"
fi
if ! grep -q '^APP_URL=' "\$env_path"; then
    printf 'APP_URL=https://{$siteHost}\n' >> "\$env_path"
fi
if ! grep -q '^APP_KEY=' "\$env_path"; then
    app_key=\$(openssl rand -base64 32 | tr -d '\n')
    printf 'APP_KEY=base64:%s\n' "\$app_key" >> "\$env_path"
fi
runtime_output=\$(printf %s {$runtimeProgram} | base64 -d | python3 - "\$env_path" {$runtimeEncoded} {$secretArgument} {$manifestEncoded})
printf '%s\n' "\$runtime_output"
BASH;
    }

    $backupToken = bin2hex(random_bytes(8));
    $environmentBackup = "{$sharedPath}/.gimme-env-backup-{$backupToken}";
    $manifestPath = "{$sharedPath}/.gimme-secret-manifest.json";
    $manifestBackup = "{$sharedPath}/.gimme-manifest-backup-{$backupToken}";
    $backup = <<<BASH
umask 077
if [ -L "{$manifestPath}" ]; then
    printf 'Refusing to manage symlinked secret manifest\n' >&2
    exit 1
fi
if [ -f "{$envPath}" ]; then cp -p "{$envPath}" "{$environmentBackup}"; fi
if [ -f "{$manifestPath}" ]; then cp -p "{$manifestPath}" "{$manifestBackup}"; fi
BASH;
    $script = $backup . "\n" . $script;

    try {
        try {
            $resourceOutput = run(
                'flock -w 300 ' . escapeshellarg("{$sharedPath}/.gimme-resource.lock") .
                ' bash -c ' . escapeshellarg($script)
            );
        } finally {
            if ($localSecretFile !== '') {
                run('rm -f ' . escapeshellarg($remoteSecretFile));
            }
        }
        $environmentChanged = str_contains($resourceOutput, 'GIMME_ENVIRONMENT_CHANGED|yes');
        if ($framework === 'laravel') {
            $currentPath = get('deploy_path') . '/current';
            $hasCurrentRelease = test(
                '[ -f ' . escapeshellarg("{$currentPath}/artisan") . ' ]'
            );
            if ($hasCurrentRelease) {
                run(
                    'cd ' . escapeshellarg($currentPath) .
                    ' && {{bin/php}} artisan optimize:clear && {{bin/php}} artisan optimize'
                );
            }
            $unitsChanged = false;
            if ($hasProcessState) {
                $processOutput = run(
                    'sudo -n /usr/local/sbin/gimme-provision-processes ' .
                    escapeshellarg($instance),
                    forceOutput: true,
                    timeout: 1800,
                );
                $unitsChanged = str_contains($processOutput, 'process.units_changed=yes');
            }
            if ($hasCurrentRelease && $environmentChanged && !$unitsChanged) {
                invoke('gimme:restart:workers');
            }
            if ($hasCurrentRelease) {
                assert_laravel_configuration_health($health, $siteHost, $appsRoot);
            }
        }
    } catch (\Throwable) {
        $restore =
            'set -eu; ' .
            'if [ -f ' . escapeshellarg($environmentBackup) . ' ]; then ' .
            'mv -f ' . escapeshellarg($environmentBackup) . ' ' . escapeshellarg($envPath) .
            '; else rm -f ' . escapeshellarg($envPath) . '; fi; ' .
            'if [ -f ' . escapeshellarg($manifestBackup) . ' ]; then ' .
            'mv -f ' . escapeshellarg($manifestBackup) . ' ' . escapeshellarg($manifestPath) .
            '; else rm -f ' . escapeshellarg($manifestPath) . '; fi';
        try {
            run(
                'flock -w 300 ' . escapeshellarg("{$sharedPath}/.gimme-resource.lock") .
                ' bash -c ' . escapeshellarg($restore)
            );
            if ($framework === 'laravel') {
                $currentPath = get('deploy_path') . '/current';
                $hasCurrentRelease = test(
                    '[ -f ' . escapeshellarg("{$currentPath}/artisan") . ' ]'
                );
                if ($hasCurrentRelease) {
                    run(
                        'cd ' . escapeshellarg($currentPath) .
                        ' && {{bin/php}} artisan optimize:clear && {{bin/php}} artisan optimize'
                    );
                }
                if ($hasProcessState) {
                    run(
                        'sudo -n /usr/local/sbin/gimme-provision-processes ' .
                        escapeshellarg($instance),
                        forceOutput: true,
                        timeout: 1800,
                    );
                } elseif ($hasCurrentRelease) {
                    invoke('gimme:restart:workers');
                }
                if ($hasCurrentRelease) {
                    assert_laravel_configuration_health($health, $siteHost, $appsRoot);
                }
            }
        } catch (\Throwable) {
            throw new \RuntimeException(
                'Deployment environment activation failed and rollback is degraded'
            );
        }
        throw new \RuntimeException(
            'Deployment environment activation failed; the prior protected state was restored'
        );
    } finally {
        run('rm -f ' . escapeshellarg($environmentBackup) . ' ' . escapeshellarg($manifestBackup));
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
        configured_php_binary(),
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

task('gimme:preflight:processes', function () use (
    $app,
    $appsRoot,
    $hostname,
    $mdnsName,
    $remoteUser,
): void {
    if ($app === '' || (getenv('GIMME_FRAMEWORK') ?: 'common') !== 'laravel') {
        throw new \RuntimeException('Process management requires a Laravel application');
    }
    $policy = privileged_helper_policy(
        configured_packages(),
        configured_services(),
        $hostname,
        $mdnsName,
        $remoteUser,
        $appsRoot,
        configured_sites($appsRoot, $mdnsName),
    );
    $policyLine = escapeshellarg("# GIMME_POLICY_ID={$policy}");
    $helperReady = test(
        '[ -x /usr/local/sbin/gimme-provision-processes ] && ' .
        "grep -Fqx {$policyLine} /usr/local/sbin/gimme-provision-processes && " .
        'sudo -n -l /usr/local/sbin/gimme-provision-processes >/dev/null 2>&1'
    );
    $currentPath = get('deploy_path') . '/current';
    $currentReady = test('[ -f ' . escapeshellarg("{$currentPath}/artisan") . ' ]');
    $workers = configured_workers();
    $workersEnabled = is_array($workers) && ($workers['enabled'] ?? null) === true;
    $horizonRequired = $workersEnabled && ($workers['driver'] ?? null) === 'horizon';
    $pcntlReady = !$workersEnabled || test(
        escapeshellarg(configured_php_binary()) . " -r 'exit(extension_loaded(\"pcntl\") ? 0 : 1);'"
    );
    $posixReady = !$horizonRequired || test(
        escapeshellarg(configured_php_binary()) . " -r 'exit(extension_loaded(\"posix\") ? 0 : 1);'"
    );
    $horizonReady = !$horizonRequired || test(
        '[ -d ' . escapeshellarg("{$currentPath}/vendor/laravel/horizon") . ' ] && ' .
        '[ -f ' . escapeshellarg("{$currentPath}/config/horizon.php") . ' ]'
    );

    writeln('GIMME_PROCESS_HELPER|' . ($helperReady ? 'ready' : 'bootstrap_required'));
    writeln('GIMME_CURRENT_RELEASE|' . ($currentReady ? 'ready' : 'missing'));
    writeln(
        'GIMME_PCNTL|' . (!$workersEnabled ? 'not_required' : ($pcntlReady ? 'ready' : 'missing'))
    );
    writeln(
        'GIMME_POSIX|' . (!$horizonRequired ? 'not_required' : ($posixReady ? 'ready' : 'missing'))
    );
    writeln(
        'GIMME_HORIZON|' .
        (!$horizonRequired ? 'not_required' : ($horizonReady ? 'ready' : 'missing'))
    );
});

task('gimme:provision:processes', function () use ($app, $instance, $appsRoot, $remoteUser): void {
    if ($app === '' || (getenv('GIMME_FRAMEWORK') ?: 'common') !== 'laravel') {
        throw new \RuntimeException('Process management requires a Laravel application');
    }
    $workers = configured_workers();
    if (is_array($workers) && ($workers['enabled'] ?? null) === true &&
        ($workers['driver'] ?? null) === 'horizon') {
        $cachePrefix = required_env('GIMME_CACHE_PREFIX');
        $deployPath = get('deploy_path');
        $envPath = "{$deployPath}/shared/.env";
        $currentPath = "{$deployPath}/current";
        $horizonPrefix = horizon_prefix($cachePrefix);
        $quotedEnvPath = escapeshellarg($envPath);
        $configureRedis = <<<BASH
set -eu
env_path={$quotedEnvPath}
if [ -L "\$env_path" ] || [ ! -f "\$env_path" ]; then
    printf 'Horizon requires a regular shared environment file\n' >&2
    exit 1
fi
if grep -q '^QUEUE_CONNECTION=' "\$env_path"; then
    sed -i 's/^QUEUE_CONNECTION=.*/QUEUE_CONNECTION=redis/' "\$env_path"
else
    printf '\nQUEUE_CONNECTION=redis\n' >> "\$env_path"
fi
if grep -q '^HORIZON_PREFIX=' "\$env_path"; then
    sed -i 's|^HORIZON_PREFIX=.*|HORIZON_PREFIX={$horizonPrefix}|' "\$env_path"
else
    printf 'HORIZON_PREFIX=%s\n' '{$horizonPrefix}' >> "\$env_path"
fi
chmod 0600 "\$env_path"
BASH;
        run('bash -c ' . escapeshellarg($configureRedis));
        run(
            'cd ' . escapeshellarg($currentPath) .
            ' && ' . escapeshellarg(configured_php_binary()) .
            ' artisan --no-interaction config:clear'
        );
    }
    $statePath = "{$appsRoot}/.gimme/processes/{$instance}.json";
    run('bash -c ' . escapeshellarg(process_state_write_command(
        $statePath,
        $instance,
        $appsRoot,
        get('deploy_path'),
        $remoteUser,
        $workers,
        configured_scheduler(),
        configured_php_binary(),
    )));
    run(
        'sudo -n /usr/local/sbin/gimme-provision-processes ' . escapeshellarg($instance),
        forceOutput: true,
        timeout: 1800,
    );
});

task('gimme:processes:status', function () use ($app, $instance): void {
    if ($app === '' || (getenv('GIMME_FRAMEWORK') ?: 'common') !== 'laravel') {
        throw new \RuntimeException('Process management requires a Laravel application');
    }
    $status = static function (string $unit): string {
        if (!preg_match('/^gimme-[a-z0-9@.-]+\.(?:service|timer)$/', $unit)) {
            throw new \RuntimeException('Unsafe process unit name');
        }
        $properties = run(
            'systemctl show --no-pager ' . escapeshellarg($unit) .
            ' --property=LoadState,LoadError,ActiveState,SubState,MainPID,NRestarts ' .
            '2>/dev/null || true'
        );
        if ($properties === '') {
            return 'missing';
        }
        $result = str_replace("\n", ',', $properties);
        if (str_contains($properties, 'LoadState=bad-setting')) {
            $unitPath = '/etc/systemd/system/' . $unit;
            $verification = run(
                '/usr/bin/systemd-analyze verify ' . escapeshellarg($unitPath) .
                ' 2>&1 || true'
            );
            $verification = preg_replace('/\s+/', ' ', trim($verification)) ?? '';
            $result .= ',VerifyError=' . substr($verification, 0, 2000);
        }
        return $result;
    };
    $workers = configured_workers();
    if (!is_array($workers) || ($workers['enabled'] ?? null) !== true) {
        writeln('process.worker=disabled');
    } elseif (($workers['driver'] ?? null) === 'queue') {
        $processes = $workers['processes'] ?? null;
        if (!is_int($processes) || $processes < 1 || $processes > 16) {
            throw new \RuntimeException('Invalid queue worker process count');
        }
        for ($index = 1; $index <= $processes; $index++) {
            $unit = "gimme-worker-{$instance}@{$index}.service";
            writeln("process.worker.{$index}=" . $status($unit));
        }
    } elseif (($workers['driver'] ?? null) === 'horizon') {
        writeln('process.horizon=' . $status("gimme-horizon-{$instance}.service"));
    } else {
        throw new \RuntimeException('Invalid worker driver');
    }
    $scheduler = configured_scheduler();
    if (is_array($scheduler) && ($scheduler['enabled'] ?? null) === true) {
        writeln('process.scheduler=' . $status("gimme-scheduler-{$instance}.timer"));
    } else {
        writeln('process.scheduler=disabled');
    }
});

task('gimme:recovery:schedule-status', function () use ($app): void {
    if ($app === '') {
        throw new \RuntimeException('Recovery Schedule status requires a Deployment');
    }
    $deployment = required_env('GIMME_DEPLOYMENT');
    if (!preg_match('/^[a-z][a-z0-9-]{0,63}$/', $deployment)) {
        throw new \RuntimeException('Unsafe Deployment identity');
    }
    $unit = "gimme-recovery-{$deployment}.timer";
    if (!preg_match('/^gimme-recovery-[a-z][a-z0-9-]{0,63}\.timer$/', $unit)) {
        throw new \RuntimeException('Unsafe Recovery Schedule unit name');
    }
    $loadState = trim(run(
        'systemctl show --no-pager ' . escapeshellarg($unit) .
        ' --property=LoadState --value 2>/dev/null || true'
    ));
    if ($loadState === '' || $loadState === 'not-found') {
        writeln('GIMME_RECOVERY_TIMER|missing|inactive');
        return;
    }
    if (!in_array($loadState, ['loaded', 'masked'], true)) {
        throw new \RuntimeException('Unexpected Recovery Schedule unit state');
    }
    $enabled = test('systemctl is-enabled --quiet ' . escapeshellarg($unit));
    $active = test('systemctl is-active --quiet ' . escapeshellarg($unit));
    writeln(
        'GIMME_RECOVERY_TIMER|' . ($enabled ? 'enabled' : 'disabled') .
        '|' . ($active ? 'active' : 'inactive')
    );
    $status = trim(run(
        '/usr/local/libexec/gimme-recovery-runner status ' . escapeshellarg($deployment) .
        ' 2>/dev/null || true'
    ));
    if ($status !== '' && preg_match(
        '/^GIMME_RECOVERY_STATUS\|[A-Za-z0-9+\/=]{1,24576}$/', $status
    )) {
        writeln($status);
    }
});

task('gimme:recovery:runtime-status', function (): void {
    $version = trim(run(
        "/usr/bin/python3 -c " . escapeshellarg(
            'import boto3; print(boto3.__version__, end="")'
        ) . ' 2>/dev/null'
    ));
    if (!preg_match('/^[0-9]+(?:\.[0-9]+){1,3}$/', $version)) {
        throw new \RuntimeException('Recovery Schedule boto3 runtime is unavailable');
    }
    writeln("GIMME_RECOVERY_RUNTIME|boto3|{$version}");
});

task('gimme:recovery:schedule-reconcile', function () use (
    $app,
    $appsRoot,
    $hostname,
    $mdnsName,
    $remoteUser,
): void {
    if ($app === '') {
        throw new \RuntimeException('Recovery Schedule reconciliation requires a Deployment');
    }
    $deployment = required_env('GIMME_DEPLOYMENT');
    if (!preg_match('/^[a-z][a-z0-9-]{0,63}$/', $deployment)) {
        throw new \RuntimeException('Unsafe Recovery Schedule Deployment identity');
    }
    $authority = json_decode(
        required_env('GIMME_RECOVERY_SCHEDULE_JSON'),
        true,
        flags: JSON_THROW_ON_ERROR,
    );
    $cadenceKind = is_array($authority) && is_array($authority['cadence'] ?? null)
        ? ($authority['cadence']['kind'] ?? null)
        : null;
    if (!in_array($cadenceKind, ['manual', 'hourly', 'daily', 'weekly'], true)) {
        throw new \RuntimeException('Invalid Recovery Schedule cadence authority');
    }
    if ($cadenceKind !== 'manual') {
        invoke('gimme:recovery:runtime-status');
    }
    $policy = privileged_helper_policy(
        configured_packages(),
        configured_services(),
        $hostname,
        $mdnsName,
        $remoteUser,
        $appsRoot,
        configured_sites($appsRoot, $mdnsName),
    );
    $policyLine = escapeshellarg("# GIMME_POLICY_ID={$policy}");
    if (!test(
        '[ -x /usr/local/sbin/gimme-provision-recovery-schedule ] && ' .
        "grep -Fqx {$policyLine} /usr/local/sbin/gimme-provision-recovery-schedule && " .
        'sudo -n -l /usr/local/sbin/gimme-provision-recovery-schedule ' .
        escapeshellarg($deployment) . ' >/dev/null 2>&1'
    )) {
        throw new \RuntimeException(
            'Recovery Schedule reconciliation requires the current privileged helper'
        );
    }
    $directory = "{$appsRoot}/.gimme/recovery-schedules";
    $statePath = "{$directory}/{$deployment}.json";
    run('bash -c ' . escapeshellarg(recovery_schedule_state_write_command(
        $statePath,
        $deployment,
    )));
    $localCredential = getenv('GIMME_SECRET_FILE') ?: '';
    $remoteCredential = "{$directory}/{$deployment}.credentials";
    $localValkeyCredential = getenv('GIMME_RECOVERY_SCHEDULE_VALKEY_FILE') ?: '';
    $remoteValkeyCredential = "{$directory}/{$deployment}.valkey-credentials";
    try {
        if ($localCredential !== '') {
            if (!is_file($localCredential) || is_link($localCredential)) {
                throw new \RuntimeException('Unsafe local Recovery Schedule credential transfer');
            }
            upload($localCredential, $remoteCredential);
            run('chmod 0600 ' . escapeshellarg($remoteCredential));
        }
        if ($localValkeyCredential !== '') {
            if (!is_file($localValkeyCredential) || is_link($localValkeyCredential)) {
                throw new \RuntimeException(
                    'Unsafe local Recovery Schedule Valkey credential transfer'
                );
            }
            upload($localValkeyCredential, $remoteValkeyCredential);
            run('chmod 0600 ' . escapeshellarg($remoteValkeyCredential));
        }
        run(
            'sudo -n /usr/local/sbin/gimme-provision-recovery-schedule ' .
            escapeshellarg($deployment),
            forceOutput: true,
            timeout: 1800,
        );
    } finally {
        run(
            'rm -f ' . escapeshellarg($remoteCredential) . ' ' .
            escapeshellarg($remoteValkeyCredential)
        );
    }
});

task('gimme:recovery:on-demand', function () use ($app, $appsRoot): void {
    if ($app === '') {
        throw new \RuntimeException('On-demand recovery requires a Deployment');
    }
    $deployment = required_env('GIMME_DEPLOYMENT');
    $request = required_env('GIMME_RECOVERY_ON_DEMAND_REQUEST_ID');
    if (!preg_match('/^[a-z][a-z0-9-]{0,63}$/', $deployment) ||
        !preg_match('/^[a-z0-9][a-z0-9-]{0,63}$/', $request)) {
        throw new \RuntimeException('Unsafe on-demand Recovery identity');
    }
    // Different reviewed requests may arrive from independent controllers. Keep their
    // protected transfer/state material separate; the runner's Deployment lock serializes
    // the actual mutation after each request has loaded its own snapshot.
    $root = "{$appsRoot}/.gimme/recovery-on-demand/{$deployment}/{$request}";
    $credentials = "{$root}/credentials";
    $state = "{$root}/state";
    $capture = "{$state}/capture";
    $authority = "{$credentials}/authority";
    $aws = "{$credentials}/aws";
    $valkey = "{$credentials}/valkey";
    run('install -d -m 0700 ' . escapeshellarg($credentials) . ' ' . escapeshellarg($state));
    run('bash -c ' . escapeshellarg(recovery_schedule_state_write_command(
        $authority,
        $deployment,
    )));
    $localAws = getenv('GIMME_SECRET_FILE') ?: '';
    $localValkey = getenv('GIMME_RECOVERY_SCHEDULE_VALKEY_FILE') ?: '';
    try {
        foreach ([[$localAws, $aws], [$localValkey, $valkey]] as [$local, $remote]) {
            if ($local === '') {
                continue;
            }
            if (!is_file($local) || is_link($local)) {
                throw new \RuntimeException('Unsafe on-demand Recovery credential transfer');
            }
            upload($local, $remote);
            run('chmod 0600 ' . escapeshellarg($remote));
        }
        $output = run(
            'CREDENTIALS_DIRECTORY=' . escapeshellarg($credentials) . ' ' .
            'STATE_DIRECTORY=' . escapeshellarg($state) . ' ' .
            '/usr/local/libexec/gimme-recovery-runner on-demand ' .
            escapeshellarg($deployment) . ' ' . escapeshellarg($request),
            timeout: 7200,
        );
        if (!preg_match('/^GIMME_RECOVERY_RESULT\|[A-Za-z0-9+\/=]{1,24576}$/', $output)) {
            throw new \RuntimeException('Invalid on-demand Recovery result');
        }
        writeln($output);
    } finally {
        run(
            'rm -f ' . escapeshellarg($authority) . ' ' . escapeshellarg($aws) . ' ' .
            escapeshellarg($valkey)
        );
        run(
            'rmdir ' . escapeshellarg($capture) . ' ' . escapeshellarg($credentials) . ' ' .
            escapeshellarg($state) . ' ' . escapeshellarg($root) . ' 2>/dev/null || true'
        );
    }
});

task('gimme:restart:workers', function (): void {
    $workers = configured_workers();
    if (!is_array($workers) || ($workers['enabled'] ?? null) !== true) {
        return;
    }
    $driver = $workers['driver'] ?? null;
    $command = match ($driver) {
        'queue' => 'queue:restart',
        'horizon' => 'horizon:terminate',
        default => throw new \RuntimeException('Invalid worker driver'),
    };
    $currentPath = get('deploy_path') . '/current';
    run(
        'cd ' . escapeshellarg($currentPath) . ' && ' .
        escapeshellarg(configured_php_binary()) . ' artisan --no-interaction ' .
        escapeshellarg($command)
    );
});

task('gimme:remove:environment', function () use (
    $app,
    $environmentName,
    $instance,
    $appsRoot,
    $remoteUser,
): void {
    if ($app === '' || $environmentName === 'default') {
        throw new \RuntimeException('Only non-default environments may be removed');
    }
    $expectedPath = "{$appsRoot}/{$app}/environments/{$environmentName}";
    if (get('deploy_path') !== $expectedPath) {
        throw new \RuntimeException('Environment removal path mismatch');
    }

    $statePath = "{$appsRoot}/.gimme/processes/{$instance}.json";
    run('bash -c ' . escapeshellarg(process_state_write_command(
        $statePath,
        $instance,
        $appsRoot,
        $expectedPath,
        $remoteUser,
        null,
        null,
        configured_php_binary(),
    )));
    run(
        'sudo -n /usr/local/sbin/gimme-provision-processes ' . escapeshellarg($instance),
        forceOutput: true,
        timeout: 1800,
    );

    if ((getenv('GIMME_FRAMEWORK') ?: 'common') !== 'static') {
        $database = required_env('GIMME_DATABASE_IDENTIFIER');
        $cachePrefix = required_env('GIMME_CACHE_PREFIX');
        if (!preg_match('/^[a-z][a-z0-9_]{0,62}$/', $database) ||
            !preg_match('/^[a-zA-Z0-9:_-]{1,160}$/', $cachePrefix)) {
            throw new \RuntimeException('Unsafe environment resource identity');
        }
        $lua = <<<'LUA'
local cursor = "0"
repeat
    local result = redis.call("SCAN", cursor, "MATCH", ARGV[1] .. "*", "COUNT", 500)
    cursor = result[1]
    if #result[2] > 0 then redis.call("UNLINK", unpack(result[2])) end
until cursor == "0"
return 1
LUA;
        run(
            'valkey-cli --raw EVAL ' . escapeshellarg($lua) . ' 0 ' .
            escapeshellarg($cachePrefix) . ' >/dev/null'
        );
        run('dropdb --if-exists --force ' . escapeshellarg($database));
        run('dropuser --if-exists ' . escapeshellarg($database));
    }

    $quotedPath = escapeshellarg($expectedPath);
    $quotedParent = escapeshellarg("{$appsRoot}/{$app}/environments");
    $remove = <<<BASH
set -eu
path={$quotedPath}
expected_parent={$quotedParent}
if [ -L "\$path" ]; then
    printf 'Refusing to remove a symlinked environment root\n' >&2
    exit 1
fi
if [ -e "\$path" ]; then
    resolved_parent=\$(readlink -f -- "\$(dirname -- "\$path")")
    expected_resolved_parent=\$(readlink -f -- "\$expected_parent")
    if [ "\$resolved_parent" != "\$expected_parent" ] || \
       [ "\$expected_resolved_parent" != "\$expected_parent" ]; then
        printf 'Refusing to remove an environment through a symlinked parent\n' >&2
        exit 1
    fi
    rm -rf -- "\$path"
fi
BASH;
    run('bash -c ' . escapeshellarg($remove));
    run('rm -f -- ' . escapeshellarg($statePath));
});

task('gimme:remove:deployment', function () use (
    $app,
    $instance,
    $appsRoot,
    $remoteUser,
): void {
    if ($app === '' || getenv('GIMME_CONTROL_V3') !== '1') {
        throw new \RuntimeException('Deployment removal requires v3 placement context');
    }
    $deployPath = get('deploy_path');
    if (!str_starts_with($deployPath, "{$appsRoot}/") || str_contains($deployPath, '..')) {
        throw new \RuntimeException('Deployment removal path escapes the applications root');
    }
    $statePath = "{$appsRoot}/.gimme/processes/{$instance}.json";
    run('bash -c ' . escapeshellarg(process_state_write_command(
        $statePath,
        $instance,
        $appsRoot,
        $deployPath,
        $remoteUser,
        null,
        null,
        configured_php_binary(),
    )));
    run(
        'sudo -n /usr/local/sbin/gimme-provision-processes ' . escapeshellarg($instance),
        forceOutput: true,
        timeout: 1800,
    );
    if ((getenv('GIMME_FRAMEWORK') ?: 'common') !== 'static') {
        $database = required_env('GIMME_DATABASE_IDENTIFIER');
        $cachePrefix = required_env('GIMME_CACHE_PREFIX');
        $lua = <<<'LUA'
local cursor = "0"
repeat
    local result = redis.call("SCAN", cursor, "MATCH", ARGV[1] .. "*", "COUNT", 500)
    cursor = result[1]
    if #result[2] > 0 then redis.call("UNLINK", unpack(result[2])) end
until cursor == "0"
return 1
LUA;
        run('valkey-cli --raw EVAL ' . escapeshellarg($lua) . ' 0 ' .
            escapeshellarg($cachePrefix) . ' >/dev/null');
        run('dropdb --if-exists --force ' . escapeshellarg($database));
        run('dropuser --if-exists ' . escapeshellarg($database));
    }
    $quotedPath = escapeshellarg($deployPath);
    $quotedRoot = escapeshellarg($appsRoot);
    $remove = <<<BASH
set -eu
path={$quotedPath}
root={$quotedRoot}
if [ -L "\$path" ]; then
    printf 'Refusing to remove a symlinked deployment root\n' >&2
    exit 1
fi
resolved_root=\$(readlink -f -- "\$root")
resolved_parent=\$(readlink -f -- "\$(dirname -- "\$path")")
case "\$resolved_parent/" in "\$resolved_root/"*) ;; *) exit 1 ;; esac
rm -rf -- "\$path"
BASH;
    run('bash -c ' . escapeshellarg($remove));
    run('rm -f -- ' . escapeshellarg($statePath));
});

if ($health === []) {
    after('deploy:symlink', 'gimme:restart:workers');
} else {
    after('gimme:health:live', 'gimme:restart:workers');
}
after('rollback', 'gimme:restart:workers');

task('gimme:service:status', function (): void {
    $service = get('gimme_service');
    $allowed = ['postgresql', 'valkey-server', 'caddy'];
    if (!in_array($service, $allowed, true)) {
        throw new \RuntimeException('Service is not allowlisted');
    }
    writeln(run('systemctl status --no-pager ' . escapeshellarg($service)));
});

after('deploy:failed', 'deploy:unlock');
