# Gimme MCP

Gimme is a local Python MCP server that provisions a fixed Ubuntu host and deploys
registered PHP applications through [Deployer](https://deployer.org/). The desired
stack is declared separately from discovered host state; nothing currently installed
on `devbox.local` is treated as part of the specification.

Stack bootstrap uses a fixed IP endpoint configured by you. Provisioning sets the
machine hostname to `devbox`, installs and starts the manifest-declared
`avahi-daemon`, and therefore advertises `devbox.local` over mDNS. All normal Gimme
operations then connect to `devbox.local`, so SSH configuration such as agent
forwarding is applied to the advertised hostname. Stack repair remains available
through the fixed bootstrap endpoint if mDNS is unavailable.

Registered applications are published as HTTPS sites such as
`https://example-app.devbox.local`. Stack reconciliation writes an explicit Avahi host
alias (mDNS has no wildcard subdomains), generates a Caddy site rooted at the
framework's public directory, validates the complete Caddy configuration, and reloads
Caddy. Aliases sharing the VM's address are kept alive by dedicated systemd services
using `avahi-publish --no-reverse`; static `/etc/avahi/hosts` entries would collide on
the reverse address record. Caddy and PHP-FPM receive traverse-only ACLs on the
application root. `.local` names cannot receive publicly trusted certificates, so
Caddy uses its local CA and exports its public root certificate to
`/srv/gimme/apps/.caddy-local-root.crt`.

The server intentionally has no arbitrary shell, SQL, hostname, package, service, or
path parameters. Application names and Git metadata are validated, every remote
mutation has a separate read-only plan tool, and PostgreSQL passwords are generated
on the remote host without being returned through MCP.

Laravel applications expose only manifest-allowlisted Artisan commands through a
separate `plan_artisan` / `run_artisan` pair. The argument vector is bounded, rejects
control characters and alternate environment selection, crosses the process boundary
as JSON, and is shell-escaped one item at a time. Gimme never accepts PHP code or a
free-form shell command. Artisan output is application-controlled and may itself
contain sensitive data, so commands that dump configuration or secrets should not be
allowlisted.

Repository definitions accept only credential-free HTTPS or SSH Git URLs and safe Git
branch names. Put authentication in SSH agents, deploy keys, or a credential helper;
tokens embedded in repository URLs are rejected because manifests and plans are
browsable MCP resources. The Deployer child process receives an explicit environment
allowlist, so unrelated API tokens and shell-session secrets do not cross that process
boundary.

Remote mutation tools are marked destructive in their MCP annotations. `inspect_host`
reports health, permissions, and application-log file counts, but never returns raw
application or service log content or SSH key identities. For application mDNS,
inspection reports publisher state separately from host-local resolution and marks
client resolution as not observable; a VM failing to resolve its own published alias
does not imply that clients cannot resolve it. Deployment roots are limited to dedicated
subdirectories beneath `/srv`, `/var/www`, `/opt`, or `/home`.
Generated Caddy sites hide environment files and version-control metadata, and Gimme
refuses symlinked application `.env` files while enforcing owner-only permissions.

Standalone static frontends and PHP applications with frontend assets can declare an
npm build. Gimme runs `npm ci` from the committed lockfile followed by one validated
npm script. It does not accept arbitrary install or build commands.

## Install

```bash
uv sync
composer install
cp config/server.example.json config/server.json
cp config/stack.example.json config/stack.json
cp config/apps.example.json config/apps.json
```

Review `config/server.json` and `config/stack.json`. `bootstrap_hostname` is the
fixed address used only by stack preflight/provisioning; `hostname` is the advertised
name used by every normal operation. The stack manifest is an explicit list of
desired APT packages and systemd services, not a claim that they exist in any Ubuntu
release. `plan_stack` queries the reset host's configured package sources for installed
and candidate versions. Any unavailable package blocks provisioning and requires a
manifest change or an explicitly approved repository setup. Preflight also runs an
APT simulation across the complete package set so dependency conflicts or inaccessible
repositories block the plan before any mutation.

APT installs use the same `--no-install-recommends` policy as preflight, stream their
progress, and have a 30-minute command timeout. If a client disconnects or times out,
inspect the remote APT process before retrying; never remove `dpkg` lock files while
their owning process is alive.
`plan_stack` also reports active `apt-get` process IDs and remains unready until those
transactions exit.

Runtime manifests under `config/*.json` are intentionally ignored by Git. The
tracked `*.example.json` files document the schema without publishing host details
or registered private applications.

Before bootstrap, SSH must work non-interactively against the stable IP endpoint:

```bash
ssh <bootstrap-ip>
ssh -o BatchMode=yes <bootstrap-ip> true
```

After provisioning, normal operations use the advertised hostname. Put agent
forwarding and any GitHub identity selection under this SSH host entry:

```sshconfig
Host devbox.local
    HostName devbox.local
    User deployer
    ForwardAgent yes
```

Verify both mDNS and the non-interactive SSH path:

```bash
ssh -o BatchMode=yes devbox.local true
ssh devbox.local 'ssh -T git@github.com'
```

Agent forwarding allows processes running as the remote deployment user to request
signatures from the forwarded key. Use a dedicated, repository-scoped key where
possible, and do not deploy code you have not reviewed while a broad personal agent is
forwarded.

On the first connection after the reset, verify the presented SSH host-key fingerprint
through a trusted channel before accepting it. Gimme keeps strict host-key checking
enabled and will not silently accept or replace a key.

## One-time privilege bootstrap

Provisioning is deliberately non-interactive. The example host requires a sudo
password, and passwords must not be sent through an MCP tool or placed in process
arguments. Choose one of these approaches before calling a provisioning tool:

1. Connect Deployer as a separately secured root SSH principal; or
2. Configure a dedicated deployment user with tightly reviewed sudo permissions; or
3. Run the initial stack task yourself from an interactive terminal, then grant only
   the remaining service-management/database commands needed by your chosen workflow.

For this local development host, run the two bootstrap tasks from an interactive
terminal. The explicit environment flag permits sudo to prompt only for these manual
commands. Stack reconciliation is sent as one root transaction, so it asks for the
sudo password once rather than once per package, service, or configuration file. It
also installs root-owned `/usr/local/sbin/gimme-provision-stack` and
`/usr/local/sbin/gimme-provision-processes` helpers with exact sudoers rules. The
stack helper accepts no arguments; the process helper accepts exactly one validated
registered application name:

```bash
GIMME_INTERACTIVE_SUDO=1 vendor/bin/dep --file=deploy.php gimme:provision:stack devbox
GIMME_INTERACTIVE_SUDO=1 vendor/bin/dep --file=deploy.php gimme:bootstrap:database-admin devbox
```

Run the stack task again after registering an application so its Caddy site and Avahi
alias are reconciled. On a macOS client, trust this VM's Caddy root once:

```bash
scp devbox.local:/srv/gimme/apps/.caddy-local-root.crt /tmp/gimme-caddy-root.crt
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain /tmp/gimme-caddy-root.crt
```

Only trust a CA from a VM you control: possession of its CA private key permits issuing
certificates trusted by that client. The private key remains under Caddy's protected
data directory on the VM; Gimme exports only the public root certificate.

The second task grants the SSH user PostgreSQL `CREATEDB` and `CREATEROLE`, allowing
it to create isolated per-app roles and databases through PostgreSQL's local peer
authentication without ongoing operating-system sudo. These are powerful database
privileges, but they do not grant operating-system root access.

The MCP server never sets `GIMME_INTERACTIVE_SUDO` and disconnects stdin, so stack
and process provisioning fail closed until the helpers are bootstrapped. After that
one-time step,
`provision_stack` writes validated desired state beneath `/srv/gimme/apps/.gimme` and
may invoke only the root-owned stack helper without a password. Process provisioning
writes a separate owner-only document for one registered application and invokes the
process helper with that validated application name. Both helpers verify state
ownership and strictly validate every managed value before writing system files. They
never grant arbitrary shell access. Do not add an unrestricted `NOPASSWD: ALL` rule.

## Run

The default transport is stdio:

```bash
uv run gimme-mcp
```

Example MCP client configuration:

```json
{
  "mcpServers": {
    "gimme": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/gimme",
        "run",
        "gimme-mcp"
      ]
    }
  }
}
```

## Intended workflow

1. `inspect_host`
2. Review `config/stack.json`
3. `plan_stack`; resolve unavailable packages, then approve `provision_stack`
4. `register_app`
5. For concurrent branches, call `register_environment` with an explicit environment
   slug and remote branch
   Existing registrations cannot be silently retargeted: use `plan_update_app` /
   `update_app` or `plan_update_environment` / `update_environment` and review the
   complete before/after definition.
6. `plan_app_resources`, then approve `provision_app_resources` for the selected
   environment; this also reconciles its HTTPS route and mDNS alias
7. Complete framework-specific values in the environment's remote `shared/.env`
8. Configure inherited or environment-specific Laravel health gates as needed
9. `plan_deploy`, review the exact commit and health gates, then approve `deploy_app`
10. Use `list_releases` and `rollback_app` for environment-scoped releases
11. For Laravel maintenance, call `plan_artisan`, review its exact argv, then pass its
   `plan_id` unchanged to `run_artisan`
12. For background execution, call `configure_app_processes`, then
    `plan_app_processes`, approve `provision_app_processes`, and inspect it later with
    `app_process_status`
13. Remove a non-default environment only through `plan_remove_environment` followed by
    `remove_environment` with its exact plan and confirmation phrase

## Resources

Gimme exposes its validated desired state as browsable, read-only JSON resources:

| URI | Content |
| --- | --- |
| `gimme://config/server` | Canonical and bootstrap hostnames, SSH user, and application root |
| `gimme://config/stack` | Desired APT packages and managed systemd services |
| `gimme://config/apps` | Registered application definitions |

It also exposes environment-aware resource templates:

| URI template | Content |
| --- | --- |
| `gimme://apps/{name}` | Registration, deployment path, and HTTPS URL |
| `gimme://apps/{name}/releases` | Live read-only Deployer release history |
| `gimme://apps/{name}/environments` | All registered environments for an application |
| `gimme://apps/{name}/environments/{environment}` | Environment branch, policy, path, and URL |
| `gimme://apps/{name}/environments/{environment}/releases` | Environment release history |

Template parameters are validated as registered application names. They cannot select
arbitrary hosts or filesystem paths. The release resource contacts the configured
host when read; the three manifest resources and application detail are local.

## Concurrent branch environments

One application can expose multiple remote branches concurrently. The reserved `default`
environment preserves the original deployment path and hostname. Additional environments
use isolated Deployer roots and exact Avahi aliases:

| Environment | Deployment root | HTTPS URL |
| --- | --- | --- |
| `default` | `/srv/gimme/apps/example` | `https://example.devbox.local` |
| `feature-x` | `/srv/gimme/apps/example/environments/feature-x` | `https://feature-x.example.devbox.local` |

These are worktree-style environments backed by independent immutable Deployer releases,
not mutable Git worktree checkouts. Branches must exist on the remote. `plan_deploy`
resolves the branch to an exact commit; `deploy_app` rejects the plan if the branch head
or rendered task graph changes before apply.

Each non-default environment receives an isolated PostgreSQL database and role, `.env`,
runtime storage, release history, and Valkey prefix. Workers and the scheduler are disabled
unless explicitly configured. Health checks inherit the application policy by default and
may be overridden or disabled per environment.

Internal database, process, Caddy, and Avahi identifiers include a deterministic digest;
human-readable application and environment names therefore cannot alias one another even
when their hyphens and separators would otherwise normalize to the same value.

```json
{
  "repository": "git@github.com:example/application.git",
  "framework": "laravel",
  "health": {"path": "/up"},
  "environments": {
    "default": {
      "branch": "main",
      "health": "inherit",
      "workers": {"driver": "horizon", "enabled": true},
      "scheduler": {"enabled": true}
    },
    "feature-x": {
      "branch": "feature/x",
      "health": "inherit",
      "workers": null,
      "scheduler": null
    }
  }
}
```

Legacy top-level `branch`, `workers`, and `scheduler` fields remain accepted and are
written back in nested form on the next registry mutation. Exact-plan environment removal
deletes only that environment's route, processes, database, Valkey keys, releases, and
storage; it never modifies the Git branch or repository.

## Deployment health gates

Laravel health checks are opt-in and apply to deployments, not stack provisioning.
Configure them with `configure_app_health` or in the private application manifest:

```json
{
  "health": {
    "path": "/up",
    "expected_status": 200,
    "attempts": 10,
    "delay_seconds": 2,
    "timeout_seconds": 5
  }
}
```

The first gate runs before `deploy:symlink`. It boots the candidate release from its
immutable release directory and dispatches a GET request through Laravel's HTTP kernel.
Only the status code is retained; response bodies and exception details are discarded.
A failure leaves `current` untouched.

After the atomic symlink switch, the second gate sends the same GET through the real
local Caddy HTTPS route and PHP-FPM. DNS is pinned to `127.0.0.1`, TLS is verified with
the VM's exported Caddy CA, redirects are not followed, and response bodies are
discarded. If this gate exhausts its bounded retries, Gimme invokes Deployer's rollback
task to restore the previous non-bad release. Queue workers or Horizon restart only
after the live gate succeeds; rollback also restarts them against the restored release.

Health paths are strict absolute paths without queries, fragments, percent escapes, or
traversal. The endpoint should be side-effect-free and should verify only dependencies
that must be available for the application to serve traffic. Database migrations still
need to be backward-compatible because Laravel's deployment recipe runs them before the
candidate health gate.

## Laravel Artisan commands

Laravel registrations receive a conservative default Artisan allowlist. Override it
per application when a project needs fewer commands or explicitly reviewed custom
commands:

```json
{
  "repository": "git@github.com:example/application.git",
  "framework": "laravel",
  "environments": {"default": {"branch": "main"}},
  "artisan": {
    "allowed_commands": [
      "about",
      "cache:clear",
      "migrate",
      "migrate:status",
      "queue:restart"
    ]
  }
}
```

`tinker`, `db:wipe`, and `migrate:fresh` are deliberately absent from the default.
`run_artisan` executes in the live `current` release, always adds `--no-interaction`,
and requires the exact `plan_id` returned for the same application, command, and
arguments. For example, production migrations can be planned with `command: "migrate"`
and `arguments: ["--force"]`.

## Laravel workers, Horizon, and scheduler

An application may select standard Laravel queue workers or one Horizon master. The
two modes are mutually exclusive, while the scheduler may be enabled independently.
Nothing starts unless the application manifest opts in.
The following fragments belong inside an `environments.<slug>` definition.

Standard workers are explicit, bounded systemd instances:

```json
{
  "workers": {
    "driver": "queue",
    "enabled": true,
    "processes": 2,
    "connection": "database",
    "queues": ["high", "default"],
    "sleep_seconds": 3,
    "tries": 3,
    "timeout_seconds": 60,
    "memory_mb": 256,
    "max_time_seconds": 3600,
    "max_jobs": 0,
    "backoff_seconds": 0
  },
  "scheduler": {"enabled": true}
}
```

Horizon keeps its supervisor counts, queues, balancing, timeouts, and retry policy in
the application's version-controlled `config/horizon.php`; Gimme supervises only the
single Horizon master process:

```json
{
  "workers": {
    "driver": "horizon",
    "enabled": true,
    "stop_wait_seconds": 3600
  },
  "scheduler": {"enabled": true}
}
```

Horizon must already be installed in the application with `composer require
laravel/horizon`, and its production environment must be configured in
`config/horizon.php`. Horizon requires a Redis queue connection; Gimme uses the
provisioned local Valkey service through the Redis protocol, sets the protected
`QUEUE_CONNECTION=redis` value and an environment-specific `HORIZON_PREFIX` during
resource and process provisioning, and clears cached Laravel configuration without
returning environment contents. Multiple environments of the same application therefore
do not share Horizon metadata or control state.

Gimme generates hardened systemd services running as the deployment user, with no
new privileges, an empty capability set, read-only system paths, and write access
limited to the application's shared runtime and bootstrap cache. The scheduler is a
persistent every-minute systemd timer invoking `schedule:run`. Raw journals remain
excluded from MCP responses.

After a successful deployment or rollback symlink switch, Gimme runs `queue:restart`
for standard workers or `horizon:terminate` for Horizon. Active jobs finish gracefully,
then systemd restarts the process against the new `current` release. Ensure worker or
Horizon timeouts remain shorter than the queue connection's `retry_after` setting to
avoid duplicate processing. See the official
[queue worker lifecycle](https://laravel.com/docs/queues#queue-workers) and
[Horizon deployment guidance](https://laravel.com/docs/horizon#deploying-horizon).

Example Vite/static application entry in `config/apps.json`:

```json
{
  "repository": "git@github.com:example/dashboard.git",
  "framework": "static",
  "environments": {"default": {"branch": "main"}},
  "frontend": {
    "package_manager": "npm",
    "build_script": "build",
    "output_dir": "dist"
  }
}
```

Use `out` for a Next.js static export or `.output/public` for a Nuxt-generated static
site. Server-rendered Node processes are not yet supported by this static deployment
recipe.

Each application receives a distinct Valkey key prefix, which prevents accidental
collisions but is not a security boundary. The Valkey listen address and firewall must
be explicitly verified after a fresh install; Gimme does not rely on package defaults.
Applications requiring hostile-tenant isolation should receive dedicated Valkey
instances or containers.

## Verify

```bash
uv run pytest
vendor/bin/dep --file=deploy.php gimme:inspect devbox --no-interaction
```

The manually triggered `Disposable VM integration` GitHub Actions workflow starts from
an ephemeral Ubuntu runner, bootstraps the complete package stack over loopback SSH, and
verifies real PostgreSQL, Valkey-backed environment configuration, Caddy, Avahi, sudo,
and collision-safe site identities. Its test harness refuses to run unless both `CI=true`
and `GIMME_INTEGRATION_DISPOSABLE=1` are set; never bypass that guard on a persistent host.
