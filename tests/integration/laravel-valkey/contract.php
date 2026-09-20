<?php

declare(strict_types=1);

require __DIR__.'/vendor/autoload.php';

use Illuminate\Cache\RedisStore;
use Illuminate\Cache\Repository as CacheRepository;
use Illuminate\Config\Repository as ConfigRepository;
use Illuminate\Container\Container;
use Illuminate\Foundation\Application;
use Illuminate\Queue\RedisQueue;
use Illuminate\Redis\RedisManager;
use Illuminate\Session\CacheBasedSessionHandler;
use Laravel\Horizon\JobPayload;
use Laravel\Horizon\Repositories\RedisJobRepository;

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

expect_value(required_environment('GIMME_VALKEY_CONTRACT') === 'laravel-cluster-v1', 'contract');
expect_value(required_environment('GIMME_VALKEY_CLUSTER') === 'true', 'cluster');
expect_value(required_environment('GIMME_VALKEY_VERIFY_PEER') === 'true', 'verify-peer');
expect_value(required_environment('GIMME_VALKEY_USES') === 'cache,session,queue', 'uses');

$app = new Application(__DIR__);
Container::setInstance($app);
$app->instance('config', new ConfigRepository([
    'horizon' => [
        'prefix' => required_environment('GIMME_VALKEY_HORIZON_PREFIX'),
        'trim' => [
            'recent' => 60, 'pending' => 60, 'completed' => 60,
            'failed' => 60, 'recent_failed' => 60, 'monitored' => 60,
        ],
    ],
]));

// Laravel cache traffic, including its Lua-backed lock implementation.
$cache = new CacheRepository(new RedisStore(
    cluster_manager($app, required_environment('GIMME_VALKEY_CACHE_PREFIX'))
));
expect_value($cache->put('real-cache', 'value', 60), 'cache-put');
expect_value($cache->get('real-cache') === 'value', 'cache-get');
expect_value($cache->increment('counter') === 1, 'cache-increment');
$lock = $cache->lock('lock', 30);
expect_value($lock->get(), 'cache-lock');
$lock->release();

// Laravel's cache-backed session handler, isolated on the session prefix.
$sessionCache = new CacheRepository(new RedisStore(
    cluster_manager($app, required_environment('GIMME_VALKEY_SESSION_PREFIX'))
));
$sessions = new CacheBasedSessionHandler($sessionCache, 60);
expect_value($sessions->write('real-session', 'payload'), 'session-write');
expect_value($sessions->read('real-session') === 'payload', 'session-read');
expect_value($sessions->destroy('real-session'), 'session-destroy');
expect_value($sessions->read('real-session') === '', 'session-delete');

// Laravel's Redis queue executes its production push/pop Lua scripts against the cluster.
$queueManager = cluster_manager($app, required_environment('GIMME_VALKEY_QUEUE_PREFIX'));
$queue = new RedisQueue($queueManager, '{default}', 'default', 60);
$queue->setContainer($app);
$payload = json_encode([
    'id' => 'real-queue-job', 'uuid' => 'real-queue-job',
    'displayName' => 'GimmeRealJob', 'job' => 'GimmeRealJob', 'attempts' => 0, 'data' => [],
], JSON_THROW_ON_ERROR);
expect_value($queue->pushRaw($payload) === 'real-queue-job', 'queue-push');
expect_value($queue->size('{default}') === 1, 'queue-size');
$job = $queue->pop('{default}');
expect_value($job !== null && $job->getRawBody() === $payload, 'queue-pop');
$job->delete();

// Horizon's real Redis repository writes and reads its pending-job indexes and hash.
$horizonManager = cluster_manager($app, required_environment('GIMME_VALKEY_HORIZON_PREFIX'));
$horizon = new RedisJobRepository($horizonManager);
$horizonPayload = new JobPayload(json_encode([
    'id' => 'real-horizon-job', 'uuid' => 'real-horizon-job',
    'displayName' => 'GimmeHorizonJob', 'job' => 'GimmeHorizonJob', 'data' => [],
], JSON_THROW_ON_ERROR));
$horizon->pushed('redis', '{default}', $horizonPayload);
expect_value($horizon->totalRecent() === 1, 'horizon-count');
expect_value($horizon->getRecent()->first()->id === 'real-horizon-job', 'horizon-read');

// Go through Laravel's Redis connection without a prefix to prove the binding ACL itself denies
// both another Deployment namespace and a forbidden keyspace-discovery command.
$unprefixed = cluster_manager($app, '')->connection();
foreach ([
    fn () => $unprefixed->set('{gimme:other}:cache:escape', 'forbidden'),
    fn () => $unprefixed->keys('*'),
] as $denied) {
    try {
        $denied();
        throw new RuntimeException('acl-open');
    } catch (Throwable $error) {
        expect_value(
            str_contains(strtoupper($error->getMessage()), 'NOPERM'),
            'acl-denial:'.$error->getMessage()
        );
    }
}

echo "GIMME_LARAVEL_VALKEY|ready\n";
