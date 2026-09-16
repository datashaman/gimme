# First local deployment

This tutorial creates a local Ubuntu development target and a Laravel deployment. It
assumes Gimme is installed and the MCP server is connected to your client.

## 1. Prepare SSH

Give the target a stable SSH alias and enable agent forwarding for private Git
repositories:

```sshconfig
Host devbox devbox.local
    HostName 192.0.2.10
    User deployer
    ForwardAgent yes
```

The IP address is bootstrap-only. After Avahi is provisioned, Gimme uses
`devbox.local` so the hostname-specific SSH configuration remains effective.

## 2. Start from valid desired state

Copy `config/state.example.json` to the private directory configured by
`GIMME_STATE_DIR`, then replace only the example target, repository, domain, and exact
version values. Do not commit operational inventory to this public repository.

Alternatively, ask the MCP client to register the target, application, named
PostgreSQL/Valkey resources, and deployment with the corresponding registration
tools.

## 3. Review the target plan

Ask the MCP client to call `inspect_target` and `plan_target_stack`. Review:

- the SSH bootstrap and normal hostnames;
- every APT package and candidate;
- services to enable;
- whether `privileged_helper` is `ready` or `bootstrap_required`.

## 4. Bootstrap privilege once

When the plan reports `bootstrap_required`, use a terminal so the sudo password never
crosses MCP:

```bash
# From the Gimme repository root:
uv run gimme-bootstrap-target devbox
uv run gimme-bootstrap-database devbox
```

Repeat `plan_target_stack`, then apply its exact `plan_id` with
`apply_target_stack`.

## 5. Reconcile the deployment

Run the plan/apply pairs in this order:

1. `plan_deployment_runtimes` → `apply_deployment_runtimes`
2. `plan_deployment_resources` → `apply_deployment_resources`
3. `plan_deployment` → `apply_deployment`

The release plan resolves one Git commit and shows the candidate health gate before
the symlink switch and the live HTTPS gate afterward.

## 6. Trust local HTTPS

Copy the public Caddy root certificate to the development workstation and add it to
the workstation trust store:

```bash
scp devbox.local:/srv/gimme/apps/.caddy-local-root.crt /tmp/gimme-caddy-root.crt
```

The private CA key remains on the target. Open the deployment URL recorded in its
placement, such as `https://example-local.devbox.local`.

