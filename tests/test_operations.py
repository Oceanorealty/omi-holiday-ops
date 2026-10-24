"""Synthetic records only; no network, production database, mail or scheduler."""
import copy
import io
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.models import (Booking, BookingStatus, CleaningStatus, CleaningTask, Guest, IcalFeed,
    IntegrationRun, MessageDelivery, MessageLog, MessageTemplate, OwnerStatement, PlatformName,
    Property, ShadowReservation, StaffRole, StaffUser, Transaction, TriggerEvent)
from app.sync import ical_sync
from app.sync.cleaning import sync_cleaning_tasks
from app.comms import triggers
from app.comms.owner_reports import _generate_statement, finalize_statement, send_owner_reports
from app.integrations.hostaway import HostawayReader, IntegrationError, ingest_shadow, refresh_shadow
from app.operations.tools import OpsTools
from app.operations.mcp_server import call, serve
from app.reconcile.summary import monthly_summary
from app import migrate


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("OPS_MESSAGE_DISPATCH_ENABLED", raising=False)
    monkeypatch.delenv("OPS_OWNER_EMAIL_ENABLED", raising=False)
    engine = create_engine("sqlite:///" + str(tmp_path / "synthetic.db"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


def prop(db, name="Synthetic", **kw):
    p = Property(name=name, time_zone="Australia/Brisbane", commission_pct=Decimal("20"), **kw)
    db.add(p); db.commit(); return p


def booking(db, p=None, **kw):
    p = p or prop(db)
    defaults = dict(property_id=p.id, platform=PlatformName.airbnb, uid="uid" + str(db.query(Booking).count()),
                    check_in=datetime(2026, 10, 21), check_out=datetime(2026, 10, 23),
                    status=BookingStatus.confirmed, created_at=datetime(2026, 10, 20), amount=Decimal("200"))
    defaults.update(kw)
    b = Booking(**defaults); db.add(b); db.commit(); return b


def feed(db, p, url="https://calendar.invalid/feed"):
    f = IcalFeed(property_id=p.id, platform=PlatformName.airbnb, url=url)
    db.add(f); db.commit(); return f


def event(uid="evt", **kw):
    e = dict(uid=uid, summary="Synthetic", check_in=datetime(2026, 10, 21), check_out=datetime(2026, 10, 23), cancelled=False)
    e.update(kw); return e


def test_missing_event_preserves_booking_and_isolates_feeds(db, monkeypatch):
    p = prop(db); f1, f2 = feed(db, p), feed(db, p)
    b1, b2 = booking(db, p, source_feed_id=f1.id), booking(db, p, source_feed_id=f2.id)
    monkeypatch.setattr(ical_sync, "fetch_and_parse", lambda _: [])
    assert ical_sync.sync_feed(db, f1)["missing"] == 1
    assert b1.status == BookingStatus.confirmed and b1.source_missing_since
    assert b2.source_missing_since is None


def test_explicit_cancel_and_repeated_import(db, monkeypatch):
    p = prop(db); f = feed(db, p)
    monkeypatch.setattr(ical_sync, "fetch_and_parse", lambda _: [event()])
    ical_sync.sync_feed(db, f); ical_sync.sync_feed(db, f)
    b = db.query(Booking).one()
    assert not b.notifications_enabled
    monkeypatch.setattr(ical_sync, "fetch_and_parse", lambda _: [event(cancelled=True)])
    ical_sync.sync_feed(db, f)
    assert b.status == BookingStatus.cancelled and db.query(Booking).count() == 1


def test_invalid_batch_no_partial_booking_changes(db, monkeypatch):
    p = prop(db); f = feed(db, p)
    monkeypatch.setattr(ical_sync, "fetch_and_parse", lambda _: [event(), event("bad", check_out=datetime(2020, 1, 1))])
    assert ical_sync.sync_feed(db, f)["error"]
    assert db.query(Booking).count() == 0


def test_network_failure_does_not_expose_feed_url(db, monkeypatch):
    p = prop(db); f = feed(db, p); b = booking(db, p, source_feed_id=f.id)
    def fail(_): raise RuntimeError("secret_in_url")
    monkeypatch.setattr(ical_sync, "fetch_and_parse", fail)
    result = ical_sync.sync_feed(db, f)
    assert "secret_in_url" not in str(result) and b.status == BookingStatus.confirmed


def test_source_collision_no_overwrite(db, monkeypatch):
    p = prop(db); f1, f2 = feed(db, p), feed(db, p)
    b = booking(db, p, uid="evt", source_feed_id=f2.id)
    monkeypatch.setattr(ical_sync, "fetch_and_parse", lambda _: [event(check_in=datetime(2026, 10, 22))])
    assert ical_sync.sync_feed(db, f1)["error"]
    assert b.check_in == datetime(2026, 10, 21)


def test_local_calendar_time():
    aware = datetime(2026, 10, 20, 15, tzinfo=timezone.utc)
    assert ical_sync._to_datetime(aware, "Australia/Brisbane") == datetime(2026, 10, 21, 1)


def test_cleaning_preserves_locked_and_completed_schedules(db):
    b = booking(db); sync_cleaning_tasks(db)
    t = db.query(CleaningTask).one(); t.schedule_locked = True
    original = t.due_date; b.check_out += timedelta(days=1); db.commit()
    sync_cleaning_tasks(db)
    assert t.due_date == original and t.schedule_review_required
    t.schedule_locked = False; sync_cleaning_tasks(db)
    assert t.due_date == b.check_out
    t.status = CleaningStatus.done; b.check_out += timedelta(days=1); db.commit()
    sync_cleaning_tasks(db)
    assert t.schedule_review_required and t.due_date != b.check_out


def test_historical_bookings_do_not_receive_expired_messages(db):
    b = booking(db, check_in=datetime(2025, 1, 1), check_out=datetime(2025, 1, 3), notifications_enabled=True)
    assert triggers._due_events(b, date(2026, 10, 20)) == []
    b.notifications_enabled = False
    assert triggers._due_events(b, date(2025, 1, 1)) == []


def message_setup(db, monkeypatch):
    guest = Guest(name="Synthetic", email="guest@example.invalid"); db.add(guest); db.commit()
    b = booking(db, guest_id=guest.id, notifications_enabled=True)
    db.add(MessageTemplate(trigger_event=TriggerEvent.booking_confirmed, subject="Synthetic", body="Hello")); db.commit()
    monkeypatch.setattr(triggers, "smtp_configured", lambda: True)
    monkeypatch.setattr(triggers, "render", lambda *args: ("Test", "Synthetic"))
    return b


def test_dry_run_has_no_persistent_or_external_side_effect(db, monkeypatch):
    message_setup(db, monkeypatch)
    def never(*a): pytest.fail("SMTP called")
    monkeypatch.setattr(triggers, "send_email", never)
    result = triggers.process_due_messages(db, date(2026, 10, 20), dry_run=True)
    assert result["held"] == 1 and db.query(MessageDelivery).count() == 0


def test_smtp_unknown_is_not_retried(db, monkeypatch):
    message_setup(db, monkeypatch); monkeypatch.setenv("OPS_MESSAGE_DISPATCH_ENABLED", "1")
    sends = []
    def uncertain(*a): sends.append(1); raise TimeoutError()
    monkeypatch.setattr(triggers, "send_email", uncertain)
    triggers.process_due_messages(db, date(2026, 10, 20))
    triggers.process_due_messages(db, date(2026, 10, 20))
    assert sends == [1] and db.query(MessageDelivery).one().state == "unknown"


def test_successful_dispatch_is_not_repeated(db, monkeypatch):
    message_setup(db, monkeypatch); monkeypatch.setenv("OPS_MESSAGE_DISPATCH_ENABLED", "1")
    sends=[]; monkeypatch.setattr(triggers, "send_email", lambda *a: sends.append(1))
    triggers.process_due_messages(db, date(2026, 10, 20)); triggers.process_due_messages(db, date(2026, 10, 20))
    assert sends == [1] and db.query(MessageLog).count() == 1


def test_missing_financial_amount_blocks_finalization(db):
    p = prop(db); booking(db, p, amount=None)
    row = monthly_summary(db)[0]
    assert row["expected"] is None and row["variance"] is None
    statement = _generate_statement(db, p, "2026-10")
    assert statement.missing_amount_count == 1
    with pytest.raises(ValueError): finalize_statement(db, statement)
    assert not statement.finalized


def test_reported_payout_not_bank_receipt(db):
    b = booking(db)
    t = Transaction(booking_id=b.id, source="synthetic", amount=200, occurred_at=datetime(2026, 10, 24), matched=True, confirmed_received=False)
    db.add(t); db.commit()
    assert monthly_summary(db)[0]["received"] == 0
    t.confirmed_received = True; db.commit()
    assert monthly_summary(db)[0]["received"] == 200


def test_finalized_statement_frozen_and_drafts_not_sent(db, monkeypatch):
    p = prop(db, owner_email="owner@example.invalid"); b = booking(db, p)
    s = _generate_statement(db, p, "2026-10"); finalize_statement(db, s); db.commit()
    b.amount = 999; db.commit()
    assert _generate_statement(db, p, "2026-10").gross_revenue == 200
    finalize_statement(db, s); assert s.finalized
    result = send_owner_reports(db, datetime(2026, 11, 1))
    assert result["sent"] == 0


def record(**kw):
    r = dict(id=12, listingMapId=5, status="new", arrivalDate="2026-10-21", departureDate="2026-10-23",
             totalPrice="200", currency="AUD", updatedOn="2026-10-20T00:00:00Z", guestEmail="must-not-store", doorCode="private")
    r.update(kw); return r


def test_shadow_idempotent_private_data_dropped_no_business_writes(db):
    for _ in range(2): ingest_shadow(db, provider="hostaway", account="synthetic", records=[record()]); db.commit()
    row = db.query(ShadowReservation).one()
    assert "must-not-store" not in row.payload and "private" not in row.payload
    assert row.state == "unmapped" and db.query(Booking).count() == db.query(MessageLog).count() == 0


def test_shadow_older_and_ambiguous_updates_held(db):
    ingest_shadow(db, provider="hostaway", account="a", records=[record()]); db.commit()
    result = ingest_shadow(db, provider="hostaway", account="a", records=[record(status="cancelled", updatedOn="2026-10-19")]); db.commit()
    assert result["held"] == 1 and json.loads(db.query(ShadowReservation).one().payload)["status"] == "new"


def test_invalid_shadow_batch_is_atomic(db):
    with pytest.raises(ValueError): ingest_shadow(db, provider="hostaway", account="a", records=[record(), record(id=13,totalPrice="NaN")])
    assert db.query(ShadowReservation).count() == 0


class Response:
    def __init__(self, result=None, status_code=200, headers=None):
        self.result, self.status_code, self.headers = result, status_code, headers or {}
    def json(self): return {"status":"success", "result":self.result}


class Transport:
    def __init__(self, responses): self.responses, self.calls = iter(responses), []
    def get(self, url, **kwargs): self.calls.append((url, kwargs)); return next(self.responses)


def test_cursor_reads_until_empty_and_disables_redirects():
    t = Transport([Response([record()]), Response([])])
    r = HostawayReader("synthetic-token", session=t).reservations(listing_id=5, arrival_start="2026-10-01", arrival_end="2026-10-31")
    assert len(r) == 1 and t.calls[1][1]["params"]["afterId"] == "12"
    assert t.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("responses,code", [([Response(status_code=403)],"authorization_required"),
    ([Response(status_code=429, headers={"X-RateLimit-Retry-After":"1800000000"})],"rate_limited"),
    ([Response([record(listingMapId=99)])],"scope_mismatch"),
    ([Response([record()]),Response([record()])],"pagination_stalled")])
def test_reader_errors_are_bounded(responses, code):
    with pytest.raises(IntegrationError) as caught:
        HostawayReader("synthetic-token", session=Transport(responses)).reservations(listing_id=5, arrival_start="2026-10-01", arrival_end="2026-10-31")
    assert caught.value.code == code and "token" not in str(caught.value)


def test_failed_full_read_does_not_import_partial_pages(db):
    reader = HostawayReader("test", session=Transport([Response([record()]),Response(status_code=500)]))
    with pytest.raises(IntegrationError): refresh_shadow(db, reader, account="a",listing_id=5,arrival_start="2026-10-01",arrival_end="2026-10-31")
    assert db.query(ShadowReservation).count() == 0 and db.query(IntegrationRun).one().state == "failed"


def test_tools_enforce_scope_and_hide_secrets(db):
    p1, p2 = prop(db), prop(db,"Other")
    booking(db,p1); booking(db,p2)
    service = OpsTools(db,[p1.id])
    with pytest.raises(PermissionError): service.property_facts(p2.id)
    assert "owner_portal_token" not in service.property_facts(p1.id)
    assert len(service.arrivals("2026-10-01","2026-10-31")["items"]) == 1
    assert len(service.reconciliation()["items"]) == 1
    with pytest.raises(ValueError): service.arrivals("2026-01-01","2026-12-31")


def test_mcp_protocol_and_unknown_mutation_tool(db):
    p = prop(db)
    src = io.StringIO('\n'.join(json.dumps(x) for x in [
        {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}},
        {"jsonrpc":"2.0","id":2,"method":"tools/list"},
        {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"delete_booking","arguments":{}}}])+'\n')
    out = io.StringIO(); serve(db.bind,[p.id],src,out)
    results = [json.loads(x)["result"] for x in out.getvalue().splitlines()]
    assert results[0]["protocolVersion"] == "2025-03-26"
    assert len(results[1]["tools"]) == 4 and results[2]["isError"]
    assert call(db.bind,[p.id],"get_property_facts",{"property_id":p.id})["name"] == p.name


