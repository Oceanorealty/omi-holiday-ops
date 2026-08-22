"""
One cleaning task per booking, due the day of checkout. Runs after every
sync so a newly-seen booking gets a task, and a cancelled booking's task
(if not already done) is removed rather than left dangling.
"""

from sqlalchemy.orm import Session

from app.models import Booking, BookingStatus, CleaningStatus, CleaningTask


def sync_cleaning_tasks(db: Session) -> None:
    # Manual blocks (owner use, maintenance) don't need a cleaning task
    # auto-created — there's no guest checkout driving one.
    bookings = db.query(Booking).filter(Booking.is_block.is_(False)).all()

    for booking in bookings:
        task = booking.cleaning_task

        if booking.status == BookingStatus.cancelled:
            if task and task.status != CleaningStatus.done:
                db.delete(task)
            continue

        if task is None:
            db.add(
                CleaningTask(
                    booking_id=booking.id,
                    property_id=booking.property_id,
                    due_date=booking.check_out,
                    assignee=booking.property.default_cleaner,
                    status=(
                        CleaningStatus.assigned
                        if booking.property.default_cleaner
                        else CleaningStatus.pending
                    ),
                )
            )
        else:
            # Checkout date may have shifted (guest extended/shortened the stay).
            task.due_date = booking.check_out

    db.commit()
