"""Synthetic fixtures only; exercise the isolated workflow rather than live ops."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
import socket
import threading
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, MetaData, String, Table, inspect, select
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE, make_session_cookie
from app.db import Base, get_db
from app.models import (Booking, Cleaner, CleaningTask, Guest, IcalFeed, MessageDelivery,
                        MessageLog, OwnerStatement, Property, Transaction, WorkOrder)
from app.operations import internal_trial as sources
from app.operations import internal_trial_workflows as flow
from app.operations.internal_trial_router import router
from app.operations.tasks import form_token
from test_internal_trial import make_test_engine


def source_payload(title="Synthetic source studio"):
    return [{"kind": "listing", "provider": "airbnb", "source_account": "synthetic@example.invalid",
             "source_id": "synthetic-listing-1", "title": title, "suburb": "Synthetic test region",
             "provenance": "Synthetic source fixture; no actual listing, account or guest"}]


@pytest.fixture
def engine(tmp_path):
    engine, cleanup = make_test_engine(tmp_path)
    Base.metadata.create_all(engine); sources.install_schema(engine); flow.prepare(engine)
    content = json.dumps(source_payload()).encode()
    sources.apply(engine, content, expected_digest=sources.preview(content)["id"])
    with Session(engine) as db:
        db.add_all([Property(name="Existing synthetic property " + str(i), published=False) for i in range(3)])
        db.add_all([Cleaner(name="Existing synthetic cleaner " + str(i), active=True) for i in range(2)])
        db.commit()
    yield engine
    engine.dispose(); cleanup()


def create(engine, *, command=None, check_in="2026-11-01", check_out="2026-11-03", rate="125.50"):
    card = flow.listing_cards(engine)[0]
    return flow.create(engine, source_key=card["key"], fingerprint=card["fingerprint"],
        check_in=check_in, check_out=check_out, nightly_rate=rate,
        command_digest=command or uuid.uuid4().hex, actor="synthetic-admin")


def act(engine, scenario, action, *, revision=None, command=None):
    row = next(r for r in flow.read(engine, include_archived=True) if r["id"] == scenario)
    return flow.apply_action(engine, scenario_id=scenario, action=action,
        revision=row["revision"] if revision is None else revision,
        command_digest=command or uuid.uuid4().hex, actor="synthetic-admin")


def test_multistep_simulation_keeps_every_operational_table_unchanged(engine, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Simulation attempted outbound network")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    scenario = create(engine)
    for action in ("confirm", "create_task", "complete_task", "simulate_payment", "preview_message",
                   "assign_cleaning", "check_in", "check_out", "complete_cleaning"):
        act(engine, scenario, action)
    state = flow.read(engine)[0]["data"]
    assert state["label"] == flow.LABEL and state["guest"] == "SYNTHETIC TEST GUEST"
    assert state["reservation_status"] == "checked_out"
    assert state["cleaning_status"] == state["task_status"] == "done"
    assert state["payment_state"] == "simulated_paid" and state["simulated_total"] == "251.00"
    assert "no message sent" in state["message_preview"]
    assert "no real staff assigned" in state["cleaner"]
    from app.sync.ical_sync import sync_all
    from app.sync.cleaning import sync_cleaning_tasks
    from app.operations.task_plans import materialize_schedules
    from app.comms.triggers import process_due_messages
    from app.comms.owner_reports import send_owner_reports
    monkeypatch.setenv("OPS_MESSAGE_DISPATCH_ENABLED", "1")
    monkeypatch.setenv("OPS_OWNER_EMAIL_ENABLED", "1")
    with Session(engine) as db:
        sync_all(db); sync_cleaning_tasks(db); materialize_schedules(db)
        assert process_due_messages(db)["sent"] == 0
        send_owner_reports(db)
        assert db.query(Property).count() == 3 and db.query(Cleaner).count() == 2
        for model in (Booking, CleaningTask, Guest, IcalFeed, MessageLog, MessageDelivery,
                      OwnerStatement, Transaction, WorkOrder):
            assert db.query(model).count() == 0, model.__tablename__
    assert len(flow.history(engine, scenario)) == 10
    act(engine, scenario, "archive")
    assert flow.read(engine) == [] and flow.read(engine, include_archived=True)[0]["archived"]
    assert len(flow.listing_cards(engine)) == 1


def test_overlap_cancellation_and_simulated_refund_release_only_test_calendar(engine):
    first = create(engine); second = create(engine)
    act(engine, first, "confirm"); act(engine, first, "simulate_payment")
    with pytest.raises(flow.SimulationError, match="overlap"):
        act(engine, second, "confirm")
    calendar = flow.calendar(engine, flow.listing_cards(engine), date(2026, 11, 1), days=3)
    assert calendar["rows"][0]["cells"] == [True, True, False]
    act(engine, first, "cancel")
    first_row = next(r for r in flow.read(engine) if r["id"] == first)
    assert first_row["data"]["payment_state"] == "simulated_refund"
    act(engine, second, "confirm")


def test_adjacent_simulated_stays_do_not_conflict(engine):
    first = create(engine); second = create(engine, check_in="2026-11-03", check_out="2026-11-05")
    act(engine, first, "confirm"); act(engine, second, "confirm")
    assert all(r["data"]["reservation_status"] == "confirmed" for r in flow.read(engine))


def test_retry_stale_revision_and_invalid_transition_are_safe(engine):
    command = uuid.uuid4().hex
    scenario = create(engine, command=command)
    assert create(engine, command=command) == scenario
    with pytest.raises(flow.SimulationError, match="different simulation details"):
        create(engine, command=command, rate="150")
    with pytest.raises(flow.SimulationError, match="unavailable"):
        act(engine, scenario, "check_out")
    confirm = uuid.uuid4().hex
    act(engine, scenario, "confirm", revision=0, command=confirm)
    assert act(engine, scenario, "confirm", revision=0, command=confirm)["already_applied"]
    with pytest.raises(flow.SimulationError, match="changed"):
        act(engine, scenario, "check_in", revision=0)
    assert len(flow.history(engine, scenario)) == 2


def test_concurrent_confirmations_cannot_both_take_one_simulated_slot(engine):
    first, second = create(engine), create(engine)
    barrier = threading.Barrier(2)
    def confirm(scenario):
        barrier.wait(timeout=10)
        try:
            act(engine, scenario, "confirm")
            return "confirmed"
        except flow.SimulationError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, (first, second)))
    assert sorted(results) == ["confirmed", "conflict"]


@pytest.mark.parametrize("start,end,rate", [("bad","2026-11-03","10"),
    ("2026-11-03","2026-11-01","10"),("2026-11-01","2027-11-01","10"),
    ("2026-11-01","2026-11-03","NaN"),("2026-11-01","2026-11-03","-1")])
def test_invalid_simulation_input_is_rejected(engine, start, end, rate):
    with pytest.raises(flow.SimulationError): create(engine, check_in=start, check_out=end, rate=rate)
    assert flow.read(engine) == []


def test_withdrawn_source_cannot_drive_new_actions_and_scenarios_can_be_archived(engine):
    scenario = create(engine)
    card = flow.listing_cards(engine)[0]
    sources.rollback(engine, card["batch_id"], note="Synthetic source withdrawal")
    with pytest.raises(flow.SimulationError, match="missing"):
        act(engine, scenario, "confirm")
    act(engine, scenario, "archive")
    assert flow.read(engine) == []


def test_source_dedup_and_conflicts_do_not_guess_physical_units(engine):
    rows = source_payload() + [{**source_payload()[0], "source_id": "synthetic-listing-2", "title": "Synthetic second listing"}]
    content = json.dumps(rows).encode(); sources.apply(engine, content, expected_digest=sources.preview(content)["id"])
    assert len(flow.listing_cards(engine)) == 2
    changed = json.dumps(source_payload(title="Conflicting synthetic title")).encode()
    sources.apply(engine, changed, expected_digest=sources.preview(changed)["id"])
    assert any(card["source_conflict"] for card in flow.listing_cards(engine))


def test_schema_mismatch_does_not_create_more_tables(tmp_path):
    engine, cleanup = make_test_engine(tmp_path)
    bad = MetaData(); Table("internal_trial_scenarios", bad, Column("wrong", String, primary_key=True))
    bad.create_all(engine)
    try:
        with pytest.raises(flow.SimulationError, match="needs review"): flow.prepare(engine)
        assert "internal_trial_scenario_events" not in inspect(engine).get_table_names()
    finally:
        engine.dispose(); cleanup()


def test_workspace_auth_cards_create_and_action_tokens(engine, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "synthetic-workspace-secret-" + "x"*32)
    monkeypatch.setenv("ADMIN_USER", "synthetic-admin"); monkeypatch.setenv("ADMIN_PASSWORD", "synthetic-only")
    app = FastAPI(); app.include_router(router)
    def session():
        with Session(engine) as db: yield db
    app.dependency_overrides[get_db] = session
    with TestClient(app) as client:
        assert client.get("/operations/internal-test").status_code == 403
        client.cookies.set(SESSION_COOKIE, make_session_cookie("synthetic-admin"))
        page = client.get("/operations/internal-test")
        assert page.status_code == 200 and "Synthetic source studio" in page.text and "真实来源" in page.text
        card = flow.listing_cards(engine)[0]
        purpose = "trial-scenario-create:" + card["key"] + ":" + card["fingerprint"]
        token = form_token("synthetic-admin", purpose=purpose)
        response = client.post("/operations/internal-test/simulations", data={
            "source_key":card["key"],"fingerprint":card["fingerprint"],"check_in":"2026-11-01",
            "check_out":"2026-11-03","nightly_rate":"100","token":token})
        assert response.status_code == 200 and 'type="email"' not in response.text  # no guest contact form
        row = flow.read(engine)[0]
        bad = client.post("/operations/internal-test/simulations/"+row["id"]+"/confirm",data={"token":"bad"})
        assert bad.status_code == 403
        from types import SimpleNamespace
        action_token = form_token("synthetic-admin", SimpleNamespace(id=row["id"],version=0), purpose="trial-scenario-action:confirm")
        response = client.post("/operations/internal-test/simulations/"+row["id"]+"/confirm",data={"token":action_token})
        assert response.status_code == 200 and "confirmed" in response.text
        assert client.post("/operations/internal-test/simulations/"+row["id"]+"/cancel",data={"token":action_token}).status_code == 403
        assert client.get("/operations/internal-test/status").json()["active_synthetic_scenarios"] == 1
