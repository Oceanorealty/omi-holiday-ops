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

    # Pricing strategy — "manual" (default) means rates are set on each OTA
    # directly; "dynamic" flags intent to hook up a third-party pricing
    # engine (e.g. PriceLabs, Beyond) later, which needs its own paid API
    # account — not something this field alone makes happen.
    pricing_mode = Column(String, default="manual")

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

    # Manual door/lockbox code for this stay. Keynest (or similar) could
    # auto-generate and sync this via their API, but that needs a Keynest
    # account + API credentials we don't have — this field just gives staff
    # somewhere to record the code by hand in the meantime.
    door_code = Column(String, nullable=True)

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
