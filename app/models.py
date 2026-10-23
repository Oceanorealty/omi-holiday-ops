import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import backref, relationship

from app.db import Base


class PlatformName(str, enum.Enum):
    airbnb = "airbnb"
    booking_com = "booking_com"
    ctrip = "ctrip"
    direct = "direct"
    other = "other"


class BookingStatus(str, enum.Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"


class CleaningStatus(str, enum.Enum):
    pending = "pending"
    assigned = "assigned"
    done = "done"


class TriggerEvent(str, enum.Enum):
    booking_confirmed = "booking_confirmed"
    pre_arrival = "pre_arrival"
    check_in_day = "check_in_day"
    post_checkout = "post_checkout"
    guest_re_engagement = "guest_re_engagement"


class MessageStatus(str, enum.Enum):
    sent = "sent"
    failed = "failed"
    skipped_no_email = "skipped_no_email"
    skipped_no_smtp = "skipped_no_smtp"


class StaffRole(str, enum.Enum):
    admin = "admin"  # full access, including managing other staff accounts
    manager = "manager"  # same access as staff; distinguishes seniority in the UI
    staff = "staff"  # everything except the Staff management page
    cleaner = "cleaner"  # same access as staff; distinguishes housekeeping logins in the UI


class StaffUser(Base):
    """A named login, replacing the single shared ADMIN_USER/ADMIN_PASSWORD
    account. That env-var login still works as a fallback (see app/auth.py)
    so this is additive, not a breaking change."""

    __tablename__ = "staff_users"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    username = Column(String, nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    role = Column(Enum(StaffRole), default=StaffRole.staff)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Property(Base):
    __tablename__ = "properties"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    address = Column(String, nullable=True)
    default_cleaner = Column(String, nullable=True)
    time_zone = Column(String, nullable=False, default="UTC")
    created_at = Column(DateTime, default=datetime.utcnow)

    # Hotel-model support: a property can be a sub-unit of a larger listing
    # (e.g. "OMI Collection Broadbeach" the building, with "Superior Studio",
    # "Two-Bedroom Sea View" etc as separately-bookable child units). Each
    # unit still has its own calendar/bookings; occupancy and revenue can
    # roll up to the parent for reporting.
    parent_property_id = Column(Integer, ForeignKey("properties.id"), nullable=True)

    # Housekeeping: how this property's default cleaner is paid, so labor
    # cost can be estimated per task instead of just tracked as a name.
    cleaner_pay_type = Column(String, nullable=True)  # "per_clean" or "hourly"
    cleaner_pay_rate = Column(Numeric(10, 2), nullable=True)

    # Owner portal: the property owner's contact + the commission Omi takes,
    # used to generate owner statements (see OwnerStatement below).
    owner_name = Column(String, nullable=True)
    owner_email = Column(String, nullable=True)
    commission_pct = Column(Numeric(5, 2), nullable=True)  # e.g. 20.00 = 20%
    owner_portal_token = Column(String, nullable=True, unique=True)

    # Outbound combined-availability feed (bookings + manual blocks) —
    # pasted into another platform's "import calendar" field so double-
    # bookings are prevented there, not just flagged here after the fact.
    # See app/sync/ical_export.py.
    ical_export_token = Column(String, nullable=True, unique=True)

    # Pricing strategy — "manual" (default) means rates are set on each OTA
    # directly; "dynamic" uses the rule-based engine below (see PriceRule)
    # to suggest a nightly rate from base_nightly_rate. This is a self-
    # contained rules engine, not a real market-data pricing service like
    # PriceLabs/Beyond — those need their own paid API account.
    pricing_mode = Column(String, default="manual")
    base_nightly_rate = Column(Numeric(10, 2), nullable=True)

    # Public listing fields — surfaced on the omiholiday.com website via
    # /api/public/properties when published=True. Not used anywhere else
    # in the ops tool itself.
    published = Column(Boolean, default=False)
    suburb = Column(String, nullable=True)
    photo_url = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    bedrooms = Column(Integer, nullable=True)
    bathrooms = Column(Integer, nullable=True)
    max_guests = Column(Integer, nullable=True)
    listing_url = Column(String, nullable=True)

    # Guest portal content — shown on the guest's own magic-link page
    # (see Booking.guest_portal_token) so check-in/WiFi/house-rules info
    # doesn't have to be re-typed into every pre-arrival email by hand.
    wifi_info = Column(Text, nullable=True)
    check_in_instructions = Column(Text, nullable=True)
    house_rules = Column(Text, nullable=True)

    # Real per-platform review links, used in the post-checkout email so
    # guests land on the actual review form instead of a generic ask.
    airbnb_review_url = Column(String, nullable=True)
    google_review_url = Column(String, nullable=True)

    ical_feeds = relationship("IcalFeed", back_populates="property", cascade="all, delete-orphan")
    bookings = relationship("Booking", back_populates="property", cascade="all, delete-orphan")
    units = relationship(
        "Property",
        backref=backref("parent", remote_side=[id]),
        foreign_keys="Property.parent_property_id",
    )
    expenses = relationship("Expense", back_populates="property", cascade="all, delete-orphan")


class IcalFeed(Base):
    __tablename__ = "ical_feeds"

    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False)
    platform = Column(Enum(PlatformName), nullable=False)
    url = Column(Text, nullable=False)
    last_synced_at = Column(DateTime, nullable=True)
    last_sync_error = Column(Text, nullable=True)
    active = Column(Boolean, default=True)

    property = relationship("Property", back_populates="ical_feeds")


class Guest(Base):
    __tablename__ = "guests"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    # "en" or "zh" — picks which language a bilingual message template
    # renders in. Defaults to English since platform iCal feeds don't
    # report a language, so staff set this by hand when it matters.
    language = Column(String, nullable=True, default="en")

    bookings = relationship("Booking", back_populates="guest")


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        UniqueConstraint("property_id", "platform", "uid", name="uq_booking_source"),
    )

    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False)
    guest_id = Column(Integer, ForeignKey("guests.id"), nullable=True)
    source_feed_id = Column(Integer, ForeignKey("ical_feeds.id"), nullable=True)
    source_last_seen_at = Column(DateTime, nullable=True)
    source_missing_since = Column(DateTime, nullable=True)
    notifications_enabled = Column(Boolean, nullable=False, default=False)
    marketing_consent = Column(Boolean, nullable=False, default=False)

    platform = Column(Enum(PlatformName), nullable=False)
    uid = Column(String, nullable=False)  # raw UID from the source iCal event, for dedup
    summary = Column(String, nullable=True)

    check_in = Column(DateTime, nullable=False)
    check_out = Column(DateTime, nullable=False)
    status = Column(Enum(BookingStatus), default=BookingStatus.confirmed)

    # iCal feeds don't carry price info, so this is filled in by hand — it's
    # what reconciliation compares incoming transactions against.
    amount = Column(Numeric(10, 2), nullable=True)

    has_conflict = Column(Boolean, default=False)

    # A manually-created hold (owner use, maintenance) rather than a real
    # guest stay — created directly in the app, not synced from a platform.
    # Participates in the same overlap-conflict check as real bookings, but
    # is excluded from the guest-facing bookings table, cleaning-task
    # generation, and guest messaging (see dashboard(), sync_cleaning_tasks(),
    # process_due_messages()).
    is_block = Column(Boolean, default=False)

    # Manual door/lockbox code for this stay. Keynest (or similar) could
    # auto-generate and sync this via their API, but that needs a Keynest
    # account + API credentials we don't have — this field just gives staff
    # somewhere to record the code by hand in the meantime.
    door_code = Column(String, nullable=True)

    # Magic-link token for this guest's own booking page (check-in info,
    # door code, WiFi, house rules) — generated lazily the first time it's
    # needed, same pattern as the owner/cleaner portals.
    guest_portal_token = Column(String, nullable=True, unique=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    property = relationship("Property", back_populates="bookings")
    guest = relationship("Guest", back_populates="bookings")
    cleaning_task = relationship(
        "CleaningTask", back_populates="booking", uselist=False, cascade="all, delete-orphan"
    )
    transactions = relationship("Transaction", back_populates="booking")


class Cleaner(Base):
    """A cleaner staff member. Optional — CleaningTask.assignee (free text)
    still works standalone for quick ad-hoc assignment; linking a Cleaner
    record here is what unlocks the cleaner's own portal + pay tracking."""

    __tablename__ = "cleaners"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    pay_type = Column(String, nullable=True)  # "per_clean" or "hourly"
    pay_rate = Column(Numeric(10, 2), nullable=True)
    portal_token = Column(String, nullable=True, unique=True)
    active = Column(Boolean, default=True)

    tasks = relationship("CleaningTask", back_populates="cleaner")


class CleaningTask(Base):
    __tablename__ = "cleaning_tasks"

    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False)
    cleaner_id = Column(Integer, ForeignKey("cleaners.id"), nullable=True)
    due_date = Column(DateTime, nullable=False)
    assignee = Column(String, nullable=True)
    status = Column(Enum(CleaningStatus), default=CleaningStatus.pending)

    # Quality check — a supervisor marks the clean as inspected/passed
    # after the cleaner reports it done.
    quality_checked = Column(Boolean, default=False)
    quality_notes = Column(Text, nullable=True)
    schedule_locked = Column(Boolean, nullable=False, default=False)
    schedule_review_required = Column(Boolean, nullable=False, default=False)
    schedule_reviewed_checkout = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    booking = relationship("Booking", back_populates="cleaning_task")
    property = relationship("Property")
    cleaner = relationship("Cleaner", back_populates="tasks")


