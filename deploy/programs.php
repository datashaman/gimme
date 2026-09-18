<?php

declare(strict_types=1);

namespace Deployer;

function laravel_candidate_health_script(): string
{
    return <<<'PHP'
$path = getenv('GIMME_HEALTH_PATH');
$host = getenv('GIMME_HEALTH_HOST');
$expected = filter_var(getenv('GIMME_HEALTH_EXPECTED'), FILTER_VALIDATE_INT);
try {
    require getcwd() . '/vendor/autoload.php';
    $app = require getcwd() . '/bootstrap/app.php';
    $kernel = $app->make(\Illuminate\Contracts\Http\Kernel::class);
    $request = \Illuminate\Http\Request::create(
        $path,
        'GET',
        [],
        [],
        [],
        ['HTTP_HOST' => $host, 'HTTPS' => 'on', 'SERVER_PORT' => 443]
    );
    $response = $kernel->handle($request);
    $status = $response->getStatusCode();
    $kernel->terminate($request, $response);
    fwrite(STDOUT, "GIMME_HEALTH_STATUS|{$status}\n");
    exit($status === $expected ? 0 : 1);
} catch (\Throwable) {
    fwrite(STDOUT, "GIMME_HEALTH_STATUS|exception\n");
    exit(1);
}
PHP;
}

function laravel_live_health_script(): string
{
    return <<<'PHP'
$url = getenv('GIMME_HEALTH_URL');
$host = getenv('GIMME_HEALTH_HOST');
$ca = getenv('GIMME_HEALTH_CA');
$expected = filter_var(getenv('GIMME_HEALTH_EXPECTED'), FILTER_VALIDATE_INT);
$timeout = filter_var(getenv('GIMME_HEALTH_TIMEOUT'), FILTER_VALIDATE_INT);
try {
    $handle = curl_init($url);
    if ($handle === false) {
        throw new \RuntimeException('curl initialization failed');
    }
    $options = [
        CURLOPT_CONNECTTIMEOUT => $timeout,
        CURLOPT_FOLLOWLOCATION => false,
        CURLOPT_RESOLVE => ["{$host}:443:127.0.0.1"],
        CURLOPT_RETURNTRANSFER => false,
        CURLOPT_TIMEOUT => $timeout,
        CURLOPT_WRITEFUNCTION => static fn ($curl, string $body): int => strlen($body),
    ];
    if (is_string($ca) && $ca !== '' && is_file($ca)) {
        $options[CURLOPT_CAINFO] = $ca;
    }
    curl_setopt_array($handle, $options);
    $ok = curl_exec($handle);
    $status = curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
    curl_close($handle);
    fwrite(STDOUT, "GIMME_HEALTH_STATUS|{$status}\n");
    exit($ok !== false && $status === $expected ? 0 : 1);
} catch (\Throwable) {
    fwrite(STDOUT, "GIMME_HEALTH_STATUS|exception\n");
    exit(1);
}
PHP;
}

