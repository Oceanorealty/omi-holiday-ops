# Internal custom fields — local candidate

Administrator-managed definitions and record values cover properties, reservations and work orders. Five fixed types: text (4000 characters), finite number (absolute value <= 1e12, six decimal places), ISO date, explicit boolean, and a predefined choice. Each scope supports 50 definitions including archived definitions. Keys and types are immutable; archive/restore retain values. Blank clears a value while retaining revision history.

All active fields are submitted together. A signed form binds actor, record type, record ID, definition versions and current value revisions. Changed definitions or record values reject stale submissions. Values are validated before writes, and the shared database write guard serializes changes. Changes and reasons are recorded transactionally; type, label and before/after values are included in record history. Definition creation/archive/restore also records an operations command. Internal fields never join public listing, guest portal or outbound channel payloads.

Additive tables: custom_field_definitions, custom_field_values, custom_field_changes. No provider credentials or production data required. Values have server-validated polymorphic record references; no automatic record deletion cascade exists. Guest questionnaires, automation variables and employee field permissions are separate workflows. Candidate is local, not deployed.


## Search and CSV update follow-up

Administrator search covers one record type at a time with optional field/property filters, literal case-sensitive value matching, archived-field inclusion and 50-value pages. Cleared values, orphan records and reservation blocks are omitted. CSV exports use the same filters, include up to 5000 values and neutralize formula-like cells. No public or guest endpoint includes these data.

UTF-8 CSV import accepts up to 100 field values / 512 KiB for one record type. Existing active field keys and records only; blank clears a value, other fields remain. Preview has no database writes and shows before/after values. A signed plan binds the actor, content and all affected record/definition revisions for 15 minutes. Apply runs all record updates and their audits in one transaction under the shared write guard. Any stale row, invalid value or invalid reason rolls back the entire batch. Replay is rejected. No new schema or provider account is required. Exported formula protection is retained if reimported literally; review leading apostrophes in the preview before applying.

Import does not create records/definitions, perform guest registration, evaluate spreadsheet formulas or create automation variables. Candidate remains local until published and deployed separately.
