"""Synthetic original-module calendar: no operational writes or outbound effects."""
from datetime import date, datetime
import re
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.db import get_db
from app.models import Guest, PlatformName, Booking, MessageLog, OpsCommand
from app.operations.calendar import calendar_data
from app.operations.calendar_board import add_board
from app.operations.router import router, require_admin
from test_operations import db, prop, booking


def board(db, start='2026-10-21', end='2026-10-25'):
    return add_board(db, calendar_data(db, date.fromisoformat(start), date.fromisoformat(end)))


def test_bar_clipping_and_adjacent_stays_share_lane(db):
    p = prop(db)
    a = booking(db,p,check_in=datetime(2026,10,20),check_out=datetime(2026,10,23))
    b = booking(db,p,check_in=datetime(2026,10,23),check_out=datetime(2026,10,27))
    row = board(db)['rows'][0]
    assert row['board_lanes'] == 1
    first, second = row['board_stays']
    assert (first['column'],first['span'],first['continues_before']) == (1,2,True)
    assert (second['column'],second['span'],second['continues_after']) == (3,2,True)
    assert [first['id'],second['id']] == [a.id,b.id]


def test_overlapping_bars_do_not_hide_each_other_and_flag_review(db):
    p=prop(db);booking(db,p);booking(db,p,platform=PlatformName.booking_com)
    row=board(db)['rows'][0]
    assert row['board_lanes']==2 and row['board_review']
    assert {s['lane'] for s in row['board_stays']}=={1,2}
    assert {s['channel'] for s in row['board_stays']}=={'Airbnb','Booking.com'}
    assert all(s['needs_review'] for s in row['board_stays'])


def test_checkout_boundary_day_visit_and_blocks_not_counted_as_guests(db):
    p=prop(db)
    booking(db,p,check_in=datetime(2026,10,20),check_out=datetime(2026,10,21))
    visit=booking(db,p,check_in=datetime(2026,10,22,9),check_out=datetime(2026,10,22,15))
    block=booking(db,p,is_block=True,check_in=datetime(2026,10,23),check_out=datetime(2026,10,24))
    result=board(db);stays=result['rows'][0]['board_stays']
    assert len(stays)==2
    assert next(s for s in stays if s['id']==visit.id)['same_day']
    assert next(s for s in stays if s['id']==block.id)['label']=='Blocked'
    assert result['rows'][0]['reserved_nights']==0


def test_new_property_automatically_appears_and_missing_guest_not_invented(db):
    p=prop(db);b=booking(db,p)
    assert board(db)['rows'][0]['board_stays'][0]['label']==f'Reservation #{b.id}'
    prop(db,name='New synthetic listing')
    assert len(board(db)['rows'])==2


def test_channel_is_recorded_source_not_guessed_from_summary(db):
    p=prop(db);booking(db,p,platform=PlatformName.other,summary='Expedia source claimed in free text')
    stay=board(db)['rows'][0]['board_stays'][0]
    assert stay['channel']=='Other' and stay['color']=='other'


def test_cancelled_and_invalid_stays_are_not_drawn_as_valid_bars(db):
    from app.models import BookingStatus
    p=prop(db);booking(db,p,status=BookingStatus.cancelled)
    booking(db,p,check_out=datetime(2026,10,20))
    row=board(db)['rows'][0]
    assert row['board_stays']==[] and row['board_review']


def test_default_calendar_is_read_only_escaped_and_honest_about_unknown_data(db):
    p=prop(db,name='Synthetic listing')
    guest=Guest(name='<script>Guest</script>',email='PRIVATE_EMAIL',phone='PRIVATE_PHONE');db.add(guest);db.commit()
    b=booking(db,p,guest_id=guest.id,door_code='PRIVATE_DOOR',guest_portal_token='PRIVATE_TOKEN')
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with TestClient(app) as client:
        page=client.get('/operations/calendar?start=2026-10-21&end=2026-10-25')
        assert page.status_code==200 and 'Property by date calendar' in page.text
        assert '&lt;script&gt;Guest&lt;/script&gt;' in page.text and '<script>Guest' not in page.text
        assert f'/operations/reservations/{b.id}' in page.text and 'Airbnb' in page.text
        assert 'Live occupancy: <strong>not verified</strong>' in page.text
        assert 'not confirmed availability' in page.text
        assert '1 available' not in page.text and '0.0%' not in page.text
        assert 'PRIVATE_' not in page.text
        assert 'draft_check_in=2026-10-21' in page.text
        assert page.headers['cache-control']=='no-store'
    assert db.query(Booking).count()==1 and db.query(MessageLog).count()==db.query(OpsCommand).count()==0


def test_board_admin_gate_and_property_scope(db):
    p=prop(db);other=prop(db,name='Not selected');booking(db,other)
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        url=f'/operations/calendar?start=2026-10-21&end=2026-10-25&property_id={p.id}'
        assert client.get(url).status_code==403
        app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
        text=client.get(url).text
        assert 'Not selected' not in text and 'No recorded stay' in text
        visible = re.sub(r'<style\b[^>]*>.*?</style>', '', text, flags=re.S)
        visible = re.sub(r'<[^>]*>', '', visible)
        assert '0%' not in visible


def test_home_lands_on_calendar_and_source_records_route_retained(monkeypatch,db):
    import app.main as main
    monkeypatch.setattr(main,'SessionLocal',lambda:db)
    app=FastAPI()
    # Exercise the actual original application's landing route without middleware
    # or its scheduler; staff authorization is verified by test_staff_access.
    for route in main.app.routes:
        if getattr(route,'path',None)=='/':app.router.routes.append(route)
    with TestClient(app) as client:
        response=client.get('/',follow_redirects=False)
        assert response.status_code==303 and response.headers['location']=='/operations/calendar'
    assert any(getattr(r,'path',None)=='/source-booking-records' for r in main.app.routes)