class MessageTemplate(Base):
    """Editable in the dashboard. Seeded with defaults on first startup."""

    __tablename__ = "message_templates"

    id = Column(Integer, primary_key=True)
    trigger_event = Column(Enum(TriggerEvent), unique=True, nullable=False)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    # Optional Chinese versions — used instead of subject/body when the
    # guest's language is "zh". Omi Holiday's whole brand is bilingual
    # EN/中文, so this isn't a generic i18n framework, just the two
    # languages the business actually operates in.
    subject_zh = Column(String, nullable=True)
    body_zh = Column(Text, nullable=True)
    active = Column(Boolean, default=True)


class MessageLog(Base):
    """One row per (booking, trigger_event) — the unique constraint is what makes
    the send idempotent: re-running the trigger check never double-sends."""

    __tablename__ = "message_logs"
    __table_args__ = (
        UniqueConstraint("booking_id", "trigger_event", name="uq_message_booking_trigger"),
    )

    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    trigger_event = Column(Enum(TriggerEvent), nullable=False)
    status = Column(Enum(MessageStatus), nullable=False)
    error = Column(Text, nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow)

    booking = relationship("Booking")


class Transaction(Base):
    """One row per line of an imported payout/bank CSV. Matched by hand against
    a Booking — see app/reconcile/ for the import + matching logic."""

    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=True)
    source = Column(String, nullable=False)  # e.g. "airbnb_payout", "bank_statement"
    amount = Column(Numeric(10, 2), nullable=False)
    occurred_at = Column(DateTime, nullable=False)
    matched = Column(Boolean, default=False)
    # Being matched to a booking just means the amounts line up on paper —
    # this separately confirms the money has actually arrived in the bank,
    # since a platform can report a payout before it settles.
    confirmed_received = Column(Boolean, default=False)
    raw_note = Column(Text, nullable=True)
    imported_at = Column(DateTime, default=datetime.utcnow)

    property = relationship("Property")
    booking = relationship("Booking", back_populates="transactions")


