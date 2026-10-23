"""Explicit-currency booking ledger. Records external money; never moves it."""
import hashlib
import json
from datetime import date,datetime
from decimal import Decimal,InvalidOperation
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from sqlalchemy.exc import IntegrityError
from app.models import Booking,BookingAccount,BookingAccountStatement,BookingLedgerEntry
from app.operations.agreements import booking_fingerprint
from app.operations.stock import lock_stock
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError,form_token,verify_token

CURRENCIES=('AUD','NZD','USD','EUR','GBP','CAD','SGD','HKD','CNY')
KINDS=('charge','credit','payment','refund')


def money(value,allow_zero=False):
    try:
        amount=Decimal(value.strip())
        if not amount.is_finite() or amount<0 or amount>Decimal('99999999.99') or amount!=amount.quantize(Decimal('.01')):
            raise ValueError()
        if not allow_zero and amount==0:raise ValueError()
        return amount.quantize(Decimal('.01'))
    except (InvalidOperation,ValueError,AttributeError):
        raise TaskError('Use a positive amount with at most two decimal places, up to 99999999.99') from None


def create_token(actor,booking):
    return form_token(actor,SimpleNamespace(id=booking.id,version=0),purpose='account-create:'+booking_fingerprint(booking))


def account_token(actor,account,purpose='account-entry'):
    return form_token(actor,SimpleNamespace(id=account.booking_id,version=account.version),purpose=purpose)


def entries(db,booking_id):
    rows=db.query(BookingLedgerEntry).filter_by(booking_id=booking_id).order_by(BookingLedgerEntry.id).limit(10001).all()
    if len(rows)>10000:raise TaskError('Account history requires a narrower accounting review')
    return rows


def totals(rows):
    voided={r.voids_id for r in rows if r.kind=='void'}
    active=[r for r in rows if r.kind!='void' and r.id not in voided]
    charges=sum((r.amount if r.kind=='charge' else -r.amount for r in active if r.kind in ('charge','credit')),Decimal(0))
    payments=sum((r.amount for r in active if r.kind=='payment'),Decimal(0))
    refunds=sum((r.amount for r in active if r.kind=='refund'),Decimal(0))
    received=payments-refunds;balance=charges-received
    state='credit balance' if balance<0 else ('settled' if balance==0 and charges!=0 else
        ('no balance due' if balance==0 else ('part paid' if received>0 else 'unpaid')))
    return {'charges':charges,'payments':payments,'refunds':refunds,'net_received':received,'balance':balance,
        'state':state,'voided':voided,'active':active}


def create_account(db,*,actor,booking_id,token,currency,opening_amount,note):
    if currency not in CURRENCIES:raise TaskError('Choose a supported currency explicitly')
    opening=money(opening_amount,allow_zero=True) if opening_amount.strip() else None
    try:
        lock_stock(db);db.expire_all();booking=db.get(Booking,booking_id)
        if not booking or booking.is_block:raise TaskError('Guest reservation not found')
        verify_token(token,actor,booking.id,purpose='account-create:'+booking_fingerprint(booking))
        if db.get(BookingAccount,booking_id):raise TaskError('An account already exists for this reservation')
        row=BookingAccount(booking_id=booking_id,currency=currency);db.add(row);db.flush()
        if opening:
            db.add(BookingLedgerEntry(booking_id=booking_id,kind='charge',amount=opening,
                description='Opening reservation charge',occurred_on=datetime.now(ZoneInfo(booking.property.time_zone)).date().isoformat(),actor=actor,evidence_note=note.strip()))
        record_command(db,token,'account-create',actor,note,{'booking_id':booking_id,'currency':currency,'opening_amount':str(opening) if opening is not None else None})
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('Account already exists or this command was already used') from None
    except Exception:
        db.rollback();raise


def entry_date(booking,value):
    try:
        parsed=date.fromisoformat(value)
        if parsed.isoformat()!=value or parsed<date(2000,1,1) or parsed>datetime.now(ZoneInfo(booking.property.time_zone)).date():raise ValueError()
        return value
    except (ValueError,TypeError):raise TaskError('Choose an actual transaction date between 2000-01-01 and today') from None


