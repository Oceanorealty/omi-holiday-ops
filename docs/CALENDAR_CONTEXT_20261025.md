# Calendar context

Portfolio grid and detailed list now offer an optional context checkbox. It adds current locally suggested AUD nightly rates, links to explicit-currency reservation accounts with their current ledger balance/state, active operations tasks due that day, and iCal feed status/history. The checkbox is off by default and retained across property, date and grid/list links. Month/year views remain unchanged. Data explanations are collapsed and date navigation contrast improved.

Prices share the quote engine's arithmetic: date overrides, active global/property rules in ID order, then min/max limits. Calendar prefetch avoids a database query per property-night. Rates are current suggestions, not historical booked rates or OTA publication confirmation. Invalid timezones withhold the rate.

Account balances include credits, payments, refunds and void corrections. Missing accounts remain explicitly untracked. Empty accounts are not labelled paid. Currency stays on each account; no cross-currency total is calculated. Sandbox links are labelled as test funds. Departure-only dates retain account links without occupying that night.

Task dates are stored Brisbane operational dates, not converted timestamps. Only open/in-progress operations tasks are included; old cleaning-dispatch records and unscheduled work are separate. Feed status contains no feed URLs or raw errors and is not proof of an OTA API connection.

Context is administrator-only and non-cacheable. It never writes business records or sends messages. Caps: 1000 relevant pricing rules, 20000 ledger entries, 2000 tasks, 1000 feeds, in addition to the existing 100-property/62-night calendar bounds. Above these limits the page retains the basic inventory with an explicit warning and omits context rather than silently truncating totals. No schema or credentials change; rollback restores the previous UI.

Verification: 499 full SQLite/disposable PostgreSQL checks passed. Added tests cover rate parity and bounded queries, currencies/voids/empty accounts, departures, task/feed scope and secret exclusion, invalid timezone/negative historical prices, opt-in admin routes, escaping and fallback. Local browser showed a synthetic AUD260 part-paid account alongside AUD180 nightly suggestions and a due task; grid/list preserved context. These features do not close remaining entity-room reassignment, children/pets, live payment or channel integration gaps.
