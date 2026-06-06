# Security Policy

## Reporting a vulnerability

Please report security issues through GitHub Security Advisories instead of a
public Issue. Do not include OpenStack credentials, tokens, internal addresses,
resource identifiers, or production screenshots in public reports.

## Deployment warning

OpenStack Audit can delete compute instances, storage volumes, and network
ports. The current version does not include built-in authentication,
authorization, approval workflows, or CSRF protection.

Before using this project outside a test environment:

- Deploy it only on a controlled network.
- Add authentication and access control at a trusted reverse proxy or gateway.
- Use a least-privilege OpenStack service account.
- Keep credentials in environment variables or a secret manager.
- Back up the SQLite database.
- Validate synchronization and recycle behavior in a non-production project.

## Supported versions

Security fixes are currently applied to the latest commit on the `main` branch.
