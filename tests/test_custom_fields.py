import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import CustomFieldDefinition,CustomFieldValue,CustomFieldChange
from app.operations.custom_fields import create_definition,save_values,value_token,archive_definition
from app.operations.custom_field_router import router
from app.operations.tasks import form_token,TaskError
from app.public_api import router as public_router
from test_operations import db,prop

@pytest.fixture
def property(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','fields-synthetic-'+32*'x')
    monkeypatch.setenv('ADMIN_USER','fields-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    return prop(db,published=True)

def define(db,key='internal',kind='text',scope='property',options=''):
    id=create_definition(db,actor='fields-admin',token=form_token('fields-admin',purpose='custom-field-create'),scope=scope,key=key,label=key,kind=kind,options=options,note='Synthetic field definition')
    return db.get(CustomFieldDefinition,id)

def save(db,property,values,token=None,note='Synthetic field update'):
    return save_values(db,actor='fields-admin',scope='property',entity_id=property.id,token=token or value_token(db,'fields-admin','property',property.id),values=values,note=note)

def test_values_audit_clear_archive_restore_and_stale_forms(db,property):
    field=define(db);stale=value_token(db,'fields-admin','property',property.id)
    save(db,property,{field.id:'Internal synthetic value'})
    with pytest.raises(TaskError):save(db,property,{field.id:'stale'},stale)
    token=value_token(db,'fields-admin','property',property.id)
    archive_definition(db,actor='fields-admin',field_id=field.id,token=form_token('fields-admin',field,purpose='custom-field-archive'),archived=True,note='Archive synthetic field')
    with pytest.raises(TaskError):save(db,property,{field.id:'stale'},token)
    assert db.query(CustomFieldValue).one().value=='Internal synthetic value'
    archive_definition(db,actor='fields-admin',field_id=field.id,token=form_token('fields-admin',field,purpose='custom-field-archive'),archived=False,note='Restore synthetic field')
    save(db,property,{field.id:''})
    assert db.query(CustomFieldValue).one().value is None
    assert db.query(CustomFieldChange).count()==2

@pytest.mark.parametrize('kind,valid,invalid,options',[
    ('number','1.234567','NaN',''),('number','-12','1.0000001',''),('number','1000000000000','1000000000001',''),
    ('date','2028-02-29','2026-02-29',''),('boolean','false','yes',''),('choice','B','C','A\nB'),('text','valid','x'*4001,'')])
def test_typed_validation_is_atomic(db,property,kind,valid,invalid,options):
    field=define(db,kind=kind,options=options)
    save(db,property,{field.id:valid})
    with pytest.raises(TaskError):save(db,property,{field.id:invalid})
    assert db.query(CustomFieldValue).one().value==valid
    assert db.query(CustomFieldChange).count()==1

def test_wrong_scope_extra_fields_and_bad_reason_rollback(db,property):
    field=define(db);other=define(db,key='task_only',scope='task')
    for values in ({},{field.id:'x',other.id:'wrong'}):
        with pytest.raises(TaskError):save(db,property,values)
    with pytest.raises(TaskError):save(db,property,{field.id:'must rollback'},note='short')
    assert db.query(CustomFieldValue).count()==0
    assert db.query(CustomFieldChange).count()==0

def test_admin_pages_escape_values_and_public_exclusion(db,property):
    field=define(db);save(db,property,{field.id:'<script>INTERNAL_FIELDS_883</script>'})
    app=FastAPI();app.include_router(router);app.include_router(public_router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get('/operations/custom-fields').status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('fields-admin'))
        assert client.get('/operations/custom-fields').status_code==200
        page=client.get(f'/operations/custom-fields/property/{property.id}')
        assert page.status_code==200 and '&lt;script&gt;INTERNAL_FIELDS_883&lt;/script&gt;' in page.text
        assert '<script>INTERNAL_FIELDS_883</script>' not in page.text
        assert client.get('/operations/custom-fields/unknown/1').status_code==404
        assert client.get('/operations/custom-fields/property/999999').status_code==404
        assert client.get('/operations/custom-field-record?scope=property&entity_id=1',follow_redirects=False).status_code==303
        response=client.get(f'/api/public/listings/{property.id}')
        assert response.status_code==200 and 'INTERNAL_FIELDS_883' not in response.text
        assert client.post(f'/operations/custom-fields/property/{property.id}',data={'token':value_token(db,'fields-admin','property',property.id),'field_1':'x','note':'Synthetic cross origin'},headers={'Origin':'https://evil.invalid'}).status_code==403

@pytest.mark.parametrize('key,kind,options', [('Bad key','text',''),('ok','unknown',''),('ok','choice',''),('ok','choice','A\nA'),('ok','text','Unexpected')])
def test_invalid_definitions_leave_no_records(db,property,key,kind,options):
    with pytest.raises(TaskError):define(db,key=key,kind=kind,options=options)
    assert db.query(CustomFieldDefinition).count()==0

def test_new_definition_invalidates_loaded_record_form(db,property):
    first=define(db);token=value_token(db,'fields-admin','property',property.id)
    second=define(db,key='second')
    with pytest.raises(TaskError):save(db,property,{first.id:'old'},token)
    save(db,property,{first.id:'current',second.id:'new'})
    assert db.query(CustomFieldValue).count()==2
