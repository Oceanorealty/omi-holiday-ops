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
            "parent_property_id": "INTEGER",
            "cleaner_pay_type": "VARCHAR",
            "cleaner_pay_rate": "NUMERIC(10, 2)",
            "owner_name": "VARCHAR",
            "owner_email": "VARCHAR",
            "commission_pct": "NUMERIC(5, 2)",
            "owner_portal_token": "VARCHAR",
            "pricing_mode": "VARCHAR DEFAULT 'manual'",
            "wifi_info": "TEXT",
            "check_in_instructions": "TEXT",
            "house_rules": "TEXT",
            "airbnb_review_url": "VARCHAR",
            "google_review_url": "VARCHAR",
        }
        with engine.begin() as conn:
            for name, ddl_type in new_columns.items():
                if name not in columns:
                    conn.execute(text(f"ALTER TABLE properties ADD COLUMN {name} {ddl_type}"))

    if "bookings" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("bookings")}
        new_columns = {
            "door_code": "VARCHAR",
            "guest_portal_token": "VARCHAR",
        }
        with engine.begin() as conn:
            for name, ddl_type in new_columns.items():
                if name not in columns:
                    conn.execute(text(f"ALTER TABLE bookings ADD COLUMN {name} {ddl_type}"))

    if "guests" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("guests")}
        if "language" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE guests ADD COLUMN language VARCHAR DEFAULT 'en'"))

    if "message_templates" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("message_templates")}
        new_columns = {"subject_zh": "VARCHAR", "body_zh": "TEXT"}
        with engine.begin() as conn:
            for name, ddl_type in new_columns.items():
                if name not in columns:
                    conn.execute(text(f"ALTER TABLE message_templates ADD COLUMN {name} {ddl_type}"))

    if "cleaning_tasks" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("cleaning_tasks")}
        new_columns = {
            "cleaner_id": "INTEGER",
            "quality_checked": "BOOLEAN DEFAULT FALSE",
            "quality_notes": "TEXT",
        }
        with engine.begin() as conn:
            for name, ddl_type in new_columns.items():
                if name not in columns:
                    conn.execute(text(f"ALTER TABLE cleaning_tasks ADD COLUMN {name} {ddl_type}"))

    if "transactions" in existing_tables:
        columns = {c["name"] for c in inspector.get_columns("transactions")}
        if "confirmed_received" not in columns:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE transactions ADD COLUMN confirmed_received BOOLEAN DEFAULT FALSE")
                )

    # Postgres backs SQLAlchemy Enum columns with a real native enum type,
    # which create_all() never alters once it exists — adding a Python enum
    # member (like TriggerEvent.guest_re_engagement) needs an explicit
    # ALTER TYPE here, or every insert using it fails with
    # "invalid input value for enum". SQLite has no such type, which is why
    # this only ever breaks in production. Adding a value is safe to run
    # every startup — IF NOT EXISTS makes it a no-op once applied.
    if engine.dialect.name == "postgresql" and "message_templates" in existing_tables:
        with engine.begin() as conn:
            conn.execute(text("ALTER TYPE triggerevent ADD VALUE IF NOT EXISTS 'guest_re_engagement'"))
