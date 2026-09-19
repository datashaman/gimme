# Use AWS RDS for PostgreSQL as a managed Resource

Gimme can provision one private, encrypted, Multi-AZ RDS for PostgreSQL instance as a
Resource ([ADR 0008](../adr/0008-aws-rds-postgresql-resources.md)), create an isolated
database and role for a Deployment on it, and store that Deployment's credential in AWS
Secrets Manager. The control-plane process is the only component that receives AWS
authority; no credential is returned by a tool, plan, journal, or error.

This page describes what is implemented today. Where it differs from the ADR, the ADR is
the target design and this page is the current behavior.

## Current scope

Implemented: registration, provisioning, converging an existing instance onto reviewed
updates, live inspection with drift reporting, creating a Deployment's database and workload
secret, and non-destructive removal.

Not implemented yet:

- runtime wiring of a managed database into a Deployment. A Deployment whose database
  binding is a managed Resource is fenced off: `plan_deployment_resources` reports a
  readiness issue, `plan_deployment` refuses, and Recovery Points reject it;
- workload credential rotation, Detached Allocation rebind, and Retained Resource forget;
- destructive deletion. Removal never deletes the instance;
- major-version upgrades, storage decreases, backup and maintenance-window policy, and moving
  a Resource to another Network or region, all of which need a new Resource.
Differences from the ADR: two roles are used instead of four, the Administration Target
runs plain `psql` instead of a root-owned helper (verifying the certificate against a bundle
delivered per bind rather than one that helper installs), and secret-free observations are
cached in `observed-resources/<name>.json` beside desired state.

## Prerequisites

Gimme creates only the RDS instance, its DB subnet group and DB parameter group, and
workload secrets. Everything else must already exist and is never edited:

- an AWS Network: one VPC and exactly two private subnets in different Availability Zones;
- two security groups in that VPC, one for the Administration Target and one per eligible
  Deployment Target. Gimme attaches them to the instance; it does not add ingress rules, so
  allow port 5432 from those groups yourself;
- an Administration Target: a registered Target with `"role": "administration"` that
  can reach the instance's private endpoint and has `psql` installed. It never hosts a
  Deployment;
- a Provider Account with the two roles below, and an AWS Secrets Manager Secret Store for
  workload credentials (see [`use-aws-secret-stores.md`](use-aws-secret-stores.md)).

The instance is never public and always uses gp3 storage, `StorageEncrypted`, seven-day
backups, deletion protection, and no automatic minor upgrades. Its Resource-owned parameter
group (`gimme-<name>-params`, family `postgres<major>`) sets `rds.force_ssl` to `1`, so the
server rejects unencrypted connections on every PostgreSQL version, not only 15 and later.
Gimme reuses an existing group of that name only if it carries this Resource's
`gimme:resource` tag and the expected family; otherwise it refuses with
`aws_rds_parameter_group_ownership_mismatch` and changes nothing.
The group is attached only when Gimme creates the instance; it does not change an existing one.
Multi-AZ doubles the instance cost; Gimme reports structure but does not price it.

## IAM roles

Registration fixes one account ID and two distinct same-account roles. The ambient identity
that runs Gimme must be able to assume both, and needs no other AWS permission.
`sts:AssumeRole` cannot be called with root user credentials, so do not run Gimme as root.

