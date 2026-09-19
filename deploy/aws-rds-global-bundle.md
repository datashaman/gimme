# Pinned AWS RDS trust bundle

`aws-rds-global-bundle.pem` is the AWS commercial-region global RDS trust bundle. The bind
step uploads it to the Administration Target next to the secret file and runs `psql` with
`sslmode=verify-full` against it. Gimme never installs it system-wide.

- Source: <https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem>
- Version: S3 object version `OidS_dEwM7Wp653JIp5TymZXG3AykICt`, last modified 2025-07-24
- SHA-256: `e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3`
- Content: 108 self-signed root CA certificates and nothing else. AWS advises against
  registering intermediates; `tests/test_deployer_recipe.py` checks this.
- Not covered: `us-gov-*` regions (`bind_resource` refuses them) and China regions.

## Refresh

1. Download the file from the source URL over HTTPS and read AWS's
   [trust store documentation](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html)
   for what changed.
2. Confirm every certificate is a self-signed CA (subject equals issuer, `CA:TRUE`).
3. Replace `aws-rds-global-bundle.pem`, then update `RDS_TRUST_BUNDLE_SHA256` in
   `src/gimme/resources_postgres.py` and the version and digest above.
4. Run `bash scripts/gimme-verify`. Replacing the file changes the execution fingerprint, so
   earlier plans are rejected as stale.
