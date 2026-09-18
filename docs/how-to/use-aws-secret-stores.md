# Use AWS Secrets Manager for Deployment secrets

Gimme schema v4 supports AWS Secrets Manager as its first external Secret Store. The
control-plane process is the only component that receives AWS authority. Targets never
receive AWS credentials, and AWS secret values are resolved only while applying an exact
Deployment resource plan.

## Trust and authentication model

Authentication begins with the standard AWS SDK ambient credential chain. Gimme accepts no
access key, profile name, external ID, or caller-defined STS parameter. A registered AWS
Provider Account fixes one expected 12-digit account ID and two distinct same-account role
ARNs:

- the inspection role permits `secretsmanager:DescribeSecret` for the store namespace;
- the resolver role permits `secretsmanager:GetSecretValue` for that namespace and, when
  selected, `kms:Decrypt` for one exact customer-managed key.

The ambient identity must be able to assume both roles. Registration assumes each role and
verifies the resulting account identity. Planning code assumes only the inspection role.
Apply assumes the resolver role only after metadata has been revalidated against the plan.

Scope both role trust policies to the identity that runs Gimme. Scope permissions to
`arn:aws:secretsmanager:<region>:<account>:secret:<prefix>/*`; do not grant
`ListSecrets`. The resolver role needs no create, update, tagging, rotation, or deletion
permissions. Gimme never performs those operations.

## Register an account and store

Use `plan_register_provider_account`, review the verified identity and exact role ARNs, then
pass its `plan_id` to `register_provider_account`. Register the store the same way with
`plan_register_secret_store` and `register_secret_store`.

An AWS store fixes:

- one Provider Account;
- one SDK-known AWS region;
- one relative name prefix;
- the derived ownership tag `gimme:secret-store=<store-name>`;
- either the regional AWS-managed Secrets Manager key or one exact same-account,
  same-region customer-managed KMS key ARN.

Every referenced AWS secret must live below the prefix and carry the exact ownership tag.
References contain only `{store, secret, field}`. Gimme derives the complete AWS name and
accepts no ARN, version, staging label, URI, JSONPath, or plaintext through MCP.

For example:

```json
{
  "PAYMENTS_TOKEN": {
    "store": "production-apps",
    "secret": "checkout/provider",
    "field": "TOKEN"
  }
}
```

The AWS value must be a JSON object. Selected fields must be unique top-level keys with
single-line UTF-8 string values. Binary values, nested selectors, NUL/CR/LF, fields above
8 KiB, and a resolved Deployment payload above 64 KiB are rejected before target mutation.

## Plan, apply, and rotate

`plan_deployment_resources` calls `DescribeSecret`, verifies name, account, region, tag,
deletion state, KMS policy, and one unique `AWSCURRENT` version. It does not retrieve the
value and cannot validate JSON field presence. Plans expose only environment keys plus
reference and version fingerprints.

`apply_deployment_resources` repeats metadata validation and rejects a rotated, replaced,
retagged, deleting, or re-keyed secret as a stale plan. It requests the exact reviewed
version ID, resolves and validates every Deployment secret in memory, and only then begins
target work. A failure during any resolution leaves the target unchanged.

Rotate values in AWS, then request and explicitly apply a new Deployment resource plan.
Gimme never rolls out a rotation automatically. A revoked or missing reference produces a
bounded safe error code; AWS SDK messages, ARNs, secret identities, and values are not put
in the operation journal.

On the Target, values cross an owner-only temporary file and are removed on success and
failure. The protected environment is installed atomically under the Deployment resource
lock. Its Applied Secret Manifest contains only environment keys and hashed reference and
version identities. A missing or corrupt manifest requires reconciliation; it does not
delete or invalidate the current environment.

## Migrate local SOPS references

The schema-v3 to schema-v4 migration creates the fixed `local-sops` store. A legacy string
such as `checkout/provider/TOKEN` becomes:

```json
{"store":"local-sops","secret":"checkout/provider","field":"TOKEN"}
```

`local-sops` always means `<state-directory>/secrets.enc.json`; it accepts no filesystem
path. Planning parses its encrypted JSON structure and fingerprints the selected ciphertext
without decrypting. Apply rejects changed ciphertext before invoking SOPS, then decrypts at
the same narrow execution boundary used for AWS values.

## Failure and rollback behavior

Access denied, throttling, missing, disabled/deleting, revoked KMS access, invalid JSON,
missing fields, and stale versions fail with bounded codes. Plaintext is never included in
exceptions or diagnostics. If target activation fails, the protected prior environment and
manifest remain the recovery boundary; rerun a fresh plan only after correcting the cause.
Do not delete the working environment or manifest to force reconciliation.
