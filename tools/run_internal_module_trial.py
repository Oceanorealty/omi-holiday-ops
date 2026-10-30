"""Run the original OPS app locally against a NEW, guarded trial DB.

No production credentials/session/DB is copied. Source JSON stays local.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys


def prepare(root, source, *, resume=False):
    root=Path(root).resolve();source=Path(source).resolve()
    db=root/'ops-module-trial.sqlite'
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    if resume:
        marker=json.loads((root/'trial-manifest.json').read_text())
        if marker.get('source_sha256') != digest or not db.is_file():
            raise RuntimeError('Resume requires the exact previously used source and trial database')
    else:
        root.mkdir(mode=0o700,parents=True,exist_ok=False)
        (root/'trial-manifest.json').write_text(json.dumps({'scope':'local-original-module-trial','database':str(db),
            'source_sha256':digest,'physical_units_verified':False,'synthetic_bookings_only':True},indent=2))
    # Explicit allow-list; do not inherit actual DB URLs, service secrets or proxy credentials.
    env={'PATH':os.defpath,'LANG':'C','LC_ALL':'C','DATABASE_URL':'sqlite:///'+str(db),
        'OPS_INTERNAL_MODULE_TRIAL':'1','OPS_MODULE_TRIAL_ROOT':str(root),'OPS_SCHEDULER_ENABLED':'0',
        'OPS_MESSAGE_DISPATCH_ENABLED':'0','OPS_OWNER_EMAIL_ENABLED':'0','PUBLIC_BOOKING_ENABLED':'',
        'ADMIN_USER':'module-test','ADMIN_PASSWORD':'synthetic-local-test-only',
        'SECRET_KEY':secrets.token_hex(32),'PYTHONDONTWRITEBYTECODE':'1'}
    os.environ.clear();os.environ.update(env)
    from app.internal_module_trial import activate,import_source_properties
    activate()
    from app.main import app
    from app.db import engine
    added=import_source_properties(engine,source.read_bytes())
    os.chmod(db,0o600)
    return app,engine,added


def bootstrap_sql(database):
    """Reviewable non-secret DDL; run by owner only after explicit approval.

    Creates new objects only. Existing role/schema is an error, never overwritten.
    Password and LOGIN are intentionally excluded: owner sets them securely.
    """
    if not database.replace('_','').isalnum(): raise ValueError('Invalid database identifier')
    return f"""BEGIN;
CREATE ROLE omi_ops_trial_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT CONNECTION LIMIT 2;
CREATE SCHEMA omi_ops_module_trial_v1 AUTHORIZATION omi_ops_trial_app;
GRANT CONNECT ON DATABASE "{database}" TO omi_ops_trial_app;
ALTER ROLE omi_ops_trial_app IN DATABASE "{database}" SET search_path TO omi_ops_module_trial_v1;
ALTER ROLE omi_ops_trial_app SET statement_timeout TO '3s';
ALTER ROLE omi_ops_trial_app SET lock_timeout TO '500ms';
ALTER ROLE omi_ops_trial_app SET idle_in_transaction_session_timeout TO '5s';
COMMENT ON ROLE omi_ops_trial_app IS 'OMI isolated internal trial only; no B5/public table grants; expires with free DB';
COMMIT;
"""


def prepare_postgres(source=None):
    """Own role only; no production credentials, root secrets or local trial login."""
    allowed=('DATABASE_URL','OPS_TRIAL_PG_HOST','OPS_TRIAL_PG_DATABASE','OPS_TRIAL_PG_LOCAL_CHECK',
             'ADMIN_USER','ADMIN_PASSWORD','SECRET_KEY','RENDER_SERVICE_NAME','PORT','OPS_MODULE_TRIAL_HOSTED')
    env={k:os.environ[k] for k in allowed if k in os.environ}
    env.update(PATH=os.defpath,LANG='C',LC_ALL='C',OPS_INTERNAL_MODULE_TRIAL='1',OPS_MODULE_TRIAL_PG='1',
               OPS_SCHEDULER_ENABLED='0',OPS_MESSAGE_DISPATCH_ENABLED='0',OPS_OWNER_EMAIL_ENABLED='0',
               PUBLIC_BOOKING_ENABLED='',PYTHONDONTWRITEBYTECODE='1')
    if any(not env.get(k) for k in ('DATABASE_URL','OPS_TRIAL_PG_HOST','OPS_TRIAL_PG_DATABASE','ADMIN_USER','ADMIN_PASSWORD','SECRET_KEY')):
        raise RuntimeError('Secure dedicated database and login configuration is required')
    if len(env['SECRET_KEY'])<32 or len(env['ADMIN_PASSWORD'])<16:
        raise RuntimeError('Use an independent strong trial login and signing key')
    if env.get('OPS_MODULE_TRIAL_HOSTED')=='1' and env['ADMIN_PASSWORD'] in {'synthetic-local-test-only','synthetic-only-test-password','synthetic-preview-only'}:
        raise RuntimeError('Hosted mode refuses documented local synthetic credentials')
    os.environ.clear();os.environ.update(env)
    from app.internal_module_trial import activate,import_source_properties
    activate()
    from app.main import app
    from app.db import engine
    added=import_source_properties(engine,Path(source).read_bytes()) if source else []
    return app,engine,added


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source');parser.add_argument('--root');parser.add_argument('--port',type=int,default=0);parser.add_argument('--resume',action='store_true');parser.add_argument('--hosted',action='store_true')
    args=parser.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    if args.hosted:
        os.environ['OPS_MODULE_TRIAL_HOSTED']='1'
        app,engine,added=prepare_postgres()
        import uvicorn
        uvicorn.run(app,host='0.0.0.0',port=int(os.environ['PORT']),access_log=False)
        sys.exit(0)
    if not args.root or not args.source: parser.error('Local mode requires --root and --source')
    app,engine,added=prepare(args.root,args.source,resume=args.resume)
    print(f'LOCAL ORIGINAL-MODULE TRIAL: imported {len(added)} source listing properties; one synthetic test slot each, not verified physical apartments',flush=True)
    print('Loopback login: module-test / synthetic-local-test-only (synthetic local credential)',flush=True)
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=args.port)
