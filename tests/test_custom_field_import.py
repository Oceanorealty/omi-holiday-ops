import io,csv,json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import CustomFieldValue,CustomFieldChange,OpsCommand
from app.operations.custom_field_router import router
from app.operations.custom_field_import import preview_import,apply_import,MAX_CSV
from app.operations.tasks import TaskError
from test_custom_fields import db,property,define,save
from test_operations import prop


def content(rows):
    stream=io.StringIO();writer=csv.writer(stream);writer.writerow(['record_type','record_id','field_key','value']);writer.writerows(rows);return stream.getvalue().encode()


def apply(db,preview,**changes):
    args={'actor':'fields-admin','plan':preview['plan'],'token':preview['token'],'note':'Reviewed synthetic CSV update'};args.update(changes)
    return apply_import(db,**args)


def test_preview_no_write_atomic_apply_preserves_other_fields_and_history(db,property):
    field=define(db);other=define(db,key='other');save(db,property,{field.id:'old',other.id:'preserve'})
    second=prop(db)
    plan=preview_import(db,'fields-admin',content([['property',property.id,field.key,'new'],['property',second.id,field.key,'second']]))
    assert db.query(CustomFieldValue).count()==2 and plan['preview'][0]['before']=='old'
    assert apply(db,plan)==2
    rows={(r.entity_id,r.field_id):r.value for r in db.query(CustomFieldValue).all()}
    assert rows[(property.id,field.id)]=='new' and rows[(property.id,other.id)]=='preserve' and rows[(second.id,field.id)]=='second'
    assert db.query(CustomFieldChange).count()==3
    assert db.query(OpsCommand).filter_by(purpose='custom-import').count()==1
    with pytest.raises(TaskError):apply(db,plan)


def test_late_stale_record_rolls_back_earlier_record_and_audit(db,property):
    field=define(db);second=prop(db)
    plan=preview_import(db,'fields-admin',content([['property',property.id,field.key,'must rollback'],['property',second.id,field.key,'stale']]))
    save(db,second,{field.id:'concurrent update'})
    with pytest.raises(TaskError):apply(db,plan)
    rows=db.query(CustomFieldValue).all();assert len(rows)==1 and rows[0].entity_id==second.id and rows[0].value=='concurrent update'
    assert db.query(CustomFieldChange).count()==1
    assert db.query(OpsCommand).filter_by(purpose='custom-import').count()==0


def test_plan_modification_wrong_actor_bad_reason_and_clear(db,property):
    field=define(db);save(db,property,{field.id:'old'})
    plan=preview_import(db,'fields-admin',content([['property',property.id,field.key,'']]))
    for changes in [{'plan':plan['plan'].replace('property','reservation')},{'actor':'other-admin'},{'note':'short'}]:
        with pytest.raises(TaskError):apply(db,plan,**changes)
        assert db.query(CustomFieldValue).one().value=='old'
    apply(db,plan);assert db.query(CustomFieldValue).one().value is None


@pytest.mark.parametrize('rows',[
    [],[['unknown',1,'internal','x']],[['property',999999,'internal','x']],
    [['property',1,'missing','x']],[['property',1,'internal','x'],['property',1,'internal','y']],
    [['property','1.0','internal','x']],[['property',1,'internal','x'*4001]],
    [['property',1,'internal','x'],['task',1,'internal','x']]])
def test_bad_csv_rejected_without_writes(db,property,rows):
    define(db)
    with pytest.raises(TaskError):preview_import(db,'fields-admin',content(rows))
    assert db.query(CustomFieldValue).count()==0


def test_encoding_size_header_and_row_limit(db,property):
    define(db)
    for raw in [b'\xff',b'x'*(MAX_CSV+1),b'record_type,record_id,field_key,value,value\n',b'record_type,record_id,field_key,value,unknown\n']:
        with pytest.raises(TaskError):preview_import(db,'fields-admin',raw)
    rows=[]
    for i in range(101):
        p=prop(db);rows.append(['property',p.id,'internal','x'])
    with pytest.raises(TaskError):preview_import(db,'fields-admin',content(rows))


def test_import_routes_admin_private_preview_and_apply(db,property):
    field=define(db);raw=content([['property',property.id,field.key,'<script>SYNTHETIC_IMPORT</script>']])
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get('/operations/custom-fields/import').status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('fields-admin'))
        assert client.get('/operations/custom-fields/import').status_code==200
        path='/operations/custom-fields/import/preview'
        assert client.post(path,files={'file':('synthetic.csv',raw)},headers={'Origin':'https://evil.invalid'}).status_code==403
        preview=client.post(path,files={'file':('synthetic.csv',raw)})
        assert preview.status_code==200 and '&lt;script&gt;SYNTHETIC_IMPORT&lt;/script&gt;' in preview.text
        assert preview.headers['Cache-Control']=='private, no-store' and db.query(CustomFieldValue).count()==0
        plan=preview_import(db,'fields-admin',raw)
        response=client.post('/operations/custom-fields/import/apply',data={'plan':plan['plan'],'token':plan['token'],'note':'Reviewed synthetic import preview'})
        assert response.status_code==200 and db.query(CustomFieldValue).one().value=='<script>SYNTHETIC_IMPORT</script>'
