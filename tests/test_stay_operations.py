from datetime import datetime, timezone
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.db import get_db
from app.models import Booking, BookingStatus, ReservationEvent, CleaningTask, CleaningStatus
from app.operations import stay_operations as stay
from app.operations.tasks import TaskError
from app.operations.parity_router import router
from app.operations.router import require_admin
from test_operations import db, prop, booking
from test_postgres_release import pg


@pytest.fixture
def row(db, monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'synthetic-stay-operations-' + 'x'*32)
    return booking(db)


def save(db, row, action, **changes):
    args=dict(actor='synthetic-admin', booking_id=row.id, submitted_token=stay.token(db,'synthetic-admin',row),
        action=action, note='Synthetic attendance rehearsal', now=datetime(2026,10,31,tzinfo=timezone.utc))
    args.update(changes)
    return stay.record(db, **args)


def test_real_attendance_is_separate_from_booking_and_cleaning(db,row):
    task=CleaningTask(booking_id=row.id,property_id=row.property_id,due_date=row.check_out,status=CleaningStatus.pending)
    db.add(task);db.commit(); original=(row.check_in,row.check_out,row.status,task.due_date,task.status)
    assert stay.state(db,row.id)['status']=='not_recorded'
    save(db,row,'arrive',arrived_at='2026-10-21T15:10')
    assert stay.state(db,row.id)['status']=='in_house'
    save(db,row,'plan_departure',expected_departure='2026-10-23T13:30')
    save(db,row,'depart',departed_at='2026-10-23T13:15')
    assert stay.state(db,row.id)['departed_at']=='2026-10-23T13:15'
    assert (row.check_in,row.check_out,row.status,task.due_date,task.status)==original
    assert db.query(ReservationEvent).count()==3


def test_stale_duplicate_and_booking_edit_rejected(db,row):
    stale=stay.token(db,'synthetic-admin',row)
    save(db,row,'arrive',arrived_at='2026-10-21T14:00')
    with pytest.raises(TaskError):save(db,row,'depart',submitted_token=stale,departed_at='2026-10-23T11:00')
    old=stay.token(db,'synthetic-admin',row)
    row.summary='Synthetic changed reservation';db.commit()
    with pytest.raises(TaskError):save(db,row,'depart',submitted_token=old,departed_at='2026-10-23T11:00')
    assert db.query(ReservationEvent).count()==1


@pytest.mark.parametrize('action,values',[
    ('depart',dict(departed_at='2026-10-23T11:00')),
    ('arrive',dict(arrived_at='2026-11-21T14:00')),
    ('arrive',dict(arrived_at='2026-10-21T25:00')),
    ('correct',dict(target_status='departed',departed_at='2026-10-23T11:00')),
    ('correct',dict(target_status='not_recorded',arrived_at='2026-10-21T14:00')),
    ('arrive',dict(arrived_at='2026-10-21T14:00',note='short')),
])
def test_invalid_transitions_have_no_partial_write(db,row,action,values):
    with pytest.raises(TaskError):save(db,row,action,**values)
    assert db.query(ReservationEvent).count()==0


def test_correction_keeps_prior_event_and_actor(db,row):
    save(db,row,'arrive',arrived_at='2026-10-21T14:00')
    save(db,row,'correct',target_status='not_recorded',note='Synthetic mistaken arrival corrected')
    assert stay.state(db,row.id)['status']=='not_recorded'
    events=db.query(ReservationEvent).order_by(ReservationEvent.id).all()
    assert len(events)==2 and events[1].actor=='synthetic-admin'
    assert json.loads(events[1].before_state)['arrived_at']=='2026-10-21T14:00'
    assert json.loads(events[0].after_state)['status']=='in_house'


def test_departure_order_and_actor_guard(db,row):
    save(db,row,'arrive',arrived_at='2026-10-21T14:00')
    with pytest.raises(TaskError):save(db,row,'depart',departed_at='2026-10-20T12:00')
    with pytest.raises(TaskError):save(db,row,'depart',departed_at='2026-11-20T12:00')
    with pytest.raises(TaskError):save(db,row,'depart',departed_at='2026-10-23T12:00',actor='other-synthetic-admin')
    assert db.query(ReservationEvent).count()==1


