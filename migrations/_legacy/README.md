# migrations/_legacy/

Pre-v4 hand-run migration scripts. Preserved for historical reference.
The runner ignores this directory (underscore-prefixed names are skipped).

## 001_add_failed_status.sql

A one-shot script that added 'failed' to the apps.status CHECK constraint.
That work is now done by migrations/002_normalize_apps_status_check.sql,
which is idempotent and applied automatically by the migration runner.