def add_entry(db,*,actor,booking_id,token,kind,amount,description,occurred_on,reference='',payment_id=None,note):
    if kind not in KINDS:raise TaskError('Unknown account entry type')
    value=money(amount);description=description.strip();reference=reference.strip()
    if not 1<=len(description)<=240:raise TaskError('Enter a customer-visible description of 1–240 characters')
    if kind in ('payment','refund') and (not 3<=len(reference)<=120 or any(ord(c)<32 for c in reference)):
        raise TaskError('Enter a unique external transaction reference of 3–120 characters')
    expected=verify_token(token,actor,booking_id,purpose='account-entry')
    try:
        lock_stock(db);db.expire_all();account=db.get(BookingAccount,booking_id);booking=db.get(Booking,booking_id)
        if not account or account.version!=expected:raise TaskError('Account changed; reload before recording another entry')
        when=entry_date(booking,occurred_on);rows=entries(db,booking_id);summary=totals(rows)
        if kind in ('charge','credit') and payment_id is not None:raise TaskError('Only a refund can refer to a payment')
        if kind=='payment' and payment_id is not None:raise TaskError('A payment cannot refer to another payment')
        if kind=='refund':
            payment=next((r for r in summary['active'] if r.id==payment_id and r.kind=='payment'),None)
            if not payment:raise TaskError('Choose an active payment from this account')
            refunded=sum((r.amount for r in summary['active'] if r.kind=='refund' and r.payment_id==payment.id),Decimal(0))
            if value>payment.amount-refunded:raise TaskError('Recorded refunds cannot exceed the linked payment')
            if when<payment.occurred_on:raise TaskError('Refund date cannot precede the payment date')
        if len(rows)>=5000:raise TaskError('Account entry limit reached; contact your accounting team')
        reference_digest=hashlib.sha256(reference.casefold().encode()).hexdigest() if kind in ('payment','refund') else None
        if reference_digest:
            matches=db.query(BookingLedgerEntry).filter_by(reference_digest=reference_digest).limit(5001).all()
            if len(matches)>5000:raise TaskError('Transaction reference history requires an accounting review')
            ids=[r.id for r in matches]
            voided={r.voids_id for r in db.query(BookingLedgerEntry).filter(BookingLedgerEntry.voids_id.in_(ids)).all()} if ids else set()
            if any(r.id not in voided for r in matches):raise TaskError('This external transaction reference is already active in an account')
        row=BookingLedgerEntry(booking_id=booking_id,kind=kind,amount=value,description=description,occurred_on=when,actor=actor,evidence_note=note.strip(),
            reference=reference if kind in ('payment','refund') else None,
            reference_digest=reference_digest,
            payment_id=payment_id if kind=='refund' else None)
        db.add(row);db.flush()
        if kind in ('payment','refund'):
            row.receipt_snapshot=json.dumps({'entry_id':row.id,'booking_id':booking.id,'property_name':booking.property.name,
                'guest_name':booking.guest.name if booking.guest else None,'kind':kind,'currency':account.currency,
                'amount':str(value),'description':description,'occurred_on':when,'recorded_at':row.created_at.isoformat()},sort_keys=True)
            row.receipt_snapshot_hash=hashlib.sha256(row.receipt_snapshot.encode()).hexdigest()
        account.version+=1
        record_command(db,token,'account-entry',actor,note,{'booking_id':booking_id,'entry_id':row.id,'kind':kind,'amount':str(value),'currency':account.currency})
        db.commit();return row.id
    except IntegrityError:
        db.rollback();raise TaskError('This submission or external transaction reference is already recorded; check the account history') from None
    except Exception:
        db.rollback();raise


def void_entry(db,*,actor,booking_id,entry_id,token,note):
    expected=verify_token(token,actor,booking_id,purpose='account-void')
    try:
        lock_stock(db);db.expire_all();account=db.get(BookingAccount,booking_id)
        if not account or account.version!=expected:raise TaskError('Account changed; reload before correcting a record')
        rows=entries(db,booking_id);summary=totals(rows)
        original=next((r for r in summary['active'] if r.id==entry_id),None)
        if not original:raise TaskError('Choose an active account entry')
        if original.kind=='payment' and any(r.kind=='refund' and r.payment_id==original.id for r in summary['active']):
            raise TaskError('Correct the linked refund records before voiding this mistaken payment record')
        booking=db.get(Booking,booking_id)
        db.add(BookingLedgerEntry(booking_id=booking_id,kind='void',amount=0,description=f'Correction of account entry #{entry_id}',
            occurred_on=datetime.now(ZoneInfo(booking.property.time_zone)).date().isoformat(),actor=actor,evidence_note=note.strip(),voids_id=entry_id))
        account.version+=1
        record_command(db,token,'account-void',actor,note,{'booking_id':booking_id,'voided_entry_id':entry_id})
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('This entry was already corrected') from None
    except Exception:
        db.rollback();raise


def issue_statement(db,*,actor,booking_id,token,note):
    expected=verify_token(token,actor,booking_id,purpose='account-statement')
    try:
        lock_stock(db);db.expire_all();account=db.get(BookingAccount,booking_id);booking=db.get(Booking,booking_id)
        if not account or account.version!=expected:raise TaskError('Account changed; reload before issuing a statement')
        rows=entries(db,booking_id);summary=totals(rows)
        data={'booking_id':booking_id,'property_name':booking.property.name,'guest_name':booking.guest.name if booking.guest else None,
            'currency':account.currency,'account_version':account.version,
            'entries':[{'id':r.id,'kind':r.kind,'amount':str(r.amount),'description':r.description,'occurred_on':r.occurred_on,
                'payment_id':r.payment_id} for r in summary['active']],
            'totals':{k:str(summary[k]) for k in ('charges','payments','refunds','net_received','balance')},
            'state':summary['state']}
        encoded=json.dumps(data,sort_keys=True);row=BookingAccountStatement(booking_id=booking_id,snapshot=encoded,
            snapshot_hash=hashlib.sha256(encoded.encode()).hexdigest(),actor=actor)
        db.add(row);db.flush();account.version+=1
        record_command(db,token,'account-statement',actor,note,{'booking_id':booking_id,'statement_id':row.id,'snapshot_hash':row.snapshot_hash})
        db.commit();return row.id
    except IntegrityError:
        db.rollback();raise TaskError('This statement request was already used') from None
    except Exception:
        db.rollback();raise


def guest_billing_context(db,booking):
    account=db.get(BookingAccount,booking.id) if booking else None
    if not account:return {}
    rows=entries(db,booking.id);summary=totals(rows)
    statements=db.query(BookingAccountStatement).filter_by(booking_id=booking.id).order_by(BookingAccountStatement.id.desc()).limit(20).all()
    return {'stay_account':account,'stay_account_summary':summary,'stay_account_rows':summary['active'],
        'stay_account_statements':statements}
