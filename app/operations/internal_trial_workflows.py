"""Isolated, synthetic workflow exercises linked to authentic source listings.

No application operational models, delivery clients, payment clients or channel
clients are imported. No real reservation, assignment or transaction is created.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import uuid

from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer, MetaData,
                        String, Table, Text, UniqueConstraint, inspect, select, text)
from app.operations import internal_trial as sources

metadata = MetaData()
scenarios = Table("internal_trial_scenarios", metadata,
    Column("id", String(32), primary_key=True),
    Column("source_key", String(64), nullable=False, index=True),
    Column("source_batch_id", String(64), nullable=False),
    Column("source_identity", String(300), nullable=False),
    Column("title_snapshot", String(240), nullable=False),
    Column("state", Text, nullable=False),
    Column("revision", Integer, nullable=False, default=0),
    Column("archived", Boolean, nullable=False, default=False),
    Column("create_digest", String(64), nullable=False, unique=True),
    Column("create_request_hash", String(64), nullable=False),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow))
events = Table("internal_trial_scenario_events", metadata,
    Column("id", Integer, primary_key=True),
    Column("scenario_id", String(32), ForeignKey(scenarios.c.id), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("action", String(40), nullable=False),
    Column("actor", String(100), nullable=False),
    Column("command_digest", String(64), nullable=False, unique=True),
    Column("before_state", Text, nullable=False),
    Column("after_state", Text, nullable=False),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow),
    UniqueConstraint("scenario_id", "revision"))

LABEL = "SYNTHETIC TEST — not a real booking"
ACTIONS = {"confirm", "check_in", "check_out", "cancel", "simulate_payment",
           "assign_cleaning", "complete_cleaning", "create_task", "complete_task",
           "preview_message", "archive"}


class SimulationError(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def ready(engine):
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    for table in metadata.sorted_tables:
        if table.name in existing:
            columns = {c["name"] for c in inspector.get_columns(table.name)}
            if columns != set(table.c.keys()):
                raise SimulationError("Existing simulation schema needs review; no changes made")
    return set(metadata.tables) <= existing


def prepare(engine):
    ready(engine)  # validate existing tables before any additive creation
    metadata.create_all(engine)


def listing_cards(engine):
    if not set(sources.metadata.tables) <= set(inspect(engine).get_table_names()):
        return []
    unique = {}
    with engine.connect() as conn:
        query = select(sources.records.c.identity, sources.records.c.batch_id,
                       sources.records.c.payload).join(sources.batches,
            sources.records.c.batch_id == sources.batches.c.id).where(
                sources.batches.c.state == "quarantined").order_by(sources.records.c.identity)
        for row in conn.execute(query).mappings():
            data = json.loads(row["payload"])
            if data["kind"] != "listing":
                continue
            key = digest(row["identity"])
            if key in unique:
                if unique[key]["payload"] != data:
                    unique[key]["source_conflict"] = True
                continue
            unique[key] = {"key": key, "batch_id": row["batch_id"],
                "identity": row["identity"], "payload": data,
                "fingerprint": digest(row["batch_id"] + row["payload"]),
                "source_conflict": False}
    return sorted(unique.values(), key=lambda item: item["payload"]["title"].casefold())


def _listing(engine, key, fingerprint=None):
    card = next((card for card in listing_cards(engine) if card["key"] == key), None)
    if not card or card["source_conflict"]:
        raise SimulationError("Source listing is missing, withdrawn or has conflicting snapshots")
    if fingerprint is not None and card["fingerprint"] != fingerprint:
        raise SimulationError("Source changed; reload the property card")
    return card


def _dates(check_in, check_out):
    try:
        start, end = date.fromisoformat(check_in), date.fromisoformat(check_out)
        if start.isoformat() != check_in or end.isoformat() != check_out or not 1 <= (end-start).days <= 90:
            raise ValueError
        return start, end
    except (ValueError, TypeError):
        raise SimulationError("Use valid simulation dates spanning 1 to 90 nights") from None


def create(engine, *, source_key, fingerprint, check_in, check_out, nightly_rate,
           command_digest, actor):
    card = _listing(engine, source_key, fingerprint)
    start, end = _dates(check_in, check_out)
    try:
        rate = Decimal(nightly_rate)
        if not rate.is_finite() or not Decimal("0") < rate <= Decimal("100000"):
            raise ValueError
        rate = rate.quantize(Decimal("0.01"))
    except (ValueError, InvalidOperation, TypeError):
        raise SimulationError("Use a positive simulated nightly rate, at most 100000") from None
    state = {"label": LABEL, "guest": "SYNTHETIC TEST GUEST", "reservation_status": "draft",
        "check_in": start.isoformat(), "check_out": end.isoformat(), "currency": "AUD",
        "nightly_rate": str(rate), "simulated_total": str(rate * (end-start).days),
        "payment_state": "not_collected", "cleaning_status": "not_due",
        "cleaner": "Unassigned simulation", "task_status": "none", "message_preview": ""}
    scenario_id = uuid.uuid4().hex
    request_hash = digest(card["fingerprint"] + encode(state))
    with engine.begin() as conn:
        _lock(conn, source_key)
        existing = conn.execute(select(scenarios.c.id, scenarios.c.create_request_hash).where(
            scenarios.c.create_digest == command_digest)).mappings().first()
        if existing:
            if existing["create_request_hash"] != request_hash:
                raise SimulationError("This form was used with different simulation details; reload")
            return existing["id"]
        conn.execute(scenarios.insert().values(id=scenario_id, source_key=source_key,
            source_batch_id=card["batch_id"], source_identity=card["identity"],
            title_snapshot=card["payload"]["title"], state=encode(state), revision=0,
            archived=False, create_digest=command_digest, create_request_hash=request_hash))
        conn.execute(events.insert().values(scenario_id=scenario_id, revision=0,
            action="create", actor=actor[:100], command_digest=command_digest,
            before_state="{}", after_state=encode(state)))
    return scenario_id


def _lock(conn, key):
    if conn.dialect.name == "postgresql":
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(key[:15], 16)})
    elif conn.dialect.name == "sqlite":
        conn.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        raise SimulationError("Simulation writes require verified PostgreSQL or SQLite locking")


def _conflict(conn, row, state):
    others = conn.execute(select(scenarios.c.id, scenarios.c.state).where(
        scenarios.c.source_key == row["source_key"], scenarios.c.archived.is_(False),
        scenarios.c.id != row["id"])).mappings()
    for other in others:
        booking = json.loads(other["state"])
        if booking["reservation_status"] in {"confirmed", "checked_in", "checked_out"} and (
                state["check_in"] < booking["check_out"] and state["check_out"] > booking["check_in"]):
            return True
    return False


def available_actions(state):
    status = state["reservation_status"]
    allowed = {
        "confirm": status == "draft", "check_in": status == "confirmed",
        "check_out": status == "checked_in", "cancel": status in {"draft", "confirmed"},
        "simulate_payment": status == "confirmed" and state["payment_state"] == "not_collected",
        "assign_cleaning": state["cleaning_status"] in {"planned", "pending"},
        "complete_cleaning": status == "checked_out" and state["cleaning_status"] in {"pending", "assigned"},
        "create_task": status != "cancelled" and state["task_status"] == "none",
        "complete_task": state["task_status"] == "open",
        "preview_message": status in {"confirmed", "checked_in", "checked_out"}, "archive": True}
    return [action for action, enabled in allowed.items() if enabled]


def apply_action(engine, *, scenario_id, action, revision, command_digest, actor):
    if action not in ACTIONS:
        raise SimulationError("Unknown simulation action")
    with engine.connect() as lookup:
        key = lookup.execute(select(scenarios.c.source_key).where(
            scenarios.c.id == scenario_id)).scalar_one_or_none()
    if not key:
        raise SimulationError("Simulation not found")
    if action != "archive":
        _listing(engine, key)
    with engine.begin() as conn:
        _lock(conn, key)
        previous = conn.execute(select(events.c.scenario_id, events.c.action).where(
            events.c.command_digest == command_digest)).mappings().first()
        if previous:
            if previous["scenario_id"] != scenario_id or previous["action"] != action:
                raise SimulationError("Command identity does not match this action")
            return {"already_applied": True}
        row = conn.execute(select(scenarios).where(scenarios.c.id == scenario_id)).mappings().one()
        if row["revision"] != revision or row["archived"]:
            raise SimulationError("Simulation changed or was archived; reload before continuing")
        state = json.loads(row["state"]); before = encode(state)
        status = state["reservation_status"]
        if action not in available_actions(state):
            raise SimulationError("Action is unavailable in the current simulation state")
        if action == "confirm":
            if _conflict(conn, row, state):
                raise SimulationError("Simulation dates overlap another confirmed test booking")
            state.update(reservation_status="confirmed", cleaning_status="planned")
        elif action == "check_in": state["reservation_status"] = "checked_in"
        elif action == "check_out":
            state["reservation_status"] = "checked_out"
            if state["cleaning_status"] == "planned": state["cleaning_status"] = "pending"
        elif action == "cancel":
            state.update(reservation_status="cancelled", cleaning_status="cancelled")
            if state["payment_state"] == "simulated_paid": state["payment_state"] = "simulated_refund"
        elif action == "simulate_payment": state["payment_state"] = "simulated_paid"
        elif action == "assign_cleaning":
            state.update(cleaning_status="assigned", cleaner="SIMULATED CLEANER — no real staff assigned")
        elif action == "complete_cleaning": state["cleaning_status"] = "done"
        elif action == "create_task": state["task_status"] = "open"
        elif action == "complete_task": state["task_status"] = "done"
        elif action == "preview_message":
            state["message_preview"] = ("SIMULATION ONLY — no message sent. " + row["title_snapshot"] +
                "; SYNTHETIC TEST GUEST; " + state["check_in"] + " to " + state["check_out"])
        result = conn.execute(scenarios.update().where(scenarios.c.id == scenario_id,
            scenarios.c.revision == revision).values(state=encode(state), revision=revision+1,
                                                    archived=(action == "archive")))
        if result.rowcount != 1:
            raise SimulationError("Concurrent simulation update; reload")
        conn.execute(events.insert().values(scenario_id=scenario_id, revision=revision+1,
            action=action, actor=actor[:100], command_digest=command_digest,
            before_state=before, after_state=encode(state)))
    return {"already_applied": False}


def read(engine, *, include_archived=False):
    if not ready(engine):
        return []
    with engine.connect() as conn:
        query = select(scenarios).order_by(scenarios.c.created_at.desc(), scenarios.c.id)
        if not include_archived: query = query.where(scenarios.c.archived.is_(False))
        return [{**dict(row), "data": json.loads(row["state"])}
                for row in conn.execute(query.limit(500)).mappings()]


def history(engine, scenario_id):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(events.c.action, events.c.revision,
            events.c.actor, events.c.created_at).where(events.c.scenario_id == scenario_id)
            .order_by(events.c.revision)).mappings()]


def calendar(engine, cards, start, days=14):
    from datetime import timedelta
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    active = read(engine)
    rows = []
    for card in cards:
        reservations = [r for r in active if r["source_key"] == card["key"] and
            r["data"]["reservation_status"] in {"confirmed", "checked_in", "checked_out"}]
        rows.append({"title": card["payload"]["title"], "cells": [any(
            r["data"]["check_in"] <= day < r["data"]["check_out"] for r in reservations) for day in dates]})
    return {"dates": dates, "rows": rows}