class Expense(Base):
    """Ad-hoc maintenance/operating expense logged against a property,
    entered any time — not tied to a specific booking. Feeds into owner
    statements and the monthly financial summary."""

    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False)
    category = Column(String, nullable=True)  # e.g. "maintenance", "supplies", "cleaning"
    description = Column(String, nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    occurred_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    property = relationship("Property", back_populates="expenses")


class PriceRuleType(str, enum.Enum):
    day_of_week = "day_of_week"  # e.g. boost Fri/Sat nights
    date_range = "date_range"  # e.g. summer holidays, a festival week
    last_minute = "last_minute"  # discount when check-in is soon and still open


class PriceRule(Base):
    """One multiplier applied to Property.base_nightly_rate for nights that
    match. Multiple active matching rules stack multiplicatively — see
    app/pricing.py. property_id=None means the rule applies to every
    property in dynamic pricing mode."""

    __tablename__ = "price_rules"

    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=True)
    name = Column(String, nullable=False)
    rule_type = Column(Enum(PriceRuleType), nullable=False)

    # day_of_week: comma-separated ints, Monday=0 .. Sunday=6, e.g. "4,5"
    days_of_week = Column(String, nullable=True)
    # date_range: inclusive
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)
    # last_minute: applies when (check_in - today).days <= this
    days_before = Column(Integer, nullable=True)

    multiplier = Column(Numeric(4, 2), nullable=False)  # e.g. 1.25 = +25%, 0.85 = -15%
    active = Column(Boolean, default=True)

    property = relationship("Property")


