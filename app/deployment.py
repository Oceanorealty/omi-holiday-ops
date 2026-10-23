"""One-time, same-database rollback snapshot before this additive release.

This is not an off-site disaster-recovery backup. Never logs row contents or URLs.
"""
from sqlalchemy import inspect, text

SNAPSHOT_SCHEMA = 'ops_backup_20261021'


def ensure_release_snapshot(engine, snapshot_schema=SNAPSHOT_SCHEMA):
    if engine.dialect.name != 'postgresql':
        return {'state':'not_postgresql','tables':0}
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
        with connection.begin():
            return _snapshot(connection, engine, snapshot_schema)


def _snapshot(conn, engine, snapshot_schema=SNAPSHOT_SCHEMA):
    conn.execute(text('SELECT pg_advisory_xact_lock(20261021, 1)'))
    source = conn.execute(text('SELECT current_schema()')).scalar_one()
    names = inspect(conn).get_table_names(schema=source)
    if not names:
        return {'state':'empty_database','tables':0}
    quote = engine.dialect.identifier_preparer.quote
    # The snapshot belongs to the deployment role; no grants or external exports.
    conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS {quote(snapshot_schema)}'))
    marker = f'{quote(snapshot_schema)}.release_manifest'
    conn.execute(text(f'CREATE TABLE IF NOT EXISTS {marker} (source_schema TEXT PRIMARY KEY, tables_count INTEGER NOT NULL, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)'))
    previous = conn.execute(text(f'SELECT tables_count FROM {marker} WHERE source_schema=:source'),{'source':source}).scalar_one_or_none()
    if previous is not None:
        return {'state':'already_saved','tables':previous}
    # Production uses public. Test schemas use unique prefixes to avoid collision.
    for name in names:
        target = name if source == 'public' else source + '__' + name
        if len(target.encode())>63:
            raise RuntimeError('Snapshot identifier too long')
        conn.execute(text(f'CREATE TABLE {quote(snapshot_schema)}.{quote(target)} AS TABLE {quote(source)}.{quote(name)}'))
    conn.execute(text(f'INSERT INTO {marker}(source_schema,tables_count) VALUES (:source,:count)'),{'source':source,'count':len(names)})
    return {'state':'saved','tables':len(names)}
