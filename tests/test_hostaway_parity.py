"""Synthetic reservation/inventory workflows, public quotes and reporting."""
import csv
import json
import html
import io
import re
from datetime import date, datetime
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from app.auth import SESSION_COOKIE, make_session_cookie
from app.db import Base, get_db
from app.models import (Booking, BookingStatus, CleaningStatus, CleaningTask, Guest,
                        InventoryDay, InventoryEvent, PlatformName, ReservationEvent)
from app.operations.calendar import calendar_data
from app.operations.inventory import inventory_token, restriction_reason, save_inventory
from app.operations.parity_router import report_data, router
from app.operations.reservations import reservation_token, save_reservation, snapshot
from app.operations.tasks import TaskError
from app.pricing import suggested_price
from app.public_api import router as public_router
from test_operations import db, prop, booking


@pytest.fixture
def actor(monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'parity-synthetic-' + 'x'*32)
    monkeypatch.setenv('ADMIN_USER', 'parity-admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'synthetic-only')
    return 'parity-admin'


def reservation_args(db, actor, p, row=None, **changes):
    values = dict(actor=actor, entity_id=row.id if row else 0,
        token=reservation_token(actor, row), property_id=p.id, check_in='2026-10-21', check_out='2026-10-23',
        amount='200.00', guest_name='Synthetic guest', guest_email='guest@example.invalid',
        note='Guest requested this direct reservation')
    values.update(changes)
    return values


def inventory_args(db, actor, p, **changes):
    start, end = changes.pop('start', '2026-10-21'), changes.pop('end', '2026-10-23')
    values = dict(actor=actor, property_id=p.id, start=start, end=end,
        token=inventory_token(db, actor, p.id, start, end), nightly_rate='150.00',
        note='Manager reviewed these date restrictions')
    values.update(changes)
    return values


def test_local_reservation_lifecycle_and_cleaning_are_atomic(db, actor):
    p = prop(db)
    entity_id = save_reservation(db, **reservation_args(db, actor, p))
    row = db.get(Booking, entity_id)
    task = db.query(CleaningTask).one()
    assert row.platform == PlatformName.direct and row.guest.email == 'guest@example.invalid'
    assert task.due_date == row.check_out and not row.notifications_enabled
    task.schedule_locked = True
    db.commit()
    save_reservation(db, **reservation_args(db, actor, p, row, check_out='2026-10-24'))
    assert task.schedule_review_required and task.due_date == datetime(2026, 10, 23)
    save_reservation(db, **reservation_args(db, actor, p, row, action='cancel'))
    assert row.status == BookingStatus.cancelled and task.cancelled_at
    save_reservation(db, **reservation_args(db, actor, p, row, action='restore'))
    assert row.status == BookingStatus.confirmed and task.cancelled_at is None
    assert db.query(ReservationEvent).count() == 4
    assert all('door_code' not in e.after_state for e in db.query(ReservationEvent))


def test_replay_stale_and_shared_guest_updates_do_not_overwrite(db, actor):
    p = prop(db)
    args = reservation_args(db, actor, p)
    entity_id = save_reservation(db, **args)
    with pytest.raises(TaskError):
        save_reservation(db, **args)
    row = db.get(Booking, entity_id)
    stale = reservation_args(db, actor, p, row)
    guest = row.guest
    guest.name = 'Changed through original guest editor'
    db.commit()
    with pytest.raises(TaskError):
        save_reservation(db, **stale)
    other = booking(db, p, guest_id=guest.id, check_in=datetime(2026, 11, 1), check_out=datetime(2026, 11, 3))
    save_reservation(db, **reservation_args(db, actor, p, row, guest_name='Different guest'))
    assert other.guest.name == 'Changed through original guest editor'
    assert row.guest_id != other.guest_id
    assert db.query(ReservationEvent).count() == 2


def test_overlaps_blocks_cancelled_and_checkout_boundaries(db, actor):
    p = prop(db)
    booking(db, p, is_block=True)
    with pytest.raises(TaskError):
        save_reservation(db, **reservation_args(db, actor, p))
    assert db.query(Guest).count() == db.query(ReservationEvent).count() == 0
    adjacent = save_reservation(db, **reservation_args(db, actor, p, check_in='2026-10-23', check_out='2026-10-24'))
    row = db.get(Booking, adjacent)
    save_reservation(db, **reservation_args(db, actor, p, row, action='cancel'))
    replacement = save_reservation(db, **reservation_args(db, actor, p, check_in='2026-10-23', check_out='2026-10-24'))
    with pytest.raises(TaskError):
        save_reservation(db, **reservation_args(db, actor, p, row, action='restore'))
    assert row.status == BookingStatus.cancelled and replacement != row.id


@pytest.mark.parametrize('changes', [dict(amount='NaN'), dict(amount='Infinity'), dict(amount='-1'),
    dict(amount='100000000'), dict(amount='1.001'), dict(check_in='2026-02-30'),
    dict(check_out='2026-10-21'), dict(check_out='2028-10-21'), dict(guest_email='bad'),
    dict(note='short'), dict(token='forged'), dict(actor='other-admin')])
def test_invalid_reservations_leave_no_partial_rows(db, actor, changes):
    p = prop(db)
    args = reservation_args(db, actor, p)
    args.update(changes)
    with pytest.raises(TaskError):
        save_reservation(db, **args)
    assert db.query(Booking).count() == db.query(Guest).count() == db.query(ReservationEvent).count() == 0


def test_channel_reservation_cannot_be_locally_cancelled(db, actor):
    p = prop(db)
    row = booking(db, p)
    with pytest.raises(TaskError):
        save_reservation(db, **reservation_args(db, actor, p, row, action='cancel'))
    assert row.status == BookingStatus.confirmed


def test_cancel_clears_original_dashboard_conflict_flags(db, actor):
    p = prop(db)
    direct = booking(db, p, platform=PlatformName.direct, has_conflict=True)
    imported = booking(db, p, has_conflict=True)
    save_reservation(db, **reservation_args(db, actor, p, direct, action='cancel'))
    assert not imported.has_conflict and not direct.has_conflict
    assert json.loads(db.query(ReservationEvent).one().after_state) == snapshot(direct)


def test_concurrent_local_reservations_only_one_claims_inventory(db, actor):
    p = prop(db)
    p_id = p.id
    bind = db.bind
    session_factory = sessionmaker(bind=bind)
    tokens = [reservation_token(actor), reservation_token(actor)]
    def run(token):
        with session_factory() as session:
            try:
                save_reservation(session, **reservation_args(session, actor, p, token=token))
                return 'saved'
            except TaskError:
                return 'held'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, tokens))
    assert sorted(results) == ['held', 'saved']
    db.expire_all()
    assert db.query(Booking).filter_by(property_id=p_id).count() == 1


