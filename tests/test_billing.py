from datetime import datetime,timedelta
from decimal import Decimal
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import BookingAccount,BookingAccountStatement,BookingLedgerEntry,BookingStatus,OpsCommand
from app.operations.billing import (account_token,add_entry,create_account,create_token,entries,
    guest_billing_context,issue_statement,money,totals,void_entry)
from app.operations.billing_router import router
from app.operations.guest_checkin_router import router as guest_router
from app.operations.tasks import TaskError
from test_operations import db,prop,booking


@pytest.fixture
def stay(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-billing-'+32*'x')
    monkeypatch.setenv('ADMIN_USER','billing-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    start=datetime.utcnow()+timedelta(days=7)
    return booking(db,prop(db),check_in=start,check_out=start+timedelta(days=2),amount=300,
        guest_portal_token='synthetic-billing-link')


def create(db,stay,**changes):
    values=dict(actor='billing-admin',booking_id=stay.id,token=create_token('billing-admin',stay),currency='AUD',
        opening_amount='300.00',note='Confirm synthetic account currency and charge')
    values.update(changes);return create_account(db,**values)


def add(db,stay,**changes):
    account=db.get(BookingAccount,stay.id)
    values=dict(actor='billing-admin',booking_id=stay.id,token=account_token('billing-admin',account),kind='payment',
        amount='100',description='Synthetic external payment',occurred_on=datetime.utcnow().date().isoformat(),
        reference='synthetic-bank-ref-1',note='Verified against synthetic external payment evidence')
    values.update(changes);return add_entry(db,**values)


def void(db,stay,entity_id,**changes):
    account=db.get(BookingAccount,stay.id)
    values=dict(actor='billing-admin',booking_id=stay.id,entry_id=entity_id,token=account_token('billing-admin',account,'account-void'),
        note='Correct synthetic mistaken account record')
    values.update(changes);return void_entry(db,**values)


def statement(db,stay,**changes):
    account=db.get(BookingAccount,stay.id)
    values=dict(actor='billing-admin',booking_id=stay.id,token=account_token('billing-admin',account,'account-statement'),
        note='Issue synthetic fixed account statement')
    values.update(changes);return issue_statement(db,**values)


def test_charge_payment_credit_refund_and_balance_are_separate_from_legacy_amount(db,stay):
    create(db,stay);payment=add(db,stay)
    s=totals(entries(db,stay.id));assert s['balance']==200 and s['state']=='part paid'
    add(db,stay,kind='charge',amount='50',description='Optional service charge',reference='')
    add(db,stay,kind='credit',amount='20',description='Reviewed discount',reference='')
    add(db,stay,kind='refund',amount='30',payment_id=payment,reference='synthetic-refund-ref-1')
    s=totals(entries(db,stay.id))
    assert (s['charges'],s['payments'],s['refunds'],s['net_received'],s['balance'])==(330,100,30,70,260)
    assert stay.amount==300 and db.get(BookingAccount,stay.id).currency=='AUD'


def test_currency_must_be_explicit_and_account_creation_cannot_be_replayed(db,stay):
    with pytest.raises(TaskError):create(db,stay,currency='')
    assert db.query(BookingAccount).count()==0
    token=create_token('billing-admin',stay);create(db,stay,token=token,currency='USD',opening_amount='')
    with pytest.raises(TaskError):create(db,stay,token=token)
    assert db.get(BookingAccount,stay.id).currency=='USD' and db.query(BookingLedgerEntry).count()==0
    assert totals(entries(db,stay.id))['state']=='no balance due'


@pytest.mark.parametrize('amount',['NaN','Infinity','-1','0','0.001','100000000','1,000','bad'])
def test_invalid_amount_does_not_create_entries(db,stay,amount):
    create(db,stay)
    with pytest.raises(TaskError):add(db,stay,amount=amount)
    assert db.query(BookingLedgerEntry).count()==1 and db.query(OpsCommand).count()==1


def test_exact_money_and_credit_balance(db,stay):
    assert money('0.10')==Decimal('0.10')
    create(db,stay);add(db,stay,amount='350.10')
    s=totals(entries(db,stay.id));assert s['balance']==Decimal('-50.10') and s['state']=='credit balance'


def test_duplicate_reference_is_global_case_insensitive_and_replay_safe(db,stay):
    create(db,stay);token=account_token('billing-admin',db.get(BookingAccount,stay.id));add(db,stay,token=token)
    with pytest.raises(TaskError):add(db,stay,token=token)
    with pytest.raises(TaskError):add(db,stay,reference='SYNTHETIC-BANK-REF-1')
    other=booking(db,guest_portal_token='other-billing-link');create(db,other)
    with pytest.raises(TaskError):add(db,other)
    assert db.query(BookingLedgerEntry).filter_by(kind='payment').count()==1


def test_refund_is_linked_bounded_and_cannot_precede_payment(db,stay):
    create(db,stay);payment=add(db,stay)
    with pytest.raises(TaskError):add(db,stay,kind='refund',amount='101',payment_id=payment,reference='refund-1')
    with pytest.raises(TaskError):add(db,stay,kind='refund',payment_id=payment,occurred_on='2000-01-01',reference='refund-2')
    add(db,stay,kind='refund',amount='60',payment_id=payment,reference='refund-3')
    with pytest.raises(TaskError):add(db,stay,kind='refund',amount='41',payment_id=payment,reference='refund-4')
    with pytest.raises(TaskError):add(db,stay,kind='refund',amount='1',payment_id=99999,reference='refund-5')
    other=booking(db);create(db,other)
    with pytest.raises(TaskError):add(db,other,kind='refund',amount='1',payment_id=payment,reference='refund-6')
    assert totals(entries(db,stay.id))['refunds']==60


def test_void_retains_history_and_allows_corrected_reference_without_duplicate_live_receipt(db,stay):
    create(db,stay);payment=add(db,stay);original=db.get(BookingLedgerEntry,payment).receipt_snapshot
    void(db,stay,payment)
    assert db.get(BookingLedgerEntry,payment).receipt_snapshot==original
    assert totals(entries(db,stay.id))['payments']==0 and db.query(BookingLedgerEntry).count()==3
    with pytest.raises(TaskError):void(db,stay,payment)
    corrected=add(db,stay,amount='120')
    assert corrected!=payment and totals(entries(db,stay.id))['payments']==120


def test_payment_with_active_refunds_cannot_be_voided_until_refunds_corrected(db,stay):
    create(db,stay);payment=add(db,stay)
    refund=add(db,stay,kind='refund',amount='20',payment_id=payment,reference='refund-void-test')
    with pytest.raises(TaskError):void(db,stay,payment)
    void(db,stay,refund);void(db,stay,payment)
    assert totals(entries(db,stay.id))['payments']==totals(entries(db,stay.id))['refunds']==0
    assert db.query(BookingLedgerEntry).count()==5


def test_statements_and_receipt_snapshots_do_not_change_when_booking_or_account_changes(db,stay):
    create(db,stay);payment=add(db,stay)
    receipt=db.get(BookingLedgerEntry,payment).receipt_snapshot
    first=db.get(BookingAccountStatement,statement(db,stay));snapshot=first.snapshot
    add(db,stay,reference='synthetic-bank-ref-2',amount='200');stay.amount=500;db.commit()
    second=db.get(BookingAccountStatement,statement(db,stay))
    assert first.snapshot==snapshot and json.loads(first.snapshot)['totals']['balance']=='200.00'
    assert json.loads(second.snapshot)['totals']['balance']=='0.00'
    assert db.get(BookingLedgerEntry,payment).receipt_snapshot==receipt
    assert 'synthetic-bank-ref-1' not in receipt
    assert 'Evidence' not in first.snapshot and stay.guest_portal_token not in first.snapshot


@pytest.mark.parametrize('change',[{'occurred_on':'1999-12-31'},{'occurred_on':'2999-01-01'},
    {'occurred_on':'invalid'},{'description':' '},{'reference':''},{'note':'short'},{'payment_id':999}])
def test_invalid_entry_metadata_rolls_back(db,stay,change):
    create(db,stay)
    with pytest.raises(TaskError):add(db,stay,**change)
    assert db.query(BookingLedgerEntry).count()==1 and db.query(OpsCommand).count()==1


def test_cancelled_booking_retains_account_and_external_refund_records(db,stay):
    create(db,stay);payment=add(db,stay);stay.status=BookingStatus.cancelled;db.commit()
    add(db,stay,kind='credit',amount='300',description='Cancellation credit',reference='')
    add(db,stay,kind='refund',amount='100',payment_id=payment,reference='cancel-refund-test')
    assert totals(entries(db,stay.id))['balance']==0


def test_routes_protect_guest_scope_internal_notes_and_voided_receipts(db,stay):
    create(db,stay);payment=add(db,stay);entity_id=statement(db,stay)
    other=booking(db,guest_portal_token='other-account-portal')
    app=FastAPI();app.include_router(router);app.include_router(guest_router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get('/operations/accounts').status_code==403
        base='/guest/'+stay.guest_portal_token
        page=client.get(base);assert page.status_code==200 and 'Your booking account' in page.text
        assert 'synthetic-bank-ref-1' not in page.text and 'billing-admin' not in page.text
        receipt=f'{base}/account/receipts/{payment}';statement_url=f'{base}/account/statements/{entity_id}'
        assert client.get(receipt).status_code==client.get(statement_url).status_code==200
        assert client.get('/guest/other-account-portal/account/receipts/'+str(payment)).status_code==404
        assert client.get('/guest/other-account-portal/account/statements/'+str(entity_id)).status_code==404
        client.cookies.set(SESSION_COOKIE,make_session_cookie('billing-admin'))
        assert client.get('/operations/accounts/'+str(stay.id)).status_code==200
        values=dict(token=account_token('billing-admin',db.get(BookingAccount,stay.id)),kind='payment',amount='1',
            description='Cross-site test',occurred_on=datetime.utcnow().date().isoformat(),reference='new-ref',note='Synthetic evidence reviewed')
        assert client.post(f'/operations/accounts/{stay.id}/entries',data=values,headers={'Origin':'https://other.invalid'}).status_code==403
        void(db,stay,payment)
        assert 'VOIDED' in client.get(receipt).text
        assert 'External reference' not in client.get(receipt).text
        stay.status=BookingStatus.cancelled;db.commit()
        assert client.get(receipt).status_code==404
        assert client.get(f'/operations/accounts/{stay.id}/receipts/{payment}').status_code==200
