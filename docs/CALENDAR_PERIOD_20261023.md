# Monthly and yearly property calendars — local candidate

Adds selected-property month and year views with Monday-aligned weeks, leap-day support, previous/next navigation and year-to-month date links. Daily availability, reservation links, arrivals, departures, block/stop-sell and review indicators reuse existing explicit shared-resource calculations. Monthly cells show internal notes and single-date note editing links. The existing portfolio grid links to both views with its selected property and dates.

The annual view assembles twelve bounded monthly queries for one property. Counts from linked listings must not be added together. This is a live read-only view, not a snapshot or channel synchronization confirmation. Years are bounded to 2–9998 so adjacent dates remain representable. No schema or external setting changes.

A regression exposed an older calendar selection bug: bookings checking out exactly at midnight on the first displayed date were omitted entirely, hiding their departure marker. They are now included for departure display; they still consume no inventory on the checkout night.

Synthetic tests cover leap February, departure-only first days, 366-day yearly rendering, property scope, secret exclusion, admin access, invalid inputs and lower/upper supported years. This is a local candidate, not deployed; all real property/staff onboarding remains deferred until full operational acceptance.