def test_inventory_rates_restrictions_audit_stale_and_reset(db, actor):
    p = prop(db, base_nightly_rate=100)
    args = inventory_args(db, actor, p, minimum_stay=3, closed_arrival=True)
    stale = inventory_args(db, actor, p, nightly_rate='999')
    save_inventory(db, **args)
    assert db.query(InventoryDay).count() == 2 and db.query(InventoryEvent).count() == 1
    assert suggested_price(db, p, date(2026, 10, 21)) == Decimal('150.00')
    assert suggested_price(db, p, date(2026, 10, 23)) == Decimal('100.00')
    with pytest.raises(TaskError):
        save_inventory(db, **stale)
    with pytest.raises(TaskError):
        save_inventory(db, **args)
    assert restriction_reason(db, p.id, date(2026, 10, 21), date(2026, 10, 23)) == 'arrival_closed'
    save_inventory(db, **inventory_args(db, actor, p, reset=True))
    assert db.query(InventoryDay).count() == 0 and db.query(InventoryEvent).count() == 2
    assert suggested_price(db, p, date(2026, 10, 21)) == Decimal('100.00')


@pytest.mark.parametrize('changes,reason', [({'minimum_stay': 3}, 'minimum_stay'),
    ({'maximum_stay': 1}, 'maximum_stay'), ({'closed_arrival': True}, 'arrival_closed'),
    ({'stop_sell': True}, 'stop_sell'),
    ({'start': '2026-10-23', 'end': '2026-10-24', 'closed_departure': True}, 'departure_closed')])