function laravel_environment_reconcile_script(): string
{
    return <<<'PYTHON'
import base64
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

path = Path(sys.argv[1])
updates = json.loads(base64.b64decode(sys.argv[2]).decode())
secret_argument = sys.argv[3] if len(sys.argv) > 3 else "-"
if secret_argument != "-":
    secret_path = Path(secret_argument)
    secret_details = secret_path.lstat()
    if secret_path.is_symlink() or not stat.S_ISREG(secret_details.st_mode):
        raise RuntimeError("refusing to read a non-regular secret document")
    secrets = json.loads(secret_path.read_text())
    if not isinstance(secrets, dict):
        raise RuntimeError("secret document must be an object")
    for key, value in secrets.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RuntimeError("secret document contains an invalid value")
    updates.update(secrets)
manifest = (
    json.loads(base64.b64decode(sys.argv[4]).decode())
    if len(sys.argv) > 4 else []
)
if not isinstance(manifest, list) or len(manifest) > 128:
    raise RuntimeError("secret manifest must be a bounded list")
manifest_path = path.parent / ".gimme-secret-manifest.json"
details = path.lstat()
if path.is_symlink() or not stat.S_ISREG(details.st_mode):
    raise RuntimeError("refusing to reconcile a non-regular environment file")

runtime_keys = {"APP_ENV", "APP_DEBUG"}
seen = set()
rendered = []
runtime_changed = False
original = path.read_text()
for line in original.splitlines():
    key = line.split("=", 1)[0]
    if key not in updates:
        rendered.append(line)
        continue
    if key in seen:
        if key in runtime_keys:
            runtime_changed = True
        continue
    desired = f"{key}={updates[key]}"
    if line != desired and key in runtime_keys:
        runtime_changed = True
    rendered.append(desired)
    seen.add(key)

for key, value in updates.items():
    if key in seen:
        continue
    rendered.append(f"{key}={value}")
    if key in runtime_keys:
        runtime_changed = True

desired_content = "\n".join(rendered) + "\n"
manifest_content = json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
manifest_original = manifest_path.read_text() if manifest_path.is_file() else None
environment_changed = desired_content != original
if not environment_changed and manifest_content == manifest_original:
    print("GIMME_RUNTIME_CHANGED|no")
    print("GIMME_ENVIRONMENT_CHANGED|no")
    raise SystemExit(0)

descriptor, temporary = tempfile.mkstemp(prefix=".env.", dir=path.parent)
temporary_path = Path(temporary)
try:
    with os.fdopen(descriptor, "w") as handle:
        handle.write(desired_content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary_path, 0o600)
    os.chown(temporary_path, details.st_uid, details.st_gid)
    os.replace(temporary_path, path)
except BaseException:
    temporary_path.unlink(missing_ok=True)
    raise

manifest_descriptor, manifest_temporary = tempfile.mkstemp(
    prefix=".gimme-secret-manifest.", dir=path.parent
)
manifest_temporary_path = Path(manifest_temporary)
try:
    with os.fdopen(manifest_descriptor, "w") as handle:
        handle.write(manifest_content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(manifest_temporary_path, 0o600)
    os.chown(manifest_temporary_path, details.st_uid, details.st_gid)
    os.replace(manifest_temporary_path, manifest_path)
except BaseException:
    manifest_temporary_path.unlink(missing_ok=True)
    rollback_descriptor, rollback_temporary = tempfile.mkstemp(prefix=".env.rollback.", dir=path.parent)
    rollback_path = Path(rollback_temporary)
    try:
        with os.fdopen(rollback_descriptor, "w") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(rollback_path, 0o600)
        os.chown(rollback_path, details.st_uid, details.st_gid)
        os.replace(rollback_path, path)
    except BaseException:
        rollback_path.unlink(missing_ok=True)
    raise

print("GIMME_RUNTIME_CHANGED|" + ("yes" if runtime_changed else "no"))
print("GIMME_ENVIRONMENT_CHANGED|" + ("yes" if environment_changed else "no"))
PYTHON;
}

function assert_laravel_configuration_health(
    array $health,
    string $siteHost,
    string $appsRoot,
): void {
    foreach ($health as $probe) {
        if (!in_array('live', $probe['phases'], true)) {
            continue;
        }
        $expected = $probe['expected_status'];
        $command =
            'GIMME_HEALTH_URL=' . escapeshellarg("https://{$siteHost}{$probe['path']}") . ' ' .
            'GIMME_HEALTH_HOST=' . escapeshellarg($siteHost) . ' ' .
            'GIMME_HEALTH_CA=' . escapeshellarg("{$appsRoot}/.caddy-local-root.crt") . ' ' .
            'GIMME_HEALTH_EXPECTED=' . escapeshellarg((string) $expected) . ' ' .
            'GIMME_HEALTH_TIMEOUT=' . escapeshellarg((string) $probe['timeout_seconds']) . ' ' .
            '{{bin/php}} -d display_errors=0 -r %health_script% 2>/dev/null || true';
        for ($attempt = 1; $attempt <= $probe['attempts']; $attempt++) {
            $output = run($command, secrets: [
                'health_script' => escapeshellarg(laravel_live_health_script()),
            ]);
            if (trim($output) === "GIMME_HEALTH_STATUS|{$expected}") {
                continue 2;
            }
            if ($attempt < $probe['attempts'] && $probe['delay_seconds'] > 0) {
                run('/usr/bin/sleep ' . escapeshellarg((string) $probe['delay_seconds']));
            }
        }
        throw new \RuntimeException('Configuration health verification failed');
    }
}
