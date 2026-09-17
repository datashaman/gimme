# Two-Replica AWS high availability without a resident controller

- Status: Accepted
- Date: 2026-09-17

Gimme's first highly available Deployment topology is deliberately narrow: exactly two explicitly
registered Ubuntu EC2 Targets in distinct Availability Zones, one dedicated internet-facing
Application Load Balancer, one RDS for PostgreSQL Multi-AZ DB instance with a synchronous standby,
one ElastiCache for Valkey Multi-AZ replication group, and S3-backed artifacts and recovery data.
The two Deployment Replicas run the same immutable Application Artifact and share one Deployment
identity, route, configuration generation, and managed data identity. Applications remain
stateless on their Targets.

The operator supplies pre-existing EC2 instances, VPC, subnets, security groups, ACM certificate,
and Route53 hosted zone through bounded Provider Account, Provider Attachment, and AWS Network
registrations. Gimme validates those prerequisites and creates only the Deployment-owned ALB,
target group, HTTP-to-HTTPS and HTTPS listeners, target registrations, and DNS alias. It does not
create, stop, terminate, or replace EC2 instances and does not mutate the registered network
prerequisites. One dedicated ALB per HA Deployment avoids shared listener-rule priority and
lifecycle coupling.

The ALB terminates TLS and reaches Caddy over private HTTP on fixed port 8080. Instance-ID target
registration, restrictive security-group relationships, and trusted forwarded-header CIDRs bind
that origin to the registered ALB. The effective primary live health probe is the ALB health check;
other live probes remain deployment gates and diagnostics. Initial DNS publication requires both
Replicas to be healthy. AWS ALB fails open when every registered target is unhealthy, so Gimme
reports the topology unavailable but cannot promise fail-closed routing without another active
control layer.

Release applies are rolling and transactional at the Deployment boundary. Both Replicas reserve
temporary capacity and verify the same artifact before mutation; one Replica is drained, activated,
health-checked, and returned to service before the other changes. No release migration runs.
Failure restores every changed Replica to the prior artifact and verifies the surviving public
route. Environment and secret updates likewise stage one generation on both Replicas and never
intentionally leave mixed configuration after apply.

Both Replicas run declared queue workers and Horizon with unique supervisor identities. Both run a
minute scheduler timer, while a fixed Deployment-derived Valkey lease with renewable token ownership
permits only one scheduler execution. Loss of the lease service skips a tick rather than risking
duplicate execution. Ordinary Artisan commands execute once on a deterministic healthy control
Replica; process refreshes fan out to both.

This topology needs no resident Gimme controller. AWS supplies routing and managed-service failover,
and the Valkey lease supplies scheduler leadership. Secret-free observed topology and resumable
phase state are cached locally but reconstructible from desired state, provider identities and
ownership markers, ALB membership, and Target manifests. Missing or conflicting identities fail
closed. Manual Replica replacement requires the old EC2 instance to be stopped or terminated and
fences it with a new generation before a compatible replacement can join.

Within the supported single-region failure model, one Target or Availability Zone loss should leave
web service available within 60 seconds, scheduler execution should resume within two minutes, and
RDS and ElastiCache connectivity should recover within five minutes subject to AWS failover. RDS
synchronous Multi-AZ replication targets zero committed-data loss. Valkey is not a system of record
and has no recovery-point guarantee. Region failure, automatic EC2 replacement, shared filesystems,
cross-region recovery, and guaranteed fail-closed ALB behavior are outside this decision.

## Considered alternatives

- A generic provider-neutral HA abstraction was rejected because it would conceal materially
  different routing, identity, failover, and permission semantics before a second topology exists.
- Scheduler ownership on one preferred Target was rejected because machine loss would require a
  controller to transfer leadership.
- Shared ALBs were rejected because listener rules, priority allocation, permissions, and deletion
  would couple otherwise independent Deployments.
- Target-local uploads, replicated directories, EFS, and NFS were rejected in favor of an explicit
  stateless contract with external object storage and managed data services.
- Automatic EC2 replacement was rejected because registered Targets are operator-owned machines and
  Gimme has no authority to create or terminate them.
- A continuously running reconciliation service was rejected because applied Deployments must keep
  operating when the local MCP control plane is offline.

## Consequences

- AWS-specific Provider Accounts, AWS Networks, managed PostgreSQL, managed Valkey, immutable
  artifacts, recovery storage, and deterministic capacity are prerequisites.
- The topology is fixed at two Replicas and cannot auto-scale or auto-replace a failed Target.
- Applications must reconnect through stable RDS and ElastiCache endpoints and use expand/contract
  database changes.
- Health cannot distinguish every application failure, and ALB's documented all-targets-unhealthy
  behavior remains visible to operators.
- HA improves availability but does not replace Recovery Points or regional disaster recovery.
