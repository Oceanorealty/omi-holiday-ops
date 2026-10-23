# Guest portal release rules — development candidate

Baseline: deployed main 731976b92fde9621d916b50c0e538b31d27087fd. This change is not deployed. The operator plans to introduce real listings and employees only after the remaining features and operational acceptance are complete. Use synthetic data for development; the currently deployed site is not evidence of formal operational readiness.

## Behavior

Each property can independently gate its portal door code, WiFi and check-in instructions. All selected conditions must pass: hours before stored property-local check-in, current arrival submission, acknowledgement of the latest issued agreement, cleared and reviewed booking account, and optionally hide from checkout onward. Gate values are omitted server-side, not merely hidden by CSS. Check-in, agreement, balance and conversation actions remain accessible while private details are withheld. No saved rule preserves existing field display. Clearing all conditions restores that display. Rules affect existing and future stays for the property.

The account gate requires an active charge, no outstanding balance, and an explicit operator review bound to both current reservation details and ledger history. Date/amount/guest/status changes or ledger changes invalidate that review. Issuing a fixed statement does not. Manual receipts are not payment-provider verification; the operator must reconcile the account against the stay and external evidence before confirming. Creating an empty account cannot release details. This does not move money or execute refunds.

Hours are measured against aware UTC instants converted from the property's stored local times, including daylight-saving transitions. Check-in times stored at midnight are still midnight; arrival-form preferences do not silently change them. Invalid configured timezone/date state withholds gated fields. Confirm real property timezones before use.

These rules control the three portal fields only. They cannot retract already viewed/copied information or control historical messages, emails, uploaded documents or independently entered text containing secrets. They do not verify identity, remotely lock devices, activate payment providers or constitute staff/property permission management.

## Storage and review

Additive tables `guest_access_rules` and `guest_account_clearances`. Signed actor/purpose/revision forms, same-origin validation, shared reservation write guard, deduplicated command audit and stale-review checks. No existing schema columns or provider settings change. Rollback retains the tables, but an old app ignores these gates and exposes the legacy portal fields; restrict portal traffic or restore reviewed access behavior before rolling back.

## Verification

288 full tests passed, zero skipped, with SQLite and disposable PostgreSQL 16.2. Tests cover secret omission, unchanged defaults, independently gated fields, conditions together, changed agreements/dates/accounts, empty accounts, statement stability, invalid inputs, stale forms, admin/origin checks, daylight saving and concurrent rule submissions. The full PostgreSQL smoke list includes the new route.

Synthetic browser verification saved a 24-hour gate for demonstration instructions. The guest page omitted the original instruction text, displayed the closer-to-check-in message and retained the arrival form. No production rule, guest record or real listing was changed. External CI remains a separate gate. Screenshot capture has been unavailable in this environment.

## Coverage

This addresses the portal-visibility portion of audit F70. Pre-authorization conditions remain unavailable until an actual payment provider is implemented. All other audit rows retain their individual status; 288 tests do not mean complete product parity or formal readiness.
