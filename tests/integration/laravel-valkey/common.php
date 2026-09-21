<?php

declare(strict_types=1);

require __DIR__.'/vendor/autoload.php';

use Illuminate\Container\Container;
use Illuminate\Redis\RedisManager;

function required_environment(string $name): string
{
    $value = getenv($name);
    if ($value === false || $value === '') {
        throw new RuntimeException("missing {$name}");
    }

    return $value;
}

function expect_value(bool $condition, string $message): void
{
    if (! $condition) {
        throw new RuntimeException($message);
    }
}

function cluster_manager(Container $app, string $prefix): RedisManager
{
    $node = [
        'scheme' => required_environment('GIMME_VALKEY_SCHEME'),
        'host' => required_environment('GIMME_VALKEY_HOST'),
        'port' => (int) required_environment('GIMME_VALKEY_PORT'),
        'username' => required_environment('GIMME_VALKEY_USERNAME'),
        'password' => required_environment('GIMME_VALKEY_PASSWORD'),
        'ssl' => [
            'cafile' => required_environment('GIMME_VALKEY_CA_FILE'),
            'verify_peer' => true,
            'verify_peer_name' => true,
        ],
    ];

    return new RedisManager($app, 'predis', [
        'options' => [
            'cluster' => 'redis',
            'parameters' => [
                'timeout' => (float) required_environment('GIMME_VALKEY_TIMEOUT_SECONDS'),
            ],
        ],
        'clusters' => [
            'default' => [$node],
            'horizon' => [$node],
            'options' => ['cluster' => 'redis', 'prefix' => $prefix],
        ],
    ]);
}
