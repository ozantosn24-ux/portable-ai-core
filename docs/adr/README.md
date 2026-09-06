# Architecture Decision Records

Nygard-style records of the decisions in this repository that are expensive to relearn, each
citing the source file, test or drill measurement it rests on.

**An ADR is amended by a new ADR, not edited.** A superseded record keeps its text and gains a
status line pointing at its replacement.

| # | Decision | Status |
|---|---|---|
| [0001](0001-failover-only-at-request-boundary.md) | Provider failover only at a request boundary; never splice two providers' output | Accepted 2026-09-06 |
| [0002](0002-ambiguous-outcome-rule.md) | A non-idempotent request is never reissued after an ambiguous failure | Accepted 2026-09-06 |
| [0003](0003-provider-neutral-ports-and-usage-honesty.md) | Provider-neutral ports, optional SDKs, and a `Usage` that admits when it is guessing | Accepted 2026-09-06 |
| [0004](0004-append-only-attempt-ledger.md) | The append-only attempt ledger is the record of truth | Accepted 2026-09-06 |
| [0005](0005-file-based-secrets-in-compose.md) | File-based secrets in Compose, and the mode trap that had to be measured | Accepted 2026-09-06 |
| [0006](0006-healthy-is-not-stable.md) | "Healthy" is not "stable": health, readiness and restore are asserted, not reported | Accepted 2026-09-06 |
| [0007](0007-tenant-acl-in-sql-and-fail-closed-identity.md) | Tenant and ACL are enforced inside the SQL query; identity defaults are fail-closed | Accepted 2026-09-06 — recorded retrospectively |
| 0008 | OIDC login and resource-level authorization | Pending — number reserved until the identity spike lands |
