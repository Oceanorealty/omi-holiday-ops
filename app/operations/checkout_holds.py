"""Read-only lease queries shared by local stock and sandbox checkout."""
from datetime import datetime, timezone
from app.models import PublicCheckoutHold, PublicPayment

HOLD_MINUTES = 15


def active_hold_query(db, now=None):
    return db.query(PublicPayment.id, PublicPayment.property_id, PublicPayment.check_in,
        PublicPayment.check_out).join(PublicCheckoutHold,PublicCheckoutHold.payment_id==PublicPayment.id).filter(
        PublicPayment.state.in_(['creating','awaiting_payment']),
        PublicCheckoutHold.expires_at > (now or datetime.utcnow()))


def hold_status(db,payment):
    hold=db.get(PublicCheckoutHold,payment.id)
    now=datetime.utcnow()
    active=bool(hold and hold.expires_at>now and payment.state in ('creating','awaiting_payment'))
    return {'hold_expires_at':hold.expires_at.replace(tzinfo=timezone.utc).isoformat() if hold else None,
        'hold_active':active,
        'hold_seconds_remaining':max(0,int((hold.expires_at-now).total_seconds())) if active else 0}
