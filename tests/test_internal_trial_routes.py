import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE, make_session_cookie
from app.db import Base, get_db
from app.models import Booking, CleaningTask, Property, StaffRole, StaffUser
from app.operations.internal_trial_router import router
from app.operations import internal_trial as trial
from app.operations.tasks import form_token
from test_internal_trial import encoded, make_test_engine


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "synthetic-internal-trial-secret-" + "x" * 32)
    monkeypatch.setenv("ADMIN_USER", "synthetic-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "synthetic-test-only")
    engine, cleanup = make_test_engine(tmp_path)
    Base.metadata.create_all(engine)
    app = FastAPI(); app.include_router(router)
    def get_session():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = get_session
    with TestClient(app) as client:
        client.engine = engine
        client.cookies.set(SESSION_COOKIE, make_session_cookie("synthetic-admin"))
        yield client
    engine.dispose()
    cleanup()


def initialize(client):
    return client.post("/operations/internal-test/initialize", data={
        "token": form_token("synthetic-admin", purpose="trial-initialize")}, follow_redirects=False)


def test_no_public_or_non_admin_access_and_no_implicit_schema_creation(client):
    client.cookies.clear()
    assert client.get("/operations/internal-test").status_code == 403
    client.cookies.set(SESSION_COOKIE, make_session_cookie("synthetic-cleaner"))
    with Session(client.engine) as db:
        db.add(StaffUser(name="Synthetic cleaner", username="synthetic-cleaner",
                         password_hash="unused", role=StaffRole.cleaner, active=True)); db.commit()
    assert client.get("/operations/internal-test").status_code == 403
    assert not inspect(client.engine).has_table("internal_trial_batches")


def test_page_does_not_initialize_on_get_and_mutations_require_actor_bound_token(client):
    response = client.get("/operations/internal-test")
    assert response.status_code == 200 and "Prepare test tables" in response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert not inspect(client.engine).has_table("internal_trial_batches")
    assert client.post("/operations/internal-test/initialize", data={"token": "bad"}).status_code == 403
    token = form_token("other-user", purpose="trial-initialize")
    assert client.post("/operations/internal-test/initialize", data={"token": token}).status_code == 403
    assert initialize(client).status_code == 303


def test_private_preview_apply_and_rollback_ui_preserve_live_tables(client):
    assert initialize(client).status_code == 303
    content = encoded(); digest = trial.preview(content)["id"]
    response = client.post("/operations/internal-test/preview", data={
        "token": form_token("synthetic-admin", purpose="trial-preview")},
        files={"file": ("synthetic.json", content, "application/json")})
    assert response.status_code == 200 and "1 source listings" in response.text
    assert trial.observations(client.engine, digest) == []
    response = client.post("/operations/internal-test/apply", data={
        "manifest": trial.preview(content)["manifest"], "digest": digest,
        "token": form_token("synthetic-admin", purpose="trial-apply:" + digest)})
    assert response.status_code == 200 and "Synthetic studio" in response.text
    assert "source-cleaner-9" not in response.text  # not an operational assignment control
    assert len(trial.observations(client.engine, digest)) == 3
    with Session(client.engine) as db:
        assert db.query(Property).count() == db.query(Booking).count() == db.query(CleaningTask).count() == 0
    response = client.post("/operations/internal-test/" + digest + "/rollback", data={
        "token": form_token("synthetic-admin", purpose="trial-rollback:" + digest),
        "note": "Synthetic review completed"})
    assert response.status_code == 200 and "archived" in response.text
    assert trial.observations(client.engine, digest) == []


def test_payload_changes_cannot_reuse_preview_token_and_html_is_escaped(client):
    initialize(client)
    rows = json.loads(encoded()); rows[0]["title"] = "<script>alert('synthetic')</script>"
    content = json.dumps(rows).encode(); plan = trial.preview(content)
    token = form_token("synthetic-admin", purpose="trial-apply:" + plan["id"])
    changed = plan["manifest"].replace("Synthetic export", "Changed export")
    assert client.post("/operations/internal-test/apply", data={
        "manifest": changed, "digest": plan["id"], "token": token}).status_code == 409
    response = client.post("/operations/internal-test/apply", data={
        "manifest": plan["manifest"], "digest": plan["id"], "token": token})
    assert response.status_code == 200 and "&lt;script&gt;" in response.text
    assert "<script>alert('synthetic')</script>" not in response.text


def test_partial_schema_is_never_overwritten(client):
    trial.batches.create(client.engine)
    assert initialize(client).status_code == 409
    assert not inspect(client.engine).has_table("internal_trial_records")


def test_upload_limit_is_enforced_before_preview(client):
    response = client.post("/operations/internal-test/preview", data={
        "token": form_token("synthetic-admin", purpose="trial-preview")},
        files={"file": ("oversized.json", b"x" * (512 * 1024 + 1), "application/json")})
    assert response.status_code == 413


def test_status_is_admin_only_and_reports_separate_counts(client):
    initialize(client)
    content = encoded(); digest = trial.preview(content)["id"]
    trial.apply(client.engine, content, expected_digest=digest)
    response = client.get("/operations/internal-test/status")
    assert response.status_code == 200
    data = response.json()
    assert data["source_observations"] == {"listing": 1, "reservation": 1, "cleaning": 1}
    assert data["active_batches"] == 1 and not data["trial_started"]
    assert all(v == 0 for v in data["live_counts"].values())
    client.cookies.clear()
    assert client.get("/operations/internal-test/status").status_code == 403

