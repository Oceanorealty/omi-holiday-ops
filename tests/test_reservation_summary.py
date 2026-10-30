"""Read-only original reservation workflow summary, synthetic data only."""
from datetime import datetime
from decimal import Decimal
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.db import get_db
from app.models import (BookingAccount,BookingLedgerEntry,CleaningTask,CleaningStatus,MessageLog,OpsCommand)
from app.operations.parity_router import router
from app.operations.router import require_admin
from test_operations import db,prop,booking


def client_for(db):
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    return app,TestClient(app)


def test_summary_preserves_unknown_balance_and_read_permissions(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-reservation-summary-key')
    p=prop(db,name='<Synthetic listing>');b=booking(db,p)
    app,c=client_for(db)
    with c:
        assert c.get(f'/operations/reservations/{b.id}').status_code==403
        app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
        response=c.get(f'/operations/reservations/{b.id}')
    assert response.status_code==200
    body=response.text
    assert '&lt;Synthetic listing&gt;' in body and '2 night(s)' in body
    assert 'Australia/Brisbane' in body and 'Account not configured — balance unknown.' in body
    assert 'No cleaning task recorded.' in body
    assert f'/properties?property_id={p.id}' in body and f'property_id={p.id}&amp;start=' in body
    assert all(db.query(model).count()==0 for model in [BookingAccount,CleaningTask,MessageLog,OpsCommand])


def test_summary_uses_same_ledger_and_highlights_cleaning_schedule_review(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-reservation-summary-key')
    p=prop(db);b=booking(db,p)
    db.add(BookingAccount(booking_id=b.id,currency='AUD'))
    db.add(CleaningTask(booking_id=b.id,property_id=p.id,due_date=b.check_out,
        status=CleaningStatus.assigned,assignee='Synthetic cleaner',schedule_review_required=True))
    for kind,amount in [('charge','200'),('payment','50')]:
        db.add(BookingLedgerEntry(booking_id=b.id,kind=kind,amount=Decimal(amount),description='Synthetic',
            occurred_on='2026-10-21',actor='synthetic-admin'))
    db.commit()
    app,c=client_for(db);app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with c:response=c.get(f'/operations/reservations/{b.id}')
    assert response.status_code==200
    assert 'Recorded balance AUD 150.00' in response.text and 'part paid' in response.text
    assert 'Synthetic cleaner' in response.text and 'Schedule needs review' in response.text
    assert db.query(BookingLedgerEntry).count()==2 and db.query(MessageLog).count()==0


def test_long_or_invalid_stays_always_link_to_valid_calendar_range(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-reservation-summary-key')
    p=prop(db);b=booking(db,p,check_in=datetime(2026,1,1),check_out=datetime(2026,5,1))
    app,c=client_for(db);app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with c:
        body=c.get(f'/operations/reservations/{b.id}').text
        assert 'first 62 nights' in body and 'end=2026-03-04' in body
        b.check_out=b.check_in;db.commit()
        body=c.get(f'/operations/reservations/{b.id}').text
        assert 'Dates need review' in body and 'end=2026-01-02' in body
