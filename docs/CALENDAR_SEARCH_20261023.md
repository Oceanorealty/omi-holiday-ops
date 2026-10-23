# Portfolio calendar and reservation search follow-up

Based on deployed main 4035eaa4c0c46e17152b0ae88d8b450439afeb26. This follow-up is not deployed.

The calendar now defaults to a horizontal property/night grid, with sticky date/property headings, available-unit counts, links to reservations, arrivals/departures, stop-sell, blocks and review flags. Previous/next period and detailed-list switching preserve dates and property scope. Existing shared-resource calculations remain authoritative; listing availability cannot be summed as physical room stock. This is a read-only view, not drag-and-drop editing or live OTA synchronization.

Reservation lookup adds guest-name/email/source-reference substring search, exact numeric ID matching, property/source/status filters, overlapping-night date filters and bounded 200-row pages. Search wildcards are treated literally. Pagination retains filters and stable check-in/ID ordering. Pages are a live view, so concurrent record edits may change results. Administrator access remains required. No new public guest fields, financial actions or schema changes.

Validation: 274 tests passed, zero skipped, SQLite and disposable PostgreSQL 16.2. Synthetic browser checks verified date/property view switching and combined guest/source/night filters; checkout-night searches excluded the departing stay. No production records were changed. Pixel screenshot capture remains unavailable. External CI is a separate publication gate.
