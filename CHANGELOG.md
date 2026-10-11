# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-11

First release.

### Added

- Write probe: one committed INSERT on a fixed schedule, with libpq keepalives,
  `tcp_user_timeout` and `statement_timeout`, reconnecting (and re-resolving
  DNS) after every error. Every attempt is recorded; failures are data.
- Analysis: outages, client-side write gap, node switches (server address or
  postmaster start time), acknowledged-write loss check, in-doubt writes, and a
  DR check (RPO from writes missing on a promoted replica).
- `pghadr drill`: HA drill with `planned-failover`, `forced-failover`,
  `restart`, any shell `command`, or `none`.
- `pghadr dr-drill`: forced promote of a cross-region read replica, measuring
  RPO and the time until the replica accepts writes.
- `pghadr probe`, `status`, `init` and `cost`.
- Markdown + JSON reports and per-attempt JSON lines.
- Bicep for a zone-redundant HA `Standard_D2ds_v5` Flexible Server and an
  optional cross-region read replica; `scripts/up.sh` and `scripts/down.sh`.
- Unit tests with a fake connection, integration tests that cause real outages
  on Postgres 16, and a CI drill that restarts the Postgres container.
