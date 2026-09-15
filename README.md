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
also installs a root-owned, argument-free `/usr/local/sbin/gimme-provision-stack`
helper and an exact sudoers rule for that executable:

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
provisioning fails closed until the helper is bootstrapped. After that one-time step,
`provision_stack` writes validated desired state beneath `/srv/gimme/apps/.gimme` and
may invoke only the root-owned helper without a password. The helper accepts no
arguments, checks that state is owned by the invoking sudo user, validates every
package, service, application, framework, and path, and hard-allowlists managed
services. It never grants arbitrary shell access. Do not add an unrestricted
`NOPASSWD: ALL` rule.

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
5. `plan_app_resources`, then approve and call `provision_app_resources`
6. Complete framework-specific values in the remote `shared/.env`
7. `plan_deploy`, then approve and call `deploy_app`
8. Use `list_releases` and `rollback_app` for release operations

## Resources

Gimme exposes its validated desired state as browsable, read-only JSON resources:

| URI | Content |
| --- | --- |
| `gimme://config/server` | Canonical and bootstrap hostnames, SSH user, and application root |
| `gimme://config/stack` | Desired APT packages and managed systemd services |
| `gimme://config/apps` | Registered application definitions |

It also exposes two resource templates for registered applications:

| URI template | Content |
| --- | --- |
| `gimme://apps/{name}` | Registration, deployment path, and HTTPS URL |
| `gimme://apps/{name}/releases` | Live read-only Deployer release history |

Template parameters are validated as registered application names. They cannot select
arbitrary hosts or filesystem paths. The release resource contacts the configured
host when read; the three manifest resources and application detail are local.

Example Vite/static application entry in `config/apps.json`:

```json
{
  "repository": "git@github.com:example/dashboard.git",
  "framework": "static",
  "branch": "main",
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
