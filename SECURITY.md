# Security policy

## Supported versions

Gimme is pre-1.0 software. Security fixes are applied to the latest revision on the
`main` branch.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. Please do not open
a public issue for suspected credential exposure, command execution, privilege
escalation, or SSH trust-boundary vulnerabilities.

Include the affected revision, reproduction steps, impact, and any suggested
mitigation. Do not include live credentials or destructive proof-of-concept payloads.

The most security-sensitive surfaces are the root-owned provisioning helper, its
sudoers rule, SSH and agent-forwarding behavior, manifest validation, and generated
application environment files.
