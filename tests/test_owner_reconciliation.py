import hashlib
import json
from decimal import Decimal
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE, make_session_cookie
from app.db import get_db
from app.models import BookingAccount, BookingAccountStatement, BookingLedgerEntry, BookingStatus, ReservationEvent
from app.operations.billing import create_account, create_token, account_token, add_entry, issue_statement, void_entry
from app.operations.billing_router import router
from app.operations.owner_reconciliation import reconciliation
from app.operations.tasks import TaskError
from app.comms.owner_reports import _generate_statement, finalize_statement
from test_operations import db, prop, booking


@pytest.fixture
def row(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-reconciliation-'+'x'*32)
    monkeypatch.setenv('ADMIN_USER','synthetic-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    return booking(db)


def account(db,row,currency='AUD'):
    create_account(db,actor='synthetic-admin',booking_id=row.id,token=create_token('synthetic-admin',row),
        currency=currency,opening_amount='200',note='Synthetic original charge evidence')
    return db.get(BookingAccount,row.id)


def entry(db,row,kind,value,when='2026-10-01',**changes):
    values=dict(actor='synthetic-admin',booking_id=row.id,token=account_token('synthetic-admin',db.get(BookingAccount,row.id)),
        kind=kind,amount=value,description='Synthetic account evidence',occurred_on=when,
        reference='synthetic-'+str(db.query(BookingLedgerEntry).count()),note='Synthetic evidence, no external payment')
    values.update(changes)
    return add_entry(db,**values)


def test_partial_refund_discount_cross_period_and_frozen_snapshot(db,row):
    acc=account(db,row)
    payment=entry(db,row,'payment','80','2026-09-30')
    partial=reconciliation(db,row)
    assert partial['ledger']['summary']['balance']==120
    statement_id=issue_statement(db,actor='synthetic-admin',booking_id=row.id,
        token=account_token('synthetic-admin',acc,'account-statement'),note='Synthetic immutable partial account snapshot')
    frozen=db.get(BookingAccountStatement,statement_id).snapshot
    entry(db,row,'payment','120')
    entry(db,row,'refund','50','2026-10-02',payment_id=payment)
    entry(db,row,'credit','50','2026-10-03')
    result=reconciliation(db,row);ledger=result['ledger']
    assert (ledger['gross_charges'],ledger['credits'],ledger['summary']['charges'])==(200,50,150)
    assert (ledger['summary']['payments'],ledger['summary']['refunds'],ledger['summary']['net_received'],ledger['summary']['balance'])==(200,50,150,0)
    assert [p['period'] for p in ledger['periods']]==['2026-09','2026-10']
    assert ledger['periods'][0]['net_received']==80 and ledger['periods'][1]['net_received']==70
    assert result['snapshots'][0]['received_change']==70
    assert db.get(BookingAccountStatement,statement_id).snapshot==frozen
    assert result['monetary_difference'] is None and result['owner_entitlement'] is None
    assert row.amount==200


def test_voided_entries_excluded_but_history_retained(db,row):
    acc=account(db,row);extra=entry(db,row,'charge','25')
    void_entry(db,actor='synthetic-admin',booking_id=row.id,entry_id=extra,
        token=account_token('synthetic-admin',acc,'account-void'),note='Synthetic duplicate charge correction')
    result=reconciliation(db,row)['ledger']
    assert result['gross_charges']==200 and extra in result['summary']['voided']
    assert len(result['rows'])==3


def test_finalized_owner_aggregate_never_recomputed_or_allocated(db,row):
    statement=_generate_statement(db,row.property,'2026-10');db.flush()
    finalize_statement(db,statement);db.commit()
    previous=(statement.gross_revenue,statement.net_payout,statement.generated_at,statement.finalized)
    account(db,row);entry(db,row,'credit','50')
    row.status=BookingStatus.cancelled;db.commit()
    result=reconciliation(db,row)
    assert not result['current_legacy_included']
    assert result['owner_statement'].id==statement.id
    assert (statement.gross_revenue,statement.net_payout,statement.generated_at,statement.finalized)==previous
    assert any('historical contribution is unverified' in reason for reason in result['review_reasons'])
    assert not db.dirty and not db.new and not db.deleted


def test_no_account_missing_amount_and_commission_stay_unknown(db,row):
    row.amount=None;row.property.commission_pct=None;db.commit()
    result=reconciliation(db,row)
    assert result['ledger']['summary'] is None and result['ledger']['currency'] is None
    assert result['original_local_amount'] is None and result['owner_statement'] is None
    assert any('commission is not configured' in reason for reason in result['review_reasons'])
    assert result['owner_entitlement'] is None


@pytest.mark.parametrize('currency',['AUD','USD','NZD'])
def test_ledger_currency_never_assigned_to_legacy_amount(db,row,currency):
    account(db,row,currency)
    result=reconciliation(db,row)
    assert result['ledger']['currency']==currency
    assert result['monetary_difference'] is None


def test_tampered_snapshot_withheld_and_first_local_amount_labelled(db,row):
    account(db,row)
    db.add(ReservationEvent(booking_id=row.id,token_digest='synthetic-create',actor='synthetic-admin',action='create',
        note='Synthetic creation evidence',before_state='{}',after_state=json.dumps({'amount':'180.00'})))
    db.add(BookingAccountStatement(booking_id=row.id,snapshot='{}',snapshot_hash='incorrect',actor='synthetic-admin'));db.commit()
    result=reconciliation(db,row)
    assert result['original_local_amount']==Decimal('180')
    assert result['snapshots'][0]['valid'] is False


def test_invalid_history_totals_withheld(db,row):
    account(db,row)
    db.add(BookingLedgerEntry(booking_id=row.id,kind='unknown',amount=10,description='Synthetic corrupted historical row',
        occurred_on='2026-10-01',actor='synthetic-admin'));db.commit()
    assert reconciliation(db,row)['ledger']['summary'] is None


def test_account_changed_during_read_requires_reload(db,row,monkeypatch):
    from app.operations import owner_reconciliation as module
    acc=account(db,row)
    original=module.frozen_accounts
    def competing_read(*args):
        result=original(*args)
        acc.version+=1;db.commit()
        return result
    monkeypatch.setattr(module,'frozen_accounts',competing_read)
    with pytest.raises(TaskError,match='Account changed during comparison'):
        reconciliation(db,row)


def test_readonly_original_routes_and_owner_cannot_read_admin_comparison(db,row):
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    row.property.owner_portal_token='synthetic-owner-capability';db.commit()
    with TestClient(app) as client:
        url=f'/operations/accounts/{row.id}/owner-reconciliation'
        assert client.get(url).status_code==403
        assert client.get(url+'?token=synthetic-owner-capability').status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('synthetic-owner'))
        assert client.get(url).status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('synthetic-admin'))
        before=db.query(BookingLedgerEntry).count()
        response=client.get(url)
        assert response.status_code==200 and response.headers['cache-control']=='private, no-store'
        assert 'Net receipts and balance: unknown' in response.text and '<form' not in response.text.split('<main',1)[1]
        assert client.post(url).status_code==405
        assert client.get('/operations/accounts/999999/owner-reconciliation').status_code==404
        assert db.query(BookingLedgerEntry).count()==before and not db.dirty
        assert 'Compare account records and owner statement basis' in client.get(f'/operations/accounts/{row.id}').text