class OwnerStatement(Base):
    """One row per (property, month) — auto-generated from bookings +
    expenses, with a manual adjustment allowed before it's finalized and
    sent to the owner."""

    __tablename__ = "owner_statements"
    __table_args__ = (
        UniqueConstraint("property_id", "period", name="uq_statement_property_period"),
    )

    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False)
    period = Column(String, nullable=False)  # "YYYY-MM"

    gross_revenue = Column(Numeric(10, 2), nullable=False, default=0)
    total_expenses = Column(Numeric(10, 2), nullable=False, default=0)
    commission_amount = Column(Numeric(10, 2), nullable=False, default=0)
    adjustment_amount = Column(Numeric(10, 2), nullable=False, default=0)
    adjustment_note = Column(Text, nullable=True)
    net_payout = Column(Numeric(10, 2), nullable=False, default=0)

    missing_amount_count = Column(Integer, nullable=False, default=0)
    finalized = Column(Boolean, default=False)
    sent_at = Column(DateTime, nullable=True)
    generated_at = Column(DateTime, default=datetime.utcnow)

    property = relationship("Property")


class MessageDelivery(Base):
    """Persisted dispatch claim. SMTP outcomes may be unknown, never blindly retried."""
    __tablename__ = "message_deliveries"
    __table_args__ = (UniqueConstraint("booking_id", "event", "scheduled_for", name="uq_delivery"),)
    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    event = Column(String, nullable=False)
    scheduled_for = Column(String, nullable=False)
    state = Column(String, nullable=False, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class ExternalMapping(Base):
    __tablename__ = "external_mappings"
    __table_args__ = (UniqueConstraint("provider", "account", "external_id", name="uq_external_mapping"),)
    id = Column(Integer, primary_key=True)
    provider = Column(String, nullable=False)
    account = Column(String, nullable=False)
    external_id = Column(String, nullable=False)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False)


class ShadowReservation(Base):
    """Minimal, non-PII provider observations; never trigger operational side effects."""
    __tablename__ = "shadow_reservations"
    __table_args__ = (UniqueConstraint("provider", "account", "external_id", name="uq_shadow_reservation"),)
    id = Column(Integer, primary_key=True)
    provider = Column(String, nullable=False)
    account = Column(String, nullable=False)
    external_id = Column(String, nullable=False)
    external_listing_id = Column(String, nullable=False)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=True)
    payload = Column(Text, nullable=False)
    payload_hash = Column(String, nullable=False)
    source_updated_at = Column(DateTime, nullable=True)
    observed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    state = Column(String, nullable=False, default="unmapped")


