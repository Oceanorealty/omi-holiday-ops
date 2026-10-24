"""Link a newly fulfilled sandbox checkout to its isolated booking account atomically."""
import hashlib,json
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from app.models import BookingAccount,BookingLedgerEntry,PublicPaymentAccount
from app.operations.tasks import TaskError
from app.operations.task_plans import record_command


def link_new_payment(db,payment,booking):
    if db.get(PublicPaymentAccount,payment.id):raise TaskError('Payment account link already exists')
    if db.get(BookingAccount,booking.id):raise TaskError('An existing account needs reconciliation before linking')
    if payment.state!='confirmed' or payment.booking_id!=booking.id or booking.amount!=payment.amount:
        raise TaskError('Payment snapshot does not match the reservation')
    when=datetime.now(timezone.utc).astimezone(ZoneInfo(booking.property.time_zone)).date().isoformat()
    db.add(BookingAccount(booking_id=booking.id,currency=payment.currency,version=1));db.flush()
    reference='stripe-sandbox:'+payment.payment_intent_id
    if db.query(BookingLedgerEntry).filter_by(reference_digest=hashlib.sha256(reference.casefold().encode()).hexdigest()).first():
        raise TaskError('Provider reference is already recorded')
    common={'booking_id':booking.id,'amount':payment.amount,'occurred_on':when,'actor':'stripe-sandbox',
        'evidence_note':'Signed Stripe sandbox success callback verified; no real money moved.'}
    charge=BookingLedgerEntry(**common,kind='charge',description='Sandbox reservation accommodation')
    received=BookingLedgerEntry(**common,kind='payment',description='Sandbox Stripe payment; not real funds',
        reference=reference,reference_digest=hashlib.sha256(reference.casefold().encode()).hexdigest())
    db.add_all([charge,received]);db.flush()
    receipt={'entry_id':received.id,'booking_id':booking.id,'property_name':booking.property.name,
        'guest_name':booking.guest.name if booking.guest else None,'kind':'payment','currency':payment.currency,
        'amount':str(payment.amount),'description':received.description,'occurred_on':when,
        'recorded_at':received.created_at.isoformat(),'environment':'sandbox'}
    received.receipt_snapshot=json.dumps(receipt,sort_keys=True)
    received.receipt_snapshot_hash=hashlib.sha256(received.receipt_snapshot.encode()).hexdigest()
    snapshot=json.dumps({'environment':'sandbox','checkout_id':payment.id,'booking_id':booking.id,'intent_id':payment.payment_intent_id,
        'currency':payment.currency,'amount':str(payment.amount),'charge_entry_id':charge.id,'payment_entry_id':received.id},sort_keys=True)
    db.add(PublicPaymentAccount(payment_id=payment.id,booking_id=booking.id,charge_entry_id=charge.id,payment_entry_id=received.id,
        snapshot=snapshot,snapshot_hash=hashlib.sha256(snapshot.encode()).hexdigest()))
    record_command(db,'stripe-ledger-v1:'+payment.id,'stripe-sandbox-ledger','stripe-sandbox',
        'Created isolated sandbox charges and payment entries from verified provider success.',json.loads(snapshot))
    db.flush()


def link_state(db,link):
    if not link:return 'not_linked'
    if hashlib.sha256(link.snapshot.encode()).hexdigest()!=link.snapshot_hash:return 'needs_review'
    charge=db.get(BookingLedgerEntry,link.charge_entry_id);received=db.get(BookingLedgerEntry,link.payment_entry_id)
    if not charge or not received or charge.booking_id!=link.booking_id or received.booking_id!=link.booking_id:
        return 'needs_review'
    if db.query(BookingLedgerEntry).filter(BookingLedgerEntry.voids_id.in_([charge.id,received.id])).first():return 'needs_review'
    return 'linked'
