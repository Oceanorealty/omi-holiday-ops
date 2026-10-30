"""Subprocess-isolated guard/actual-module test; source fixtures are synthetic."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_original_routes_and_fail_closed_guards_in_disposable_database(tmp_path):
    source=tmp_path/'synthetic.json'
    source.write_text(json.dumps([{'kind':'listing','provider':'airbnb','source_account':'synthetic@example.invalid',
        'source_id':'synthetic-source-1','title':'Synthetic source property','suburb':'Synthetic test region',
        'provenance':'Synthetic source fixture, no actual property/account data'}]))
    target=tmp_path/'trial'
    result=subprocess.run([sys.executable,'tools/check_internal_module_trial.py','--source',str(source),'--root',str(target)],
        cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=45,
        env={'PATH':os.defpath,'PYTHONDONTWRITEBYTECODE':'1'})
    assert result.returncode==0,result.stdout+result.stderr
    report=json.loads((target/'verification.json').read_text())
    assert report['counts']['properties']==report['counts']['bookings']==1
    assert report['counts']['message_logs']==report['counts']['published_properties']==0
    assert report['counts']['cleaning_tasks_done']==report['counts']['work_orders_done']==1
    assert not any(row['status']=='failed' for row in report['matrix'])


def test_guard_refuses_non_trial_database_before_app_import(tmp_path):
    marker=tmp_path/'trial-manifest.json'
    marker.write_text(json.dumps({'scope':'local-original-module-trial','database':str(tmp_path/'ops-module-trial.sqlite')}))
    env={'PATH':os.defpath,'PYTHONDONTWRITEBYTECODE':'1','OPS_INTERNAL_MODULE_TRIAL':'1',
         'OPS_MODULE_TRIAL_ROOT':str(tmp_path),'DATABASE_URL':'postgresql://synthetic.invalid/not-allowed',
         'ADMIN_PASSWORD':'synthetic-only','SECRET_KEY':'synthetic-only'}
    code='from app.internal_module_trial import activate; activate()'
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,env=env)
    assert result.returncode!=0 and 'refuses any other DATABASE_URL' in result.stderr
    assert not (tmp_path/'ops-module-trial.sqlite').exists()


def test_bootstrap_scope_is_explicit_without_credentials_or_b5_mutations():
    from tools.run_internal_module_trial import bootstrap_sql
    script=bootstrap_sql('synthetic_b5')
    assert 'NOLOGIN' in script and 'NOINHERIT' in script and 'CONNECTION LIMIT 2' in script
    assert 'CREATE SCHEMA omi_ops_module_trial_v1' in script
    assert 'public.' not in script and 'PASSWORD' not in script and 'DROP' not in script
    assert 'GRANT CONNECT ON DATABASE "synthetic_b5"' in script


def test_postgresql_owner_role_is_refused_before_any_connection(tmp_path):
    env={'PATH':os.defpath,'PYTHONDONTWRITEBYTECODE':'1','OPS_INTERNAL_MODULE_TRIAL':'1','OPS_MODULE_TRIAL_PG':'1',
         'OPS_TRIAL_PG_HOST':'synthetic.invalid','OPS_TRIAL_PG_DATABASE':'synthetic_b5',
         'DATABASE_URL':'postgresql://synthetic_owner:synthetic-password@synthetic.invalid/synthetic_b5?sslmode=require',
         'ADMIN_PASSWORD':'synthetic-password','SECRET_KEY':'synthetic-only'}
    result=subprocess.run([sys.executable,'-c','from app.internal_module_trial import activate; activate()'],capture_output=True,text=True,env=env)
    assert result.returncode!=0 and 'dedicated role' in result.stderr