The **inspection role** describes and creates the infrastructure and writes workload
secrets. It does not read secret values. Replace `<region>`, `<account>`, and
`<store-prefix>` (the Secret Store prefix):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RdsCreateAndDescribe",
      "Effect": "Allow",
      "Action": [
        "rds:CreateDBInstance",
        "rds:CreateDBSubnetGroup",
        "rds:AddTagsToResource",
        "rds:DescribeDBInstances"
      ],
      "Resource": [
        "arn:aws:rds:<region>:<account>:db:gimme-*",
        "arn:aws:rds:<region>:<account>:subgrp:gimme-*",
        "arn:aws:rds:<region>:<account>:pg:gimme-*",
        "arn:aws:rds:<region>:<account>:pg:default.*",
        "arn:aws:rds:<region>:<account>:og:default:*"
      ]
    },
    {
      "Sid": "RdsModifyAndReboot",
      "Effect": "Allow",
      "Action": [
        "rds:ModifyDBInstance",
        "rds:RebootDBInstance"
      ],
      "Resource": [
        "arn:aws:rds:<region>:<account>:db:gimme-*",
        "arn:aws:rds:<region>:<account>:pg:gimme-*"
      ]
    },
    {
      "Sid": "RdsParameterGroup",
      "Effect": "Allow",
      "Action": [
        "rds:CreateDBParameterGroup",
        "rds:ModifyDBParameterGroup",
        "rds:DescribeDBParameterGroups",
        "rds:ListTagsForResource",
        "rds:AddTagsToResource"
      ],
      "Resource": "arn:aws:rds:<region>:<account>:pg:gimme-*"
    },
    {
      "Sid": "Ec2Describe",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeAccountAttributes",
        "ec2:DescribeAvailabilityZones",
        "ec2:DescribeInternetGateways",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSubnets",
        "ec2:DescribeVpcAttribute",
        "ec2:DescribeVpcs"
      ],
      "Resource": "*"
    },
    {
      "Sid": "WorkloadSecrets",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:CreateSecret",
        "secretsmanager:PutSecretValue",
        "secretsmanager:TagResource"
      ],
      "Resource": "arn:aws:secretsmanager:<region>:<account>:secret:<store-prefix>/*"
    },
    {
      "Sid": "RdsManagedMasterSecret",
      "Effect": "Allow",
      "Action": ["secretsmanager:CreateSecret", "secretsmanager:TagResource"],
      "Resource": "arn:aws:secretsmanager:<region>:<account>:secret:rds!db-*"
    },
    {
      "Sid": "KmsViaRdsAndSecretsManager",
      "Effect": "Allow",
      "Action": [
        "kms:DescribeKey",
        "kms:GenerateDataKey",
        "kms:Decrypt",
        "kms:CreateGrant"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "kms:ViaService": [
            "rds.<region>.amazonaws.com",
            "secretsmanager.<region>.amazonaws.com"
          ]
        }
      }
    }
  ]
}
```

The **resolver role** reads only the RDS-managed master credential, and only at apply time:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RdsManagedMasterSecret",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:<region>:<account>:secret:rds!db-*",
      "Condition": {
        "StringLike": {
          "aws:ResourceTag/aws:rds:primaryDBInstanceArn": "arn:aws:rds:<region>:<account>:db:gimme-*"
        }
      }
    }
  ]
}
```

Both policies were exercised against a live account with the AWS-managed `aws/rds` and
`aws/secretsmanager` keys, except the `RdsParameterGroup` statement, the `RdsModifyAndReboot` statement, and the
`pg:gimme-*` resource on `RdsCreateAndDescribe`, which have only been exercised against
botocore stubs. A customer-managed KMS key for storage, the master secret, or the
workload Secret Store needs a key policy that admits these roles and RDS; that has not been
verified. Trust policies should name only the identity that runs Gimme.

## Register the network and Resource

Register the Provider Account and Secret Store first, then the administration Target. The
Resource and AWS Network are ordinary local desired-state entries; registration makes no
AWS calls. `config/state.example.json` contains a complete example:

```json
{
  "aws_networks": {
    "primary": {
      "provider_account": "main",
      "region": "us-east-1",
      "vpc_id": "vpc-0123456789abcdef0",
      "private_subnet_ids": ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"]
    }
  },
  "resources": {
    "example-rds-postgres": {
      "kind": "postgres",
      "provider": "aws_rds_postgres",
      "aws_network": "primary",
      "administration_target": "adminbox",
      "engine_version": "17.2",
      "instance_class": "db.t3.medium",
      "allocated_storage_gb": 20,
      "administration_security_group_id": "sg-0123456789abcdef0",
      "deployment_security_group_ids": {"devbox": "sg-0123456789abcdef1"},
      "workload_secret_store": "workload-secrets",
      "retain_on_removal": true
    }
  }
}
```

