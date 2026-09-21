# ADR 0010: Portable Deployment intent and execution profiles

- Status: Proposed
- Date: 2026-09-21

## Context

Gimme currently places a Deployment Replica on one explicitly registered Ubuntu Target and
materializes its processes through fixed, policy-bound host behavior. This gives the control plane
a deliberately narrow execution boundary, but it makes the current Target model appear to be the
only possible way to run an Application.

Gimme is also gaining provider-backed Resources. A Provider Account answers which external
service owns a Resource implementation, while an AWS Network bounds the existing AWS topology
within which particular managed Resources and the supported two-Replica topology operate. Neither
term answers the separate question of how an Application's web, worker, scheduler, or realtime
processes are materialized and operated.

That distinction matters when the same Application and Deployment should be able to run as native
processes on an Ubuntu Target, containers on one Target, an ECS/Fargate service, a bounded
Kubernetes workload, or a serverless function. Copying provider- or engine-native configuration
into each Deployment would make desired state difficult to review, destroy portability, and expose
the MCP surface to arbitrary manifests, task definitions, shell, paths, or provider parameters.

Conversely, pretending that all engines have identical semantics would be unsafe. A fixed Target
cannot honestly provide horizontal replica scaling; Lambda concurrency is not a process replica;
and a Kubernetes manifest is not a safe input contract. The model needs one small portable intent
while allowing engine-specific renderers to reject unsupported intent before an apply.

This decision refines, but does not supersede, the current Target, Deployment, Deployment Replica,
Provider Account, AWS Network, Resource, plan/apply, artifact, rollout, and AWS two-Replica HA
decisions. The current systemd-backed Target remains the only implemented execution behavior
until a later accepted implementation decision says otherwise.

## Decision

Gimme will separate a Deployment's portable runtime intent from the execution-specific
materialization of that intent.

### Portable Deployment intent

A Deployment declares only the execution-neutral facts Gimme can validate and safely plan:

- its Application Artifact or permitted source-mode release and its exact runtime compatibility;
- named process roles, selected from the bounded roles `web`, `worker`, `scheduler`, and
  `realtime`, with Application-declared fixed commands rather than caller-supplied shell;
- Resource Bindings and their supported uses;
- Secret References and non-secret environment values;
- public route and bounded health contract;
- a portable availability/scale intent, expressed only through a future bounded vocabulary;
- lifecycle intent for release activation, fixed migration policy, health verification, and
  rollback.

Process role is intent, not an arbitrary process declaration. `web` accepts public traffic;
`worker` consumes one declared queue use; `scheduler` performs the Application's declared
schedule without duplicate ownership; `realtime` serves the Application's supported realtime
contract. Application metadata continues to own the fixed executable and framework-specific
behavior. Deployments do not accept commands, images, manifests, service names, ports,
filesystem paths, ingress snippets, Helm values, or raw provider configuration.

Portable scale intent describes an outcome, not a renderer mechanism. It may later include a
bounded minimum and maximum replica count or an explicitly singleton role, but has no effect
until the selected Execution Profile supports it. An engine must reject, at plan time, an intent
it cannot represent faithfully. It must never silently reduce a requested topology, turn a
singleton scheduler into multiple active schedulers, or invent an autoscaling policy.

### Execution Profiles

An **Execution Profile** is a named, versioned, policy-bound declaration selecting how a
Deployment is materialized. It contains:

- one execution engine kind and its versioned renderer contract;
- bounded placement identity and admission policy;
- an explicit list of supported process roles, topology/scale semantics, Resource kinds, route
  modes, secret-delivery mechanisms, and lifecycle operations;
- reviewed defaults and fixed implementation policy;
- the engine- and profile-specific observations required to identify resulting runtime objects
  without retaining unsafe configuration.

An Execution Profile is not a generic cloud account, a target, a container image, or arbitrary
infrastructure-as-code. It selects a reviewed implementation under existing ownership and network
boundaries. Provider Accounts and AWS Networks remain the authorities for external identity and
allowed AWS topology; Targets remain machine identities where an engine uses registered machines.

An Execution Profile may be selected directly by a Deployment or through a future bounded
placement policy. Once materialized, its placement and renderer identity are immutable for that
Deployment generation. Moving a live Deployment between profiles is an explicit migration with
compatibility, data, routing, and recovery evidence; it is never an incidental configuration
edit or automatic failover.

