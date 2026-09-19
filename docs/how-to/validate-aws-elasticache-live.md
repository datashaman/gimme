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

Register that inventory in an isolated `GIMME_STATE_DIRECTORY`, then run the create, inspect,
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
