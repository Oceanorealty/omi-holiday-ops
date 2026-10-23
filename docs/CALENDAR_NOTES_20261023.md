# Internal calendar notes — development candidate

Adds one internal note per property-local night, visible in the portfolio grid and detailed calendar. Administrators can load 1–62 nights, replace all selected notes or clear them. Each calendar cell opens a single-night editor; date-range editing remains available on the note page. Clearing retains the previous text and revision in the audit record. Notes never block inventory, change rates, send messages or create tasks.

Signed actor/property/range/state forms, same-origin checks and the shared database write guard reject stale or concurrent edits instead of overwriting newer notes. Audit indexes are scoped by property and overlapping dates; the page displays the latest 50 relevant commands and clearly indicates truncation. Note text is escaped in HTML. Public listing/calendar endpoints do not expose internal notes.

Storage: additive `calendar_notes` and `calendar_note_changes` tables, linked to the existing immutable command audit. No existing columns change. The previous application ignores notes on rollback; stock and booking semantics are unchanged.

Validation: cumulative suite 299 passed, zero skipped, on SQLite and disposable PostgreSQL 16.2. Includes atomic three-night writes, stale forms, changed ranges, invalid inputs, clear-history preservation, cross-property isolation, unchanged stock, public API exclusion and two PostgreSQL writers using one old version. Native browser verification used a Guest session and synthetic local data: created three notes, viewed them in the grid alongside unchanged available units, then cleared one night and verified before/after history.

This local candidate includes the preceding guest-portal release rules (288-test baseline). It is not deployed. The current GitHub connector cannot write and the former in-app browser session is unavailable, so this cumulative notes candidate has not been published. A patch and source bundle are available in outputs. No real listings, staff, guests, notifications or payments were introduced. Formal operational activation remains pending the broader feature completion and acceptance work.
