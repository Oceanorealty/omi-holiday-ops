"""
Lightweight, hand-written migrations — no Alembic yet. Each one is guarded
to be a no-op if already applied, so this is safe to run on every startup.
"""

from sqlalchemy import inspect, text


def run(engine) -> None:
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()

    if "bookings" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("bookings")}
        if "amount" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE bookings ADD COLUMN amount NUMERIC(10, 2)"))

    if "transactions" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("transactions")}
        if "imported_at" not in columns:
            # Pre-Phase-4 shape (amount was a string, no imported_at marker) and
            # the table has never had real rows written to it — safe to drop
            # and let create_all() rebuild it with the current schema.
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE transactions"))

    if "properties" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("properties")}
        new_columns = {
            "published": "BOOLEAN DEFAULT FALSE",
            "suburb": "VARCHAR",
            "photo_url": "VARCHAR",
            "description": "TEXT",
            "bedrooms": "INTEGER",
            "bathrooms": "INTEGER",
            "max_guests": "INTEGER",
            "listing_url": "VARCHAR",
        }
        with engine.begin() as conn:
            for name, ddl_type in new_columns.items():
                if name not in columns:
                    conn.execute(text(f"ALTER TABLE properties ADD COLUMN {name} {ddl_type}"))
