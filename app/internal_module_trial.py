"""Opt-in, loopback-only safety boundary for testing ORIGINAL OPS modules.

Inactive in normal deployments. The dedicated launcher supplies a new SQLite DB
and an explicit marker; no production environment or data is copied.
"""
import hashlib
import json
import os
from pathlib import Path
import sys

ACTIVE = False
TRIAL_ROOT = None


def activate():
    global ACTIVE, TRIAL_ROOT
    if os.environ.get('OPS_INTERNAL_MODULE_TRIAL') != '1': return False
    if ACTIVE: return True
    pg_mode = os.environ.get('OPS_MODULE_TRIAL_PG') == '1'
    root = Path(os.environ.get('OPS_MODULE_TRIAL_ROOT','')).resolve()
    db = root/'ops-module-trial.sqlite'
    if pg_mode:
        configure_postgres_guard()
    else:
        marker = root/'trial-manifest.json'
        if not marker.is_file():
            raise RuntimeError('Internal module trial requires its dedicated manifest')
        manifest = json.loads(marker.read_text())
        if db.is_symlink() or manifest.get('scope') != 'local-original-module-trial' or manifest.get('database') != str(db):
            raise RuntimeError('Unrecognised isolated trial database')
        if os.environ.get('DATABASE_URL') != 'sqlite:///'+str(db):
            raise RuntimeError('Trial refuses any other DATABASE_URL, including PostgreSQL')
    sensitive = ('SMTP_', 'STRIPE_', 'HOSTAWAY_', 'BOOKING_COM_', 'EXPEDIA_')
    if any(value for key,value in os.environ.items() if key.startswith(sensitive)):
        raise RuntimeError('Trial refuses external service credentials/configuration')
    if not os.environ.get('SECRET_KEY') or not os.environ.get('ADMIN_PASSWORD'):
        raise RuntimeError('Trial requires its own login')
    for key in ('OPS_SCHEDULER_ENABLED','OPS_MESSAGE_DISPATCH_ENABLED','OPS_OWNER_EMAIL_ENABLED'):
        os.environ[key] = '0'
    os.environ['PUBLIC_BOOKING_ENABLED'] = ''
    TRIAL_ROOT = root
    def deny_egress(event, args):
        if event in {'socket.connect','socket.connect_ex','socket.sendto','socket.sendmsg','socket.getaddrinfo',
                     'subprocess.Popen','os.system','os.exec','os.posix_spawn'}:
            raise PermissionError('Internal module trial blocks network egress and child processes')
        if event == 'sqlite3.connect':
            target = str(args[0])
            if target.startswith('file:') or Path(target).resolve() != db or Path(target).is_symlink():
                raise PermissionError('Internal module trial refuses every other SQLite database')
        if event == 'socket.bind':
            address = args[1]
            allowed_host = '0.0.0.0' if os.environ.get('OPS_MODULE_TRIAL_HOSTED') == '1' else '127.0.0.1'
            if not isinstance(address,tuple) or address[0] != allowed_host or (allowed_host == '0.0.0.0' and address[1] != int(os.environ['PORT'])):
                raise PermissionError('Internal module trial refuses an unapproved listener')
    sys.addaudithook(deny_egress)
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    @event.listens_for(Session,'before_flush')
    def check_local_records(session, flush_context, instances):
        from app.models import Property, Booking, IcalFeed, Guest
        for row in set(session.new).union(session.dirty):
            if isinstance(row,Property) and row.published:
                raise ValueError('Trial properties cannot be published')
            if isinstance(row,Booking) and (row.notifications_enabled or row.marketing_consent):
                raise ValueError('Trial reservations cannot enable notifications')
            if isinstance(row,IcalFeed):
                raise ValueError('Trial cannot store live calendar feeds')
            if isinstance(row,Guest) and row.email:
                raise ValueError('Trial guests must have no contact email')
    ACTIVE = True
    return True


class TrialBoundary:
    """Allow original internal routes, deny external portals/publication/sync."""
    def __init__(self, app): self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type'] != 'http':
            return await self.app(scope,receive,send)
        path = scope['path']; method = scope['method']
        from starlette.responses import PlainTextResponse
        prefixes=('/api/public/','/owner/','/cleaner/','/guest/','/ical/','/operations/integrations',
                  '/integrations/','/operations/internal-test')
        unsafe = path.startswith(prefixes) or path == '/sync'
        if method not in {'GET','HEAD'} and (path.endswith(('/listing','/feeds','/guest-info')) or
                any(word in path for word in ('checkout','webhook','refund','send','notify','credentials'))):
            unsafe=True
        if unsafe:
            return await PlainTextResponse('Blocked: original-module INTERNAL TEST; external action disabled',403)(scope,receive,send)
        scope.setdefault('state',{})['internal_module_trial']=True
        return await self.app(scope,receive,send)


