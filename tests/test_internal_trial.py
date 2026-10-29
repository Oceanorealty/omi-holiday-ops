"""Synthetic-only isolation checks against the existing OPS runtime modules."""
import json
import socket
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.orm import Session
from sqlalchemy.engine import make_url

from app.db import Base, get_db
from app.models import (Property, Booking, Cleaner, CleaningTask, IcalFeed, Guest,
                        MessageLog, MessageDelivery, OwnerStatement, Transaction,
                        WorkOrder, StatementDelivery)
from app.operations import internal_trial as trial


def payload():
    common = dict(provider="airbnb", source_account="trial@example.invalid",
                  provenance="Synthetic export, 2026-10-29; no live account")
    return [dict(common, kind="listing", source_id="listing-1", title="Synthetic studio"),
            dict(common, kind="reservation", source_id="reservation-1", listing_id="listing-1",
                 check_in="2026-10-29", check_out="2026-10-31", status="confirmed", guest_count=2),
            dict(common, kind="cleaning", source_id="cleaning-1", listing_id="listing-1",
                 reservation_id="reservation-1", due_date="2026-10-31", status="assigned",
                 cleaner_reference="source-cleaner-9")]


def encoded(rows=None):
    return json.dumps(payload() if rows is None else rows).encode()


@pytest.fixture
def engine(tmp_path):
    engine, cleanup = make_test_engine(tmp_path)
    Base.metadata.create_all(engine)
    trial.install_schema(engine)
    yield engine
    engine.dispose()
    cleanup()


def make_test_engine(tmp_path):
    url = os.environ.get("OPS_INTERNAL_TRIAL_POSTGRES_URL")
    if not url:
        return create_engine("sqlite:///" + str(tmp_path / "synthetic-only.sqlite"),
                             connect_args={"check_same_thread": False}), lambda: None
    parsed = make_url(url)
    if parsed.host not in {"127.0.0.1", "localhost"} or parsed.database != "ops_trial_test":
        raise RuntimeError("Trial tests require disposable loopback PostgreSQL ops_trial_test")
    schema = "trial_test_" + uuid.uuid4().hex
    from sqlalchemy import text
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text('CREATE SCHEMA "' + schema + '"'))
    engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
    def cleanup():
        with admin.begin() as conn:
            conn.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()
    return engine, cleanup


def test_preview_deduplicates_by_source_and_is_order_independent():
    rows = payload()
    result = trial.preview(encoded(rows + [rows[0]]))
    assert result["counts"] == {"listing": 1, "reservation": 1, "cleaning": 1}
    assert result["id"] == trial.preview(encoded(list(reversed(rows))))["id"]
    assert result["physical_units_reconciled"] is False


def test_external_listing_ids_are_not_assumed_distinct_physical_apartments():
    rows = payload()[:1]
    rows += [{**rows[0], "source_id": "listing-2", "title": "Same possible apartment, another listing"}]
    plan = trial.preview(encoded(rows))
    assert plan["counts"]["listing"] == 2 and not plan["physical_units_reconciled"]


def test_actual_ops_workers_and_public_api_cannot_see_trial_records(engine, monkeypatch):
    from app.sync.ical_sync import sync_all
    from app.sync.cleaning import sync_cleaning_tasks
    from app.comms.triggers import process_due_messages
    from app.comms.owner_reports import send_owner_reports
    from app.operations.task_plans import materialize_schedules
    from app.public_api import router
    def outbound_forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network attempt during trial isolation test")
    monkeypatch.setattr(socket.socket, "connect", outbound_forbidden)
    # Deliberately enable existing delivery switches in the disposable database:
    # quarantine must remain isolated even when real operational workers run.
    monkeypatch.setenv("OPS_MESSAGE_DISPATCH_ENABLED", "1")
    monkeypatch.setenv("OPS_OWNER_EMAIL_ENABLED", "1")
    with Session(engine) as db:
        baseline = Property(name="Existing public listing", published=True,
                            time_zone="Australia/Brisbane")
        cleaner = Cleaner(name="Existing synthetic cleaner", active=True)
        db.add_all([baseline, cleaner]); db.commit()
        baseline_id, cleaner_id = baseline.id, cleaner.id
    content = encoded()
    trial.apply(engine, content, expected_digest=trial.preview(content)["id"])
    with Session(engine) as db:
        sync_all(db)
        sync_cleaning_tasks(db)
        materialize_schedules(db)
        result = process_due_messages(db)
        send_owner_reports(db)
        assert result["sent"] == 0
        for model in (Booking, CleaningTask, IcalFeed, Guest, MessageLog, MessageDelivery,
                      OwnerStatement, Transaction, WorkOrder, StatementDelivery):
            assert db.query(model).count() == 0, model.__tablename__
        assert db.query(Property).count() == 1
        assert db.get(Property, baseline_id).name == "Existing public listing"
        assert db.query(Cleaner).count() == 1 and db.get(Cleaner, cleaner_id).active
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as client:
            response = client.get("/api/public/listings")
            assert response.status_code == 200
            assert "Existing public listing" in response.text
            assert "Synthetic studio" not in response.text
            assert "reservation-1" not in response.text


