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
from sqlalchemy.orm import relationship

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

    ical_feeds = relationship("IcalFeed", back_populates="property", cascade="all, delete-orphan")
    bookings = relationship("Booking", back_populates="property", cascade="all, delete-orphan")


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

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    property = relationship("Property", back_populates="bookings")
    guest = relationship("Guest", back_populates="bookings")
    cleaning_task = relationship(
        "CleaningTask", back_populates="booking", uselist=False, cascade="all, delete-orphan"
    )
    transactions = relationship("Transaction", back_populates="booking")


class CleaningTask(Base):
    __tablename__ = "cleaning_tasks"

    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False)
    due_date = Column(DateTime, nullable=False)
    assignee = Column(String, nullable=True)
    status = Column(Enum(CleaningStatus), default=CleaningStatus.pending)

    booking = relationship("Booking", back_populates="cleaning_task")


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
    raw_note = Column(Text, nullable=True)
    imported_at = Column(DateTime, default=datetime.utcnow)

    property = relationship("Property")
    booking = relationship("Booking", back_populates="transactions")