`engine_version` must be an exact version AWS lists for your region and the instance class
must support Multi-AZ; otherwise creation fails at apply time. Every key of
`deployment_security_group_ids` must be a registered deployment Target, and only those
Targets may bind Deployments to the Resource. Use `register_resource`, or
`plan_update_resource` and `update_resource` to change it.

Updates are checked locally, with no AWS call, and refused with a fixed
`aws_rds_update_forbidden_<field>` code when ADR 0008 says the change needs a new Resource:
`aws_network`, an engine major version change (`engine_major`), a decrease of
`allocated_storage_gb`, `workload_secret_store` while any allocation exists (active or
detached), removing a Target from `deployment_security_group_ids` while a Deployment bound to
this Resource is placed on it, and any change between a managed and a target-local Resource
(`provider`). A refused update leaves desired state unchanged. Same-major engine versions, `instance_class`,
an increased `allocated_storage_gb`, `administration_target`,
`administration_security_group_id`, other `deployment_security_group_ids` changes,
`retain_on_removal`, and `workload_secret_store` with no allocations register as before.

## Provision

`plan_apply_resource` then `apply_resource` creates the DB subnet group, the DB parameter
group (with `rds.force_ssl` set to `1`), and the instance.
The instance is tagged `gimme:resource=<name>` and its AWS identifier is derived from the
Resource name (`gimme-<name>`). Gimme refuses an instance whose tag does not derive that
identifier, and never adopts an unrelated instance.

Multi-AZ creation takes roughly 12 to 15 minutes. `apply_resource` polls for at most 30
seconds and returns `phase: pending`. Request a new plan and apply again to resume; a
resume describes the instance and never creates a second one. The plan changes as the
instance advances, so an earlier plan is rejected as stale. The result is `phase: ready`
once AWS reports `available` with no unapplied managed change.

### Updating an existing instance

Edit desired state with `plan_update_resource` and `update_resource`, then plan and apply
again. For an existing `available` instance, `apply_resource` describes it, compares it with
desired state, and sends one `ModifyDBInstance` with `ApplyImmediately` and only the fields
that differ: a same-major `engine_version`, `instance_class`, an increased
`allocated_storage_gb`, the security-group set, and the Resource-owned parameter group (which
also moves an instance created before `rds.force_ssl` was managed onto it). It never sets
`AllowMajorVersionUpgrade` and sends no other field. The result lists `modified_fields`
(names only) and `rebooted`.

Changes are disruptive and start immediately, so review the plan's effects first: an instance
class change fails over a Multi-AZ instance, and an engine version change or attaching the
parameter group restarts it. RDS may also refuse a change, for example a storage increase
under 10%, with the generic `aws_rds_modify_unavailable`; read the reason with the same AWS
API and role in your own terminal.

Because planning cannot call AWS, apply checks the live instance and refuses, before any
change, `aws_rds_modify_forbidden_allocated_storage_gb` (desired is below the live value),
`aws_rds_modify_forbidden_engine_downgrade`, and `aws_rds_modify_forbidden_engine_major`.
Values AWS already has pending count as applied, so applying again while a modification is in
flight sends nothing and reports `phase: pending`.

When the parameter group reports `pending-reboot`, the instance is `available`, and nothing is
pending, apply reboots it once without forced failover, then polls. That includes the first
apply after creation, which restarts the new instance once so `rds.force_ssl` takes effect.
If a modification is still settling when the 30 seconds end, the next apply does the reboot.

## Bind a Deployment

Set the Deployment's `resources.database` to the Resource name, then `plan_bind_resource`
and `bind_resource`. Binding requires `phase: ready`. Gimme resolves the master credential
through the resolver role, generates a workload password, and sends both to the
Administration Target through the same protected temporary file used for Deployment secrets.
The Target's `psql` creates a role and database named after the Deployment's database
identifier and sets the role's password. The step is idempotent, and re-binding replaces the
password.