class IntegrationRun(Base):
    __tablename__ = "integration_runs"
    id = Column(Integer, primary_key=True)
    provider = Column(String, nullable=False)
    account = Column(String, nullable=False)
    state = Column(String, nullable=False)
    record_count = Column(Integer, nullable=False, default=0)
    error_code = Column(String, nullable=True)
    observed_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class StatementDelivery(Base):
    __tablename__ = "statement_deliveries"
    id = Column(Integer, primary_key=True)
    statement_id = Column(Integer, ForeignKey("owner_statements.id"), nullable=False, unique=True)
    state = Column(String, nullable=False, default="pending")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class OperationsReview(Base):
    """Append-only operator decisions. Contains references and hashes, never credentials."""
    __tablename__ = "operations_reviews"
    id = Column(Integer, primary_key=True)
    token_digest = Column(String, nullable=False, unique=True)
    actor = Column(String, nullable=False)
    entity_kind = Column(String, nullable=False)
    entity_id = Column(Integer, nullable=False)
    action = Column(String, nullable=False)
    note = Column(String(1000), nullable=False)
    before_hash = Column(String, nullable=False)
    after_hash = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class WorkOrder(Base):
    """Independent maintenance/operations task; does not block inventory or charge owners."""
    __tablename__ = 'work_orders'
    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey('properties.id'), nullable=False, index=True)
    title = Column(String(160), nullable=False)
    description = Column(Text, nullable=False, default='')
    category = Column(String(24), nullable=False, default='maintenance')
    priority = Column(String(16), nullable=False, default='normal')
    status = Column(String(24), nullable=False, default='open', index=True)
    assignee_id = Column(Integer, ForeignKey('staff_users.id'), nullable=True)
    due_date = Column(String(10), nullable=True)  # ISO date, Brisbane operational date
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class WorkOrderEvent(Base):
    __tablename__ = 'work_order_events'
    id = Column(Integer, primary_key=True)
    work_order_id = Column(Integer, ForeignKey('work_orders.id'), nullable=False, index=True)
    token_digest = Column(String(64), nullable=False, unique=True)
    actor = Column(String, nullable=False)
    note = Column(String(1000), nullable=False)
    snapshot = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class WorkOrderCheck(Base):
    __tablename__ = 'work_order_checks'
    id = Column(Integer, primary_key=True)
    work_order_id = Column(Integer, ForeignKey('work_orders.id'), nullable=False, index=True)
    label = Column(String(240), nullable=False)
    required = Column(Boolean, nullable=False, default=True)
    completed = Column(Boolean, nullable=False, default=False)
    checked_by = Column(String, nullable=True)
    checked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ReservationEvent(Base):
    """Append-only local reservation history; deduplicates submitted commands."""
    __tablename__ = 'reservation_events'
    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey('bookings.id'), nullable=False, index=True)
    token_digest = Column(String(64), nullable=False, unique=True)
    actor = Column(String, nullable=False)
    action = Column(String(24), nullable=False)
    note = Column(String(1000), nullable=False)
    before_state = Column(Text, nullable=False)
    after_state = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class InventoryDay(Base):
    __tablename__ = 'inventory_days'
    __table_args__ = (UniqueConstraint('property_id', 'night', name='uq_inventory_property_night'),)
    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey('properties.id'), nullable=False, index=True)
    night = Column(String(10), nullable=False)
    nightly_rate = Column(Numeric(10, 2), nullable=True)
    minimum_stay = Column(Integer, nullable=False, default=1)
    maximum_stay = Column(Integer, nullable=True)
    closed_arrival = Column(Boolean, nullable=False, default=False)
    closed_departure = Column(Boolean, nullable=False, default=False)
    stop_sell = Column(Boolean, nullable=False, default=False)


