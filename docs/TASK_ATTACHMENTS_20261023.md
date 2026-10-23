# Task attachments — local candidate

Administrators can upload, download, archive and restore task evidence files. Supported extensions: PDF, PNG, JPEG and UTF-8 text. Filename normalization, matching header checks, server-selected media types, 5 MiB files and a streaming request-body ceiling apply. Original bytes, SHA-256 and metadata are stored in PostgreSQL/SQLite rather than ephemeral application disk. Files are always downloaded as attachments with no-sniff/sandbox/private-cache headers, never embedded inline.

Limits: 20 files per task and 100 MiB total stored bytes, including archived files. The global write guard and task version claim protect quota/state changes. Archive is reversible and preserves the original file and audit; it does not purge bytes or free quota. Downloads validate the task scope, current archive state, byte count and hash. No public, guest or employee self-service attachment access is exposed. These checks are not a malware-scanning or identity-verification service.

Additive `task_attachments` table. Previous application ignores file records on rollback. No existing column, external storage service, real staff account or production data is changed. Generic property/reservation custom fields and broader file workflows remain separate audit items. This candidate is local and not deployed.
