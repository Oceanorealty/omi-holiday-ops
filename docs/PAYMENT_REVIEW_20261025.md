# Payment attempt review · 2026-10-25

The admin payment page now offers a review queue for pending/failed refunds, creating or awaiting-payment attempts older than 23 hours, and confirmed attempts without a booking-account link. Filters combine with state selection and persist across pagination. Creation timestamps use UTC.

Age is a review signal, never proof that a charge failed or a payment intent expired. The page does not call Stripe, retry a payment, refund, mutate records, reconcile old accounts automatically or enable live payments. The queue does not include every possible accounting inconsistency; existing linked-account checks remain visible in the full list.

Validation: 36 focused SQLite payment tests; 451 passed / 14 skipped in the SQLite full run; 501 passed with disposable local PostgreSQL enabled. Tests use synthetic data and mock Stripe. No live key was created, read or saved. Live-account payment/payout activation was checked in the Stripe Dashboard only; it does not establish OPS production payment readiness.