def import_source_properties(engine, content):
    from sqlalchemy.engine import make_url
    if not ACTIVE or engine.url != make_url(os.environ['DATABASE_URL']):
        raise RuntimeError('Import permitted only inside the guarded trial database')
    from app.operations.internal_trial import preview
    from app.models import Property
    from sqlalchemy import MetaData,Table,Column,String,Integer,select
    from sqlalchemy.orm import Session
    rows=json.loads(preview(content)['manifest'])
    if not rows or any(r['kind']!='listing' for r in rows):
        raise ValueError('Supply verified basic listing observations only')
    meta=MetaData();mapping=Table('internal_module_trial_sources',meta,
        Column('identity',String(64),primary_key=True),Column('property_id',Integer,nullable=False,unique=True),
        Column('payload',String,nullable=False),Column('payload_sha256',String(64),nullable=False))
    meta.create_all(engine)
    added=[]
    with Session(engine) as db, db.begin():
        for row in rows:
            payload=json.dumps(row,sort_keys=True,ensure_ascii=False)
            digest=hashlib.sha256(payload.encode()).hexdigest()
            identity=hashlib.sha256(json.dumps([row['provider'],row['source_account'],row['source_id']]).encode()).hexdigest()
            existing=db.execute(select(mapping).where(mapping.c.identity==identity)).mappings().first()
            if existing:
                if existing['payload_sha256'] != digest: raise ValueError('Source changed; explicit reconciliation required')
                if db.get(Property,existing['property_id']) is None:raise ValueError('Trial source mapping is inconsistent')
                continue
            prop=Property(name=row['title'],address=None,published=False,
                description='INTERNAL TEST: source listing only; physical unit/address/capacity unverified. One synthetic test slot; not real inventory.')
            db.add(prop);db.flush()
            db.execute(mapping.insert().values(identity=identity,property_id=prop.id,payload=payload,payload_sha256=digest))
            added.append(prop.id)
    return added


PG_ROLE = 'omi_ops_trial_app'
PG_SCHEMA = 'omi_ops_module_trial_v1'


def configure_postgres_guard():
    """Two bounded connections, one explicit DB identity, schema-only permissions. No owner DSN."""
    import sqlalchemy
    from sqlalchemy.engine import make_url
    from sqlalchemy import event
    expected = make_url(os.environ.get('DATABASE_URL',''))
    host = os.environ.get('OPS_TRIAL_PG_HOST','')
    database = os.environ.get('OPS_TRIAL_PG_DATABASE','')
    if (expected.drivername not in {'postgresql','postgresql+psycopg2'} or not host or not database or
        expected.host != host or expected.database != database or expected.username != PG_ROLE or not expected.password):
        raise RuntimeError('Trial requires the explicitly approved PostgreSQL host/database and dedicated role')
    local = host == '127.0.0.1' and os.environ.get('OPS_TRIAL_PG_LOCAL_CHECK') == '1'
    if not local and expected.query.get('sslmode') != 'require':
        raise RuntimeError('Hosted trial requires TLS to its dedicated PostgreSQL role')
    if os.environ.get('OPS_MODULE_TRIAL_HOSTED') == '1':
        if os.environ.get('RENDER_SERVICE_NAME') != 'omi-holiday-ops-internal-test' or local:
            raise RuntimeError('Hosted trial refuses an unexpected service identity')
    original = sqlalchemy.create_engine
    def guarded_create_engine(url, **kwargs):
        if make_url(url) != expected:
            raise PermissionError('Trial refuses every other SQLAlchemy database target')
        args = dict(kwargs.pop('connect_args',{}))
        args.update(options=f'-csearch_path={PG_SCHEMA} -cstatement_timeout=3000 -clock_timeout=500 -cidle_in_transaction_session_timeout=5000',connect_timeout=5)
        kwargs.update(connect_args=args,pool_size=2,max_overflow=0,pool_timeout=3)
        engine = original(url,**kwargs)
        @event.listens_for(engine,'connect')
        def verify_connection(conn, record):
            with conn.cursor() as cursor:
                cursor.execute('SELECT current_user,current_database(),current_schema()')
                if cursor.fetchone() != (PG_ROLE,database,PG_SCHEMA):
                    raise PermissionError('Trial PostgreSQL identity/schema check failed')
                cursor.execute("SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,rolconnlimit FROM pg_roles WHERE rolname=current_user")
                row=cursor.fetchone()
                if row is None or any(row[:5]) or row[5] != 2:
                    raise PermissionError('Trial role is more privileged than approved')
                cursor.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema',%s) AND n.nspname NOT LIKE 'pg_toast%%' AND n.nspname NOT LIKE 'pg_temp%%' AND c.relkind IN ('r','p','v','m','f') AND has_table_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')",(PG_SCHEMA,))
                if cursor.fetchone()[0]:
                    raise PermissionError('Trial role can access non-trial data; refusing startup')
                cursor.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema',%s) AND c.relkind='S' AND has_sequence_privilege(current_user,c.oid,'USAGE,SELECT,UPDATE')",(PG_SCHEMA,))
                if cursor.fetchone()[0]:
                    raise PermissionError('Trial role can access non-trial sequences; refusing startup')
                cursor.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog',%s) AND has_function_privilege(current_user,p.oid,'EXECUTE')",(PG_SCHEMA,))
                if cursor.fetchone()[0]:
                    raise PermissionError('Non-trial security-definer function exposure; refusing startup')
            conn.rollback()
        return engine
    sqlalchemy.create_engine = guarded_create_engine