### Renderers and plans

Each execution engine provides a renderer. Given validated portable Deployment intent, its
Execution Profile, resolved Resource Binding identities, and safe observations, the renderer
produces one canonical, secret-free desired runtime representation. The plan records only the
derived effects, renderer/profile identity and version, safe object identities, observations,
and the execution fingerprint required by ADR 0001. Apply regenerates the representation and
rejects stale plan, profile, provider, renderer, or observation identity.

Renderers may use fixed engine-native objects internally:

| Execution engine | Example derived materialization |
| --- | --- |
| `ubuntu-systemd` | fixed web routing and policy-bound systemd units/timers on registered Targets |
| `docker-compose` | derived Compose services, networks, and bounded volumes on one registered Target |
| `ecs-fargate` | derived task definition, service, task roles, and bounded AWS routing objects |
| `kubernetes` | derived workload, service, job/cron-job, and approved ingress objects |
| `lambda` | derived request/queue functions, event mappings, and bounded concurrency policy |

These are renderer output, never generic MCP input. A renderer may only create, update, observe,
or remove objects with Gimme-derived ownership identities and only inside its Profile's provider,
network, identity, and lifecycle boundaries. It must use content-addressed plan/apply operations,
keep secrets out of plans and observations, and expose bounded status rather than raw provider
output. Privileged Target operations remain behind existing policy-bound helpers.

### Target Capability Profiles

For engines that run on registered Ubuntu Targets, a **Target Capability Profile** is the
host-oriented counterpart to an Execution Profile. It is a named, versioned, policy-bound
declaration of the exact reviewed machine capabilities a Target may host. It replaces a generic
"server type" or caller-provided package list with a small fixed catalog, for example:

| Target Capability Profile | Permitted materialization |
| --- | --- |
| `laravel-app` | Laravel web, worker, scheduler, and supported realtime roles |
| `laravel-web` | Laravel web and supported realtime roles only |
| `laravel-worker` | Laravel worker and scheduler roles only |
| `postgresql` | PostgreSQL Resource provider only |
| `mysql` | MySQL or MariaDB Resource provider only |
| `valkey` | Valkey Resource provider only |
| `memcached` | Memcached Resource provider only |
| `meilisearch` | Meilisearch Resource provider only |
| `edge-routing` | approved routing/load-balancer behavior only |

Each profile selects reviewed package, runtime, service, filesystem, and network policy. It does
not expose arbitrary packages, services, unit files, ports, paths, web-server configuration, or
database configuration as desired-state or MCP input. A Target's existing exact APT stack and mise
policy become the profile's validated realization rather than a second free-form configuration
surface.

Target Capability Profiles are not universal runtime roles. A `worker` Process Role rendered by
ECS/Fargate is a service/task and needs no `laravel-worker` Target Capability Profile; a managed
RDS MariaDB Resource needs no `mysql` Target Capability Profile. Conversely, one Target may be
eligible for a `laravel-app` Execution Profile without being allowed to provide a database
Resource. Capability Profiles say what a machine may host; Execution Profiles say how a
Deployment is run; Resources say what service is bound; routing topology says how traffic reaches
the `web` role.

### Initial profile sequence

The reference Execution Profile is `ubuntu-systemd`, expressed using the current Target and
Deployment Replica model. Introducing the abstraction must first make this existing behavior
conform to the renderer contract without changing its operational guarantees.

The same vertical slice introduces the implicit `laravel-app` Target Capability Profile for
existing Deployment Targets. It must be derived losslessly from their current fixed package and
runtime policy, so adopting the profile changes neither remote state nor admission behavior.

The next candidate is `docker-compose`, because it tests container materialization while retaining
the registered Target execution boundary. `ecs-fargate` is the first candidate managed/cloud
engine: it can exercise provider-backed runtime identity, service scaling, task execution, and
AWS routing without exposing a general Kubernetes surface. Kubernetes/EKS remains deferred until
a separate decision defines a narrow supported Laravel workload contract. Gimme will not accept
arbitrary Kubernetes YAML, Helm values, `kubectl` arguments, ECS task definitions, Dockerfiles, or
Lambda configuration as a shortcut.

