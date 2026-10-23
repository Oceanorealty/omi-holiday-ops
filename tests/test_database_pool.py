"""Simulate a server-closed pooled connection; no production connection is used."""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError


def engine_from_module(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "test.db"))
    spec = importlib.util.spec_from_file_location("pool_test_db", Path(__file__).parents[1] / "app/db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.engine


def prime_and_disconnect(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE synthetic (value INTEGER)"))
        connection.execute(text("INSERT INTO synthetic VALUES (42)"))
    # Return it to the pool, then simulate the database closing the socket.
    pooled = engine.raw_connection()
    driver = pooled.driver_connection
    pooled.close()
    driver.close()


def test_without_pre_ping_reproduces_stale_connection_failure(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "old.db"))
    try:
        prime_and_disconnect(engine)
        with pytest.raises(DBAPIError):
            with engine.connect() as connection:
                connection.execute(text("SELECT value FROM synthetic"))
    finally:
        engine.dispose()


def test_application_reconnects_before_first_business_query(monkeypatch, tmp_path):
    engine = engine_from_module(monkeypatch, tmp_path)
    try:
        prime_and_disconnect(engine)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT value FROM synthetic")).scalar_one() == 42
    finally:
        engine.dispose()