def test_ambiguous_dst_rejected_and_missing_expected_hour_remains_unknown(db,row):
    from zoneinfo import ZoneInfo
    with pytest.raises(TaskError):stay.local_time('2026-04-05T02:30',ZoneInfo('Australia/Sydney'))
    assert stay.state(db,row.id)['expected_departure']==''


def test_cancelled_and_blocks_not_normal_arrivals(db,row):
    row.status=BookingStatus.cancelled;db.commit()
    with pytest.raises(TaskError):save(db,row,'arrive',arrived_at='2026-10-21T14:00')
    row.is_block=True;db.commit()
    with pytest.raises(TaskError):save(db,row,'correct',target_status='not_recorded')


def test_same_day_handover_warning_no_inferred_arrival_or_fee(db,row):
    following=booking(db,row.property,check_in=row.check_out,check_out=datetime(2026,10,25))
    save(db,row,'plan_departure',expected_departure='2026-10-23T13:30')
    warnings=stay.context(db,row,'synthetic-admin')['stay_warnings']
    assert len(warnings)==2 and str(following.id) in warnings[0]
    assert following.check_in==datetime(2026,10,23)
    assert row.amount==200


def test_original_route_render_and_post_and_origin_guard(db,row):
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with TestClient(app) as client:
        page=client.get(f'/operations/reservations/{row.id}')
        assert page.status_code==200 and 'Record check-in' in page.text and 'attendance unknown' in page.text
        data=dict(token=stay.token(db,'synthetic-admin',row),action='arrive',arrived_at='2026-10-21T14:00',note='Synthetic original form rehearsal')
        bad=client.post(f'/operations/reservations/{row.id}/attendance',data=data,headers={'Origin':'https://unrelated.invalid'},follow_redirects=False)
        assert bad.status_code==403 and db.query(ReservationEvent).count()==0
        good=client.post(f'/operations/reservations/{row.id}/attendance',data=data,follow_redirects=False)
        assert good.status_code==303 and good.headers['location'].endswith('#attendance')
        assert 'Record check-out' in client.get(good.headers['location']).text


def test_disposable_postgres_attendance_race_retry_and_rollback(pg):
    """Only existing CI fixture's disposable loopback ops_release_test is permitted."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session
    from app.db import Base
    engine, _, _ = pg
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        row=booking(db)
        entity_id=row.id
        submitted=stay.token(db,'synthetic-admin',row)
    barrier=Barrier(2)
    def compete():
        with Session(engine) as db:
            barrier.wait(timeout=10)
            try:
                stay.record(db,actor='synthetic-admin',booking_id=entity_id,submitted_token=submitted,
                    action='arrive',arrived_at='2026-10-21T14:00',note='Synthetic competing PostgreSQL attendance',
                    now=datetime(2026,10,31,tzinfo=timezone.utc))
                return 'saved'
            except TaskError:
                return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:compete(),range(2)))
    assert sorted(results)==['rejected','saved']
    with Session(engine) as db:
        row=db.get(Booking,entity_id)
        assert db.query(ReservationEvent).count()==1
        with pytest.raises(TaskError):save(db,row,'arrive',submitted_token=submitted,arrived_at='2026-10-21T14:00')
        # Invalid writes after acquiring the shared guard must roll back without an event.
        with pytest.raises(TaskError):save(db,row,'depart',departed_at='2026-10-20T11:00')
        assert db.query(ReservationEvent).count()==1 and stay.state(db,row.id)['status']=='in_house'
        old=stay.token(db,'synthetic-admin',row)
        row.summary='Synthetic concurrent reservation edit';db.commit()
        with pytest.raises(TaskError):save(db,row,'depart',submitted_token=old,departed_at='2026-10-23T11:00')
        save(db,row,'depart',departed_at='2026-10-23T11:00')
        assert db.query(ReservationEvent).count()==2 and stay.state(db,row.id)['status']=='departed'
