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
    staff = "staff"  # everything except the Staff management page


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

    finalized = Column(Boolean, default=False)
    sent_at = Column(DateTime, nullable=True)
    generated_at = Column(DateTime, default=datetime.utcnow)

    property = relationship("Property")
