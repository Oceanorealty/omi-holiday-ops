# B5 sandbox checkout — rollout and verification

This is a **backend draft**, not a live payment launch. The routes remain
disabled by default and reject live Stripe keys/events. No credentials are
included. Do not enable sandbox payments on the production database: the normal
scheduler would treat test bookings as real stays and could send guest emails.

## Configuration on a separate test deployment

- Use a separate database with test listings, a base nightly rate, and only
  controlled test guest addresses. Keep outbound mail disabled.
- Set `PUBLIC_BOOKING_ENABLED=test`.
- Set a random `SECRET_KEY` of at least 32 characters. It signs quotes and
  checkout status tokens; keep it stable while checkouts are outstanding.
- Configure the sandbox `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` in the
  hosting environment. Never commit their values. Live keys are rejected.
- Register a **snapshot** `payment_intent.succeeded` webhook at
  `/api/public/stripe-webhook`. The raw request body is signature-verified.
- `Base.metadata.create_all()` creates the additive `public_payments` table on
  startup. Existing booking columns and records are unchanged. Review this on a
  copied PostgreSQL database before deploying.

## API flow

1. `GET /api/public/quote?property_id=…&check_in=…&check_out=…&guests=…`.
   Only proceed when `bookable` is true. Calendar, quote and checkout now all
   respect manual versus dynamic pricing.
2. Generate a UUID for this checkout session. `POST /api/public/booking`:

   ```json
   {
     "checkout_id": "a unique UUID for this checkout session",
     "property_id": 3,
     "check_in": "YYYY-MM-DD",
     "check_out": "YYYY-MM-DD",
     "guests": 2,
     "guest": {"name": "Test Guest", "email": "controlled-test@example.com", "language": "en"},
     "quote_token": "the unexpired signed token from step 1"
   }
   ```

   Returns `payment_intent_id`, `client_secret`, `access_token`, amount and AUD
   currency. Keep these in the checkout session; never put them in logs or URLs.
   Retry the same UUID with identical booking/customer fields after a network
   error. A different purchase uses a new UUID. An ambiguous intent creation
   older than 23 hours is blocked for reconciliation, since Stripe may prune
   its idempotency key after 24 hours.
3. Use Stripe.js and the sandbox publishable key to confirm the card payment.
   A sandbox embed template is included in `docs/B5-wordpress-embed.html` and
   `app/static/public-booking.js`. Replace its test service origin, test property
   ID and public `pk_test_` key. It must be served from an allowed CORS origin or
   the same test service origin. Never place secret keys in this component.
4. Stripe's signed callback validates the persisted payment snapshot and
   creates one direct/confirmed booking. No guest details go into Stripe metadata.
5. Poll `GET /api/public/booking/{payment_intent_id}` with
   `Authorization: Bearer {access_token}`. Response states include `creating`,
   `awaiting_payment`, `confirmed`, `refund_pending`, `refund_failed`, `refunded`.
   Only `refunded` means the provider reported successful completion.
   The endpoint never returns a guest-portal magic link or guest details.

## Integrity and recovery

- PaymentIntent creation is idempotent per persisted checkout. Existing intent
  IDs are retrieved, not recreated. Provider errors return 503 without secrets.
- Callbacks match intent ID, test mode, success status, received amount and
  currency against the local snapshot before booking.
- SQLite uses `BEGIN IMMEDIATE`; PostgreSQL serializes checkout/fulfillment with
  `SHARE ROW EXCLUSIVE` locks on bookings and public payments (5s lock timeout).
  Two website payments cannot both pass the check and commit overlapping stays.
  The deliberately coarse locks may reduce throughput and must be tested on
  PostgreSQL under realistic imports before rollout.
- Conflicts or unpublished listings trigger a full refund. `refund_pending` is
  committed before the provider call. Failed/pending refunds return 503 so the
  signed callback can retry; a deterministic refund idempotency key avoids a
  second request, and known refunds are retrieved on subsequent callbacks.
- Staff must reconcile `creating` attempts older than 23 hours and any
  `refund_pending`/`refund_failed` records that remain after Stripe stops
  retrying. This draft has no separate automatic reconciliation worker or alert
  dashboard. A failed refund requires investigation; never assume it succeeded.
- Existing 30-minute scheduler creates cleaning tasks and messages after booking
  commit. The callback does not send mail or run tasks for unrelated bookings.
- OTA iCal remains asynchronous and staff may explicitly override inventory.
  This mechanism does not reserve inventory on external platforms or prevent
  conflicts discovered by later imports. Full channel inventory requires a
  separate integration.

## Verification completed and still required

Run `python -m pip install -r requirements.txt -r requirements-dev.txt` then
`python -m pytest -q`. Tests use isolated SQLite databases and mocked Stripe;
they start no scheduler and make no real payment or email calls.

Before enabling any test deployment, verify PostgreSQL concurrency/lock-timeout
behavior and additive schema setup. Then use Stripe sandbox cards to verify
intent creation, signature verification, duplicate deliveries, booking insertion,
conflict refund, pending/failed refund recovery, and status polling. Complete the
WordPress frontend integration and browser payment testing separately. Live payment support and paid production rollout
are explicitly outside this draft.

To run the same suite against dedicated test PostgreSQL as well, provide
`B5_TEST_POSTGRES_URL` through the test environment. The suite rejects database
names other than `omi_holiday_ops_b5_test_db`, and creates/drops a separate random
schema per test so application tables remain untouched. No Stripe network calls
or scheduler tasks run in these tests.

References: [Stripe webhooks](https://docs.stripe.com/webhooks),
[idempotent requests](https://docs.stripe.com/api/idempotent_requests),
[PaymentIntent creation](https://docs.stripe.com/api/payment_intents/create),
[refund creation](https://docs.stripe.com/api/refunds/create).