The `psql` connection uses `sslmode=verify-full` against the pinned AWS commercial-region
global RDS trust bundle (`deploy/aws-rds-global-bundle.pem`, provenance and refresh steps in
`deploy/aws-rds-global-bundle.md`), so both the certificate chain and the endpoint hostname
are verified. The bundle is uploaded on every bind beside the secret file, into the same
`0700` directory with `0600` permissions, checked against a sha256 digest supplied by Gimme
before any connection, and removed afterwards even on failure. Failures are fixed and
secret-free: `us-gov-*` regions are refused with `aws_rds_tls_region_unsupported`
at `register_resource`, `plan_apply_resource`, `apply_resource`, `plan_bind_resource` and
`bind_resource`, before anything is created or any remote work happens (inspection and
Retained Resource cleanup still work), the program stops with
`trust bundle digest mismatch` if the uploaded bundle is not the pinned one, and
`managed PostgreSQL TLS certificate verification failed` for an untrusted certificate or a
hostname mismatch. Raw `psql` output is not returned for that failure.

To check the real chain against a live instance, bind a Deployment to a ready Resource and
expect success; then from the Administration Target run
`PGSSLMODE=verify-full PGSSLROOTCERT=<bundle> psql -h <endpoint> -U <master> -d postgres -c 'select 1'`
(it must succeed), and repeat it with the bundle path replaced by an unrelated CA file (it
must fail with `certificate verify failed`).

The workload credential is then written to Secrets Manager at
`<store-prefix>/<resource>/<deployment>` with a JSON object of `username`, `password`,
`host`, `port`, and `dbname`, tagged for its Secret Store, Resource, and Deployment. The tool
returns only the `{store, secret}` reference. Nothing yet copies these values into the
Deployment's environment.

## Inspect and remove

`inspect_resource` describes the instance through the inspection role and returns its
identity, status, engine version, endpoint, and allocations. If AWS cannot be reached it
returns the last observed state with a bounded `refresh_error` and no drift.

After a successful live read it also reports `drift`: `fields` lists, for each of
`engine_version`, `instance_class`, `allocated_storage_gb` and `security_group_ids` that differs
from desired state, its `desired` and `live` values, and `modification_pending` says whether
AWS has a pending modification. Drift is informational and is not stored. Gimme does not yet
apply it (an existing instance is only polled by `apply_resource`).

`plan_cleanup_resource` and `apply_cleanup_resource` require the exact confirmation
`RETAIN <name>` and are refused while a Deployment references the Resource. They remove
only the local registration and write a Retained Resource tombstone. The instance,
its data, the master secret, and the workload secrets remain in AWS and keep costing money,
as do its DB subnet group and DB parameter group, which are never deleted automatically.
To delete them, disable deletion protection and delete the instance yourself, then delete
the workload secrets, and expect RDS to remove automated backups and snapshots
asynchronously afterwards.

## Failure codes

Provider failures become fixed `aws_rds_<operation>_<reason>` codes, for example
`aws_rds_create_access_denied`. Reasons are `access_denied`, `missing`, `already_exists`,
`invalid_state`, `throttled`, `revoked`, and `unavailable`. AWS messages, ARNs, and values
are never included. Instance creation reports `subnet_group`, `parameter_group` (create),
`parameter_group_verify` (reading an existing group's tags and family), and
`parameter_group_modify` (the `rds.force_ssl` setting) as operations before `create`.
Updating an existing instance reports `modify` and `reboot`, and `aws_rds_modify_field_forbidden`
means the adapter was asked to send a field outside its allowlist.
`aws_rds_parameter_group_ownership_mismatch` means a same-named group exists that this
Resource does not own; rename or delete it yourself, since Gimme never deletes one.

`unavailable` covers any error Gimme does not classify, such as a rejected parameter
combination or a KMS access problem. To see the underlying error, call the same AWS API with
the same role and read the error code and message in your own terminal. Fix the cause and
plan again. A failed create can leave the Resource's DB subnet group and DB parameter group
(both free of charge), which the next apply reuses and, for the parameter group, re-converges
onto `rds.force_ssl=1`. Partial provider state is never deleted automatically.
