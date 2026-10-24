# Responsive operations interface and calendar recurrence

Desktop uses a grouped sidebar with the current group open; phones/tablets use a labelled keyboard-operable menu. The overview offers four common actions. Duplicate desktop section navigation is hidden, while mobile section navigation remains scrollable. Header navigation CSS no longer hides calendar controls or pagination. JavaScript is progressive: without it the menu stays visible and tables stay scrollable.

Inter is self-hosted from the official rsms/inter repository (Git blob 5a8d3e72ad7ffb62af3b146e1b1f54ab5813a212); license included in app/static/Inter-LICENSE.txt. External Google Fonts requests removed. Shared controls have clearer focus, contrast, touch targets, numeric alignment and system CJK fallbacks. Selected operational lists turn into labelled mobile cards; calendars and financial matrices preserve horizontal scrolling and keyboard focus.

Recurring plans now accept days (1–365), weeks (1–52), or calendar months (1–12). Monthly schedules keep their initial day, clamp to a shorter month's last day, then restore the initial day in later months. The new additive task_schedule_calendar_rules table stores immutable anchors. Existing day schedules are untouched. The per-plan transaction lock and unique occurrence remain the duplicate prevention boundary. No notification provider is added.

Validation: 515 tests passed with disposable PostgreSQL, including repeated legacy upgrade and concurrent monthly generation. Browser acceptance on a synthetic loopback database created an October 31 monthly plan, generated its task, and showed November 30 as the next date. Main routes checked at 320px; menu/Escape and calendar controls checked at 390px; task layout/current navigation checked at 768/1024/1440px. No console errors in the inspected flow. No production task or booking data used in tests.

Rollback: pause all monthly plans before deploying code predating calendar recurrence; old generators do not understand their month anchors. Preserve the additive rule table. This does not complete the full remaining product gap list, enable production payments, or establish any OTA connectivity.
