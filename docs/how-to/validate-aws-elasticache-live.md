# Validate ElastiCache Valkey against AWS

Run this only in a disposable AWS account. It is an operator runbook, not an MCP tool: creating
the VPC, IAM roles, and ElastiCache service-linked role is account bootstrap and must remain
outside Gimme's policy-bound deployment surface.

Use a fresh, DNS-safe run id such as `gimme-live-20260919-a`. Keep it in a shell variable and tag
every bootstrap object with `gimme:live-test=<run id>`. Before creating anything, list tagged
VPCs, IAM roles, users, and ElastiCache snapshots; refuse to proceed if the run id already exists.
This makes setup retry-safe and ensures teardown has an exact, bounded target set.

Create a two-AZ VPC with two private subnets, an administration security group, and an application
security group. Create the inspection, resolver, and destructive roles described in
[the Valkey how-to](use-aws-elasticache-valkey.md), plus a least-privilege bootstrap identity that
can create the ElastiCache service-linked role. Record the account id, region, private subnet ids,
security-group ids, role ARNs, and Secret Store prefix in a private state file; do not record any
secret value.

Register that inventory in an isolated `GIMME_STATE_DIR`, then run the create, inspect,
bind, destroy, final-snapshot purge, and retained-secret purge plans and applies. Use a unique
Resource name derived from the run id. Confirm the expected pending phases (`creating`,
`deleting`, and `waiting_for_user_group`) by repeating the same confirmed apply; never issue a
second create or destroy plan while one is pending.

## Validate Multi-AZ failover

Create the replication group through Gimme, not `aws elasticache create-replication-group`, so
the test covers Gimme's exact durability, encryption, authentication, and ownership contract.
After `inspect_resource` reports `ready`, invoke AWS's bounded failover test for the derived group
and its sole node group:

```bash
aws elasticache test-failover --region <region> \
  --replication-group-id gimme-<resource-name> --node-group-id 0001
```

Poll `inspect_resource` until it returns `ready` again. Record the initial pending observation,
the accepted failover request, the eventual recovery observation, and any bounded provider errors.
ElastiCache's `TestFailover` response does not reliably expose a primary-member role, so do not
infer role transitions from missing `CurrentRole` fields; use the accepted request and recovered
Multi-AZ/automatic-failover readiness instead. The `Durability` create field is Gimme's provider
contract and may not be exposed by every installed AWS CLI version, another reason the group must
be created through Gimme.

Teardown is complete only after all of the following tag-filtered checks return empty: replication
groups, snapshots, users, user groups, cache subnet groups, cache parameter groups, Secrets
Manager secrets, security groups, subnets, VPCs, IAM users, and IAM roles. Delete the
service-linked role only if this disposable account has no other ElastiCache use. Finally remove
the isolated Gimme state directory. A failed teardown is an operational failure: leave the tagged
objects intact, report their identifiers to the operator, and retry the same bounded cleanup rather
than broadening the target selection.

## What a live run has verified

One run on 2026-09-21 (`eu-central-1`, a default account with the run's tagged VPC, two private
subnets, three security groups, an IAM operator user, and the three roles) drove the managed
lifecycle through Gimme's own tools against a `cache.m7g.large`, engine 9.0.0, one-shard,
two-node Multi-AZ group, then removed every object. The AWS account root cannot call
`sts:AssumeRole`, so a run needs an IAM operator user with an access key that may assume only the
run's three roles; delete both at teardown.

Verified against AWS:

- Create, inspect, and bind. The first create apply stopped once with
  `aws_elasticache_create_invalid_state` (not root-caused; the new user group may still have been
  settling), and repeating the same apply created the group. All seven CloudWatch metrics named in the Valkey how-to returned a
  datapoint within minutes of `available`, with the `CacheClusterId` dimension.
- Detach. `modify_user` accepts the access string `off ~* -@all`, and removing the user from the
  user group leaves it with no group. The group is `modifying` for about a minute afterwards, and
  a rebind in that window is refused with `aws_elasticache_binding_resource_not_ready`; the same
  apply succeeds once the group is `available`.
- Rebind. Generation 2 gets a new ACL user, and the previous user is recorded in
  `retired_user_ids`.
- Purge, AWS steps: the current and the retired ACL user are deleted and the credential secret
  is scheduled for deletion (Secrets Manager returns its tags under `Tags`). ElastiCache can
  still be modifying the user, and the first apply then stops with
  `aws_elasticache_rotate_delete_unavailable`; repeating the same confirmed apply resumes from the
  saved phase.
- Failover. `TestFailover` is accepted and the group is `modifying` for about nine minutes with
  no `CurrentRole` on either member; inspection reports `pending`, then `ready` with no issues.
  Attached security groups are unreadable in that window, which used to be reported as a false
  `security_group` issue (fixed).
- Destroy. `deleting` lasts about 15 minutes, one repeat may stop with
  `aws_elasticache_destroy_delete_unavailable`, and the group, its users, user group, subnet and
  parameter groups are then gone. The final-snapshot and retained-secret purges complete.
- A reviewed plan is bound to the repository's execution sources: editing a tracked source file
  mid-run makes a pending destroy plan stale (`plan_id is invalid or stale`) until it is re-planned.
  Do not change the checkout during a run.

Not verified, because each needs a real administration Target (an EC2 instance in the VPC): the
`gimme:stop:processes` step of detach and unbind, the key-purge program against a live cluster
(the run stubbed that one step and still resolved the real admin credential), activation probes,
credential rotation, restore, and the recovery matrix below.

## Run the executable recovery matrix

The isolated state must contain one deployed application bound to one managed Valkey Resource.
The Resource needs the inspection, resolver, and destructive roles documented in the Valkey
how-to. Use a fresh run id for every invocation. The program refuses the repository's normal
`config` directory and requires separate exact opt-ins for AWS creation/rotation and destruction:

```bash
GIMME_STATE_DIR=/absolute/path/to/isolated-state \
GIMME_AWS_VALKEY_LIVE_CREATE=1 \
GIMME_AWS_VALKEY_LIVE_DESTROY=1 \
GIMME_AWS_VALKEY_RESOURCE=example-valkey \
GIMME_AWS_VALKEY_DEPLOYMENT=example-live \
GIMME_AWS_VALKEY_RUN_ID=gimme-live-20260920-a \
uv run python tests/integration/aws_valkey_recovery_live_smoke.py
```

The executable test converges the registered Resource, reconciles its binding, activates the
current Deployment environment, and rotates that Deployment's credential through the normal Gimme
plan/apply tools. It then simulates external loss, proves ordinary `apply_resource` refuses to
silently create an empty group, restores from the exact final snapshot through
`plan_restore_resource` / `apply_restore_resource`, verifies the bound Deployment and retained
credential, and removes the test-only snapshot. The restored registered Resource remains ready at
the end; it is the same Resource the operator supplied, not an extra test group.

Simulating loss is the one operation that cannot go through an MCP tool: exposing deletion of a
still-bound group while preserving desired state would violate Gimme's destructive safeguards.
The harness therefore calls the same narrow provider adapter used by Gimme with the exact derived
group identity and an exact run-derived final-snapshot name. It never accepts an AWS ARN, group id,
snapshot name, user id, secret id, command, or path from the caller. Snapshot cleanup uses that
same exact test identity. A mode-0600 marker in the isolated state correlates deletion, restore,
and snapshot cleanup, so repeating the same run id resumes those phases without issuing a second
group deletion. A snapshot with no matching marker fails closed. If recovery fails after loss
simulation, keep the snapshot and isolated state intact and rerun the same command or investigate
before deleting anything.