def test_additive_migration_reentrant_preserves_data(tmp_path):
    engine=create_engine("sqlite:///"+str(tmp_path/'legacy.db'))
    with engine.begin() as c:
        c.execute(text('CREATE TABLE bookings (id INTEGER PRIMARY KEY, amount NUMERIC, door_code VARCHAR, guest_portal_token VARCHAR, is_block BOOLEAN)'))
        c.execute(text('INSERT INTO bookings (id,amount) VALUES (1,123)'))
    migrate.run(engine); migrate.run(engine)
    with engine.connect() as c: assert c.execute(text('SELECT amount FROM bookings WHERE id=1')).scalar() == 123
    assert 'source_feed_id' in {c['name'] for c in inspect(engine).get_columns('bookings')}
    engine.dispose()


def test_workbench_denies_anonymous_and_non_admin(db, monkeypatch):
    from app.operations.router import router
    from app.auth import make_session_cookie, SESSION_COOKIE
    monkeypatch.setenv("SECRET_KEY", "synthetic-secret-" + "x"*32)
    monkeypatch.setenv("ADMIN_USER", "synthetic-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "synthetic-password")
    app=FastAPI(); app.include_router(router)
    def database(): yield db
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        assert client.get('/api/ops/overview').status_code == 403
        client.cookies.set(SESSION_COOKIE,make_session_cookie("nobody"))
        assert client.get('/api/ops/overview').status_code == 403
        client.cookies.set(SESSION_COOKIE,make_session_cookie("synthetic-admin"))
        assert client.get('/api/ops/overview').status_code == 200
        assert client.get('/operations').status_code == 200


def test_owner_delivery_opt_in_and_unknown_no_resend(db, monkeypatch):
    from app.comms import owner_reports
    from app.models import StatementDelivery
    p = prop(db, owner_email="owner@example.invalid"); booking(db,p)
    s = _generate_statement(db,p,"2026-10");finalize_statement(db,s);db.commit()
    monkeypatch.setenv("OPS_OWNER_EMAIL_ENABLED","1")
    monkeypatch.setattr(owner_reports,"smtp_configured",lambda: True)
    sent=[]
    def uncertain(*args): sent.append(1);raise TimeoutError()
    monkeypatch.setattr(owner_reports,"send_email",uncertain)
    send_owner_reports(db,datetime(2026,11,1));send_owner_reports(db,datetime(2026,11,1))
    assert sent==[1] and db.query(StatementDelivery).one().state=="unknown" and s.sent_at is None


def test_owner_delivery_success_once(db,monkeypatch):
    from app.comms import owner_reports
    p=prop(db,owner_email="owner@example.invalid");booking(db,p)
    s=_generate_statement(db,p,"2026-10");finalize_statement(db,s);db.commit()
    monkeypatch.setenv("OPS_OWNER_EMAIL_ENABLED","1");monkeypatch.setattr(owner_reports,"smtp_configured",lambda:True)
    sent=[];monkeypatch.setattr(owner_reports,"send_email",lambda *a:sent.append(1))
    send_owner_reports(db,datetime(2026,11,1));send_owner_reports(db,datetime(2026,11,1))
    assert sent==[1] and s.sent_at


def test_full_app_hermetic_routes(tmp_path):
    import os, subprocess, sys
    script = '''
from datetime import datetime
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import Property, Booking, PlatformName
from app.auth import make_session_cookie, SESSION_COOKIE
with SessionLocal() as db:
 p=Property(name="Synthetic",commission_pct=20);db.add(p);db.flush()
 db.add(Booking(property_id=p.id,platform=PlatformName.direct,uid="synthetic",check_in=datetime(2026,10,1),check_out=datetime(2026,10,3),amount=None));db.commit()
with TestClient(app) as c:
 r=c.post('/login',data={'username':'test-admin','password':'fake','next':'//example.invalid'},follow_redirects=False)
 assert r.status_code==303 and r.headers['location']=='/'
 assert 'HttpOnly' in r.headers['set-cookie'] and 'Secure;' not in r.headers['set-cookie']
 c.cookies.set(SESSION_COOKIE,make_session_cookie("test-admin"))
 for path in ["/healthz","/","/transactions","/cleaning","/operations","/api/ops/overview","/operations/setup","/operations/reservations","/operations/inventory","/operations/reports","/operations/channels","/operations/task-plans","/operations/guest-checkins","/operations/reservation-requests","/operations/resources","/operations/inbox","/operations/agreements","/operations/accounts"]:
  r=c.get(path);assert r.status_code==200,(path,r.status_code,r.text[:300])
 assert c.get('/healthz').json()['release']=='ops-gapfill-20261023'
 assert "Unknown (1 missing)" in c.get("/transactions").text
 assert c.post("/properties/1/statements/generate",data={"period":"2026-10"}).status_code==200
 assert c.post("/statements/1/finalize",follow_redirects=False).status_code==409
with TestClient(app,base_url='https://testserver') as c:
 r=c.post('/login',data={'username':'test-admin','password':'fake'},follow_redirects=False)
 assert r.status_code==303 and 'Secure' in r.headers['set-cookie']
'''
    env={"PATH":os.environ.get("PATH",""),"DATABASE_URL":"sqlite:///"+str(tmp_path/'app.db'),
         "OPS_SCHEDULER_ENABLED":"0","OPS_MESSAGE_DISPATCH_ENABLED":"0","OPS_OWNER_EMAIL_ENABLED":"0",
         "SECRET_KEY":"synthetic-hermetic-test-secret-"+"x"*32,"ADMIN_USER":"test-admin","ADMIN_PASSWORD":"fake"}
    result=subprocess.run([sys.executable,"-c",script],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr[-2500:]



def test_duplicate_shadow_with_autoflush_disabled(db):
    db.autoflush = False
    counts = ingest_shadow(db, provider="hostaway", account="synthetic", records=[record(),record()])
    db.commit()
    assert counts["inserted"] == 1 and counts["unchanged"] == 1
    assert db.query(ShadowReservation).count() == 1
