<?php

declare(strict_types=1);

// One long-lived Laravel cache client, as a queue worker or Horizon supervisor holds it. The
// harness restarts the server underneath it and drives the phases over stdin and stdout.

require __DIR__.'/common.php';

use Illuminate\Cache\RedisStore;
use Illuminate\Cache\Repository as CacheRepository;
use Illuminate\Config\Repository as ConfigRepository;
use Illuminate\Container\Container;
use Illuminate\Foundation\Application;

function say(string $line): void
{
    fwrite(STDOUT, $line."\n");
    fflush(STDOUT);
}

function attempt(CacheRepository $cache, string $key): bool
{
    try {
        $cache->put($key, 'value', 60);

        return $cache->get($key) === 'value';
    } catch (Throwable) {
        return false;
    }
}

function wait_for(string $command): void
{
    expect_value(trim((string) fgets(STDIN)) === $command, "expected {$command}");
}

$app = new Application(__DIR__);
Container::setInstance($app);
$app->instance('config', new ConfigRepository([]));
$manager = cluster_manager($app, required_environment('GIMME_VALKEY_CACHE_PREFIX'));
$cache = new CacheRepository(new RedisStore($manager));

expect_value(attempt($cache, 'before-outage'), 'before-outage');
say('ready');

wait_for('down');
$retries = (int) required_environment('GIMME_VALKEY_RETRIES');
$timeout = (float) required_environment('GIMME_VALKEY_TIMEOUT_SECONDS');
$started = microtime(true);
expect_value(! attempt($cache, 'during-outage'), 'outage-not-observed');
$elapsed = microtime(true) - $started;
// The contract's timeout and bounded retries cap how long one operation can block.
expect_value($elapsed <= $timeout * ($retries + 1) + 5, 'outage-unbounded:'.round($elapsed, 1));
say('outage|bounded');

wait_for('up');
$backoff = (int) required_environment('GIMME_VALKEY_BACKOFF_MS');
$cap = (int) required_environment('GIMME_VALKEY_BACKOFF_CAP_MS');
// The client instance that lost its connection, given the contract's bounded retries.
$same = false;
for ($try = 0; $try < $retries + 1 && ! $same; $try++) {
    usleep(min($cap, $backoff * (2 ** $try)) * 1000);
    $same = attempt($cache, 'after-restart');
}
say('same-client|'.($same ? 'recovered' : 'stuck'));
// Laravel's own reconnect: purging the cached connection makes the next call connect afresh.
$manager->purge('default');
expect_value(attempt($cache, 'after-purge'), 'no-recovery');
say('GIMME_LARAVEL_VALKEY|recovered');
