"""Explicit, fail-closed removal of empty internal fixture properties.

Uses SQL Core deletes, never ORM cascades. Historical command/review records
remain intact. No credentials, guest content or property access tokens appear
in the review payload.
"""
import hashlib
import json
from decimal import Decimal
from sqlalchemy import MetaData, Table, select, func, delete, inspect, text


class CleanupError(ValueError):
    pass


def parse_ids(value):
    try:
        ids = sorted({int(x.strip()) for x in value.split(',')})
        if not 1 <= len(ids) <= 5 or min(ids) < 1:
            raise ValueError()
        return ids
    except (ValueError, TypeError, AttributeError):
        raise CleanupError('Choose one to five exact property IDs') from None


def _tables(conn):
    inspector = inspect(conn)
    if conn.dialect.name == 'postgresql':
        # Include referencing tables in other schemas, not just search_path.
        rows = conn.execute(text("""SELECT DISTINCT ns.nspname, cl.relname
          FROM pg_constraint fk JOIN pg_class cl ON cl.oid=fk.conrelid
          JOIN pg_namespace ns ON ns.oid=cl.relnamespace
          WHERE fk.contype='f' AND fk.confrelid IN
          (to_regclass('properties'),to_regclass('owner_statements'))""")).all()
        keys = {(schema, name) for schema, name in rows}
        keys.update((inspector.default_schema_name, n) for n in inspector.get_table_names())
    else:
        keys = {(None, n) for n in inspector.get_table_names()}
    metadata = MetaData()
    options = {'postgresql_ignore_search_path':True} if conn.dialect.name == 'postgresql' else {}
    tables = [Table(n, metadata, schema=s, autoload_with=conn, resolve_fks=False, **options) for s, n in sorted(keys, key=str)]
    default = inspector.default_schema_name if conn.dialect.name == 'postgresql' else None
    by_name = {t.name: t for t in tables if t.schema == default}
    if 'properties' not in by_name:
        raise CleanupError('Property table unavailable')
    return tables, by_name


def _count(conn, table, condition):
    return conn.execute(select(func.count()).select_from(table).where(condition)).scalar_one()


def _target(fk, table):
    parts=fk.target_fullname.split('.')
    return (parts[-2],'.'.join(parts[:-2]) if len(parts)>2 else table.schema)