### Resources stay independent of engines

Resources remain capability contracts with a provider as a bounded implementation detail. The
initial relational and data-store capability roadmap is PostgreSQL, MySQL, MariaDB, Valkey,
Memcached, DynamoDB-backed cache, S3-compatible object storage, and queue integrations. An
Execution Profile declares which of those contracts it can bind and how it safely delivers each
binding to a runtime; it does not redefine them.

For example, a MySQL Resource can be target-provided, RDS-provided, or an approved future imported
provider implementation while a Deployment retains the same database binding contract. DynamoDB
cache remains an AWS-specific provider-backed Resource contract with table, TTL, region, and
workload-identity semantics; it is not an alias for Memcached or Valkey.

## Compatibility and migration

Schema v3 and existing Target-bound Deployments retain their current meaning. They are treated as
using the implicit `ubuntu-systemd` Execution Profile until a versioned schema migration makes the
profile explicit. That migration must be lossless, preserve immutable Target placement and
Deployment Replica identities, and produce no remote change when the derived profile exactly
matches current policy.

Existing Deployment Targets likewise use an implicit `laravel-app` Target Capability Profile until
the Target schema can name the profile explicitly. Resource-provider and Administration Targets
must receive the appropriate distinct Capability Profile as part of their own future migration;
Gimme must not infer that an application Target may host a data or edge role merely because its
current package state happens to contain compatible software.

The first implementation may add profile identity to planning and observations, but it must not
expand the MCP input surface or weaken existing Target, Resource, artifact, secret, rollout,
recovery, or HA invariants. Engine changes, profile migration, and any new Resource provider each
require their own content-addressed plan/apply contracts and accepted architecture decisions.

## Non-goals

- Generic multi-cloud infrastructure provisioning, VM creation, replacement, or termination.
- Arbitrary containers, images, shell commands, Compose files, ECS task definitions, Kubernetes
  manifests, Helm charts, provider IAM documents, or ingress configuration as desired-state input.
- A claim that process roles, scaling, networking, health, or rollback have identical semantics on
  all engines.
- Automatic workload relocation, profile failover, cross-engine rollout, or cross-engine data
  migration.
- Implementing Docker Compose, ECS/Fargate, EKS/Kubernetes, Lambda, MySQL/MariaDB, Memcached,
  DynamoDB, queues, or object storage in this decision.

## Consequences

- Application and Deployment desired state stays small and reviewable as execution options grow.
- Renderers become explicit security-sensitive boundaries, analogous to existing Deployer recipes
  and privileged helpers; each needs narrow validation, ownership rules, observations, tests, and
  documentation.
- Every engine must state its supported capability matrix and fail closed on unsupported intent.
- The current systemd implementation becomes the first compatibility test for the abstraction,
  rather than being discarded for a theoretical universal scheduler.
- Target capability admission can remain a fixed, safe catalog while runtime execution expands
  beyond machines; a Capability Profile never becomes a disguised arbitrary provisioning API.
- Provider expansion and execution expansion can proceed independently: an AWS Resource can be
  consumed from an Ubuntu Target before ECS exists, and an ECS renderer can initially consume only
  already-supported Resource contracts.
- The next implementation work is a schema-and-renderer vertical slice for the implicit
  `ubuntu-systemd` profile, followed by a separate decision on the next engine.

## Considered alternatives

### Put engine-native configuration on Deployments

Rejected. It would make a Deployment a mixture of portable application intent and opaque
provider-specific infrastructure, duplicate policy across every Deployment, and make plans harder
to constrain and review.

### Treat ECS, EKS, and Lambda as Providers

Rejected. AWS is the provider in all three cases; ECS, EKS, and Lambda have materially different
execution, identity, scaling, networking, and lifecycle semantics. They are execution engines.

### Create a generic Kubernetes or Terraform escape hatch

Rejected. Arbitrary manifests and infrastructure code conflict with the bounded MCP, plan/apply,
ownership, and privileged-operation invariants. A future engine can support a carefully selected
workload vocabulary without inheriting the entire platform API.

### Wait to introduce the abstraction until several engines exist

Rejected. The existing systemd flow provides a concrete reference implementation now. Capturing
its contract first prevents future engine work from smuggling incidental host assumptions into the
portable Deployment model.