class InventoryEvent(Base):
    __tablename__ = 'inventory_events'
    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey('properties.id'), nullable=False, index=True)
    token_digest = Column(String(64), nullable=False, unique=True)
    actor = Column(String, nullable=False)
    note = Column(String(1000), nullable=False)
    changes = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class OpsCommand(Base):
    """Deduplicated administrative changes to reusable operational definitions."""
    __tablename__ = 'ops_commands'
    id = Column(Integer, primary_key=True)
    token_digest = Column(String(64), nullable=False, unique=True)
    purpose = Column(String(40), nullable=False)
    actor = Column(String, nullable=False)
    note = Column(String(1000), nullable=False)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ChecklistTemplate(Base):
    __tablename__ = 'checklist_templates'
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    items = Column(Text, nullable=False)  # immutable JSON snapshot
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class TaskTemplateApplication(Base):
    __tablename__ = 'task_template_applications'
    __table_args__ = (UniqueConstraint('work_order_id', 'template_id', name='uq_task_template_application'),)
    id = Column(Integer, primary_key=True)
    work_order_id = Column(Integer, ForeignKey('work_orders.id'), nullable=False)
    template_id = Column(Integer, ForeignKey('checklist_templates.id'), nullable=False)


class TaskSchedule(Base):
    __tablename__ = 'task_schedules'
    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey('properties.id'), nullable=False)
    title = Column(String(160), nullable=False)
    description = Column(Text, nullable=False, default='')
    category = Column(String(24), nullable=False)
    priority = Column(String(16), nullable=False)
    assignee_id = Column(Integer, ForeignKey('staff_users.id'), nullable=True)
    template_id = Column(Integer, ForeignKey('checklist_templates.id'), nullable=True)
    checklist_snapshot = Column(Text, nullable=False, default='[]')
    interval_days = Column(Integer, nullable=False)
    next_due = Column(String(10), nullable=False)
    end_date = Column(String(10), nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    version = Column(Integer, nullable=False, default=1)
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ScheduledTaskOccurrence(Base):
    __tablename__ = 'scheduled_task_occurrences'
    __table_args__ = (UniqueConstraint('schedule_id', 'due_date', name='uq_schedule_due'),)
    id = Column(Integer, primary_key=True)
    schedule_id = Column(Integer, ForeignKey('task_schedules.id'), nullable=False)
    due_date = Column(String(10), nullable=False)
    work_order_id = Column(Integer, ForeignKey('work_orders.id'), nullable=False, unique=True)


class WorkOrderCost(Base):
    __tablename__ = 'work_order_costs'
    id = Column(Integer, primary_key=True)
    work_order_id = Column(Integer, ForeignKey('work_orders.id'), nullable=False, unique=True)
    amount = Column(Numeric(10, 2), nullable=True)
    category = Column(String(60), nullable=False, default='maintenance')
    expense_id = Column(Integer, ForeignKey('expenses.id'), nullable=True, unique=True)


class GuestCheckin(Base):
    __tablename__ = 'guest_checkins'
    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey('bookings.id'), nullable=False, unique=True)
    contact_name = Column(String(160), nullable=False)
    contact_phone = Column(String(40), nullable=False, default='')
    party_size = Column(Integer, nullable=False)
    arrival_time = Column(String(5), nullable=False, default='')
    requests = Column(Text, nullable=False, default='')
    rules_hash = Column(String(64), nullable=False)
    rules_snapshot = Column(Text, nullable=False)
    booking_fingerprint = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    submitted_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class GuestCheckinEvent(Base):
    __tablename__ = 'guest_checkin_events'
    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey('bookings.id'), nullable=False, index=True)
    token_digest = Column(String(64), nullable=False, unique=True)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class PricingLimit(Base):
    __tablename__ = 'pricing_limits'
    property_id = Column(Integer, ForeignKey('properties.id'), primary_key=True)
    minimum_rate = Column(Numeric(10, 2), nullable=True)
    maximum_rate = Column(Numeric(10, 2), nullable=True)


