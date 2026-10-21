# OPS release 2026-10-21

Production baseline: bcedbba85361f46ab6cfda8a168941ec65ec4abc, Render service srv-da36gcjbc2fs738c1en0. Release branch release/ops-20261021. Retains current Inter typography and database pre-ping/recycle. The separate B5 payment sandbox branch is not part of this release.

## Included

Admin overview, signed source/cleaning/delivery reviews, local property calendar, reserved-night statistics, audited maintenance work orders and required acceptance checklists. Source-scoped iCal handling, conservative financial finalization and read-only Hostaway shadow adapter. No live Hostaway account/API connection is enabled.

## Runtime behavior

- Scheduled calendar/cleaning sync retains the old enabled default; OPS_SCHEDULER_ENABLED=0 remains an explicit pause option.
- Guest and owner email delivery remains held unless OPS_MESSAGE_DISPATCH_ENABLED=1 / OPS_OWNER_EMAIL_ENABLED=1 are explicitly configured. Imported bookings default notifications_enabled=false. Do not enable old backlogs without review.
- Existing property time zones default UTC until individually confirmed. Calendar displays stored local dates; it does not claim shared inventory or actual guest attendance.
- /healthz remains cheap liveness, including release ID. Authenticated /operations/readiness separately tests a real DB query; failures are 503 without connection details. Avoid frequent database probes against a scale-to-zero free database.
- Non-static pages return private, no-store. Static stylesheet has a new version identifier. Do not place authenticated pages or bearer-link portals behind shared full-page caching.
- No cloud environment values, DNS records, paid plans or credentials are changed by this code release.

## Migration snapshot and rollback

Before additive migration, PostgreSQL takes a one-time consistent same-database table snapshot into ops_backup_20261021, with an atomic manifest; subsequent starts reuse it. Startup fails if the snapshot cannot be made, rather than silently skipping it. Snapshot state and table count are logged, never row contents. This is a rollback aid, not a separate/offsite backup; it shares the database's failure domain and capacity. No tables/columns are dropped. New tables are created via create_all; existing columns use idempotent migrations.

Rollback application to bcedbba via the previous Render deployment. Keep additive tables/columns and the snapshot. Do not automatically restore snapshots over newly entered business data or remove sent-delivery records. Restoring data requires a scoped comparison and reviewed recovery plan. Old application sync can remove cancelled cleaning tasks and re-enable its older email behavior, so pause those paths before any rollback traffic is resumed.

## Validation gates

Local release suite: 80 passed, 3 PostgreSQL tests skipped pending CI. Research suite had 98 tests because it also contained 19 B5 sandbox tests, which do not belong to current production main. CI runs a fresh disposable PostgreSQL 17 database, production-baseline schema migration twice, snapshot preservation, checklist lifecycle, two-writer version conflict and full-app route/readiness checks. Refuses remote/production database URLs. No production data is used in CI.

Merge only after final branch contents match the reviewed release and CI succeeds. Verify Render live commit, snapshot log and authenticated overview/calendar/tasks/readiness plus existing bookings/cleaning/finance. Do not create fake production work orders or trigger live sends/payments for smoke tests.
