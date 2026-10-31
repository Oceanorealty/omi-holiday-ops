"""Read-only evidence comparison. No payout, commission or recognition policy is inferred."""
import hashlib
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from app.models import BookingAccount, BookingAccountStatement, OwnerStatement, ReservationEvent
from app.operations.billing import CURRENCIES, entries, totals
from app.operations.tasks import TaskError


def amount(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result != result.quantize(Decimal('.01')):
            raise ValueError()
        return result
    except (ValueError, InvalidOperation, TypeError):
        raise TaskError('Recorded accounting amounts need review') from None


def account_evidence(db, booking):
    account = db.get(BookingAccount, booking.id)
    if not account:
        return dict(version=None, currency=None, summary=None, rows=[], periods=[], issue='No booking account; recorded charges and cash movement are unknown.')
    try:
        if account.currency not in CURRENCIES:
            raise TaskError('Account currency needs review')
        rows = entries(db, booking.id)
        for row in rows:
            if row.kind not in ('charge', 'credit', 'payment', 'refund', 'void') or amount(row.amount) < 0:
                raise TaskError('Account history needs review')
            if date.fromisoformat(row.occurred_on).isoformat() != row.occurred_on:
                raise TaskError('Account transaction dates need review')
        summary = totals(rows)
        groups = defaultdict(list)
        for row in summary['active']:
            groups[row.occurred_on[:7]].append(row)
        periods = [dict(period=p, **totals(group)) for p, group in sorted(groups.items())]
        gross = sum((row.amount for row in summary['active'] if row.kind == 'charge'), Decimal(0))
        credits = sum((row.amount for row in summary['active'] if row.kind == 'credit'), Decimal(0))
        return dict(version=account.version, currency=account.currency, summary=summary, rows=rows, periods=periods,
            gross_charges=gross, credits=credits, issue=None)
    except (TaskError, ValueError, TypeError):
        return dict(version=account.version, currency=account.currency if account.currency in CURRENCIES else None,
            summary=None, rows=[], periods=[], issue='Account history or currency needs review; totals are withheld.')


def frozen_accounts(db, booking, ledger):
    rows = db.query(BookingAccountStatement).filter_by(booking_id=booking.id).order_by(BookingAccountStatement.id.desc()).limit(21).all()
    snapshots = []
    for row in rows[:20]:
        result = dict(id=row.id, created_at=row.created_at, valid=False)
        try:
            if hashlib.sha256(row.snapshot.encode()).hexdigest() != row.snapshot_hash:
                raise ValueError()
            data = json.loads(row.snapshot)
            if data['booking_id'] != booking.id or data['currency'] not in CURRENCIES:
                raise ValueError()
            numbers = {key: amount(data['totals'][key]) for key in ('charges','payments','refunds','net_received','balance')}
            result.update(valid=True, currency=data['currency'], totals=numbers,
                received_change=(ledger['summary']['net_received'] - numbers['net_received'])
                    if ledger['summary'] and ledger['currency'] == data['currency'] else None)
        except (ValueError, TypeError, KeyError, TaskError):
            pass
        snapshots.append(result)
    return snapshots, len(rows) > 20


def reconciliation(db, booking):
    ledger = account_evidence(db, booking)
    snapshots, truncated = frozen_accounts(db, booking, ledger)
    latest_version = db.query(BookingAccount.version).filter_by(booking_id=booking.id).scalar()
    if latest_version != ledger['version']:
        raise TaskError('Account changed during comparison; reload for current evidence')
    period = booking.check_out.strftime('%Y-%m')
    statement = db.query(OwnerStatement).filter_by(property_id=booking.property_id, period=period).first()
    original = None
    create = db.query(ReservationEvent).filter_by(booking_id=booking.id, action='create').order_by(ReservationEvent.id).first()
    if create:
        try:
            recorded = json.loads(create.after_state).get('amount')
            original = amount(recorded) if recorded is not None else None
        except (ValueError, TypeError, AttributeError, TaskError):
            pass
    reasons = ['Reservation amount has no stored currency; a cross-system monetary difference cannot be calculated.',
        'Owner revenue basis, refund/discount treatment and recognition period are not configured in this comparison.',
        'Recorded payments/refunds are operator records, not bank or payment-provider reconciliation.']
    if ledger['issue']:
        reasons.append(ledger['issue'])
    if booking.property.commission_pct is None:
        reasons.append('Property commission is not configured; no commission or owner entitlement is inferred.')
    if booking.status.value == 'cancelled':
        reasons.append('Cancelled reservation is excluded from the current legacy owner gross calculation; its account history is retained.')
    if not statement:
        reasons.append('No owner statement exists for this checkout month. This read-only view does not generate one.')
    elif statement.finalized:
        reasons.append('Finalized owner statement is retained unchanged. It has aggregate figures, not per-reservation snapshot lines; this reservation’s historical contribution is unverified.')
    else:
        reasons.append('Existing owner statement is a stored draft; this read-only view does not refresh it.')
    return dict(booking=booking, ledger=ledger, snapshots=snapshots, snapshots_truncated=truncated,
        owner_statement=statement, checkout_period=period, original_local_amount=original,
        current_legacy_included=booking.status.value == 'confirmed' and not booking.is_block,
        monetary_difference=None, owner_entitlement=None, review_reasons=reasons)
