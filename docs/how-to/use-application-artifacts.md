# Build once and deploy an Application Artifact

Use artifact mode when staging or production Targets must release reviewed application bytes
without checking out source or installing dependencies. Gimme builds on one registered Target,
publishes to an existing versioned S3-compatible store, and gives Deployment Targets read-only
access to the exact publication.

## Prerequisites and migration

The store must already exist, have versioning enabled, require TLS, and use AES-256 or KMS
server-side encryption. Gimme does not create a bucket or IAM policy. Configure separate
publisher and reader identities: the Build Target needs write/read/exact-version-delete access;
Deployment Targets need exact-version read access only. A reader failure is terminal—Gimme never
substitutes publisher credentials.

Schema v7 requires an explicit `release_mode` for every Deployment. Use
`plan_state_migration` and `apply_state_migration` to choose `source` for local/preview work or
`artifact` for any stage. Artifact mode also requires a named Artifact Store and an Application
`build` policy with a Deployment-capable Build Target and `laravel_v1` packaging. Review the
complete migration plan before applying it; no mode or credential policy is inferred.

Register the store with `plan_register_artifact_store` / `register_artifact_store`, then prove
publisher capability on the Build Target with `plan_verify_artifact_store_publisher` /
`verify_artifact_store_publisher`. Pass its returned reader capability version to
`plan_verify_artifact_store_reader` / `verify_artifact_store_reader` for each Deployment Target.
These probes use derived object names and return bounded evidence, not credentials or object
keys.

## Build and publish

Call `plan_build_artifact` for an artifact-mode Deployment. The plan binds its exact commit,
dependency locks, runtime pins, extensions, platform capability, packaging policy, and Gimme
execution fingerprint into `build_id`. Apply the unchanged plan with `build_artifact`.

The Build Target uses fixed frozen Composer and frontend-manager behavior in an isolated
workspace. It verifies deterministic archive and immutable-tree digests, reads back the encrypted
upload, and publishes the provenance manifest last. `list_artifacts` and
`gimme://applications/{name}/artifacts/{build_id}` expose bounded publication status. A matching
repeat is idempotent. `non_reproducible_build` means the same public inputs produced different
bytes; investigate the build rather than trying to overwrite the first publication.

Build secrets are Application policy references, not Deployment secrets. Gimme resolves them
only after plan acceptance, supplies them only to fixed build processes, removes temporary
material, and scans the package for exact values. Public state, plans, resources, journals, and
results expose only counts or hashes—not secret names, references, or values.

## Deploy to multiple Targets

For each compatible Deployment, call `plan_deployment` and apply its exact `plan_id` with
`apply_deployment`. The reader resolves the exact manifest and package versions, verifies archive
and tree digests, safely extracts into a new release, and then uses the normal environment,
migration, candidate/live health, activation, and process-refresh flow.

Artifact deployment never runs Git, Composer, Node, a package-manager install, or a frontend
build on the destination. Once published, deployment remains possible if the Build Target and Git
source are unavailable. Reusing the same compatible inputs on a second Target yields the same
`build_id` and exact publication.

## Promote and roll back

Use `plan_promotion` / `promote_deployment` to reuse the source Deployment's verified live
publication. Promotion revalidates the destination context and reader access; it never rebuilds.
`artifact_incompatible` means the destination inputs imply a different build, so publish that
context first.

Use `plan_rollback_deployment`, review the selected retained release, then call
`rollback_deployment` with the exact `plan_id` and displayed confirmation. Artifact rollback
rechecks readonly metadata, the immutable tree, runtime compatibility, and health before the
symlink switch. Tampered or missing retained releases fail closed and are never repaired from Git.

## Failures, cleanup, and cost

Missing publications, reader denial, changed object versions, stale plans, checksum failure,
unsafe archives, and release tampering stop before activation with fixed error codes. Interrupted
downloads and incomplete releases are removed; retry by replanning unless the reviewed plan is
still current. Existing `keep_releases` policy cleans extracted Target releases only.

Ordinary repository verification uses local fakes and temporary directories and creates no
billable resources. External S3-compatible storage, requests, KMS, network transfer, and Build
Target runtime are operator-provided and may incur charges. Gimme does not delete, garbage-collect,
mirror, or expire published artifacts or manifests; configure no lifecycle rule that would break
the required publication durability without an independent operational decision.

See [MCP reference](../reference/mcp.md) for the complete interface contract and
[artifact acceptance evidence](../reference/application-artifact-evidence.md) for the verified
security and lifecycle coverage.