def test_repeated_apply_is_idempotent(engine):
    content = encoded(); digest = trial.preview(content)["id"]
    assert not trial.apply(engine, content, expected_digest=digest)["already_present"]
    assert trial.apply(engine, content, expected_digest=digest)["already_present"]
    assert len(trial.observations(engine, digest)) == 3
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(trial.batches)).scalar() == 1


def test_conflicting_source_identity_and_changed_preview_fail_closed(engine):
    rows = payload(); rows.append({**rows[0], "title": "Conflicting title"})
    with pytest.raises(trial.TrialImportError):
        trial.preview(encoded(rows))
    content = encoded(); digest = trial.preview(content)["id"]
    rows = payload(); rows[0]["title"] = "Changed since preview"
    with pytest.raises(trial.TrialImportError, match="Preview changed"):
        trial.apply(engine, encoded(rows), expected_digest=digest)
    assert trial.observations(engine, digest) == []


def test_database_failure_rolls_back_entire_batch(engine):
    content = encoded(); digest = trial.preview(content)["id"]
    writes = [0]
    def fail_second_record(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO internal_trial_records"):
            writes[0] += 1
            if writes[0] == 2:
                raise RuntimeError("Synthetic storage failure")
    event.listen(engine, "before_cursor_execute", fail_second_record)
    try:
        with pytest.raises(RuntimeError, match="Synthetic storage failure"):
            trial.apply(engine, content, expected_digest=digest)
    finally:
        event.remove(engine, "before_cursor_execute", fail_second_record)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(trial.batches)).scalar() == 0
        assert conn.execute(select(func.count()).select_from(trial.records)).scalar() == 0


def test_rollback_hides_batch_but_preserves_manifest_and_prevents_reactivation(engine):
    content = encoded(); digest = trial.preview(content)["id"]
    trial.apply(engine, content, expected_digest=digest)
    trial.rollback(engine, digest, note="Synthetic reviewed rollback")
    assert trial.observations(engine, digest) == []
    with engine.connect() as conn:
        assert conn.execute(select(trial.batches.c.state)).scalar() == "archived"
        assert conn.execute(select(func.count()).select_from(trial.records)).scalar() == 3
    with pytest.raises(trial.TrialImportError, match="cannot be silently reactivated"):
        trial.apply(engine, content, expected_digest=digest)


@pytest.mark.parametrize("field", ["guest_email", "passport", "card_number", "feed_url",
                                    "published", "notifications_enabled", "ops_cleaner_id"])
def test_rejects_personal_or_operational_fields(field):
    rows = payload(); rows[0][field] = "must not enter quarantine import"
    with pytest.raises(trial.TrialImportError, match="Unknown or missing fields"):
        trial.preview(encoded(rows))


def test_source_links_and_dates_are_validated():
    rows = payload(); rows[1]["listing_id"] = "unknown"
    with pytest.raises(trial.TrialImportError, match="Include each observation"):
        trial.preview(encoded(rows))
    rows = payload(); rows[2]["reservation_id"] = "unknown"
    with pytest.raises(trial.TrialImportError, match="Cleaning reservation"):
        trial.preview(encoded(rows))
    rows = payload(); rows[1]["check_out"] = rows[1]["check_in"]
    with pytest.raises(trial.TrialImportError, match="Invalid reservation dates"):
        trial.preview(encoded(rows))