def test_public_quotes_and_manual_bookings_share_restrictions(db, actor, changes, reason):
    p = prop(db, published=True, base_nightly_rate=100)
    save_inventory(db, **inventory_args(db, actor, p, **changes))
    assert restriction_reason(db, p.id, date(2026, 10, 21), date(2026, 10, 23)) == reason
    with pytest.raises(TaskError):
        save_reservation(db, **reservation_args(db, actor, p))
    app = FastAPI()
    app.include_router(public_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        result = client.get(f'/api/public/quote?property_id={p.id}&check_in=2026-10-21&check_out=2026-10-23&guests=1').json()['result']
        assert result['bookable'] is False and result['reason'] == reason


def test_public_calendar_matches_override_quote_price_and_stop_sell(db, actor):
    p = prop(db, published=True, base_nightly_rate=100)
    save_inventory(db, **inventory_args(db, actor, p))
    app = FastAPI()
    app.include_router(public_router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        path = f'/api/public/listings/{p.id}/calendar?start=2026-10-21&end=2026-10-23'
        assert client.get(path).json()['result']['days'][0]['price'] == '150.00'
        save_inventory(db, **inventory_args(db, actor, p, stop_sell=True))
        assert client.get(path).json()['result']['days'][0]['status'] == 'blocked'
    row = calendar_data(db, date(2026, 10, 21), date(2026, 10, 23))['rows'][0]
    assert row['stop_sell_nights'] == 2 and row['sellable_nights'] == 0


@pytest.mark.parametrize('changes', [dict(nightly_rate='0'), dict(nightly_rate='NaN'),
    dict(minimum_stay=0), dict(minimum_stay=5, maximum_stay=3), dict(maximum_stay=367),
    dict(note='short'), dict(token='forged')])
def test_invalid_bulk_edit_is_atomic(db, actor, changes):
    p = prop(db)
    args = inventory_args(db, actor, p)
    args.update(changes)
    with pytest.raises(TaskError):
        save_inventory(db, **args)
    assert db.query(InventoryDay).count() == db.query(InventoryEvent).count() == 0


def test_prorated_reporting_missing_amounts_overlaps_and_stopped_nights(db, actor):
    p = prop(db)
    booking(db, p, check_in=datetime(2026, 9, 30), check_out=datetime(2026, 10, 2), amount=Decimal('300'))
    booking(db, p, check_in=datetime(2026, 10, 4), check_out=datetime(2026, 10, 5), amount=None)
    row = report_data(db, date(2026, 10, 1), date(2026, 10, 6))['rows'][0]
    assert row['known_booking_value'] == '150.00' and row['missing_amounts'] == 1
    assert row['reserved_nights'] == 2 and not row['complete']
    booking(db, p, check_in=datetime(2026, 10, 1), check_out=datetime(2026, 10, 2), amount=50)
    row = report_data(db, date(2026, 10, 1), date(2026, 10, 6))['rows'][0]
    assert row['reserved_night_pct'] is None and row['conflict_days'] == 1


def test_stopped_reserved_nights_are_not_reported_as_over_100_percent(db, actor):
    p = prop(db)
    booking(db, p)
    save_inventory(db, **inventory_args(db, actor, p, stop_sell=True))
    row = report_data(db, date(2026, 10, 21), date(2026, 10, 24))['rows'][0]
    assert row['stopped_reserved_nights'] == 2 and row['reserved_night_pct'] is None
    assert not row['complete']


def test_routes_filters_permissions_csrf_html_and_csv_escape(db, actor):
    p = prop(db, name='=HYPERLINK("invalid")')
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        assert client.get('/operations/reservations').status_code == 403
        client.cookies.set(SESSION_COOKIE, make_session_cookie(actor))
        for path in ['/operations/setup', '/operations/reservations?property_id=', '/operations/reports?property_id=',
                     '/operations/inventory?property_id=']:
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
        assert client.get('/operations/reservations?property_id=bad').status_code == 422
        page = client.get('/operations/reservations')
        token = html.unescape(re.search(r'name="token" value="([^"]+)"', page.text).group(1))
        values = reservation_args(db, actor, p, token=token, guest_name='<script>unsafe</script>')
        values.pop('actor'); values.pop('entity_id')
        path = '/operations/reservations/0'
        assert client.post(path, data=values, headers={'origin': 'https://elsewhere.invalid'}).status_code == 403
        response = client.post(path, data=values)
        assert response.status_code == 200 and '&lt;script&gt;unsafe&lt;/script&gt;' in response.text
        assert '<script>unsafe</script>' not in response.text
        response = client.get('/operations/reports.csv?start=2026-10-01&end=2026-11-01')
        assert response.status_code == 200
        rows = list(csv.reader(io.StringIO(response.text.lstrip('\ufeff'))))
        assert rows[1][1].startswith("'=") and 'Guest' not in response.text
        assert client.get('/operations/reports?start=2026-01-01&end=2027-01-01').status_code == 422
        client.cookies.set(SESSION_COOKIE, make_session_cookie('not-admin'))
        assert client.get('/operations/reports').status_code == 403


def test_additive_new_tables_preserve_existing_bookings(db):
    row = booking(db)
    Base.metadata.create_all(db.bind)
    Base.metadata.create_all(db.bind)
    assert db.get(Booking, row.id).amount == 200
    assert {'reservation_events', 'inventory_days', 'inventory_events'} <= set(inspect(db.bind).get_table_names())
