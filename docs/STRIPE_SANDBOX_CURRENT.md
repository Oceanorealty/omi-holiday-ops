# Stripe sandbox adaptation for current OPS

Recovered from the October 20 B5 draft. Default disabled; this is not a live payment release. No provider credentials are included.

## Separate sandbox environment only

Use a dedicated test database and controlled test guest records. Set PUBLIC_BOOKING_ENABLED=test, OPS_PAYMENT_ENVIRONMENT=sandbox, OPS_SCHEDULER_ENABLED=0, OPS_MESSAGE_DISPATCH_ENABLED=0 and OPS_OWNER_EMAIL_ENABLED=0. Use a signing secret at least 32 characters long plus Stripe test key and sandbox webhook signing secret in environment configuration. The module refuses live keys/events. Do not enable it against the production business database.

POST /api/public/booking accepts checkout UUID, property_id, check_in, check_out, guests, guest name/email/phone/language and the current quote-v2 token from GET /api/public/quote. Retry the same UUID with identical fields. Client secrets and status tokens belong only in the checkout session, never URLs or logs. POST /api/public/stripe-webhook verifies the raw body and snapshot payment_intent.succeeded event. GET /api/public/booking/{payment_intent_id} requires the returned access token in the Authorization Bearer header.

Inventory uses the same global write guard as staff reservations and iCal imports, including shared resources. Checkout checks current prices, guest capacity, local arrival date, nightly restrictions and booking windows. Fulfillment rechecks availability and restrictions; unavailable stays request idempotent sandbox refunds. Concurrent payments cannot exceed local pool capacity. This does not reserve OTA stock, hold unpaid inventory or eliminate delayed iCal conflicts.

Refund pending/failed states remain explicit; delayed provider results cannot overwrite an already completed refund. Ambiguous creates older than 23 hours require reconciliation instead of creating another PaymentIntent. No automatic reconciliation worker is supplied. Payment attempts are reviewable at /operations/payments (administrator only, no-store). That page shows configuration booleans, not credentials, customer contact details, client secrets or access tokens.

## Outstanding before live support

A separate local sandbox has now passed a real test-card A$2 payment through Stripe Elements, CLI-forwarded signature verification and booking confirmation; an inventory conflict after payment also produced a provider-confirmed A$2 sandbox refund with no guest booking created. The same-origin test page uses the current quote format. 3DS challenge/decline tests, cross-origin WordPress embedding, production hosting and the live website flow are still not verified. No live card charging, deposits/preauthorization, staff-triggered refunds, dispute processing, installment schedule or provider-to-ledger reconciliation is claimed. Sandbox bookings must never be mixed with live guest accounts. Legacy frontend snippets are reference material, not verified current code.

Schema changes are one additive public_payments table. Existing bookings/columns are unchanged. Disabled endpoints reject requests before payment/database mutation; old application code can run with the extra table retained. Apply normal deployment backup and rollback procedures before publishing; a same-database snapshot is not an offsite backup.

Tests use synthetic keys, mocked provider calls and separate SQLite/PostgreSQL databases; they make no actual payment or email calls.

References: https://docs.stripe.com/webhooks and https://docs.stripe.com/api/idempotent_requests (reviewed October 24, 2026).

## Local test page

GET /api/public/test-checkout?property_id=... is available only when sandbox isolation is configured and STRIPE_PUBLISHABLE_KEY begins pk_test_. It is no-store/noindex with no-referrer. Card fields belong to Stripe Elements. A network-interrupted checkout keeps its original UUID and immutable payload on retry; status is confirmed only by the server callback. Keep the tab open: there is no reload/session recovery yet. No tokens or secrets are placed in application-generated URLs. The manual status action resumes polling without starting another payment.