def preview(conn, ids):
    tables, by_name = _tables(conn)
    prop = by_name['properties']
    selected = conn.execute(select(prop.c.id, prop.c.name, prop.c.published).where(prop.c.id.in_(ids)).order_by(prop.c.id)).mappings().all()
    if [r['id'] for r in selected] != ids:
        raise CleanupError('A selected property is missing; refresh the exact targets')
    blockers = []
    if conn.dialect.name == 'postgresql':
        trigger_count = conn.execute(text("""SELECT count(*) FROM pg_trigger
            WHERE NOT tgisinternal AND tgenabled <> 'D'
            AND tgrelid IN (to_regclass('properties'),to_regclass('owner_statements'))""")).scalar_one()
        if trigger_count:
            blockers.append('Custom database triggers require separate review before deletion')
    if any(r['published'] for r in selected):
        blockers.append('A selected property is published')
    refs = []
    statements = []
    statement_table = by_name.get('owner_statements')
    if statement_table is not None:
        amounts = ('gross_revenue','total_expenses','commission_amount','adjustment_amount','net_payout')
        required = ('id','property_id','period','finalized','sent_at','missing_amount_count') + amounts
        if not all(k in statement_table.c for k in required):
            raise CleanupError('Unexpected statement schema')
        rows = conn.execute(select(*(statement_table.c[k] for k in required)).where(statement_table.c.property_id.in_(ids)).order_by(statement_table.c.id)).mappings().all()
        for row in rows:
            zero = all(Decimal(str(row[k] or 0)) == 0 for k in amounts)
            eligible = zero and not row['finalized'] and row['sent_at'] is None and row['missing_amount_count'] == 0
            statements.append({'id':row['id'],'property_id':row['property_id'],'period':row['period'],'all_amounts_zero':zero,'eligible':eligible})
        if len(statements) > 1 or any(not r['eligible'] for r in statements):
            blockers.append('Only one unfinalized, unsent, entirely zero statement may be removed')
    statement_ids = [r['id'] for r in statements]
    for table in tables:
        has_property_fk = any(_target(fk,table) == ('properties',prop.schema) for fk in table.foreign_keys)
        if table.name != 'properties' and 'property_id' in table.c and not has_property_fk:
            count = _count(conn,table,table.c.property_id.in_(ids))
            refs.append({'reference':table.fullname+'.property_id (no foreign key)','count':count})
            if count: blockers.append(f'{table.fullname}: {count} property references without a foreign key')
        for fk in table.foreign_keys:
            target = _target(fk,table)
            if target == ('properties',prop.schema):
                count = _count(conn, table, fk.parent.in_(ids))
                label = f'{table.fullname}.{fk.parent.name}'
                refs.append({'reference':label,'count':count})
                if count and table is not statement_table:
                    blockers.append(f'{label}: {count} dependent records')
            elif statement_table is not None and target == ('owner_statements',statement_table.schema) and statement_ids:
                count = _count(conn, table, fk.parent.in_(statement_ids))
                label = f'{table.fullname}.{fk.parent.name}'
                refs.append({'reference':label,'count':count})
                if count:
                    blockers.append(f'{label}: {count} statement dependencies')
    # These record references intentionally have no SQL foreign key.
    if 'custom_field_changes' in by_name:
        t = by_name['custom_field_changes']
        count = _count(conn,t,(t.c.scope=='property') & t.c.entity_id.in_(ids))
        refs.append({'reference':'custom_field_changes.property','count':count})
        if count: blockers.append('Property custom-field history exists')
    if 'custom_field_values' in by_name:
        if 'custom_field_definitions' not in by_name: raise CleanupError('Custom-field schema unavailable')
        v,d = by_name['custom_field_values'],by_name['custom_field_definitions']
        count = conn.execute(select(func.count()).select_from(v.join(d,v.c.field_id==d.c.id)).where(d.c.scope=='property',v.c.entity_id.in_(ids))).scalar_one()
        refs.append({'reference':'custom_field_values.property','count':count})
        if count: blockers.append('Property custom-field values exist')
    result = {'properties':[dict(r) for r in selected],'statements':statements,'references':sorted(refs,key=lambda r:r['reference']),'blockers':sorted(set(blockers))}
    result['digest'] = hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return result


def remove(conn, ids, expected_digest, approved_statement_ids):
    if conn.dialect.name == 'postgresql':
        conn.execute(text("SET LOCAL lock_timeout = '500ms'"))
        conn.execute(text("SET LOCAL statement_timeout = '5s'"))
        tables,_ = _tables(conn)
        # Prevent new references being inserted after the final check. A busy
        # operational table causes a short timeout and a full rollback.
        preparer = conn.dialect.identifier_preparer
        relevant = [t for t in tables if t.name in ('properties','owner_statements','custom_field_values','custom_field_changes','custom_field_definitions')
                    or 'property_id' in t.c or any(_target(fk,t)[0] in ('properties','owner_statements') for fk in t.foreign_keys)]
        for table in sorted(relevant,key=lambda t:t.fullname):
            conn.execute(text('LOCK TABLE '+preparer.format_table(table)+' IN SHARE ROW EXCLUSIVE MODE'))
    current = preview(conn,ids)
    if current['digest'] != expected_digest or current['blockers']:
        raise CleanupError('Records or dependencies changed, or deletion is blocked; review again')
    actual = sorted(r['id'] for r in current['statements'])
    if actual != sorted(approved_statement_ids):
        raise CleanupError('Statement selection does not match the reviewed record')
    _,by_name = _tables(conn)
    if actual:
        result = conn.execute(delete(by_name['owner_statements']).where(by_name['owner_statements'].c.id.in_(actual)))
        if result.rowcount != len(actual): raise CleanupError('Statement changed')
    result = conn.execute(delete(by_name['properties']).where(by_name['properties'].c.id.in_(ids)))
    if result.rowcount != len(ids): raise CleanupError('Property changed')
    return current