class ReservationRequest(Base):
    __tablename__ = 'reservation_requests'
    id = Column(Integer, primary_key=True)
    reference = Column(String(64), nullable=False, unique=True)
    quote_digest = Column(String(64), nullable=False, unique=True)
    payload_digest = Column(String(64), nullable=False)
    property_id = Column(Integer, ForeignKey('properties.id'), nullable=False)
    check_in = Column(String(10), nullable=False)
    check_out = Column(String(10), nullable=False)
    guests = Column(Integer, nullable=False)
    quoted_amount = Column(Numeric(10,2), nullable=False)
    guest_name = Column(String(160), nullable=False)
    guest_email = Column(String(254), nullable=False)
    state = Column(String(24), nullable=False, default='pending')
    version = Column(Integer, nullable=False, default=1)
    booking_id = Column(Integer, ForeignKey('bookings.id'), nullable=True, unique=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class StockGuard(Base):
    __tablename__ = 'stock_guard'
    id = Column(Integer, primary_key=True)
    revision = Column(Integer, nullable=False, default=0)


class StockResource(Base):
    __tablename__ = 'stock_resources'
    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    capacity = Column(Integer, nullable=False)
    time_zone = Column(String(80), nullable=False)


class StockBinding(Base):
    __tablename__ = 'stock_bindings'
    __table_args__ = (UniqueConstraint('property_id','resource_id',name='uq_stock_binding'),)
    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey('properties.id'), nullable=False, index=True)
    resource_id = Column(Integer, ForeignKey('stock_resources.id'), nullable=False, index=True)
    quantity = Column(Integer, nullable=False, default=1)


class PortalConversation(Base):
    __tablename__ = 'portal_conversations'
    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey('bookings.id'), nullable=False, unique=True)
    assignee_id = Column(Integer, ForeignKey('staff_users.id'), nullable=True)
    state = Column(String(16), nullable=False, default='open')
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class PortalMessage(Base):
    __tablename__ = 'portal_messages'
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey('portal_conversations.id'), nullable=False, index=True)
    kind = Column(String(16), nullable=False)  # guest, reply, internal
    actor = Column(String(160), nullable=False)
    body = Column(Text, nullable=False)
    token_digest = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class PortalReadCursor(Base):
    __tablename__ = 'portal_read_cursors'
    __table_args__ = (UniqueConstraint('conversation_id','actor',name='uq_portal_read_actor'),)
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey('portal_conversations.id'), nullable=False)
    actor = Column(String(160), nullable=False)
    last_id = Column(Integer, nullable=False, default=0)


class AgreementPolicy(Base):
    __tablename__ = 'agreement_policies'
    property_id = Column(Integer, ForeignKey('properties.id'), primary_key=True)
    revision = Column(Integer, nullable=False, default=0)
    active_version_id = Column(Integer, ForeignKey('agreement_versions.id'), nullable=True)


class AgreementVersion(Base):
    __tablename__ = 'agreement_versions'
    __table_args__ = (UniqueConstraint('property_id','revision',name='uq_agreement_property_revision'),)
    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey('properties.id'), nullable=False)
    revision = Column(Integer, nullable=False)
    title = Column(String(160), nullable=False)
    body = Column(Text, nullable=False)
    actor = Column(String(160), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class BookingAgreement(Base):
    __tablename__ = 'booking_agreements'
    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey('bookings.id'), nullable=False, index=True)
    version_id = Column(Integer, ForeignKey('agreement_versions.id'), nullable=False)
    snapshot = Column(Text, nullable=False)
    snapshot_hash = Column(String(64), nullable=False)
    booking_fingerprint = Column(String(64), nullable=False)
    actor = Column(String(160), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AgreementAcceptance(Base):
    __tablename__ = 'agreement_acceptances'
    id = Column(Integer, primary_key=True)
    agreement_id = Column(Integer, ForeignKey('booking_agreements.id'), nullable=False, unique=True)
    typed_name = Column(String(160), nullable=False)
    actor = Column(String(160), nullable=False)
    token_digest = Column(String(64), nullable=False, unique=True)
    snapshot_hash = Column(String(64), nullable=False)
    accepted_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AgreementWithdrawal(Base):
    __tablename__ = 'agreement_withdrawals'
    id = Column(Integer, primary_key=True)
    agreement_id = Column(Integer, ForeignKey('booking_agreements.id'), nullable=False, unique=True)
    actor = Column(String(160), nullable=False)
    note = Column(String(1000), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
